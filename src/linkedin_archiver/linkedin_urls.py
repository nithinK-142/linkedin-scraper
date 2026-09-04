"""LinkedIn URL normalization and activity-ID extraction.

Centralizes logic that was previously duplicated (and slightly
inconsistent) across linkedin_saved.py, archive_linkedin_posts.py, and
linkedin_video_capture.py.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

ACTIVITY_URN_PATTERN = re.compile(r"urn:li:activity:(\d+)")

_ALLOWED_HOSTS = {"www.linkedin.com", "linkedin.com"}


def activity_id_from_url(url: str) -> str | None:
    match = ACTIVITY_URN_PATTERN.search(url)
    return match.group(1) if match else None


def activity_id_to_feed_url(activity_id: str) -> str:
    return f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}"


def extract_activity_urls_from_text(text: str) -> list[str]:
    """Find every activity URN in a blob of text (JSON/HTML/JS response
    body) and turn each into a canonical feed URL, preserving the order
    they were found in."""
    if not text:
        return []
    return [activity_id_to_feed_url(m.group(1)) for m in ACTIVITY_URN_PATTERN.finditer(text)]


def normalize_url(url: str | None) -> str | None:
    """Canonicalize a candidate saved-post URL, or return None if it is
    not a LinkedIn post/update URL worth keeping."""
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
