from __future__ import annotations

import asyncio
from pathlib import Path

from linkedin_archiver.linkedin_data import activity_id_from_url, normalize_url
from linkedin_archiver.media_recovery import MediaCapture


def test_normalize_linkedin_post_url():
    assert normalize_url("https://linkedin.com/feed/update/urn:li:activity:123/?x=1") == (
        "https://www.linkedin.com/feed/update/urn:li:activity:123"
    )
    assert normalize_url("https://example.com/feed/update/urn:li:activity:123") is None
    assert activity_id_from_url("https://www.linkedin.com/feed/update/urn:li:activity:123") == "123"


def test_media_capture_deduplicates_source(tmp_path: Path):
    capture = MediaCapture(media_dir=tmp_path)
    first = tmp_path / "one.jpg"
    first.write_bytes(b"data")
    capture.add_saved(first, source_url="https://example.test/a.jpg", content_type="image/jpeg", media_kind="image")
    capture.add_saved(first, source_url="https://example.test/a.jpg", content_type="image/jpeg", media_kind="image")
    assert capture.saved_count == 1
    assert capture.has_source("https://example.test/a.jpg")


def test_media_capture_unique_path(tmp_path: Path):
    capture = MediaCapture(media_dir=tmp_path)
    target = tmp_path / "video.mp4"
    target.write_bytes(b"existing")
    assert capture.unique_media_path(target).name == "video-2.mp4"


def test_fragmented_video_is_left_for_fmp4_recovery():
    from linkedin_archiver.media_recovery import _is_fragmented_video

    assert _is_fragmented_video(body=b"xxxxftypxxxxmoovxxxxmdat", content_type="video/mp4") is False
    assert _is_fragmented_video(body=b"xxxxmoofxxxxmdat", content_type="video/mp4") is True
    assert _is_fragmented_video(body=b"xxxxmdat", content_type="video/mp4") is True
