#!/usr/bin/env python3
"""List installed Chromium-family browsers and their profiles.

Replaces the old get_brave_profiles.py, which only knew about Brave's
Local State path. Usage:

    python scripts/get_profiles.py
    python scripts/get_profiles.py --browser brave
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from linkedin_archiver import browser_discovery as bd
from linkedin_archiver import profiles as pf
from linkedin_archiver.logging_utils import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Only list profiles for this browser (brave, chrome, chromium).")
    parser.add_argument("--user-data-dir", help="Explicit user-data directory to inspect.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logging("get_profiles")

    if args.user_data_dir:
        specs = [bd.SUPPORTED_BROWSERS.get(args.browser, bd.SUPPORTED_BROWSERS["brave"])] if args.browser else [None]
        targets = [(specs[0], Path(args.user_data_dir))]
    else:
        available = bd.detect_installed_browsers()
        if args.browser:
            available = [bd.resolve_browser(args.browser, available)]

        if not available:
            logger.error("No supported Chromium-family browser was detected (checked Brave, Google Chrome, Chromium).")
            raise SystemExit(1)

        targets = [(spec, bd.find_user_data_dir(spec)) for spec in available]

    for spec, user_data_dir in targets:
        label = spec.display_name if spec else "Browser"
        print(f"\n{label} — {user_data_dir}\n")
        try:
            discovered = pf.discover_profiles(user_data_dir)
        except Exception as exc:
            logger.warning(f"Could not read profiles for {label}: {exc}")
            print(f"  (could not read profiles: {exc})")
            continue

        for index, profile in enumerate(discovered, start=1):
            extra = f"  ({profile.user_name})" if profile.user_name else ""
            print(f"  {index}. {profile.display_name}{extra}  [dir: {profile.directory}]")
        logger.info(f"{label}: found {len(discovered)} profile(s) at {user_data_dir}")


if __name__ == "__main__":
    main()
