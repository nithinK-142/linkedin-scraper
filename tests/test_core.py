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


def test_state_store_persists_post_and_recovers_after_reopen(tmp_path: Path):
    from linkedin_archiver.state import StateStore

    root = tmp_path / "archive"
    with StateStore(root) as state:
        state.record_post("123", index=1, url="https://www.linkedin.com/feed/update/urn:li:activity:123", status="completed", media_count=2)
        assert state.is_post_done("123")

    with StateStore(root) as state:
        row = state.get_post("123")
        assert row["media_count"] == 2
        assert state.post_counts() == {"completed": 1}
        state.record_recovery("123", index=1, url=row["url"], status="completed", media_count=2, video={"status": "direct_media"})

    with StateStore(root) as state:
        recovery = state.get_recovery("123")
        assert recovery["video"]["status"] == "direct_media"


def test_state_store_migrates_legacy_manifests(tmp_path: Path):
    from linkedin_archiver.state import StateStore

    root = tmp_path / "archive"
    root.mkdir()
    (root / "manifest.json").write_text(
        '{"123":{"index":1,"url":"https://www.linkedin.com/feed/update/urn:li:activity:123","status":"media_failed","media_count":1}}',
        encoding="utf-8",
    )
    (root / "recovery_manifest.json").write_text(
        '{"123":{"index":1,"url":"https://www.linkedin.com/feed/update/urn:li:activity:123","status":"completed","media_count":2,"video":{"status":"direct_media"}}}',
        encoding="utf-8",
    )

    with StateStore(root) as state:
        assert state.get_post("123")["status"] == "media_failed"
        assert state.get_recovery("123")["media_count"] == 2
