def test_scoped_media_filter_excludes_ui_comments_and_manifests():
    from linkedin_archiver.media_recovery import filter_scoped_media_entries

    entries = [
        {"tag": "img", "src": "https://cdn.test/post.jpg", "width": 800, "height": 600, "attachment": True},
        {"tag": "img", "src": "https://cdn.test/avatar.jpg", "width": 400, "height": 400, "actor": True, "attachment": True},
        {"tag": "img", "src": "https://cdn.test/icon.png", "width": 24, "height": 24, "attachment": False},
        {"tag": "img", "src": "https://cdn.test/comment.jpg", "width": 800, "height": 600, "in_comment": True, "attachment": True},
        {"tag": "video", "src": "https://cdn.test/video.mp4", "attachment": True},
        {"tag": "a", "href": "https://cdn.test/document.pdf", "attachment": True},
        {"tag": "a", "href": "https://cdn.test/not-media", "attachment": True},
        {"tag": "source", "src": "https://cdn.test/video.m3u8", "inside_media": True},
    ]

    assert filter_scoped_media_entries(entries) == [
        "https://cdn.test/post.jpg",
        "https://cdn.test/video.mp4",
        "https://cdn.test/document.pdf",
    ]


def test_scoped_media_filter_excludes_large_unattached_images():
    """Regression test: a large image inside the (correctly-scoped) post
    root that isn't wrapped in a recognized attachment container must NOT
    be downloaded just because it's big. The previous size-based fallback
    (`width < 200 or height < 150`) let unrelated large images — banners,
    related-content thumbnails, cover photos — through even when nothing
    marked them as part of the post. This is the root cause behind
    "downloading unrelated media files"."""
    from linkedin_archiver.media_recovery import filter_scoped_media_entries

    entries = [
        {"tag": "img", "src": "https://cdn.test/real-post-image.jpg", "width": 900, "height": 700, "attachment": True},
        {"tag": "img", "src": "https://cdn.test/unrelated-large-banner.jpg", "width": 1200, "height": 800, "attachment": False},
    ]

    assert filter_scoped_media_entries(entries) == ["https://cdn.test/real-post-image.jpg"]


def test_scoped_media_filter_excludes_unattached_video_and_audio():
    """Regression test: video/audio tags used to bypass the attachment
    check entirely (any <video>/<audio> with a src was kept). An
    autoplaying recommendation/ad video with no attachment evidence must
    be excluded, same as everything else."""
    from linkedin_archiver.media_recovery import filter_scoped_media_entries

    entries = [
        {"tag": "video", "src": "https://cdn.test/post-video.mp4", "attachment": True},
        {"tag": "video", "src": "https://cdn.test/unrelated-autoplay.mp4", "attachment": False},
        {"tag": "audio", "src": "https://cdn.test/unrelated-audio.mp3", "attachment": False},
    ]

    assert filter_scoped_media_entries(entries) == ["https://cdn.test/post-video.mp4"]
