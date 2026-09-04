"""Safe attachment to a real, already-authenticated Chromium-family browser
profile over the Chrome DevTools Protocol (CDP).

Hard rules, carried over unchanged from the proven video-capture script:

* Never overwrite, delete, or reset the selected profile.
* Never copy cookies into another profile or create a throwaway profile.
* Never force a new LinkedIn login — always reuse the real session.
* If a browser is already running on the target user-data-dir without a
  debugging port, refuse to relaunch it (Chromium only honors
  --remote-debugging-port on the *first* launch of a given user-data-dir;
  forcing a second launch can misbehave). Ask the user to quit it first.
* If something is already listening on the target port, verify it's an
  actual CDP endpoint before trusting it, and get explicit human
  confirmation that it's the right profile — we cannot verify that
  programmatically.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from linkedin_archiver.browser_discovery import BrowserSpec


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
    """True if nothing is bound to ``port`` on localhost. Uses a bind
    attempt (not connect()) so it's correct even against a listener whose
    accept backlog is saturated — connect-based checks can misreport a
    busy port as free in that case."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def select_cdp_port(preferred: int, scan_range: int = 20) -> int:
    """Pick a CDP port to launch a *new* browser instance on.

    If a CDP endpoint is already listening on ``preferred``, that's a
    candidate for reuse (handled separately in ``ensure_browser_session``)
    rather than something to route around here. This function is only for
    choosing a port to launch a fresh instance on, so it looks for a port
    that is entirely free.
    """
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
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except FileNotFoundError:
        # No psutil, no pgrep: genuinely cannot tell. Caller decides how to
        # proceed cautiously.
        return False


def wait_for_cdp_port(port: int, timeout: float) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if is_cdp_port_open(port):
            return True
        time.sleep(0.5)
    return False


def launch_browser(
    executable: str,
    user_data_dir: Path,
    profile_directory: str,
    port: int,
    start_url: str | None = None,
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
    spec: BrowserSpec,
    executable: str,
    user_data_dir: Path,
    profile_directory: str,
    port: int,
    *,
    start_url: str | None = None,
    logger=None,
    assume_yes: bool = False,
) -> str:
    """Return a usable CDP HTTP endpoint for the requested profile, or raise
    ``BrowserSessionError`` with a clear explanation of what to do.

    This never launches Playwright's bundled Chromium and never creates,
    copies, or resets a profile. It only ever attaches to, or launches,
    the real browser executable the caller already has installed.
    """

    def log(message: str) -> None:
        if logger is not None:
            logger.info(message)
        else:
            print(message)

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
    """Reuse an existing tab already on ``url_hint`` if one exists,
    otherwise open a new tab in the same (real) browser context."""
    for existing_page in context.pages:
        if url_hint in existing_page.url:
            return existing_page
    return context.new_page()
