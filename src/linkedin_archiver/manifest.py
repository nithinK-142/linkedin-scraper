"""Resumable-run manifest handling, shared by the archiver and video
capture scripts.

The manifest is a JSON object keyed by a stable post ID (the activity ID
where available), so reruns can skip completed work and retry only
unfinished or failed items.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
    manifest: dict[str, Any],
    post_id: str,
    *,
    index: int,
    url: str,
    status: str,
    author: str | None = None,
    username: str | None = None,
    media_count: int = 0,
    error: str | None = None,
    timestamp: str | None = None,
) -> None:
    """Write/overwrite one manifest entry in place."""
    manifest[post_id] = {
        "index": index,
        "url": url,
        "status": status,
        "author": author,
        "username": username,
        "media_count": media_count,
        "error": error,
        "timestamp": timestamp,
    }


def is_done(manifest: dict[str, Any], post_id: str) -> bool:
    entry = manifest.get(post_id)
    return bool(entry) and entry.get("status") in Status.TERMINAL_SUCCESS
