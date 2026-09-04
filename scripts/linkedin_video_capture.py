#!/usr/bin/env python3
"""Stage 3: capture LinkedIn post videos via real network traffic.

This is the proven fallback for posts (often company/organization pages)
that authwall or return empty data through the normal DOM archiver, even
though the same URL loads fine in a real logged-in profile. It captures
the actual fragmented-MP4 network responses while the video plays and
reconstructs them with ffmpeg, validated by ffprobe. It never tries to
download the <video> element's blob: URL directly.

Accepts arbitrary URLs — nothing is hardcoded:

    python scripts/linkedin_video_capture.py --url <url> --url <url>
    python scripts/linkedin_video_capture.py --input data/linkedin_saved_posts.json
    python scripts/linkedin_video_capture.py --input data/failed_posts.json --profile linkedin
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from linkedin_archiver import settings as cfg
from linkedin_archiver import linkedin_data as mf
from linkedin_archiver import video_capture as vc
from linkedin_archiver.browser import (
    BrowserSessionError, ensure_browser_session,
    add_browser_selection_args, resolve_browser_target,
)
from linkedin_archiver.linkedin_data import activity_id_from_url
from linkedin_archiver.settings import setup_logging
from linkedin_archiver.settings import default_failed_posts_file, default_saved_posts_file, default_video_output_dir

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PWTimeout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_browser_selection_args(parser)
    parser.add_argument("--url", action="append", default=[], help="A post URL to capture. Repeatable.")
    parser.add_argument(
        "--input",
        help=f"JSON file of post URLs (a plain list, like {default_saved_posts_file().name}, "
             f"or a failed-posts list). If neither --url nor --input is given, "
             f"defaults to {default_failed_posts_file()} if it exists.",
    )
    parser.add_argument("--output", default=None, help=f"Output directory. Default: {default_video_output_dir()}")
    parser.add_argument(
        "--playback-timeout", type=int, default=None,
        help=f"Max seconds to wait for video playback to finish. Default: {cfg.VIDEO_PLAYBACK_TIMEOUT_SECONDS}",
    )
    return parser.parse_args()


def load_urls(args: argparse.Namespace, logger) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)

    for url in args.url:
        add(url)

    input_path = None
    if args.input:
        input_path = Path(args.input)
    elif not args.url:
        default_failed = default_failed_posts_file()
        if default_failed.exists():
            input_path = default_failed
            logger.info(f"No --url/--input given; defaulting to {default_failed}")

    if input_path:
        if not input_path.exists():
            raise SystemExit(f"Input file not found: {input_path}")
        raw = json.loads(input_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for item in raw:
                add(item if isinstance(item, str) else item.get("url", ""))
        elif isinstance(raw, dict):
            for item in raw.values():
                if isinstance(item, dict) and item.get("url"):
                    add(item["url"])

    if not urls:
        raise SystemExit(
            "No URLs to process. Pass --url one or more times, or --input a JSON file of URLs."
        )

    return urls


async def process_post(
    context, page, holder, tracker: vc.PendingTaskTracker,
    post_id: str, url: str, index: int, total: int,
    output_root: Path, playback_timeout: int, logger,
) -> dict:
    logger.info(f"=== Opening post {index}/{total}: {post_id} ===")

    post_dir = output_root / post_id
    parts_dir = post_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    state = vc.CaptureState(parts_dir=parts_dir)
    holder["state"] = state

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeout:
        logger.warning("  Navigation timed out; continuing to check the page anyway.")

    current_url = page.url
    if any(marker in current_url for marker in cfg.LOGIN_MARKERS):
        logger.warning(f"  This post requires login/challenge ({current_url}). Nothing downloaded.")
        holder["state"] = None
        await tracker.drain()
        vc.write_capture_json(post_dir, url, state, note="login/challenge required; not downloaded")
        return {"status": mf.Status.LOGIN_REQUIRED}

    ok, reason = await vc.handle_video_playback(page, playback_timeout, logger=logger)
    holder["state"] = None
    await tracker.drain()

    if not ok:
        logger.warning(f"  Could not capture this video: {reason}")
        vc.write_capture_json(post_dir, url, state, note=f"playback not captured: {reason}")
        (post_dir / "video.mp4.FAILED").write_text(f"Playback not captured: {reason}\n")
        return {"status": mf.Status.MEDIA_FAILED, "error": reason}

    vc.write_capture_json(post_dir, url, state)
    result = vc.reconstruct(post_dir, state, logger=logger)

    status_map = {
        "completed": mf.Status.COMPLETED,
        "no_data": mf.Status.MEDIA_FAILED,
        "no_init_segment": mf.Status.MEDIA_FAILED,
        "remux_failed": mf.Status.MEDIA_FAILED,
        "unverified": mf.Status.MEDIA_FAILED,
    }
    return {"status": status_map.get(result["status"], mf.Status.MEDIA_FAILED), "detail": result}


async def run(args: argparse.Namespace, logger) -> None:
    runtime_config = cfg.load_config_file()
    playback_timeout = args.playback_timeout or runtime_config.video_playback_timeout_seconds

    missing_tools = vc.check_ffmpeg_tools_available()
    if missing_tools:
        logger.error(
            f"Missing required system tool(s): {', '.join(missing_tools)}. "
            f"Install with e.g. 'sudo apt install ffmpeg' or 'sudo dnf install ffmpeg'."
        )
        raise SystemExit(1)

    urls = load_urls(args, logger)
    logger.info(f"Processing {len(urls)} URL(s).")

    output_root = Path(args.output) if args.output else default_video_output_dir()
    output_root.mkdir(parents=True, exist_ok=True)

    manifest_path = output_root / "manifest.json"
    manifest = mf.load_manifest(manifest_path)

    try:
        target = resolve_browser_target(args, default_port=runtime_config.cdp_port)
    except Exception as exc:
        logger.error(f"Could not resolve browser/profile: {exc}")
        raise SystemExit(1) from exc

    logger.info(f"Browser: {target.spec.display_name}  Profile: {target.profile.directory}  Port: {target.port}")

    try:
        cdp_url = ensure_browser_session(
            target.spec, target.executable, target.user_data_dir, target.profile.directory, target.port,
            logger=logger,
        )
    except BrowserSessionError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            logger.error(f"Failed to connect over CDP: {exc}")
            raise SystemExit(1) from exc

        if not browser.contexts:
            logger.error(
                "Connected, but no browser context was found. Refusing to create a fresh "
                "context, since that would not carry the logged-in LinkedIn session."
            )
            raise SystemExit(1)

        context = browser.contexts[0]

        try:
            page = await context.new_page()
        except Exception as exc:
            logger.error(f"Connected, but could not open a new tab: {exc}")
            raise SystemExit(1) from exc

        try:
            cdp_session = await context.new_cdp_session(page)
            await cdp_session.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception as exc:
            logger.warning(f"Could not disable cache via CDP ({exc}); continuing anyway.")

        tracker = vc.PendingTaskTracker(logger=logger)
        holder = {"state": None}

        def _on_response(response):
            state = holder["state"]
            if state is not None:
                tracker.spawn(vc.handle_response(response, state, logger=logger))

        context.on("response", _on_response)

        total = len(urls)
        completed = failed = skipped = 0

        for index, url in enumerate(urls, start=1):
            activity_id = activity_id_from_url(url)
            post_id = activity_id or url.rsplit("/", 1)[-1] or f"post_{index}"

            if mf.is_done(manifest, post_id):
                logger.info(f"[{index}/{total}] SKIP {post_id} (already completed)")
                skipped += 1
                continue

            try:
                outcome = await process_post(
                    context, page, holder, tracker, post_id, url, index, total,
                    output_root, playback_timeout, logger,
                )
                mf.record(manifest, post_id, index=index, url=url, status=outcome["status"])
                mf.save_manifest(manifest_path, manifest)
                if outcome["status"] == mf.Status.COMPLETED:
                    completed += 1
                else:
                    failed += 1
            except KeyboardInterrupt:
                logger.warning("Interrupted.")
                break
            except Exception as exc:
                mf.record(manifest, post_id, index=index, url=url, status=mf.Status.FAILED, error=str(exc))
                mf.save_manifest(manifest_path, manifest)
                logger.warning(f"  FAILED: {exc}")
                failed += 1

        holder["state"] = None
        await tracker.drain()

        try:
            await page.close()
        except Exception:
            pass

        await asyncio.sleep(0.5)
        await tracker.drain(timeout=5.0)

        mf.save_manifest(manifest_path, manifest)
        logger.info(f"Done. completed={completed} skipped={skipped} failed={failed}")
        logger.info(f"Output: {output_root.resolve()}")
        logger.info("(Browser was left running untouched.)")


def main() -> None:
    args = parse_args()
    logger = setup_logging("linkedin_video_capture")
    try:
        asyncio.run(run(args, logger))
    except KeyboardInterrupt:
        logger.warning("Interrupted. Browser was left running untouched.")
        sys.exit(130)


if __name__ == "__main__":
    main()
