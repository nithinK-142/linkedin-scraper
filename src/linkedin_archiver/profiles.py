"""Generic Chromium profile discovery.

Replaces the old ``get_brave_profiles.py``, which was hardcoded to
Brave's ``Local State`` path. Every Chromium-family browser stores the
same JSON shape at ``<user-data-dir>/Local State``, so this works for
Brave, Chrome, and Chromium alike.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    directory: str  # e.g. "Default", "Profile 4" — the on-disk folder name
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
    """Resolve a --profile argument against the discovered list.

    Accepts a 1-based index, the on-disk directory name (e.g. "Profile 4"),
    or the display name shown in the browser (case-insensitive).
    """
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
