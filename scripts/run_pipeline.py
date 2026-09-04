#!/usr/bin/env python3
"""Optional convenience wrapper: run collection, then archiving, then video
capture for whatever failed, in one browser/profile selection.

Each stage remains fully usable on its own (scripts/linkedin_saved.py,
scripts/archive_linkedin_posts.py, scripts/linkedin_video_capture.py) —
this just chains them so you don't have to re-pick the browser/profile
three times in a row.

Usage:
    python scripts/run_pipeline.py --browser brave --profile "Profile 4"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from linkedin_archiver.logging_utils import setup_logging
from linkedin_archiver.manifest import Status, load_manifest
from linkedin_archiver.paths import archive_dir, default_failed_posts_file, default_saved_posts_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--browser", help="See scripts/linkedin_saved.py --help.")
    parser.add_argument("--browser-path")
    parser.add_argument("--user-data-dir")
    parser.add_argument("--profile")
    parser.add_argument("--cdp-port", type=int)
    parser.add_argument("--skip-video-capture", action="store_true",
                         help="Stop after archiving; don't run the video-capture fallback.")
    return parser.parse_args()


def shared_flags(args: argparse.Namespace) -> list[str]:
    flags = []
    if args.browser:
        flags += ["--browser", args.browser]
    if args.browser_path:
        flags += ["--browser-path", args.browser_path]
    if args.user_data_dir:
        flags += ["--user-data-dir", args.user_data_dir]
    if args.profile:
        flags += ["--profile", args.profile]
    if args.cdp_port:
        flags += ["--cdp-port", str(args.cdp_port)]
    return flags


def run_step(cmd: list[str], logger) -> int:
    logger.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def write_failed_posts_file(manifest_path: Path, output_path: Path, logger) -> int:
    if not manifest_path.exists():
        return 0
    manifest = load_manifest(manifest_path)
    failed = [
        entry["url"] for entry in manifest.values()
        if entry.get("status") not in Status.TERMINAL_SUCCESS and entry.get("url")
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(failed, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"{len(failed)} unresolved post(s) written to {output_path}")
    return len(failed)


def main() -> None:
    args = parse_args()
    logger = setup_logging("run_pipeline")
    flags = shared_flags(args)
    scripts_dir = Path(__file__).resolve().parent

    logger.info("=== Stage 1: collecting saved-post URLs ===")
    rc = run_step([sys.executable, str(scripts_dir / "linkedin_saved.py"), *flags], logger)
    if rc != 0:
        logger.error("Stage 1 failed; stopping pipeline.")
        raise SystemExit(rc)

    logger.info("=== Stage 2: archiving posts ===")
    rc = run_step(
        [sys.executable, str(scripts_dir / "archive_linkedin_posts.py"),
         "--input", str(default_saved_posts_file()), "--resume", *flags],
        logger,
    )
    if rc != 0:
        logger.warning("Stage 2 exited non-zero; continuing to check for unresolved posts anyway.")

    manifest_path = archive_dir() / "manifest.json"
    failed_count = write_failed_posts_file(manifest_path, default_failed_posts_file(), logger)

    if args.skip_video_capture:
        logger.info("Skipping Stage 3 (--skip-video-capture).")
        return

    if failed_count == 0:
        logger.info("Nothing unresolved; skipping Stage 3.")
        return

    logger.info("=== Stage 3: video capture for unresolved posts ===")
    rc = run_step(
        [sys.executable, str(scripts_dir / "linkedin_video_capture.py"),
         "--input", str(default_failed_posts_file()), *flags],
        logger,
    )
    if rc != 0:
        logger.warning("Stage 3 exited non-zero.")

    logger.info("Pipeline finished.")


if __name__ == "__main__":
    main()
