"""Everything related to getting a usable, already-logged-in browser
session: detecting installed Chromium-family browsers, discovering their
profiles, safely attaching to (or launching) one over CDP, and the CLI
flags that tie it together. This is Chromium/CDP-specific by design —
the point is reusing an existing authenticated session, not automating a
generic browser.

Session safety rules (unchanged from the original proven scripts):
* Never overwrite, delete, or reset the selected profile.
* Never copy cookies into another profile or create a throwaway profile.
* Never force a new LinkedIn login — always reuse the real session.
* Refuse to relaunch a browser that's already running without a debug
  port (Chromium only honors --remote-debugging-port on first launch of
  a user-data-dir).
* If something's already listening on the CDP port, confirm with the
  user that it's the right profile — that can't be verified
  programmatically.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


# ============================================================ detection ===

@dataclass(frozen=True)
class BrowserSpec:
    key: str
    display_name: str
    executable_names: tuple[str, ...]
    fixed_paths: tuple[str, ...]
    user_data_dirs: tuple[str, ...]  # relative to $HOME, first existing one wins


SUPPORTED_BROWSERS: dict[str, BrowserSpec] = {
    "brave": BrowserSpec(
        key="brave",
        display_name="Brave",
        executable_names=("brave-browser", "brave-browser-stable", "brave"),
        fixed_paths=(
            "/usr/bin/brave-browser",
            "/usr/bin/brave-browser-stable",
            "/usr/bin/brave",
            "/opt/brave.com/brave/brave",
            "/snap/bin/brave",
        ),
        user_data_dirs=(".config/BraveSoftware/Brave-Browser",),
    ),
    "chrome": BrowserSpec(
        key="chrome",
        display_name="Google Chrome",
        executable_names=("google-chrome-stable", "google-chrome", "chrome"),
        fixed_paths=(
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/opt/google/chrome/chrome",
        ),
        user_data_dirs=(".config/google-chrome",),
    ),
    "chromium": BrowserSpec(
        key="chromium",
        display_name="Chromium",
        executable_names=("chromium-browser", "chromium"),
        fixed_paths=(
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ),
        user_data_dirs=(".config/chromium",),
    ),
}


def find_executable(spec: BrowserSpec, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    for name in spec.executable_names:
        found = shutil.which(name)
        if found:
            return found
    for path in spec.fixed_paths:
        if Path(path).exists():
            return path
    return None


def find_user_data_dir(spec: BrowserSpec, explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.exists() else None
    home = Path.home()
    for relative in spec.user_data_dirs:
        candidate = home / relative
        if candidate.exists():
            return candidate
    return None


def detect_installed_browsers() -> list[BrowserSpec]:
    return [
        spec for spec in SUPPORTED_BROWSERS.values()
        if find_executable(spec) and find_user_data_dir(spec)
    ]


def resolve_browser(key_or_index: str, available: list[BrowserSpec]) -> BrowserSpec:
    """Accepts a browser key, a display name (case-insensitive), or a
    1-based index into ``available``."""
    if key_or_index.isdigit():
        idx = int(key_or_index)
        if 1 <= idx <= len(available):
            return available[idx - 1]
        raise ValueError(f"--browser index {idx} is out of range (1-{len(available)}).")

    lowered = key_or_index.lower()

    if lowered in SUPPORTED_BROWSERS:
        spec = SUPPORTED_BROWSERS[lowered]
        if spec in available:
            return spec
        raise ValueError(
            f"'{spec.display_name}' was not detected on this machine "
            f"(no executable or no user-data directory found)."
        )

    for spec in available:
        if spec.display_name.lower() == lowered:
            return spec

    raise ValueError(
        f"--browser '{key_or_index}' did not match any detected browser. "
        f"Detected: {[s.key for s in available]}"
    )


def choose_browser_interactive(available: list[BrowserSpec]) -> BrowserSpec:
    if not available:
        raise RuntimeError(
            "No supported Chromium-family browser was detected "
            "(checked Brave, Google Chrome, Chromium). "
            "Use --browser-path / --user-data-dir to point at one explicitly."
        )
    if len(available) == 1:
        return available[0]

    print("Browser:\n")
    for index, spec in enumerate(available, start=1):
        print(f"{index}. {spec.display_name}")

    while True:
        choice = input("\nSelect browser: ").strip()
        try:
            return resolve_browser(choice, available)
        except ValueError as exc:
            print(str(exc))


# ============================================================= profiles ===

@dataclass(frozen=True)
class Profile:
    directory: str  # on-disk folder name, e.g. "Default", "Profile 4"
    display_name: str  # human-friendly name shown in the browser's UI
    user_name: str = ""  # associated account email/name, if any


def discover_profiles(user_data_dir: Path) -> list[Profile]:
    local_state_path = user_data_dir / "Local State"

    if not local_state_path.exists():
        raise FileNotFoundError(f"Local State not found: {local_state_path}")

    data = json.loads(local_state_path.read_text(encoding="utf-8"))
    info_cache = data.get("profile", {}).get("info_cache", {})

    if not info_cache:
        raise RuntimeError(f"No profiles found in {local_state_path}")

    profiles = [
        Profile(
            directory=directory,
            display_name=info.get("name") or info.get("shortcut_name") or directory,
            user_name=info.get("user_name", "") or "",
        )
        for directory, info in info_cache.items()
    ]
    profiles.sort(key=lambda p: p.directory)
    return profiles


def resolve_profile(arg: str, profiles: list[Profile]) -> Profile:
    """Accepts a 1-based index, the on-disk directory name, or the
    display name shown in the browser (case-insensitive)."""
    if arg.isdigit():
        idx = int(arg)
        if 1 <= idx <= len(profiles):
            return profiles[idx - 1]
        raise ValueError(f"--profile index {idx} is out of range (1-{len(profiles)}).")

    for profile in profiles:
        if arg == profile.directory or arg.lower() == profile.display_name.lower():
            return profile

    raise ValueError(
        f"--profile '{arg}' did not match any profile directory or display name. "
        f"Known profiles: {[p.directory for p in profiles]}"
    )


def choose_profile_interactive(profiles: list[Profile]) -> Profile:
    if not profiles:
        raise RuntimeError("No profiles found.")

    print("Profiles:\n")
    for index, profile in enumerate(profiles, start=1):
        extra = f"  ({profile.user_name})" if profile.user_name else ""
        print(f"{index}. {profile.display_name}{extra}  [dir: {profile.directory}]")

    while True:
        choice = input("\nSelect profile: ").strip()
        try:
            return resolve_profile(choice, profiles)
        except ValueError as exc:
            print(str(exc))


# =========================================================== CDP session ==

class BrowserSessionError(RuntimeError):
    """Raised when we cannot safely establish a CDP session."""


def _http_get_json(url: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode())
    except Exception:
        return None


def is_cdp_port_open(port: int, timeout: float = 1.5) -> bool:
    return _http_get_json(f"http://127.0.0.1:{port}/json/version", timeout) is not None


def get_cdp_ws_url(port: int, timeout: float = 2.0) -> str | None:
    data = _http_get_json(f"http://127.0.0.1:{port}/json/version", timeout)
    return data.get("webSocketDebuggerUrl") if data else None


def cdp_http_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def is_port_free(port: int) -> bool:
    """Bind-based check (not connect()) so it's correct even against a
    listener whose accept backlog is saturated."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def select_cdp_port(preferred: int, scan_range: int = 20) -> int:
    """Pick a free port to launch a *new* browser instance on."""
    if is_port_free(preferred):
        return preferred
    for offset in range(1, scan_range + 1):
        candidate = preferred + offset
        if is_port_free(candidate):
            return candidate
    raise BrowserSessionError(
        f"Could not find a free port near {preferred} "
        f"(scanned {preferred}-{preferred + scan_range})."
    )


def is_browser_process_running(spec: BrowserSpec) -> bool:
    try:
        import psutil  # type: ignore

        needles = [name.lower() for name in spec.executable_names]
        for proc in psutil.process_iter(["name", "exe"]):
            info = proc.info
            name = (info.get("name") or "").lower()
            exe = (info.get("exe") or "").lower()
            if any(needle in name or needle in exe for needle in needles):
                return True
        return False
    except ImportError:
        pass

    try:
        result = subprocess.run(
            ["pgrep", "-f", "-i", spec.executable_names[0]],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except FileNotFoundError:
        return False  # no psutil, no pgrep: genuinely cannot tell


def wait_for_cdp_port(port: int, timeout: float) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if is_cdp_port_open(port):
            return True
        time.sleep(0.5)
    return False


def launch_browser(
    executable: str, user_data_dir: Path, profile_directory: str,
    port: int, start_url: str | None = None,
) -> subprocess.Popen:
    args = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if start_url:
        args.append(start_url)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_browser_session(
    spec: BrowserSpec, executable: str, user_data_dir: Path, profile_directory: str, port: int,
    *, start_url: str | None = None, logger=None, assume_yes: bool = False,
) -> str:
    """Return a usable CDP HTTP endpoint, or raise BrowserSessionError.
    Never launches Playwright's bundled Chromium; never creates, copies,
    or resets a profile."""

    def log(message: str) -> None:
        (logger.info if logger else print)(message)

    if is_cdp_port_open(port):
        if get_cdp_ws_url(port) is None:
            raise BrowserSessionError(
                f"Port {port} is open but does not look like a usable "
                f"Chrome DevTools endpoint. Aborting rather than guessing."
            )

        log(f"Found an already-running {spec.display_name} with a usable debugging endpoint on port {port}.")
        log(f"NOTE: which profile that running instance has loaded cannot be verified programmatically. "
            f"You selected profile directory '{profile_directory}'.")

        if not assume_yes:
            answer = input(
                "Please confirm the browser is currently showing that profile, "
                "then type 'y' to continue: "
            ).strip().lower()
            if answer != "y":
                raise BrowserSessionError(
                    f"Aborting. Fully quit {spec.display_name} (all windows) and re-run "
                    f"this script so it can relaunch it with debugging enabled "
                    f"against the selected profile."
                )

        return cdp_http_url(port)

    if is_browser_process_running(spec):
        raise BrowserSessionError(
            f"{spec.display_name} appears to already be running, but without a "
            f"remote-debugging port.\n"
            f"Chromium-based browsers only honor --remote-debugging-port on the FIRST "
            f"launch of a given user-data-dir; a second launch just opens a new window "
            f"in the already-running instance and ignores the flag. Refusing to proceed, "
            f"because forcing this could corrupt or lock the real profile.\n\n"
            f"Please fully quit {spec.display_name} (all windows, check the system tray "
            f"too) and re-run this script so it can launch it with debugging enabled."
        )

    log(f"Launching {spec.display_name} with profile '{profile_directory}' "
        f"and remote debugging on port {port}...")

    launch_browser(executable, user_data_dir, profile_directory, port, start_url=start_url)

    if not wait_for_cdp_port(port, timeout=30):
        raise BrowserSessionError(
            f"{spec.display_name} did not open a usable debugging port on {port} "
            f"within 30 seconds."
        )

    if get_cdp_ws_url(port) is None:
        raise BrowserSessionError(f"{spec.display_name} launched, but the CDP endpoint is not usable. Aborting.")

    return cdp_http_url(port)


def find_or_open_page(context, url_hint: str = "linkedin.com"):
    """Reuse an existing tab already on ``url_hint``, else open a new one
    in the same (real) browser context."""
    for existing_page in context.pages:
        if url_hint in existing_page.url:
            return existing_page
    return context.new_page()


# ================================================================== CLI ===

def add_browser_selection_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("browser/profile selection")
    group.add_argument(
        "--browser",
        help="Browser to use: brave, chrome, chromium (name, or number from the "
             "interactive list). If omitted, you'll be prompted when more than "
             "one supported browser is detected.",
    )
    group.add_argument("--browser-path", help="Explicit path to the browser executable.")
    group.add_argument("--user-data-dir", help="Explicit path to the browser's user-data directory.")
    group.add_argument(
        "--profile",
        help="Profile to use: directory name (e.g. 'Profile 4'), display name, "
             "or number from the interactive list. If omitted, you'll be prompted.",
    )
    group.add_argument(
        "--cdp-port", type=int, default=None,
        help="Chrome DevTools Protocol port. Default: 9222, or the next free port near it.",
    )


@dataclass
class ResolvedBrowserTarget:
    spec: BrowserSpec
    executable: str
    user_data_dir: Path
    profile: Profile
    port: int


def _pick_port(explicit: int | None, preferred: int) -> int:
    """--cdp-port always wins. Otherwise: reuse the preferred port if
    something already speaks CDP there; if it's free, use it; if it's
    occupied by something else, pick a nearby free port."""
    if explicit:
        return explicit
    if is_cdp_port_open(preferred):
        return preferred
    if is_port_free(preferred):
        return preferred
    return select_cdp_port(preferred)


def resolve_browser_target(args: argparse.Namespace, default_port: int) -> ResolvedBrowserTarget:
    """Turn CLI args into a concrete (browser, profile, port), prompting
    interactively for whatever wasn't specified."""

    if args.browser_path or args.user_data_dir:
        available = detect_installed_browsers()
        if args.browser:
            spec = resolve_browser(args.browser, available or list(SUPPORTED_BROWSERS.values()))
        elif available:
            spec = available[0]
        else:
            spec = SUPPORTED_BROWSERS["brave"]
    else:
        available = detect_installed_browsers()
        spec = resolve_browser(args.browser, available) if args.browser else choose_browser_interactive(available)

    executable = find_executable(spec, explicit=args.browser_path)
    if not executable:
        raise SystemExit(f"Could not locate the {spec.display_name} executable. Pass --browser-path explicitly.")

    user_data_dir = find_user_data_dir(spec, explicit=args.user_data_dir)
    if not user_data_dir:
        raise SystemExit(f"Could not locate {spec.display_name}'s user-data directory. Pass --user-data-dir explicitly.")

    discovered_profiles = discover_profiles(user_data_dir)
    profile = resolve_profile(args.profile, discovered_profiles) if args.profile \
        else choose_profile_interactive(discovered_profiles)

    port = _pick_port(args.cdp_port, default_port)

    return ResolvedBrowserTarget(
        spec=spec, executable=executable, user_data_dir=user_data_dir, profile=profile, port=port,
    )
