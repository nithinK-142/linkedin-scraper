"""Detection of installed Chromium-family browsers on Linux.

This project is Chromium/CDP-specific by design (see README: "Browser
constraints") because it must reuse an existing, already-authenticated
browser profile. This module only concerns itself with *finding* a
browser executable and its user-data directory — it never launches
anything and never touches profile contents.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


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
    """Locate the browser binary. ``explicit`` (e.g. --browser-path) wins."""
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
    """Locate the browser's user-data directory. ``explicit`` (e.g.
    --user-data-dir) wins."""
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
    """Return every supported browser that has both an executable and a
    user-data directory present on this machine, in a stable order."""
    found = []
    for spec in SUPPORTED_BROWSERS.values():
        if find_executable(spec) and find_user_data_dir(spec):
            found.append(spec)
    return found


def resolve_browser(key_or_index: str, available: list[BrowserSpec]) -> BrowserSpec:
    """Resolve a --browser argument against the detected list.

    Accepts a browser key (e.g. "brave"), a display name (case-insensitive),
    or a 1-based index into ``available``.
    """
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
