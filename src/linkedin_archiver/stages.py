"""Pipeline stages shared by the CLI and legacy script entry points."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from playwright.async_api import TimeoutError as AsyncPWTimeout
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from linkedin_archiver import extractor as pe
from linkedin_archiver import linkedin_data as mf
from linkedin_archiver import settings as cfg
from linkedin_archiver import video_capture as vc
from linkedin_archiver.browser import (
    BrowserSessionError,
    ResolvedBrowserTarget,
    ensure_browser_session,
    find_or_open_page,
    resolve_browser_target,
)
from linkedin_archiver.media_recovery import MediaCapture, save_dom_media
from linkedin_archiver.linkedin_data import activity_id_from_url, extract_activity_urls_from_text, normalize_url
from linkedin_archiver.settings import (
    archive_dir,
    default_failed_posts_file,
    default_saved_posts_file,
    setup_logging,
)


def args_namespace(
    *,
    browser: str | None = None,
    browser_path: str | None = None,
    user_data_dir: str | None = None,
    profile: str | None = None,
    cdp_port: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        browser=browser,
        browser_path=browser_path,
        user_data_dir=user_data_dir,
        profile=profile,
        cdp_port=cdp_port,
    )


def resolve_target(
    *,
    browser: str | None,
    browser_path: str | None,
    user_data_dir: str | None,
    profile: str | None,
    cdp_port: int | None,
    runtime_config: cfg.RuntimeConfig,
) -> ResolvedBrowserTarget:
    args = args_namespace(
        browser=browser,
        browser_path=browser_path,
        user_data_dir=user_data_dir,
        profile=profile,
        cdp_port=cdp_port,
    )
    return resolve_browser_target(args, default_port=runtime_config.cdp_port)


def _ensure_session(target: ResolvedBrowserTarget, *, start_url: str | None, logger, assume_yes: bool) -> str:
    try:
        return ensure_browser_session(
            target.spec,
            target.executable,
            target.user_data_dir,
            target.profile.directory,
            target.port,
            start_url=start_url,
            logger=logger,
            assume_yes=assume_yes,
        )
    except BrowserSessionError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc


def _log_target(target: ResolvedBrowserTarget, logger) -> None:
    logger.info(
        f"Browser: {target.spec.display_name}  "
        f"Profile: {target.profile.directory}  Port: {target.port}"
    )


def _limit_items(items: list, limit: int | None) -> list:
    if limit is None:
        return items
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return items[:limit]


def collect_saved_posts(
    target: ResolvedBrowserTarget,
    *,
    output: Path | None = None,
    limit: int | None = None,
    assume_yes: bool = False,
    logger=None,
) -> int:
    logger = logger or setup_logging("collect")
    runtime_config = cfg.load_config_file()
    output = output or default_saved_posts_file()
    logger.info(f"Output file: {output}")
    logger.info(f"Limit: {limit if limit is not None else 'all'}")
    _log_target(target, logger)

    cdp_url = _ensure_session(
        target,
        start_url=runtime_config.saved_posts_url,
        logger=logger,
        assume_yes=assume_yes,
    )

    with sync_playwright() as p:
        logger.info(f"Connecting over CDP: {cdp_url}")
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=10_000)
        if not browser.contexts:
            logger.error("No browser context found after connecting.")
            return 1

        context = browser.contexts[0]
        page = find_or_open_page(context, url_hint="linkedin.com")
        urls: list[str] = []
        seen: set[str] = set()

        def save_urls() -> None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(urls, indent=2, ensure_ascii=False), encoding="utf-8")

        def add_urls(candidates: Iterable[str]) -> None:
            for candidate in candidates:
                normalized = normalize_url(candidate)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                urls.append(normalized)
                logger.debug(f"FOUND [{len(urls):04}] {normalized}")
                save_urls()

        def on_response(response) -> None:
            try:
                if "linkedin.com" not in response.url:
                    return
                request = response.request
                if request.resource_type not in {"document", "xhr", "fetch"}:
                    return
                content_type = response.headers.get("content-type", "").lower()
                if not any(v in content_type for v in ("json", "javascript", "html", "text")):
                    return
                add_urls(extract_activity_urls_from_text(response.text()))
            except Exception:
                pass

        page.on("response", on_response)

        try:
            logger.info(f"Opening: {runtime_config.saved_posts_url}")
            page.goto(
                runtime_config.saved_posts_url,
                wait_until="domcontentloaded",
                timeout=cfg.PAGE_NAV_TIMEOUT_MS,
            )
            page.wait_for_timeout(5000)

            if "login" in page.url.lower():
                logger.error("LinkedIn is not logged in on this profile.")
                return 1

            logger.info("Collecting saved-post URLs...")
            unchanged_rounds = 0
            last_scroll_y = -1

            for iteration in range(cfg.MAX_SCROLL_ITERATIONS):
                before = len(urls)
                if limit is not None and len(urls) >= limit:
                    break
                add_urls(_collect_dom_urls(page))
                _click_show_more(page)
                page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.60));")
                page.wait_for_timeout(cfg.SCROLL_SETTLE_TIMEOUT_MS)
                add_urls(_collect_dom_urls(page))
                if limit is not None and len(urls) >= limit:
                    break

                scroll_y = page.evaluate("window.scrollY")
                scroll_height = page.evaluate("document.documentElement.scrollHeight")
                viewport_height = page.evaluate("window.innerHeight")
                reached_bottom = scroll_y + viewport_height >= scroll_height - 100

                logger.debug(
                    f"SCAN [{iteration + 1:03}] posts={len(urls)} "
                    f"scroll={scroll_y:.0f}/{scroll_height:.0f}"
                )

                if len(urls) == before and reached_bottom and scroll_y == last_scroll_y:
                    unchanged_rounds += 1
                else:
                    unchanged_rounds = 0
                last_scroll_y = scroll_y

                if unchanged_rounds >= cfg.MAX_UNCHANGED_SCROLL_ROUNDS:
                    break
        except KeyboardInterrupt:
            logger.warning("Stopped by user.")
        except Exception as exc:
            logger.warning(f"Browser/session stopped: {exc}")
        finally:
            if limit is not None:
                urls[:] = urls[:limit]
            save_urls()
            logger.info(f"Collected: {len(urls)} unique post URLs")
            logger.info(f"Saved to: {output.resolve()}")
            logger.info("Done. (Browser was left running untouched.)")

    return 0


def _collect_dom_urls(page) -> list[str]:
    found: list[str] = []
    for link in page.locator("a[href]").all():
        try:
            href = link.get_attribute("href")
        except Exception:
            continue
        normalized = normalize_url(href)
        if normalized:
            found.append(normalized)
    return found


def _click_show_more(page) -> bool:
    selectors = (
        'button:has-text("Show more results")',
        'button:has-text("Show more")',
        '[aria-label*="Show more"]',
    )
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible():
                button.click()
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def save_post(post_dir: Path, data: dict) -> None:
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "metadata.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    lines = [f"# {data.get('author') or 'Unknown'}", ""]
    if data.get("username"):
        lines += [f"**Username:** {data['username']}", ""]
    if data.get("profile_url"):
        lines += [f"**Profile:** {data['profile_url']}", ""]
    if data.get("timestamp"):
        lines += [f"**Date:** {data['timestamp']}", ""]
    lines += [f"**URL:** {data['url']}", "", "---", "", data.get("text", "").strip(), ""]

    media = data.get("media", [])
    if media:
        lines += ["## Media", ""]
        lines += [f"- `{item['file']}`" for item in media]
        lines.append("")

    (post_dir / "post.md").write_text("\n".join(lines), encoding="utf-8")


def archive_posts(
    target: ResolvedBrowserTarget,
    *,
    input_file: Path | None = None,
    output_dir: Path | None = None,
    limit: int | None = None,
    assume_yes: bool = False,
    logger=None,
) -> int:
    logger = logger or setup_logging("archive")
    runtime_config = cfg.load_config_file()
    input_file = input_file or default_saved_posts_file()
    output_dir = output_dir or archive_dir()

    if not input_file.exists():
        logger.error(f"Input file not found: {input_file}")
        return 1

    urls = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(urls, list):
        logger.error("Input JSON must contain a list.")
        return 1

    try:
        urls = _limit_items(urls, limit)
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    logger.info(f"Input: {input_file} ({len(urls)} URLs)")
    logger.info(f"Limit: {limit if limit is not None else 'all'}")
    logger.info(f"Output: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest = mf.load_manifest(manifest_path)
    _log_target(target, logger)

    cdp_url = _ensure_session(target, start_url=None, logger=logger, assume_yes=assume_yes)

    with sync_playwright() as p:
        logger.info(f"Connecting over CDP: {cdp_url}")
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=60_000)
        if not browser.contexts:
            logger.error("No browser context found after connecting.")
            return 1

        context = browser.contexts[0]
        page = find_or_open_page(context, url_hint="linkedin.com")
        skipped = completed = failed = 0

        for index, url in enumerate(urls, start=1):
            pid = pe.stable_post_id(url)
            if mf.is_done(manifest, pid):
                logger.info(f"[{index}/{len(urls)}] SKIP {pid} (already completed)")
                skipped += 1
                continue

            post_dir = output_dir / f"{index:04d}_{pid}"
            try:
                logger.info(f"[{index}/{len(urls)}] {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=runtime_config.page_nav_timeout_ms)
                page.wait_for_timeout(cfg.POST_SETTLE_TIMEOUT_MS)

                if "login" in page.url.lower():
                    raise RuntimeError("LinkedIn session expired (redirected to login).")

                activity_id = activity_id_from_url(url)
                if not activity_id:
                    raise RuntimeError("Could not extract activity ID from URL.")

                post = pe.find_main_post(page, activity_id)
                if post is None:
                    mf.record(
                        manifest,
                        pid,
                        index=index,
                        url=url,
                        status=mf.Status.EXTRACTION_FAILED,
                        error=f"Main post element not found for activity {activity_id}.",
                    )
                    mf.save_manifest(manifest_path, manifest)
                    logger.warning(f"  EXTRACTION_FAILED: main post not found for {activity_id}")
                    failed += 1
                    continue

                try:
                    page.evaluate('el => el.scrollIntoView({block: "center"})', post)
                except Exception:
                    pass

                pe.expand_see_more(post, page)
                text = pe.extract_post_text(post)
                author, username, profile_url = pe.extract_author(post)
                timestamp = pe.extract_timestamp(post)

                media_urls = pe.extract_images(post)
                media_urls.update(pe.extract_direct_media(post, cfg.MEDIA_LINK_EXTENSIONS))
                media = pe.download_media(context, media_urls, post_dir / "media", url)

                data = {
                    "url": url,
                    "activity_id": activity_id,
                    "author": author,
                    "username": username,
                    "profile_url": profile_url,
                    "timestamp": timestamp,
                    "text": text,
                    "media": media,
                }
                save_post(post_dir, data)

                status = mf.Status.COMPLETED if len(media) == len(media_urls) else mf.Status.MEDIA_FAILED
                mf.record(
                    manifest,
                    pid,
                    index=index,
                    url=url,
                    status=status,
                    author=author,
                    username=username,
                    media_count=len(media),
                    timestamp=timestamp,
                    error=None if status == mf.Status.COMPLETED else f"Downloaded {len(media)}/{len(media_urls)} media items.",
                )
                mf.save_manifest(manifest_path, manifest)

                logger.info(
                    f"  saved author={author!r} media={len(media)}/{len(media_urls)} status={status}"
                )
                if status == mf.Status.COMPLETED:
                    completed += 1
                else:
                    failed += 1

            except KeyboardInterrupt:
                logger.warning("Stopped by user.")
                break
            except Exception as exc:
                current_url = ""
                try:
                    current_url = page.url
                except Exception:
                    pass
                status = mf.Status.LOGIN_REQUIRED if "login" in current_url.lower() else mf.Status.FAILED
                mf.record(manifest, pid, index=index, url=url, status=status, error=str(exc))
                mf.save_manifest(manifest_path, manifest)
                logger.warning(f"  {status.upper()}: {exc}")
                failed += 1

        mf.save_manifest(manifest_path, manifest)
        logger.info(f"Done. completed={completed} skipped={skipped} failed={failed}")
        logger.info(f"Archive: {output_dir.resolve()}")
        logger.info("(Browser was left running untouched.)")

    return 0 if failed == 0 else 2


def _load_urls(value: Path | None, direct_urls: tuple[str, ...], logger) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        normalized = normalize_url(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)

    for url in direct_urls:
        add(url)

    if value:
        raw = json.loads(value.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for item in raw:
                add(item if isinstance(item, str) else item.get("url", ""))
        elif isinstance(raw, dict):
            for item in raw.values():
                if isinstance(item, dict):
                    add(item.get("url", ""))

    if not urls:
        logger.error("No URLs to process.")
    return urls


async def _recover_one(
    context,
    page,
    target_dir: Path,
    url: str,
    playback_timeout: int,
    logger,
) -> dict:
    target_dir.mkdir(parents=True, exist_ok=True)
    work_dir = target_dir / "recovery"
    parts_dir = work_dir / "parts"
    media_dir = target_dir / "media"
    work_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    parts_dir.mkdir(parents=True, exist_ok=True)

    capture = vc.CaptureState(parts_dir=parts_dir)
    media_capture = MediaCapture(media_dir=media_dir, logger=logger)
    tracker = vc.PendingTaskTracker(logger=logger)

    def on_response(response) -> None:
        tracker.spawn(vc.handle_response(response, capture, logger=logger))
        tracker.spawn(media_capture.handle_response(response))

    context.on("response", on_response)

    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=cfg.PAGE_NAV_TIMEOUT_MS)
        except AsyncPWTimeout:
            logger.warning("  Navigation timed out; continuing.")

        current_url = page.url
        if any(marker in current_url for marker in cfg.LOGIN_MARKERS):
            logger.warning("  Login/authwall/challenge page detected.")

        # Capture media already represented in the DOM. This complements the
        # network listener and does not replace video fragment capture.
        await save_dom_media(context, page, media_dir, url, media_capture, logger)

        has_video = await page.locator("video").count() > 0
        video_ok = False
        video_reason = "no video element"
        video_result = {"status": "not_present"}

        if has_video:
            video_ok, video_reason = await vc.handle_video_playback(
                page, playback_timeout, logger=logger
            )
            await tracker.drain()
            vc.write_capture_json(work_dir, url, capture)

            if capture.captures:
                video_result = vc.reconstruct(work_dir, capture, logger=logger)
            else:
                video_result = {"status": "no_data"}

        await save_dom_media(context, page, media_dir, url, media_capture, logger)
        await tracker.drain()
        await media_capture.drain()

        video_path = Path(video_result["path"]) if video_result.get("path") else None
        if video_path and video_path.exists():
            final_video = media_capture.unique_media_path(media_dir / "video.mp4")
            final_video.write_bytes(video_path.read_bytes())
            media_capture.add_saved(
                final_video,
                source_url="network:fmp4",
                content_type="video/mp4",
                media_kind="video",
            )

        status = "completed" if media_capture.saved_count else "media_failed"
        return {
            "status": status,
            "media_count": media_capture.saved_count,
            "video": video_result,
            "video_playback": {"started": video_ok, "reason": video_reason},
        }
    finally:
        context.remove_listener("response", on_response)
        await tracker.drain()
        await media_capture.drain()


def _recovery_targets_from_manifest(archive_root: Path) -> list[tuple[int, str, str]]:
    manifest_path = archive_root / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = mf.load_manifest(manifest_path)
    rows: list[tuple[int, str, str]] = []
    for post_id, entry in manifest.items():
        if not entry.get("url") or entry.get("status") == mf.Status.COMPLETED:
            continue
        index = int(entry.get("index") or 0)
        rows.append((index, post_id, entry["url"]))
    rows.sort(key=lambda item: (item[0], item[1]))
    return rows


def _write_failed_posts_file(archive_root: Path, output_path: Path, logger) -> int:
    rows = _recovery_targets_from_manifest(archive_root)
    urls = [url for _, _, url in rows]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(urls, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"{len(urls)} unresolved post(s) written to {output_path}")
    return len(urls)


def recover_media(
    target: ResolvedBrowserTarget,
    *,
    urls: tuple[str, ...] = (),
    input_file: Path | None = None,
    output_dir: Path | None = None,
    archive_root: Path | None = None,
    playback_timeout: int | None = None,
    limit: int | None = None,
    assume_yes: bool = False,
    logger=None,
) -> int:
    logger = logger or setup_logging("recover")
    runtime_config = cfg.load_config_file()
    playback_timeout = playback_timeout or runtime_config.video_playback_timeout_seconds
    archive_root = archive_root or archive_dir()

    if input_file is None and not urls:
        failed_file = default_failed_posts_file()
        if failed_file.exists():
            input_file = failed_file
        else:
            _write_failed_posts_file(archive_root, failed_file, logger)
            input_file = failed_file

    direct_urls = _load_urls(input_file, urls, logger)
    try:
        direct_urls = _limit_items(direct_urls, limit)
    except ValueError as exc:
        logger.error(str(exc))
        return 1
    if not direct_urls:
        return 0

    logger.info(f"Limit: {limit if limit is not None else 'all'}")

    missing_tools = vc.check_ffmpeg_tools_available()
    if missing_tools:
        logger.error(f"Missing required system tool(s): {', '.join(missing_tools)}")
        return 1

    recovery_root = output_dir or archive_root
    recovery_root.mkdir(parents=True, exist_ok=True)

    target_rows = {url: (index, post_id) for index, post_id, url in _recovery_targets_from_manifest(archive_root)}
    manifest_path = archive_root / "manifest.json"
    archive_manifest = mf.load_manifest(manifest_path)
    recovery_manifest_path = archive_root / "recovery_manifest.json"
    recovery_manifest = mf.load_manifest(recovery_manifest_path)
    _log_target(target, logger)

    cdp_url = _ensure_session(
        target,
        start_url=direct_urls[0] if len(direct_urls) == 1 else None,
        logger=logger,
        assume_yes=assume_yes,
    )

    async def run() -> int:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url)
            except Exception as exc:
                logger.error(f"Failed to connect over CDP: {exc}")
                return 1

            if not browser.contexts:
                logger.error("Connected, but no browser context was found.")
                return 1
            context = browser.contexts[0]
            page = await context.new_page()

            completed = failed = skipped = 0
            try:
                for index, url in enumerate(direct_urls, start=1):
                    activity_id = activity_id_from_url(url)
                    post_id = (target_rows.get(url) or (index, activity_id or pe.stable_post_id(url)))[1]
                    archive_index = (target_rows.get(url) or (index, post_id))[0]
                    post_dir = archive_root / f"{archive_index:04d}_{post_id}" if output_dir is None else recovery_root / post_id

                    rec = recovery_manifest.get(post_id)
                    if rec and rec.get("status") == "completed" and (post_dir / "media").exists():
                        logger.info(f"[{index}/{len(direct_urls)}] SKIP {post_id} (already recovered)")
                        skipped += 1
                        continue

                    logger.info(f"[{index}/{len(direct_urls)}] Recovering media: {url}")
                    try:
                        result = await _recover_one(
                            context, page, post_dir, url, playback_timeout, logger
                        )
                        recovery_manifest[post_id] = {
                            "index": archive_index,
                            "url": url,
                            "status": result["status"],
                            "media_count": result["media_count"],
                            "video": result["video"],
                        }
                        recovery_manifest_path.write_text(
                            json.dumps(recovery_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
                        )

                        if result["status"] == "completed":
                            completed += 1
                            archive_entry = archive_manifest.get(post_id)
                            if archive_entry:
                                archive_entry["media_recovered"] = result["media_count"]
                                mf.save_manifest(manifest_path, archive_manifest)
                        else:
                            failed += 1
                        logger.info(f"  recovered media={result['media_count']} status={result['status']}")
                    except KeyboardInterrupt:
                        logger.warning("Interrupted.")
                        break
                    except Exception as exc:
                        failed += 1
                        recovery_manifest[post_id] = {
                            "index": archive_index,
                            "url": url,
                            "status": "failed",
                            "error": str(exc),
                        }
                        recovery_manifest_path.write_text(
                            json.dumps(recovery_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
                        )
                        logger.warning(f"  FAILED: {exc}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

            logger.info(f"Done. completed={completed} skipped={skipped} failed={failed}")
            logger.info("(Browser was left running untouched.)")
            return 0 if failed == 0 else 2

    return asyncio.run(run())


def run_all(
    target: ResolvedBrowserTarget,
    *,
    limit: int | None = None,
    skip_recover: bool = False,
    logger=None,
) -> int:
    logger = logger or setup_logging("run")
    logger.info(f"Limit: {limit if limit is not None else 'all'}")
    logger.info("Stage 1: collect")
    rc = collect_saved_posts(target, limit=limit, assume_yes=True, logger=logger)
    if rc != 0:
        logger.error("Collect failed. Stopping.")
        return rc

    logger.info("Stage 2: archive")
    archive_rc = archive_posts(target, limit=limit, assume_yes=True, logger=logger)
    if archive_rc not in (0, 2):
        logger.error("Archive failed. Stopping.")
        return archive_rc

    if skip_recover:
        logger.info("Recovery skipped.")
        return 0

    _write_failed_posts_file(archive_dir(), default_failed_posts_file(), logger)
    logger.info("Stage 3: recover media")
    recover_rc = recover_media(
        target, input_file=default_failed_posts_file(), limit=limit, assume_yes=True, logger=logger
    )
    if recover_rc not in (0, 2):
        return recover_rc
    return 0 if archive_rc == 0 and recover_rc == 0 else 2


def list_profiles(*, browser: str | None = None, user_data_dir: str | None = None, logger=None) -> int:
    from linkedin_archiver import browser as bd

    logger = logger or setup_logging("profiles")
    if user_data_dir:
        spec = bd.SUPPORTED_BROWSERS.get(browser, bd.SUPPORTED_BROWSERS["brave"]) if browser else None
        targets = [(spec, Path(user_data_dir))]
    else:
        available = bd.detect_installed_browsers()
        if browser:
            available = [bd.resolve_browser(browser, available)]
        if not available:
            logger.error("No supported Chromium-family browser detected.")
            return 1
        targets = [(spec, bd.find_user_data_dir(spec)) for spec in available]

    for spec, user_dir in targets:
        label = spec.display_name if spec else "Browser"
        print(f"\n{label} — {user_dir}\n")
        try:
            profiles = bd.discover_profiles(user_dir)
        except Exception as exc:
            logger.warning(f"Could not read profiles for {label}: {exc}")
            print(f"  (could not read profiles: {exc})")
            continue
        for index, profile in enumerate(profiles, start=1):
            extra = f"  ({profile.user_name})" if profile.user_name else ""
            print(f"  {index}. {profile.display_name}{extra}  [dir: {profile.directory}]")
        logger.info(f"{label}: {len(profiles)} profile(s) at {user_dir}")

    return 0


def show_status() -> int:
    archive_root = archive_dir()
    manifest = mf.load_manifest(archive_root / "manifest.json")
    recovery = mf.load_manifest(archive_root / "recovery_manifest.json")
    counts: dict[str, int] = {}
    for entry in manifest.values():
        status = entry.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    print(f"Posts: {len(manifest)}")
    for status in sorted(counts):
        print(f"  {status}: {counts[status]}")
    if recovery:
        recovered = sum(1 for item in recovery.values() if item.get("status") == "completed")
        print(f"Media recovered: {recovered}")
    return 0
