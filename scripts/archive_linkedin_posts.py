#!/usr/bin/env python3
"""Stage 2: open every saved-post URL and archive the actual post.

For each URL: locate the main post by its activity ID (never the first
`article` on the page — that can select a comment), expand "See more",
and save author/username/profile URL/timestamp/text/media. Resumable via
the manifest: rerunning skips posts already marked completed.

Usage:
    python scripts/archive_linkedin_posts.py
    python scripts/archive_linkedin_posts.py --input data/linkedin_saved_posts.json --output archive --resume
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from linkedin_archiver import config as cfg
from linkedin_archiver import manifest as mf
from linkedin_archiver import post_extractor as pe
from linkedin_archiver.browser import BrowserSessionError, ensure_browser_session, find_or_open_page
from linkedin_archiver.cli_common import add_browser_selection_args, resolve_browser_target
from linkedin_archiver.linkedin_urls import activity_id_from_url
from linkedin_archiver.logging_utils import setup_logging
from linkedin_archiver.media import download_media, stable_post_id
from linkedin_archiver.paths import archive_dir, default_saved_posts_file

from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_browser_selection_args(parser)
    parser.add_argument("--input", default=None, help=f"Input JSON. Default: {default_saved_posts_file()}")
    parser.add_argument("--output", default=None, help=f"Archive directory. Default: {archive_dir()}")
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip posts already marked completed in the manifest (this is also the default behavior; "
             "the flag exists for clarity/CLI symmetry with the spec).",
    )
    return parser.parse_args()


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


def main() -> None:
    args = parse_args()
    logger = setup_logging("archive_linkedin_posts")
    runtime_config = cfg.load_config_file()

    input_file = Path(args.input) if args.input else default_saved_posts_file()
    output_dir = Path(args.output) if args.output else archive_dir()

    if not input_file.exists():
        logger.error(f"Input file not found: {input_file}")
        raise SystemExit(1)

    urls = json.loads(input_file.read_text(encoding="utf-8"))
    if not isinstance(urls, list):
        logger.error("Input JSON must contain a list.")
        raise SystemExit(1)

    logger.info(f"Input: {input_file}  ({len(urls)} URLs)")
    logger.info(f"Output: {output_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
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

    with sync_playwright() as p:
        logger.info(f"Connecting over CDP: {cdp_url}")
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=60_000)

        if not browser.contexts:
            logger.error("No browser context found after connecting.")
            raise SystemExit(1)

        context = browser.contexts[0]
        page = find_or_open_page(context, url_hint="linkedin.com")

        skipped = completed = failed = 0

        for index, url in enumerate(urls, start=1):
            pid = stable_post_id(url)

            if mf.is_done(manifest, pid):
                logger.info(f"[{index}/{len(urls)}] SKIP {pid} (already completed)")
                skipped += 1
                continue

            post_dir = output_dir / f"{index:04d}_{pid}"

            try:
                logger.info(f"[{index}/{len(urls)}] {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=cfg.PAGE_NAV_TIMEOUT_MS)
                page.wait_for_timeout(cfg.POST_SETTLE_TIMEOUT_MS)

                if "login" in page.url.lower():
                    raise RuntimeError("LinkedIn session expired (redirected to login).")

                activity_id = activity_id_from_url(url)
                if not activity_id:
                    raise RuntimeError("Could not extract activity ID from URL.")

                # Locate the actual saved post — never article.first.
                post = pe.find_main_post(page, activity_id)
                if post is None:
                    mf.record(manifest, pid, index=index, url=url,
                              status=mf.Status.EXTRACTION_FAILED,
                              error=f"Main post element not found for activity {activity_id}.")
                    mf.save_manifest(manifest_path, manifest)
                    logger.warning(f"  EXTRACTION_FAILED: main post element not found for activity {activity_id}")
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

                media = download_media(context, media_urls, post_dir / "media", url)

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

                mf.record(manifest, pid, index=index, url=url, status=mf.Status.COMPLETED,
                          author=author, username=username, media_count=len(media), timestamp=timestamp)
                mf.save_manifest(manifest_path, manifest)

                logger.info(f"  saved author={author!r} media={len(media)}")
                completed += 1

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


if __name__ == "__main__":
    main()
