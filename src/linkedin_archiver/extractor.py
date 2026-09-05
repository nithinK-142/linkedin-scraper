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

from linkedin_archiver.downloader import RetryPolicy, download_sync
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
_MEDIA_NODES_JS = "nodes => nodes.map(node => node.currentSrc || node.src || node.getAttribute('src') || node.href || node.getAttribute('href') || '').filter(Boolean)"


def _candidate_matches_activity(element, activity_id: str) -> bool:
    needle = f"urn:li:activity:{activity_id}"
    for attr in ("data-urn", "data-id"):
        try:
            value = element.get_attribute(attr) or ""
            if needle in value or activity_id in value:
                return True
        except Exception:
            pass

    try:
        return element.locator(
            f'a[href*="urn:li:activity:{activity_id}"], '
            f'a[href*="/feed/update/urn:li:activity:{activity_id}"]'
        ).count() > 0
    except Exception:
        return False


def _add_root_candidate(candidates, element, activity_id: str) -> None:
    try:
        if element.count() == 0 or not element.is_visible():
            return
        element = element.first
        if _candidate_matches_activity(element, activity_id):
            candidates.add(element)
    except Exception:
        pass


def find_main_post(page, activity_id: str):
    """Locate the post containing the requested activity ID.

    LinkedIn has used both ``data-urn`` and ``data-id`` on post roots, and
    the activity ID can also be exposed only through a permalink inside the
    post. Use those as fallbacks, then score the matching root candidates.
    """
    candidates = set()
    needle = f"urn:li:activity:{activity_id}"

    direct_selectors = (
        f'[data-urn*="{needle}"]',
        f'[data-id*="{needle}"]',
    )
    for selector in direct_selectors:
        try:
            for element in page.locator(selector).all():
                if element.is_visible():
                    candidates.add(element)
        except Exception:
            pass

    # Some layouts expose the activity ID only on a permalink. Promote that
    # link to the nearest post container instead of treating the link itself
    # as the post.
    try:
        links = page.locator(
            f'a[href*="urn:li:activity:{activity_id}"], '
            f'a[href*="/feed/update/urn:li:activity:{activity_id}"]'
        ).all()
        for link in links:
            ancestor_selectors = (
                "xpath=ancestor::article[1]",
                'xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " feed-shared-update-v2 ")][1]',
                'xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " occludable-update ")][1]',
                'xpath=ancestor::*[@data-finite-scroll-hotkey-item][1]',
            )
            for ancestor_selector in ancestor_selectors:
                try:
                    root = link.locator(ancestor_selector)
                    if root.count() and root.is_visible():
                        candidates.add(root)
                        break
                except Exception:
                    continue
    except Exception:
        pass

    # Broad fallback for layouts where neither attribute uses the activity
    # URN directly. This is slower, so keep it behind the direct strategies.
    if not candidates:
        try:
            roots = page.locator(
                'article, .feed-shared-update-v2, .occludable-update, '
                'div[data-id], div[data-urn], div[data-finite-scroll-hotkey-item]'
            ).all()
            for root in roots:
                _add_root_candidate(candidates, root, activity_id)
        except Exception:
            pass

    if not candidates:
        return None

    scored = []
    for element in candidates:
        try:
            if not element.is_visible():
                continue

            score = 0
            tag = element.evaluate("el => el.tagName")
            classes = element.get_attribute("class") or ""
            data_urn = element.get_attribute("data-urn") or ""
            data_id = element.get_attribute("data-id") or ""

            if tag == "ARTICLE":
                score += 20
            if "feed-shared-update-v2" in classes:
                score += 30
            if "occludable-update" in classes:
                score += 10
            if activity_id in data_urn or activity_id in data_id:
                score += 20
            if "comment" in classes.lower():
                score -= 20

            scored.append((score, element))
        except Exception:
            continue

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


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
        urls = post.locator("a[href], video, audio, source, track").evaluate_all(_MEDIA_NODES_JS)
        for url in urls:
            lower = url.lower()
            if any(ext in lower for ext in extensions) and not url.startswith(("blob:", "data:")):
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
    """Download discovered media through the authenticated browser context."""
    media_dir.mkdir(parents=True, exist_ok=True)
    results = []
    seen_hashes: set[str] = set()

    for index, url in enumerate(sorted(urls), start=1):
        if url.lower().endswith((".m3u8", ".mpd")):
            continue
        temp_path = media_dir / f"media_{index:02d}.download"
        try:
            result = download_sync(
                context,
                url,
                temp_path,
                referer=referer,
                policy=RetryPolicy(),
            )
            content_type = result["content_type"]
            if content_type in {
                "text/html",
                "application/json",
                "application/x-mpegurl",
                "application/vnd.apple.mpegurl",
            }:
                temp_path.unlink(missing_ok=True)
                continue

            digest = result["sha256"]
            if digest in seen_hashes:
                temp_path.unlink(missing_ok=True)
                continue
            seen_hashes.add(digest)

            extension = guess_extension(url, content_type)
            file_path = media_dir / f"media_{index:02d}{extension}"
            temp_path.replace(file_path)
            results.append({
                "type": content_type,
                "url": url,
                "file": file_path.name,
                "bytes": result["bytes"],
                "sha256": digest,
            })
        except Exception:
            temp_path.unlink(missing_ok=True)
            continue

    return results
