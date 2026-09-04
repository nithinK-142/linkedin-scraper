"""Fragmented-MP4 network capture and reconstruction.

This is the proven solution for LinkedIn posts (mostly company/organization
pages) where the normal DOM-based archiver gets an authwall or empty data,
even though the post loads fine in a real logged-in profile. It captures
the actual fragmented MP4 responses while the video plays and reconstructs
them with ffmpeg — it never touches the <video> element's `blob:` URL,
which cannot be downloaded directly.

Key correctness rules, preserved from the original script:
* Never assume a URL ends in .mp4, that resource_type == "Media", or that
  the MIME type is video/*. Detect actual MP4/fMP4 box signatures instead.
* Never assume one response contains the whole video — capture and
  reassemble every fragment.
* An initialization segment (ftyp+moov) must actually be present before
  claiming success. If it isn't, save the raw parts and report failure
  explicitly rather than producing a broken file.
* "File exists" is never proof of success — always validate with ffprobe.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

BOX_TAGS = (b"ftyp", b"moov", b"moof", b"mdat", b"styp", b"sidx")


@dataclass
class CaptureState:
    parts_dir: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seq: int = 0
    captures: list[dict] = field(default_factory=list)


def detect_boxes(data: bytes) -> list[str]:
    return [tag.decode() for tag in BOX_TAGS if tag in data]


def content_range_start(header_value: str | None) -> int | None:
    if not header_value:
        return None
    try:
        # e.g. "bytes 12345-67890/999999"
        range_part = header_value.split()[1]
        return int(range_part.split("-")[0])
    except Exception:
        return None


class PendingTaskTracker:
    """Tracks background response-handler tasks so they can be drained
    before we read parts_dir, and so their exceptions are retrieved
    instead of logged as "never retrieved" noise on teardown."""

    def __init__(self, logger=None):
        self._pending: set[asyncio.Task] = set()
        self._logger = logger

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._pending.add(task)

        def _on_done(t: asyncio.Task, _task=task):
            self._pending.discard(_task)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None and "closed" not in str(exc).lower():
                message = f"    (background response handler error: {exc})"
                if self._logger:
                    self._logger.debug(message)
                else:
                    print(message)

        task.add_done_callback(_on_done)
        return task

    async def drain(self, timeout: float = 15.0) -> None:
        if not self._pending:
            return
        tasks = list(self._pending)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def handle_response(response, state: CaptureState, logger=None) -> None:
    request = response.request
    try:
        err = await response.finished()
    except Exception:
        err = "exception while waiting for response to finish"
    if err:
        return

    try:
        body = await response.body()
    except Exception:
        return

    if not body:
        return

    boxes = detect_boxes(body)
    if not boxes:
        return

    async with state.lock:
        state.seq += 1
        seq = state.seq

    headers = response.headers
    entry = {
        "sequence": seq,
        "url": response.url,
        "resource_type": request.resource_type,
        "status": response.status,
        "content_type": headers.get("content-type"),
        "content_length_header": headers.get("content-length"),
        "actual_byte_size": len(body),
        "content_range": headers.get("content-range"),
        "boxes": boxes,
        "timestamp": time.time(),
    }

    filename = f"part_{seq:04d}.bin"
    (state.parts_dir / filename).write_bytes(body)
    entry["file"] = filename
    state.captures.append(entry)

    is_init = "ftyp" in boxes and "moov" in boxes
    label = "initialization segment" if is_init else f"fragment (seq {seq})"
    message = f"    Captured {label}: boxes=[{','.join(boxes)}] bytes={len(body)} url={response.url[:100]}"
    if logger:
        logger.debug(message)
    else:
        print(message)


async def handle_video_playback(page, playback_timeout_seconds: int, logger=None) -> tuple[bool, str | None]:
    from playwright.async_api import TimeoutError as PWTimeout

    def log(message: str) -> None:
        if logger:
            logger.info(message)
        else:
            print(message)

    try:
        video_el = await page.wait_for_selector("video", timeout=30_000)
    except PWTimeout:
        return False, "no video element found"

    try:
        await video_el.scroll_into_view_if_needed()
    except Exception:
        pass

    result = await page.evaluate(
        """async () => {
            const v = document.querySelector('video');
            if (!v) return {ok: false, reason: 'no-video'};
            try {
                v.muted = true;
                await v.play();
                return {ok: true};
            } catch (e) {
                return {ok: false, reason: e && e.message};
            }
        }"""
    )

    if not result.get("ok"):
        log("    Autoplay was blocked.")
        log("    >>> Click Play on the LinkedIn video. <<<")
        try:
            await page.wait_for_function(
                """() => {
                    const v = document.querySelector('video');
                    return v && v.currentTime > 0;
                }""",
                timeout=120_000,
            )
        except PWTimeout:
            return False, "timed out waiting for manual playback to start"

    log(f"    Playback started. Waiting for it to finish (up to {playback_timeout_seconds}s)...")
    finished = await _wait_for_playback_end(page, playback_timeout_seconds)
    log("    Playback finished." if finished else "    Playback wait timed out; proceeding with whatever was captured.")
    return True, None


async def _wait_for_playback_end(page, timeout: int) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        state = await page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (!v) return null;
                return {currentTime: v.currentTime, duration: v.duration, ended: v.ended};
            }"""
        )
        if state is None:
            return False
        if state.get("ended"):
            return True
        duration = state.get("duration")
        current = state.get("currentTime") or 0
        if duration and duration > 0 and current >= duration - 0.5:
            return True
        await asyncio.sleep(1)
    return False


def split_into_tracks(captures: list[dict]) -> list[dict]:
    """Split captures into tracks by actual capture order: any entry
    containing both ftyp and moov starts a new track; everything after it
    (until the next such entry) is a fragment of that track. Never sorted
    or grouped by URL."""
    ordered = sorted(captures, key=lambda e: e["sequence"])
    tracks: list[dict] = []
    current = None
    for entry in ordered:
        is_init = "ftyp" in entry["boxes"] and "moov" in entry["boxes"]
        if is_init:
            current = {"init": entry, "fragments": []}
            tracks.append(current)
        elif current is not None:
            current["fragments"].append(entry)
    return tracks


def run_ffmpeg_remux(src: Path, dst: Path, logger=None) -> bool:
    cmd = ["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = f"    ffmpeg failed to remux {src.name}:\n    " + result.stderr.strip()[-1500:].replace("\n", "\n    ")
        (logger.warning if logger else print)(message)
        return False
    return True


def mux_video_audio(video_path: Path, audio_path: Path, dst: Path, logger=None) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        message = "    ffmpeg mux of separate audio/video tracks failed:\n    " + result.stderr.strip()[-1500:].replace("\n", "\n    ")
        (logger.warning if logger else print)(message)
        shutil.copyfile(video_path, dst)
        return False
    return True


def run_ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"valid": False}
    try:
        data = json.loads(result.stdout)
    except Exception:
        return {"valid": False}

    streams = data.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    video_codec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None)
    audio_codec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)
    duration = data.get("format", {}).get("duration")
    duration_hms = None
    if duration:
        try:
            duration_hms = str(timedelta(seconds=round(float(duration))))
        except Exception:
            pass

    return {
        "valid": has_video,
        "has_video": has_video,
        "has_audio": has_audio,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "duration": duration,
        "duration_hms": duration_hms,
    }


def check_ffmpeg_tools_available() -> list[str]:
    """Returns a list of missing tool names (empty if both are present)."""
    missing = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def reconstruct(post_dir: Path, state: CaptureState, logger=None) -> dict:
    """Reconstruct a video from captured fragments. Returns a result dict
    with at least a ``status`` key; never claims success without an
    ffprobe-verified video stream."""

    def log(message: str) -> None:
        (logger.info if logger else print)(message)

    log("    Reconstructing...")
    captures = state.captures

    if not captures:
        log("    No fragment data was captured for this post.")
        (post_dir / "video.mp4.FAILED").write_text("No responses were captured.\n")
        return {"status": "no_data"}

    tracks = split_into_tracks(captures)

    if not tracks:
        log("    Initialization segment not captured.")
        (post_dir / "video.mp4.FAILED").write_text(
            f"No ftyp+moov initialization segment was found among the "
            f"{len(captures)} captured response(s). Raw parts and capture.json "
            f"were saved for inspection.\n"
        )
        return {"status": "no_init_segment"}

    used = sum(1 + len(t["fragments"]) for t in tracks)
    orphans = len(captures) - used
    if orphans > 0:
        log(f"    Note: {orphans} fragment(s) arrived before any initialization "
            f"segment and could not be used.")

    built = []
    for idx, track in enumerate(tracks):
        frags = track["fragments"]
        if frags and all(content_range_start(f.get("content_range")) is not None for f in frags):
            frags = sorted(frags, key=lambda f: content_range_start(f["content_range"]))

        combined = bytearray((state.parts_dir / track["init"]["file"]).read_bytes())
        for f in frags:
            combined += (state.parts_dir / f["file"]).read_bytes()

        raw_path = post_dir / f"track_{idx}.fmp4"
        raw_path.write_bytes(combined)

        mp4_path = post_dir / f"track_{idx}.mp4"
        if run_ffmpeg_remux(raw_path, mp4_path, logger=logger):
            info = run_ffprobe(mp4_path)
            built.append({"path": mp4_path, "info": info})

    if not built:
        log("    ffmpeg could not remux any candidate track.")
        (post_dir / "video.mp4.FAILED").write_text(
            "An initialization segment was found, but ffmpeg failed to remux "
            "every candidate track. Raw parts and capture.json were saved.\n"
        )
        return {"status": "remux_failed"}

    final_path = post_dir / "video.mp4"

    if len(built) == 1:
        shutil.copyfile(built[0]["path"], final_path)
    else:
        video_tracks = [t for t in built if t["info"].get("has_video")]
        audio_tracks = [t for t in built if t["info"].get("has_audio") and not t["info"].get("has_video")]
        if video_tracks and audio_tracks:
            mux_video_audio(video_tracks[0]["path"], audio_tracks[0]["path"], final_path, logger=logger)
        elif video_tracks:
            shutil.copyfile(video_tracks[0]["path"], final_path)
        else:
            largest = max(built, key=lambda t: t["path"].stat().st_size)
            shutil.copyfile(largest["path"], final_path)

    verify = run_ffprobe(final_path)
    if not verify.get("valid"):
        log("    ffmpeg produced a file but ffprobe could not validate a video stream in it.")
        final_path.rename(post_dir / "video.mp4.UNVERIFIED")
        return {"status": "unverified", "ffprobe": verify}

    log("    FFmpeg remux successful.")
    log(f"    Duration: {verify.get('duration_hms')}")
    log(f"    Video: {verify.get('video_codec')}")
    log(f"    Audio: {verify.get('audio_codec')}")
    log(f"    Saved: {final_path}")
    return {"status": "completed", "ffprobe": verify, "path": str(final_path)}


def write_capture_json(post_dir: Path, url: str, state: CaptureState, note: str = "") -> None:
    payload = {
        "post_url": url,
        "note": note,
        "num_responses_captured": len(state.captures),
        "responses": state.captures,
    }
    (post_dir / "capture.json").write_text(json.dumps(payload, indent=2))
