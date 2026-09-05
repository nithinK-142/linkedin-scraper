from playwright.sync_api import sync_playwright


def test_find_main_post_matches_current_feed_detail_markup_without_data_urn():
    """Regression test grounded in a real captured LinkedIn page: the
    current 'Update Detail' page has NO data-urn/data-id attributes and NO
    <article>/role=article elements at all. The post instead renders as
    role="listitem" with a componentkey containing "FeedType_FEED_DETAIL",
    while comments use a distinct "replaceableComment_urn:li:comment:(...)"
    componentkey. Without this marker-based path, find_main_post fell
    through to a lone, heavily-penalized <a> tag and returned None for a
    genuine, real post."""
    from linkedin_archiver.extractor import find_main_post

    activity_id = "7500775585367867392"
    html = f"""
    <main>
      <section aria-label="Primary content">
        <div role="listitem" componentkey="expandedXYZFeedType_FEED_DETAIL">
          <h2><span>Feed post</span></h2>
          <a href="https://www.linkedin.com/in/someone/">Author Name</a>
          <span data-testid="expandable-text-box">This is the real post text, long enough to matter.</span>
          <a href="https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/">Send</a>
        </div>
        <div componentkey="replaceableComment_urn:li:comment:(urn:li:activity:{activity_id},999)">
          <span data-testid="expandable-text-box">This is an unrelated comment, not the post.</span>
        </div>
      </section>
      <aside aria-label="Aside">
        <footer>
          <a href="https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/">More</a>
        </footer>
      </aside>
    </main>
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path="/usr/bin/chromium")
        page = browser.new_page()
        page.set_content(html)
        root = find_main_post(page, activity_id)
        assert root is not None
        assert (root.get_attribute("componentkey") or "").find("FeedType_FEED_DETAIL") != -1
        # Must be the post, not the comment or the aside's stray footer link.
        text = root.inner_text()
        assert "real post text" in text
        assert "unrelated comment" not in text
        browser.close()


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
