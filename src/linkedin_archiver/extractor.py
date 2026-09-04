"""Extracting a single post's content from an opened LinkedIn page, and
downloading its media through the authenticated browser context.

Core rule this file exists to protect: the main post is matched by its
activity ID, never by grabbing the first ``article`` on the page —
``page.locator("article").first`` can select a comment instead of the
actual saved post. Do not regress that.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from linkedin_archiver.linkedin_data import activity_id_from_url

# ---------------------------------------------------------- post content --

SEE_MORE_SELECTORS = (
    'button:has-text("See more")',
    'button:has-text("see more")',
    'button:has-text("…see more")',
    'button:has-text("...see more")',
)

_TEXT_SELECTORS = (
    '[data-testid="expandable-text-box"]',
    "div.feed-shared-update-v2__description-wrapper",
    "div.update-components-text",
    "div.feed-shared-text",
)

_TIMESTAMP_SELECTORS = (
    "time",
    '[class*="update-components-actor__sub-description"]',
)

_IMAGE_EXTRACTION_JS = """
imgs => imgs.map(img => ({
    src: img.currentSrc || img.src,
    width: img.naturalWidth,
    height: img.naturalHeight
})).filter(x =>
    x.src &&
    !x.src.startsWith("data:") &&
    !x.src.startsWith("blob:") &&
    x.width >= 150 &&
    x.height >= 100
)
"""

_LINK_HREFS_JS = "links => links.map(a => a.href).filter(Boolean)"


def find_main_post(page, activity_id: str):
    """Locate the post element matching ``activity_id``, scoring
    candidates so the real post (not a comment referencing the same
    activity) wins."""
    selector = f'[data-urn*="urn:li:activity:{activity_id}"]'
    matches = page.locator(selector).all()

    if not matches:
        return None

    candidates = []

    for element in matches:
        try:
            if not element.is_visible():
                continue

            score = 0
            try:
                if element.evaluate("el => el.tagName") == "ARTICLE":
                    score += 10
            except Exception:
                pass

            try:
                classes = element.get_attribute("class") or ""
                if "feed-shared-update-v2" in classes:
                    score += 20
                if "feed-shared-update-v2__commentary" in classes:
                    score += 30
            except Exception:
                pass

            candidates.append((score, element))
        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def expand_see_more(post, page, wait_ms: int = 700) -> bool:
    for selector in SEE_MORE_SELECTORS:
        try:
            button = post.locator(selector).first
            if button.is_visible():
                button.click()
                page.wait_for_timeout(wait_ms)
                return True
        except Exception:
            continue
    return False


def extract_post_text(post) -> str:
    for selector in _TEXT_SELECTORS:
        try:
            node = post.locator(selector).first
            if node.is_visible():
                text = node.inner_text().strip()
                if text:
                    return text
        except Exception:
            pass

    try:
        return post.inner_text().strip()
    except Exception:
        return ""


def extract_author(post) -> tuple[str | None, str | None, str | None]:
    """Returns (author_name, username, profile_url)."""
    author = None
    profile_url = None
    username = None

    try:
        links = post.locator('a[href*="/in/"], a[href*="/company/"]').all()
        candidates = []

        for link in links:
            try:
                if not link.is_visible():
                    continue
                href = link.get_attribute("href")
                if not href:
                    continue
                name = link.inner_text().strip()
                if not name:
                    continue
                candidates.append((href, " ".join(name.split())))
            except Exception:
                continue

        if candidates:
            profile_url, author = candidates[0]
    except Exception:
        pass

    if profile_url and profile_url.startswith("/"):
        profile_url = "https://www.linkedin.com" + profile_url

    if profile_url:
        parts = urlsplit(profile_url).path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "in":
            username = parts[1]

    return author, username, profile_url


def extract_timestamp(post) -> str | None:
    for selector in _TIMESTAMP_SELECTORS:
        try:
            node = post.locator(selector).first
            if not node.is_visible():
                continue
            value = node.get_attribute("datetime") or node.inner_text().strip()
            if value:
                return value
        except Exception:
            pass
    return None


def extract_images(post) -> set[str]:
    images = set()
    try:
        for item in post.locator("img").evaluate_all(_IMAGE_EXTRACTION_JS):
            images.add(item["src"])
    except Exception:
        pass
    return images


def extract_direct_media(post, extensions: tuple[str, ...]) -> set[str]:
    media = set()
    try:
        for url in post.locator("a[href]").evaluate_all(_LINK_HREFS_JS):
            lower = url.lower()
            if any(ext in lower for ext in extensions):
                media.add(url)
    except Exception:
        pass
    return media


# --------------------------------------------------------------- media ---

def stable_post_id(url: str) -> str:
    """Activity ID when available, else a short stable hash of the URL."""
    activity_id = activity_id_from_url(url)
    if activity_id:
        return activity_id
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def guess_extension(url: str, content_type: str) -> str:
    suffix = Path(unquote(urlsplit(url).path)).suffix.lower()
    if suffix:
        return suffix
    extension = mimetypes.guess_extension(content_type.split(";")[0].strip())
    return extension or ".bin"


def download_media(context, urls: set[str], media_dir: Path, referer: str) -> list[dict]:
    """Download each URL via the browser's authenticated request context.
    Returns metadata for every file actually saved; skips non-file
    responses and failures rather than raising."""
    media_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for index, url in enumerate(sorted(urls), start=1):
        try:
            response = context.request.get(url, headers={"Referer": referer}, timeout=60_000)

            if not response.ok:
                response.dispose()
                continue

            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                response.dispose()
                continue

            extension = guess_extension(url, content_type)
            file_path = media_dir / f"media_{index:02d}{extension}"
            file_path.write_bytes(response.body())
            response.dispose()

            results.append({"type": content_type, "url": url, "file": file_path.name})
        except Exception:
            continue

    return results
