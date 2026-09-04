"""Downloading images/PDFs/documents through the authenticated browser
context (so LinkedIn's auth cookies apply), plus the activity-ID/hash
fallback used as a stable per-post directory name."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from linkedin_archiver.linkedin_urls import activity_id_from_url


def stable_post_id(url: str) -> str:
    """Activity ID when available, else a short stable hash of the URL."""
    activity_id = activity_id_from_url(url)
    if activity_id:
        return activity_id
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def guess_extension(url: str, content_type: str) -> str:
    suffix = Path(unquote(urlsplit(url).path)).suffix.lower()
    if suffix:
        return suffix
    extension = mimetypes.guess_extension(content_type.split(";")[0].strip())
    return extension or ".bin"


def download_media(context, urls: set[str], media_dir: Path, referer: str) -> list[dict]:
    """Download each URL via the browser's authenticated request context.
    Returns metadata for every file actually saved; skips non-file
    responses (HTML/JSON) and failures rather than raising."""
    media_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for index, url in enumerate(sorted(urls), start=1):
        try:
            response = context.request.get(url, headers={"Referer": referer}, timeout=60_000)

            if not response.ok:
                response.dispose()
                continue

            content_type = response.headers.get("content-type", "").lower()

            if "text/html" in content_type or "application/json" in content_type:
                response.dispose()
                continue

            extension = guess_extension(url, content_type)
            file_path = media_dir / f"media_{index:02d}{extension}"
            file_path.write_bytes(response.body())
            response.dispose()

            results.append({"type": content_type, "url": url, "file": file_path.name})
        except Exception:
            continue

    return results
