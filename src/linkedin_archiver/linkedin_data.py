"""LinkedIn URL/activity-ID handling, and the resumable-run manifest
shared by the archiver and video-capture scripts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ------------------------------------------------------------------ URLs --

ACTIVITY_URN_PATTERN = re.compile(r"urn:li:activity:(\d+)")

_ALLOWED_HOSTS = {"www.linkedin.com", "linkedin.com"}


def activity_id_from_url(url: str) -> str | None:
    match = ACTIVITY_URN_PATTERN.search(url)
    return match.group(1) if match else None


def activity_id_to_feed_url(activity_id: str) -> str:
    return f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}"


def extract_activity_urls_from_text(text: str) -> list[str]:
    """Every activity URN in a text blob, turned into a canonical feed
    URL, in the order they were found."""
    if not text:
        return []
    return [activity_id_to_feed_url(m.group(1)) for m in ACTIVITY_URN_PATTERN.finditer(text)]


def normalize_url(url: str | None) -> str | None:
    """Canonicalize a candidate saved-post URL, or None if it's not a
    LinkedIn post/update URL worth keeping."""
    if not url:
        return None
    if url.startswith("/"):
        url = "https://www.linkedin.com" + url

    parts = urlsplit(url)
    if parts.netloc not in _ALLOWED_HOSTS:
        return None

    path = parts.path
    if "/feed/update/" not in path and "/posts/" not in path:
        return None

    return urlunsplit(("https", "www.linkedin.com", path.rstrip("/"), "", ""))


# -------------------------------------------------------------- manifest --

class Status:
    COMPLETED = "completed"
    FAILED = "failed"
    LOGIN_REQUIRED = "login_required"
    CHALLENGE = "challenge"
    UNAVAILABLE = "unavailable"
    EXTRACTION_FAILED = "extraction_failed"
    MEDIA_FAILED = "media_failed"

    TERMINAL_SUCCESS = {COMPLETED}


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def record(
    manifest: dict[str, Any], post_id: str, *,
    index: int, url: str, status: str,
    author: str | None = None, username: str | None = None,
    media_count: int = 0, error: str | None = None, timestamp: str | None = None,
) -> None:
    manifest[post_id] = {
        "index": index, "url": url, "status": status,
        "author": author, "username": username,
        "media_count": media_count, "error": error, "timestamp": timestamp,
    }


def is_done(manifest: dict[str, Any], post_id: str) -> bool:
    entry = manifest.get(post_id)
    return bool(entry) and entry.get("status") in Status.TERMINAL_SUCCESS
