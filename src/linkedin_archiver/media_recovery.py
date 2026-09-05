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

from linkedin_archiver.downloader import RetryPolicy, atomic_write_bytes, download_async, sha256_bytes, sha256_file

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
        self._seen_hashes: set[str] = set()
        self._index = 0

    @property
    def saved_count(self) -> int:
        return len(self._saved)

    @property
    def saved_items(self) -> list[dict]:
        return list(self._saved)

    def has_source(self, source_url: str) -> bool:
        return source_url in self._seen_saved_sources

    def add_saved(
        self,
        path: Path,
        *,
        source_url: str,
        content_type: str | None,
        media_kind: str,
        sha256: str | None = None,
    ) -> bool:
        if source_url in self._seen_saved_sources or (sha256 and sha256 in self._seen_hashes):
            return False
        self._seen_saved_sources.add(source_url)
        if sha256:
            self._seen_hashes.add(sha256)
        self._saved.append(
            {
                "type": content_type or "application/octet-stream",
                "url": source_url,
                "file": path.name,
                "kind": media_kind,
                "bytes": path.stat().st_size if path.exists() else 0,
                "sha256": sha256 or sha256_file(path),
            }
        )
        return True

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
            digest = sha256_bytes(body)
            atomic_write_bytes(path, body)
        except Exception:
            return

        kind = content_type.split("/", 1)[0] if "/" in content_type else "media"
        if not self.add_saved(path, source_url=url, content_type=content_type, media_kind=kind, sha256=digest):
            path.unlink(missing_ok=True)
            return
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


async def save_dom_media(
    context,
    page,
    media_dir: Path,
    referer: str,
    capture: MediaCapture,
    logger=None,
    root=None,
) -> None:
    """Download only media belonging to ``root``.

    The old implementation scanned the entire page. On a LinkedIn post page
    that includes avatars, reaction icons, recommendation cards, and other
    unrelated resources. When a post root is available, only nodes inside
    that post are eligible.
    """
    media_dir.mkdir(parents=True, exist_ok=True)
    scope = root or page
    try:
        entries = await scope.locator(
            "img, picture source, video, audio, source, a[href]"
        ).evaluate_all(
            """nodes => nodes.map(node => ({
                tag: node.tagName.toLowerCase(),
                src: node.currentSrc || node.src || node.getAttribute('src') || '',
                href: node.href || node.getAttribute('href') || '',
                actor: !!node.closest('[class*=\\"update-components-actor\\"], [class*=\\"feed-shared-actor\\"], [class*=\\"avatar\\"], [aria-label*=\\"profile picture\\"]'),
            }))"""
        )
    except Exception:
        entries = []

    urls: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        tag = entry.get("tag")
        raw = entry.get("src") or entry.get("href")
        if not raw or raw.startswith(("blob:", "data:")) or raw in seen:
            continue
        if tag == "img" and entry.get("actor"):
            continue
        if tag == "a" and not _looks_like_media(raw, ""):
            continue
        if tag not in {"img", "source", "video", "audio", "a"}:
            continue
        seen.add(raw)
        urls.append(raw)

    for url in urls:
        if capture.has_source(url):
            continue
        try:
            result = await download_async(
                context,
                url,
                media_dir / f".media-{hashlib.sha1(url.encode()).hexdigest()[:12]}.download",
                referer=referer,
                policy=RetryPolicy(),
                logger=logger,
            )
            temp_path = Path(result["path"])
            content_type = result["content_type"]
            if not _looks_like_media(url, content_type):
                temp_path.unlink(missing_ok=True)
                continue
            if _is_fragmented_video(body=temp_path.read_bytes(), content_type=content_type):
                temp_path.unlink(missing_ok=True)
                continue

            extension = _extension(url, content_type)
            path = capture.unique_media_path(media_dir / f"media_{capture.saved_count + 1:02d}{extension}")
            temp_path.replace(path)
            kind = content_type.split("/", 1)[0] if "/" in content_type else "media"
            if not capture.add_saved(
                path,
                source_url=url,
                content_type=content_type,
                media_kind=kind,
                sha256=result["sha256"],
            ):
                path.unlink(missing_ok=True)
        except Exception as exc:
            try:
                Path(media_dir / f".media-{hashlib.sha1(url.encode()).hexdigest()[:12]}.download").unlink(missing_ok=True)
            except Exception:
                pass
            if logger:
                logger.debug(f"    DOM media download failed: {url[:120]} ({exc})")

