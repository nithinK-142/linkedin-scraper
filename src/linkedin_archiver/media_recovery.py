"""General media recovery used by the recovery stage.

This complements the existing fragmented-MP4 capture. It captures normal
image/audio/video/document responses and media referenced by the DOM. The
existing fMP4 video path remains in video_capture.py and is never replaced.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

MEDIA_CONTENT_PREFIXES = ("image/", "audio/", "video/")
MEDIA_CONTENT_TYPES = {"application/pdf", "application/zip"}
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg",
    ".mp4", ".webm", ".mov", ".m4v", ".mp3", ".wav", ".m4a",
    ".pdf", ".zip",
}


class MediaCapture:
    def __init__(self, *, media_dir: Path, logger=None):
        self.media_dir = media_dir
        self.logger = logger
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._pending: set[asyncio.Task] = set()
        self._seen_sources: set[str] = set()
        self._saved: list[dict] = []
        self._seen_saved_sources: set[str] = set()
        self._index = 0

    @property
    def saved_count(self) -> int:
        return len(self._saved)

    def has_source(self, source_url: str) -> bool:
        return source_url in self._seen_saved_sources

    def add_saved(self, path: Path, *, source_url: str, content_type: str | None, media_kind: str) -> None:
        if source_url in self._seen_saved_sources:
            return
        self._seen_saved_sources.add(source_url)
        self._saved.append(
            {
                "type": content_type or "application/octet-stream",
                "url": source_url,
                "file": path.name,
                "kind": media_kind,
            }
        )

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._pending.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc and self.logger:
            self.logger.debug(f"    media response handler error: {exc}")

    async def drain(self, timeout: float = 15.0) -> None:
        if not self._pending:
            return
        tasks = list(self._pending)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def unique_media_path(self, preferred: Path) -> Path:
        candidate = preferred
        if not candidate.exists():
            return candidate
        stem, suffix = preferred.stem, preferred.suffix
        counter = 2
        while True:
            candidate = preferred.with_name(f"{stem}-{counter}{suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    async def handle_response(self, response) -> None:
        request = response.request
        if request.resource_type not in {"image", "media", "document", "other"}:
            return
        if response.status < 200 or response.status >= 300:
            return
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        url = response.url
        if not _looks_like_media(url, content_type):
            return
        if url in self._seen_sources:
            return
        self._seen_sources.add(url)

        try:
            body = await response.body()
        except Exception:
            return
        if len(body) < 256:
            return
        if content_type.startswith("video/") and _is_fragmented_video(body=body, content_type=content_type):
            return

        async with self._lock:
            self._index += 1
            index = self._index

        extension = _extension(url, content_type)
        path = self.unique_media_path(self.media_dir / f"media_{index:02d}{extension}")
        try:
            path.write_bytes(body)
        except Exception:
            return

        kind = content_type.split("/", 1)[0] if "/" in content_type else "media"
        self.add_saved(path, source_url=url, content_type=content_type, media_kind=kind)
        if self.logger:
            self.logger.debug(f"    Saved media: {path.name} {content_type} {len(body)} bytes")


def _mp4_boxes(data: bytes) -> set[bytes]:
    return {tag for tag in (b"ftyp", b"moov", b"moof", b"mdat", b"styp", b"sidx") if tag in data}


def _is_fragmented_video(*, body: bytes | None, content_type: str) -> bool:
    if body is None:
        return False
    if not content_type.startswith("video/") and not content_type.startswith("application/mp4"):
        # Content-Type is unreliable on browser responses; inspect MP4 boxes anyway.
        pass
    boxes = _mp4_boxes(body)
    return b"moof" in boxes or (b"mdat" in boxes and b"ftyp" not in boxes and b"moov" not in boxes)


def _looks_like_media(url: str, content_type: str) -> bool:
    if content_type in MEDIA_CONTENT_TYPES or content_type.startswith(MEDIA_CONTENT_PREFIXES):
        return True
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in MEDIA_EXTENSIONS)


def _extension(url: str, content_type: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        return suffix
    return mimetypes.guess_extension(content_type) or ".bin"


async def save_dom_media(context, page, media_dir: Path, referer: str, capture: MediaCapture, logger=None) -> None:
    """Download media URLs visible in the page using the authenticated browser context."""
    media_dir.mkdir(parents=True, exist_ok=True)
    try:
        entries = await page.locator("img, picture source, video, audio, source, a[href]").evaluate_all(
            """nodes => nodes.map(node => ({
                tag: node.tagName.toLowerCase(),
                src: node.currentSrc || node.src || node.getAttribute('src') || '',
                href: node.href || node.getAttribute('href') || '',
                poster: node.poster || node.getAttribute('poster') || ''
            }))"""
        )
    except Exception:
        entries = []

    urls: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        candidates = [entry.get("src"), entry.get("poster"), entry.get("href")]
        for raw in candidates:
            if raw and raw not in seen and _looks_like_media(raw, ""):
                seen.add(raw)
                urls.append(raw)

    for url in urls:
        if capture.has_source(url):
            continue
        try:
            response = await context.request.get(url, headers={"Referer": referer}, timeout=60_000)
            if not response.ok:
                await response.dispose()
                continue
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            body = await response.body()
            await response.dispose()
            if not body or not _looks_like_media(url, content_type):
                continue
            if _is_fragmented_video(body=body, content_type=content_type):
                continue
            digest = hashlib.sha1(url.encode()).hexdigest()[:10]
            path = capture.unique_media_path(media_dir / f"media_{digest}{_extension(url, content_type)}")
            path.write_bytes(body)
            kind = content_type.split("/", 1)[0] if "/" in content_type else "media"
            capture.add_saved(path, source_url=url, content_type=content_type, media_kind=kind)
        except Exception as exc:
            if logger:
                logger.debug(f"    DOM media download failed: {url[:120]} ({exc})")
