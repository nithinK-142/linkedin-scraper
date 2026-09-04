"""Stable, non-secret configuration.

Defaults live here as plain constants. Anything a user might reasonably
want to change without editing code can also be set in an optional
``config.json`` at the project root (see ``load_config_file``), and
individual scripts always allow CLI overrides on top of both. No
credentials or session data belong in this file or in config.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

from linkedin_archiver.paths import project_root

SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"

DEFAULT_CDP_PORT = 9222
CDP_PORT_SCAN_RANGE = 20  # try DEFAULT_CDP_PORT .. DEFAULT_CDP_PORT + N when auto-selecting

PAGE_NAV_TIMEOUT_MS = 60_000
POST_SETTLE_TIMEOUT_MS = 4_000
SCROLL_SETTLE_TIMEOUT_MS = 2_500
MAX_UNCHANGED_SCROLL_ROUNDS = 5
MAX_SCROLL_ITERATIONS = 1_000

VIDEO_PLAYBACK_TIMEOUT_SECONDS = 300  # 5 minutes
VIDEO_AUTOPLAY_FALLBACK_TIMEOUT_MS = 120_000

LOGIN_MARKERS = ("/authwall", "/checkpoint/", "/login", "uas/login")

MEDIA_LINK_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".webm", ".mov", ".m4v", ".m3u8",
)

CONFIG_FILE_NAME = "config.json"


@dataclass
class RuntimeConfig:
    """Values that scripts most commonly want to override via config.json."""
    saved_posts_url: str = SAVED_POSTS_URL
    cdp_port: int = DEFAULT_CDP_PORT
    page_nav_timeout_ms: int = PAGE_NAV_TIMEOUT_MS
    video_playback_timeout_seconds: int = VIDEO_PLAYBACK_TIMEOUT_SECONDS


def load_config_file(path: Path | None = None) -> RuntimeConfig:
    """Load ``config.json`` from the project root if present.

    Unknown keys are ignored; missing keys keep their default. Returns
    defaults untouched if no config file exists.
    """
    config_path = path if path is not None else (project_root() / CONFIG_FILE_NAME)
    defaults = RuntimeConfig()

    if not config_path.exists():
        return defaults

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    known_fields = {f.name for f in fields(RuntimeConfig)}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    return RuntimeConfig(**{**defaults.__dict__, **filtered})
