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
