#!/usr/bin/env python3
"""Stage 1: collect every LinkedIn saved-post URL.

Captures network responses on https://www.linkedin.com/my-items/saved-posts/
and extracts activity URNs, preserving the order LinkedIn exposes them in.
DOM scanning is a secondary fallback only. Output is continuously saved so
Ctrl+C or a browser interruption never loses already-collected URLs.

Usage:
    python scripts/linkedin_saved.py
    python scripts/linkedin_saved.py --browser brave --profile "Profile 4"
    python scripts/linkedin_saved.py --output data/linkedin_saved_posts.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from linkedin_archiver import settings as cfg
from linkedin_archiver.browser import (
    BrowserSessionError, ensure_browser_session, find_or_open_page,
    add_browser_selection_args, resolve_browser_target,
)
from linkedin_archiver.linkedin_data import extract_activity_urls_from_text, normalize_url
from linkedin_archiver.settings import setup_logging
from linkedin_archiver.settings import default_saved_posts_file

from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_browser_selection_args(parser)
    parser.add_argument(
        "--output",
        default=None,
        help=f"Output JSON file. Default: {default_saved_posts_file()}",
    )
    return parser.parse_args()


def save_urls(urls: list[str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(urls, indent=2, ensure_ascii=False), encoding="utf-8")


def collect_dom_urls(page) -> list[str]:
    found = []
    for link in page.locator("a[href]").all():
        try:
            href = link.get_attribute("href")
        except Exception:
            continue
        normalized = normalize_url(href)
        if normalized:
            found.append(normalized)
    return found


def click_show_more(page) -> bool:
    selectors = [
        'button:has-text("Show more results")',
        'button:has-text("Show more")',
        '[aria-label*="Show more"]',
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible():
                button.click()
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def main() -> None:
    args = parse_args()
    logger = setup_logging("linkedin_saved")
    runtime_config = cfg.load_config_file()

    output = Path(args.output) if args.output else default_saved_posts_file()
    logger.info(f"Output file: {output}")

    try:
        target = resolve_browser_target(args, default_port=runtime_config.cdp_port)
    except SystemExit:
        raise
    except Exception as exc:
        logger.error(f"Could not resolve browser/profile: {exc}")
        raise SystemExit(1) from exc

    logger.info(f"Browser: {target.spec.display_name}  Profile: {target.profile.directory}  Port: {target.port}")

    try:
        cdp_url = ensure_browser_session(
            target.spec, target.executable, target.user_data_dir, target.profile.directory, target.port,
            start_url=runtime_config.saved_posts_url, logger=logger,
        )
    except BrowserSessionError as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc

    with sync_playwright() as p:
        logger.info(f"Connecting over CDP: {cdp_url}")
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=10_000)

        if not browser.contexts:
            logger.error("No browser context found after connecting.")
            raise SystemExit(1)

        context = browser.contexts[0]
        page = find_or_open_page(context, url_hint="linkedin.com")

        urls: list[str] = []
        seen: set[str] = set()

        def add_urls(candidates: list[str]) -> None:
            for url in candidates:
                normalized = normalize_url(url)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                urls.append(normalized)
                logger.debug(f"FOUND [{len(urls):04}] {normalized}")
                save_urls(urls, output)

        def on_response(response) -> None:
            try:
                if "linkedin.com" not in response.url:
                    return
                request = response.request
                if request.resource_type not in {"document", "xhr", "fetch"}:
                    return
                content_type = response.headers.get("content-type", "").lower()
                if not any(v in content_type for v in ("json", "javascript", "html", "text")):
                    return
                candidates = extract_activity_urls_from_text(response.text())
                if candidates:
                    add_urls(candidates)
            except Exception:
                pass

        page.on("response", on_response)

        logger.info(f"Opening: {runtime_config.saved_posts_url}")
        page.goto(runtime_config.saved_posts_url, wait_until="domcontentloaded", timeout=cfg.PAGE_NAV_TIMEOUT_MS)
        page.wait_for_timeout(5000)

        if "login" in page.url.lower():
            logger.error("LinkedIn is not logged in on this profile.")
            raise SystemExit(1)

        logger.info("Collecting network data...")

        unchanged_rounds = 0
        last_scroll_y = -1

        try:
            for iteration in range(cfg.MAX_SCROLL_ITERATIONS):
                before = len(urls)
                add_urls(collect_dom_urls(page))
                click_show_more(page)

                page.evaluate(
                    "window.scrollBy(0, Math.floor(window.innerHeight * 0.60));"
                )
                page.wait_for_timeout(cfg.SCROLL_SETTLE_TIMEOUT_MS)
                add_urls(collect_dom_urls(page))

                scroll_y = page.evaluate("window.scrollY")
                scroll_height = page.evaluate("document.documentElement.scrollHeight")
                viewport_height = page.evaluate("window.innerHeight")
                reached_bottom = scroll_y + viewport_height >= scroll_height - 100

                logger.debug(
                    f"SCAN [{iteration + 1:03}] posts={len(urls)} scroll={scroll_y:.0f}/{scroll_height:.0f}"
                )

                if len(urls) == before and reached_bottom and scroll_y == last_scroll_y:
                    unchanged_rounds += 1
                else:
                    unchanged_rounds = 0
                last_scroll_y = scroll_y

                if unchanged_rounds >= cfg.MAX_UNCHANGED_SCROLL_ROUNDS:
                    break

        except KeyboardInterrupt:
            logger.warning("Stopped by user.")
        except Exception as exc:
            logger.warning(f"Browser/session stopped: {exc}")
        finally:
            save_urls(urls, output)
            logger.info(f"Collected: {len(urls)} unique post URLs")
            logger.info(f"Saved to: {output.resolve()}")
            logger.info("Done. (Browser was left running untouched.)")


if __name__ == "__main__":
    main()
