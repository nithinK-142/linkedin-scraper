"""Shared CLI plumbing: the --browser/--browser-path/--user-data-dir/
--profile/--cdp-port arguments and the resolution logic behind them.
Every script in scripts/ uses this so behavior stays identical."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from linkedin_archiver import browser_discovery as bd
from linkedin_archiver import profiles as pf


def add_browser_selection_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("browser/profile selection")
    group.add_argument(
        "--browser",
        help="Browser to use: brave, chrome, chromium (name, or number from the "
             "interactive list). If omitted, you'll be prompted when more than "
             "one supported browser is detected.",
    )
    group.add_argument(
        "--browser-path",
        help="Explicit path to the browser executable, overriding auto-detection.",
    )
    group.add_argument(
        "--user-data-dir",
        help="Explicit path to the browser's user-data directory, overriding auto-detection.",
    )
    group.add_argument(
        "--profile",
        help="Profile to use: directory name (e.g. 'Profile 4'), display name, "
             "or number from the interactive list. If omitted, you'll be prompted.",
    )
    group.add_argument(
        "--cdp-port",
        type=int,
        default=None,
        help="Chrome DevTools Protocol port. Default: 9222, or the next free "
             "port near it if 9222 is unavailable.",
    )


@dataclass
class ResolvedBrowserTarget:
    spec: bd.BrowserSpec
    executable: str
    user_data_dir: Path
    profile: pf.Profile
    port: int


def _pick_port(explicit: int | None, preferred: int) -> int:
    """--cdp-port always wins as-is. Otherwise: reuse the preferred port if
    something already speaks CDP there (likely our own already-running
    browser); if it's free, use it; if it's occupied by something else
    entirely, pick a nearby free port instead of assuming 9222 works."""
    from linkedin_archiver.browser import is_cdp_port_open, select_cdp_port, is_port_free

    if explicit:
        return explicit
    if is_cdp_port_open(preferred):
        return preferred
    if is_port_free(preferred):
        return preferred
    return select_cdp_port(preferred)


def resolve_browser_target(args: argparse.Namespace, default_port: int) -> ResolvedBrowserTarget:
    """Turn the CLI args above into a concrete (browser, profile, port),
    prompting interactively for whatever wasn't specified."""

    if args.browser_path or args.user_data_dir:
        # An explicit override was given for at least one side; still needs
        # a BrowserSpec to know executable names/user-data defaults for
        # whichever side wasn't overridden. Try to infer from --browser,
        # else fall back to detection.
        available = bd.detect_installed_browsers()
        if args.browser:
            spec = bd.resolve_browser(args.browser, available or list(bd.SUPPORTED_BROWSERS.values()))
        elif available:
            spec = available[0]
        else:
            spec = bd.SUPPORTED_BROWSERS["brave"]
    else:
        available = bd.detect_installed_browsers()
        if args.browser:
            spec = bd.resolve_browser(args.browser, available)
        else:
            spec = bd.choose_browser_interactive(available)

    executable = bd.find_executable(spec, explicit=args.browser_path)
    if not executable:
        raise SystemExit(
            f"Could not locate the {spec.display_name} executable. "
            f"Pass --browser-path explicitly."
        )

    user_data_dir = bd.find_user_data_dir(spec, explicit=args.user_data_dir)
    if not user_data_dir:
        raise SystemExit(
            f"Could not locate {spec.display_name}'s user-data directory. "
            f"Pass --user-data-dir explicitly."
        )

    discovered_profiles = pf.discover_profiles(user_data_dir)
    if args.profile:
        profile = pf.resolve_profile(args.profile, discovered_profiles)
    else:
        profile = pf.choose_profile_interactive(discovered_profiles)

    port = _pick_port(args.cdp_port, default_port)

    return ResolvedBrowserTarget(
        spec=spec,
        executable=executable,
        user_data_dir=user_data_dir,
        profile=profile,
        port=port,
    )
