from playwright.sync_api import sync_playwright


def test_find_main_post_uses_activity_id_permalink_and_returns_smallest_post_root():
    from linkedin_archiver.extractor import find_main_post

    html = """
    <main>
      <article class="post">
        <a href="/posts/example-activity-123456789">Post permalink</a>
        <div class="text">Hello world from the post</div>
        <img src="post.jpg" width="800" height="600">
      </article>
      <article class="comment">
        <div>Comment</div>
        <img src="comment.jpg" width="800" height="600">
      </article>
    </main>
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium")
        page = browser.new_page()
        page.set_content(html)
        root = find_main_post(page, "123456789")
        assert root is not None
        assert (root.evaluate("el => el.tagName") or "").lower() == "article"
        assert root.locator("img").count() == 1
        browser.close()


def test_find_main_post_returns_none_instead_of_guessing_an_unrelated_article():
    """Regression test: when nothing on the page ties an element to the
    requested activity ID, find_main_post must report "not found" rather
    than fall back to some other, unrelated <article> on the page. The
    previous fallback (`page.locator('article').all()` with no activity-ID
    evidence at all) could pick a completely unrelated post/ad/comment,
    which then had its text and media extracted and downloaded instead of
    the actual target post — the "downloads unrelated media" bug."""
    from linkedin_archiver.extractor import find_main_post

    html = """
    <main>
      <article class="unrelated-recommended-post">
        <a href="/posts/some-other-activity-999999999">Different post</a>
        <div class="text">This is a completely different, unrelated post</div>
        <img src="unrelated.jpg" width="800" height="600">
      </article>
    </main>
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium")
        page = browser.new_page()
        page.set_content(html)
        # Ask for an activity ID that appears nowhere on the page.
        root = find_main_post(page, "123456789")
        assert root is None
        browser.close()
