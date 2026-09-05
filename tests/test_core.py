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


def test_download_sync_resumes_part_file(tmp_path: Path):
    from linkedin_archiver.downloader import RetryPolicy, download_sync

    class Response:
        status = 206
        ok = True
        headers = {"content-type": "image/jpeg", "content-range": "bytes 5-9/10"}

        def body(self):
            return b"56789"

        def dispose(self):
            pass

    class Request:
        def __init__(self):
            self.headers = None

        def get(self, url, headers=None, timeout=None):
            self.headers = headers
            return Response()

    class Context:
        def __init__(self):
            self.request = Request()

    destination = tmp_path / "image.jpg"
    Path(f"{destination}.part").write_bytes(b"01234")
    context = Context()
    result = download_sync(context, "https://example.test/image.jpg", destination, policy=RetryPolicy(max_attempts=1))
    assert destination.read_bytes() == b"0123456789"
    assert not Path(f"{destination}.part").exists()
    assert context.request.headers["Range"] == "bytes=5-"
    assert result["bytes"] == 10


def test_retryable_status_codes():
    from linkedin_archiver.downloader import is_retryable_status

    assert is_retryable_status(429)
    assert is_retryable_status(502)
    assert not is_retryable_status(404)
    assert not is_retryable_status(403)


def test_cli_exposes_sleep_flag():
    from typer.testing import CliRunner
    from linkedin_archiver.cli import app

    result = CliRunner().invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--sleep" in result.stdout


def test_sleep_range_parsing():
    from linkedin_archiver.throttle import parse_sleep

    fixed = parse_sleep("2")
    ranged = parse_sleep("2-5")
    assert (fixed.minimum, fixed.maximum) == (2.0, 2.0)
    assert (ranged.minimum, ranged.maximum) == (2.0, 5.0)


def test_safety_signals_fail_closed():
    from linkedin_archiver.safety import SafetyMonitor, SafetyStop, inspect_url_and_text

    signal = inspect_url_and_text(
        "https://www.linkedin.com/feed/",
        "We detected an unusually large number of page views from your account.",
    )
    assert signal is not None
    assert signal.kind == "restriction"

    monitor = SafetyMonitor()
    monitor.observe_response("https://www.linkedin.com/feed/update/urn:li:activity:1", 429)
    try:
        monitor.raise_if_triggered()
    except SafetyStop as exc:
        assert exc.signal.kind == "rate_limit"
    else:
        raise AssertionError("Expected SafetyStop")


def test_cli_exposes_sleep_for_all_stages():
    from typer.testing import CliRunner
    from linkedin_archiver.cli import app

    runner = CliRunner()
    for command in ("collect", "archive", "recover", "run"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--sleep" in result.stdout


def test_retry_after_seconds_parses_delta_and_http_date():
    from datetime import datetime, timezone, timedelta
    from email.utils import format_datetime
    from linkedin_archiver.downloader import retry_after_seconds

    assert retry_after_seconds({"retry-after": "12"}) == 12.0
    future = datetime.now(timezone.utc) + timedelta(seconds=3)
    delay = retry_after_seconds({"retry-after": format_datetime(future, usegmt=True)})
    assert delay is not None
    assert 0 <= delay <= 4



def test_config_toml_loads_comments_and_values(tmp_path: Path):
    from linkedin_archiver.settings import load_config_file

    config = tmp_path / "config.toml"
    config.write_text(
        """# Comment\n[invalid_section]\nignored = true\n""".replace("[invalid_section]\nignored = true\n", "") +
        """browser = \"brave\"
profile = \"Profile 4\"
limit = 25
sleep = \"2-4\"
recover_urls = [\"https://www.linkedin.com/feed/update/urn:li:activity:1\"]
skip_recover = true
""",
        encoding="utf-8",
    )

    loaded = load_config_file(config)
    assert loaded.browser == "brave"
    assert loaded.profile == "Profile 4"
    assert loaded.limit == 25
    assert loaded.sleep == "2-4"
    assert loaded.recover_urls == ("https://www.linkedin.com/feed/update/urn:li:activity:1",)
    assert loaded.skip_recover is True


def test_repo_config_is_valid_toml():
    from linkedin_archiver.settings import load_config_file, project_root

    loaded = load_config_file(project_root() / "config.toml")
    assert loaded.limit is None
    assert loaded.sleep == "0"


def test_configured_cli_flags_are_exposed():
    from typer.testing import CliRunner
    from linkedin_archiver.cli import app

    runner = CliRunner()
    for command in ("profiles", "collect", "archive", "recover", "run"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0

    result = runner.invoke(app, ["run", "--help"])
    assert "--skip-recover" in result.stdout
