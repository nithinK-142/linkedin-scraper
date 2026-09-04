"""Central path resolution for the project.

All default input/output locations are derived from the project root so
that scripts behave the same regardless of the caller's current working
directory. Every default here can be overridden on the CLI.
"""

from __future__ import annotations

from pathlib import Path


def project_root() -> Path:
    """Return the project root directory.

    This file lives at ``<root>/src/linkedin_archiver/paths.py``, so the
    root is three levels up from here. Using ``__file__`` (rather than
    ``Path.cwd()``) means defaults are correct no matter where the user
    invokes the script from.
    """
    return Path(__file__).resolve().parents[2]


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
    return project_root() / "archive" / "videos"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
