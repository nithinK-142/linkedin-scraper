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

_LINK_HREFS_JS = "links => links.map(a => a.href).filter(Boolean)"
_MEDIA_NODES_JS = """nodes => nodes.map(node => {
    const attachment = node.closest('[class*=\"feed-shared-document\"], [class*=\"update-components-document\"], [class*=\"feed-shared-article\"], [class*=\"feed-shared-attachment\"], [class*=\"feed-shared-video\"], [class*=\"feed-shared-image\"], [class*=\"feed-shared-carousel\"]');
    return {
        url: node.currentSrc || node.src || node.getAttribute('src') || node.href || node.getAttribute('href') || '',
        tag: node.tagName.toLowerCase(),
        attached: !!attachment
    };
}).filter(x => x.url && x.attached)"""


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
            f'a[href*="{activity_id}"], '
            f'[data-urn*="{activity_id}"], [data-id*="{activity_id}"]'
        ).count() > 0
    except Exception:
        return False


def _root_score(element, activity_id: str, *, dedicated_post_page: bool) -> int:
    score = 0
    try:
        tag = (element.evaluate("el => el.tagName") or "").upper()
        classes = (element.get_attribute("class") or "").lower()
        data_urn = element.get_attribute("data-urn") or ""
        data_id = element.get_attribute("data-id") or ""
        if tag == "ARTICLE":
            score += 25
        if (element.get_attribute("role") or "").lower() == "article":
            score += 25
        if "feed-shared-update-v2" in classes:
            score += 50
        if "occludable-update" in classes:
            score += 20
        if "update-components-update-v2" in classes:
            score += 25
        if activity_id in data_urn or activity_id in data_id:
            score += 50
        if _candidate_matches_activity(element, activity_id):
            score += 35
        if "comment" in classes or "reply" in classes:
            score -= 80
        if element.locator('[data-testid="expandable-text-box"], div.update-components-text, div.feed-shared-text').count():
            score += 12
        if element.locator('a[href*="/in/"], a[href*="/company/"]').count():
            score += 10
        try:
            text_len = len(element.inner_text())
            if 50 <= text_len <= 20_000:
                score += 8
            elif text_len > 60_000:
                score -= 50
        except Exception:
            pass
        if dedicated_post_page and (tag == "ARTICLE" or (element.get_attribute("role") or "").lower() == "article"):
            score += 10
    except Exception:
        return score
    return score


def _ancestor_candidates(element):
    selectors = (
        'xpath=ancestor::*[@data-urn and contains(@data-urn, "urn:li:activity:")][1]',
        'xpath=ancestor::*[@data-id and contains(@data-id, "urn:li:activity:")][1]',
        "xpath=ancestor::article[1]",
        "xpath=ancestor::*[@role='article'][1]",
        'xpath=ancestor::*[@data-finite-scroll-hotkey-item][1]',
        'xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " feed-shared-update-v2 ")][1]',
        'xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " occludable-update ")][1]',
        'xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " update-components-update-v2 ")][1]',
    )
    for selector in selectors:
        try:
            ancestor = element.locator(selector)
            if ancestor.count():
                yield ancestor.first
        except Exception:
            continue


def find_main_post(page, activity_id: str):
    """Locate the actual post root for an activity ID.

    LinkedIn changes its DOM frequently. Prefer direct activity-ID/permalink
    evidence, then choose the smallest plausible post container around that
    evidence. Never fall back to the whole page or an arbitrary first article.
    """
    dedicated_post_page = activity_id in page.url
    direct_matches = []

    def collect_direct_matches() -> None:
        selectors = (
            f'[data-urn*="urn:li:activity:{activity_id}"]',
            f'[data-id*="urn:li:activity:{activity_id}"]',
            f'[data-urn*="{activity_id}"]',
            f'[data-id*="{activity_id}"]',
            f'a[href*="urn:li:activity:{activity_id}"]',
            f'a[href*="{activity_id}"]',
        )
        for selector in selectors:
            try:
                direct_matches.extend(page.locator(selector).all())
            except Exception:
                continue

    # Let LinkedIn hydrate the dedicated post before selecting broad fallbacks.
    try:
        page.wait_for_selector(
            'article, [role="article"], [data-id], [data-urn], a[href]',
            state="attached",
            timeout=5_000,
        )
    except Exception:
        pass

    collect_direct_matches()
    if not direct_matches:
        try:
            page.wait_for_function(
                "activityId => Array.from(document.querySelectorAll('a[href], [data-id], [data-urn]')).some(el => ((el.getAttribute('href') || '') + ' ' + (el.getAttribute('data-id') || '') + ' ' + (el.getAttribute('data-urn') || '')).includes(activityId))",
                activity_id,
                timeout=12_000,
            )
        except Exception:
            pass
        collect_direct_matches()

    candidates = []
    for element in direct_matches:
        try:
            if not element.is_visible():
                continue
            candidates.append(element)
            candidates.extend(_ancestor_candidates(element))
        except Exception:
            continue

    # Only use broad containers when no direct activity/permalink evidence
    # exists. Deliberately NOT done: falling back to "any article on the
    # page" here previously caused wrong-post matches (grabbing an
    # unrelated recommended post, ad, or comment thread) whenever the
    # activity ID could not be found anywhere in the DOM. That produced
    # exactly the "downloads unrelated media" symptom this function must
    # not cause. Per this module's own rule: never fall back to the whole
    # page or an arbitrary first article — if there's no evidence tying an
    # element to this activity ID, report "not found" rather than guess.

    unique = []
    seen_handles = set()
    for element in candidates:
        try:
            key = element.evaluate(
                """el => {
                    const parts = [];
                    let current = el;
                    while (current && current.nodeType === 1) {
                        let index = 1;
                        let sibling = current.previousElementSibling;
                        while (sibling) {
                            index += 1;
                            sibling = sibling.previousElementSibling;
                        }
                        parts.unshift(current.tagName.toLowerCase() + ":" + index);
                        current = current.parentElement;
                    }
                    return parts.join(">");
                }"""
            )
        except Exception:
            key = repr(element)
        key = str(key)
        if key in seen_handles:
            continue
        seen_handles.add(key)
        unique.append(element)

    scored = []
    for element in unique:
        try:
            if not element.is_visible():
                continue
            score = _root_score(element, activity_id, dedicated_post_page=dedicated_post_page)
            # A direct permalink match is stronger than generic container shape.
            if (element.evaluate("el => el.tagName") or "").upper() == "A":
                score -= 100
            scored.append((score, element))
        except Exception:
            continue

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best = scored[0]
    if best_score < 30:
        return None
    return best


# ---------------------------------------------------------- post media --

_IMAGE_EXTRACTION_JS = """
imgs => imgs.map(img => {
    const actor = img.closest('[class*="update-components-actor"], [class*="feed-shared-actor"], [class*="avatar"], [aria-label*="profile picture"]');
    const attachment = img.closest('[class*="update-components-image"], [class*="feed-shared-image"], [class*="feed-shared-article"], [class*="feed-shared-carousel"], [class*="feed-shared-video"], [class*="document"]');
    return {
        src: img.currentSrc || img.src,
        actor: !!actor,
        attached: !!attachment && !actor
    };
}).filter(x =>
    x.src &&
    !x.src.startsWith('data:') &&
    !x.src.startsWith('blob:') &&
    x.attached
)
"""


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
            src = item.get("src") if isinstance(item, dict) else None
            if src:
                images.add(src)
    except Exception:
        pass
    return images


def extract_direct_media(post, extensions: tuple[str, ...]) -> set[str]:
    media = set()
    try:
        entries = post.locator("video, audio, source, track, a[href]").evaluate_all(_MEDIA_NODES_JS)
        for entry in entries:
            url = entry.get("url", "")
            lower = url.lower()
            if not url or url.startswith(("blob:", "data:")):
                continue
            if entry.get("tag") == "a" and not entry.get("attached"):
                continue
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
