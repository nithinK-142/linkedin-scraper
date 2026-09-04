"""Paths, logging, and configuration — all in one small module."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path

# ---------------------------------------------------------------- paths ---

def project_root() -> Path:
    """Find the local project root without depending on the current directory."""
    configured = os.environ.get("LINKEDIN_ARCHIVER_HOME")
    if configured:
        return Path(configured).expanduser().resolve()

    current = Path(__file__).resolve()
    for candidate in current.parents:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "linkedin_archiver").exists():
            return candidate
    return Path.cwd()


def data_dir() -> Path:
    return project_root() / "data"


def archive_dir() -> Path:
    return project_root() / "archive"


def logs_dir() -> Path:
    return project_root() / "logs"


def default_saved_posts_file() -> Path:
    return data_dir() / "linkedin_saved_posts.json"


def default_failed_posts_file() -> Path:
    return data_dir() / "failed_posts.json"


def default_video_output_dir() -> Path:
    return archive_dir() / "videos"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# -------------------------------------------------------------- logging ---

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    script_name: str,
    *,
    log_dir: Path | None = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """Console + per-script log file. Never logs cookies/tokens/credentials."""
    logger = logging.getLogger(script_name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger  # already configured

    target_dir = ensure_dir(log_dir if log_dir is not None else logs_dir())
    log_file = target_dir / f"{script_name}.log"
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    return logger


# --------------------------------------------------------------- config ---

SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"

DEFAULT_CDP_PORT = 9222
CDP_PORT_SCAN_RANGE = 20

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
    """Values overridable via an optional config.json. No credentials here."""
    saved_posts_url: str = SAVED_POSTS_URL
    cdp_port: int = DEFAULT_CDP_PORT
    page_nav_timeout_ms: int = PAGE_NAV_TIMEOUT_MS
    video_playback_timeout_seconds: int = VIDEO_PLAYBACK_TIMEOUT_SECONDS


def load_config_file(path: Path | None = None) -> RuntimeConfig:
    config_path = path if path is not None else (project_root() / CONFIG_FILE_NAME)
    defaults = RuntimeConfig()

    if not config_path.exists():
        return defaults

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    known_fields = {f.name for f in fields(RuntimeConfig)}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    return RuntimeConfig(**{**defaults.__dict__, **filtered})
