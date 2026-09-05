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
