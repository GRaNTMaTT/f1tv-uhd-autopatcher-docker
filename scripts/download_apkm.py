#!/usr/bin/env python3

"""
Download the F1 TV Android TV APKM bundle from APKMirror using Playwright.

APKMirror download flow:

1. Release page
   - Contains one or more APK/APKM variants.

2. Variant page
   - Contains the "Download APK Bundle" button.

3. Download trigger page
   - Contains the final #download-link URL.

4. Browser download
   - Captured directly with Playwright page.expect_download().

Debug screenshots are saved at each major step.
"""

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PwTimeout,
)


BASE = "https://www.apkmirror.com"


# ---------------------------------------------------------------------------
# Ad / tracking blocking
# ---------------------------------------------------------------------------

AD_DOMAIN_KEYWORDS = [
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "adservice.google",
    "pagead2.googlesyndication",
    "tpc.googlesyndication",
    "fundingchoicesmessages.google",
    "amazon-adsystem.com",
    "adskeeper.co.uk",
    "adnxs.com",
    "adsrvr.org",
    "outbrain.com",
    "taboola.com",
    "criteo.com",
    "pubmatic.com",
    "rubiconproject.com",
    "openx.net",
    "casalemedia.com",
    "moatads.com",
    "serving-sys.com",
    "quantserve.com",
    "scorecardresearch.com",
    "hotjar.com",
    "facebook.net",
    "connect.facebook",
    "cdn.privacy-mgmt.com",
    "sp-prod.net",
    "consent.cookiebot",
    "consensu.org",
    "gstatic.com/adsense",
]


# ---------------------------------------------------------------------------
# JavaScript used to remove overlays / ads / consent dialogs
# ---------------------------------------------------------------------------

NUKE_ADS_JS = """
() => {
    const selectors = [
        '[id*="google_ads"]',
        '[id*="aswift"]',
        '[class*="ad-overlay"]',
        '[class*="ad-container"]',
        '[class*="ad-wrapper"]',
        '[class*="interstitial"]',
        '[class*="modal-backdrop"]',

        'iframe[src*="doubleclick"]',
        'iframe[src*="googlesyndication"]',
        'iframe[id*="aswift"]',
        'iframe[id*="google_ads"]',

        '[id*="consent"]',
        '[class*="consent"]',
        '[class*="cookie-banner"]',

        '.fc-dialog-container',
        '.fc-consent-root',

        '#cmpbox',
        '#cmpbox2',

        '[id*="sp_message"]',
        '[class*="sp_message"]'
    ];

    let removed = 0;

    for (const selector of selectors) {
        for (const element of document.querySelectorAll(selector)) {
            element.remove();
            removed++;
        }
    }

    /*
     * Remove large fixed/sticky overlays that may intercept clicks.
     */
    for (const element of document.querySelectorAll(
        'div, aside, section'
    )) {
        const style = window.getComputedStyle(element);

        const fixed =
            style.position === 'fixed' ||
            style.position === 'sticky';

        const highZ =
            parseFloat(style.zIndex || '0') > 999;

        const large =
            element.offsetWidth > window.innerWidth * 0.5 &&
            element.offsetHeight > window.innerHeight * 0.3;

        if (fixed && highZ && large) {
            element.remove();
            removed++;
        }
    }

    /*
     * Some dialogs disable page scrolling even after the dialog itself
     * has been removed.
     */
    if (document.body) {
        document.body.style.overflow = 'auto';
    }

    document.documentElement.style.overflow = 'auto';

    return removed;
}
"""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    print(
        f"[download] {message}",
        file=sys.stderr,
        flush=True,
    )


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------

def screenshot(page, output_dir: Path, name: str) -> None:
    path = output_dir / f"debug_{name}.png"

    try:
        page.screenshot(
            path=str(path),
            full_page=True,
        )

        log(f"  screenshot: {path}")

    except Exception as exc:
        log(
            f"  WARN: Could not save screenshot "
            f"{path}: {exc}"
        )


# ---------------------------------------------------------------------------
# Remove advertisements and overlays
# ---------------------------------------------------------------------------

def nuke_ads(page) -> None:
    try:
        removed = page.evaluate(NUKE_ADS_JS)

        if removed:
            log(
                f"  Removed {removed} "
                f"ad/overlay elements"
            )

    except Exception:
        # Page may be navigating when this runs.
        pass


# ---------------------------------------------------------------------------
# Cloudflare handling
# ---------------------------------------------------------------------------

def wait_for_cloudflare(
    page,
    timeout: int = 15,
) -> None:

    for attempt in range(timeout):

        try:
            title = page.title().lower()

        except Exception:
            title = ""

        challenge = (
            "just a moment" in title
            or "checking" in title
            or "cloudflare" in title
        )

        if challenge:

            if attempt == 0:
                log(
                    "  Cloudflare challenge detected, "
                    "waiting..."
                )

            /*
             * Calling wait_for_timeout keeps Playwright involved rather
             * than using a long blocking Python sleep.
             */
            page.wait_for_timeout(1000)

        else:
            return

    log(
        "  WARN: Cloudflare may not have resolved"
    )


# ---------------------------------------------------------------------------
# Find APK Bundle variant
# ---------------------------------------------------------------------------

def find_bundle_variant_url(page) -> str | None:

    /*
     * Strategy 1:
     * Look through the variants table for rows containing BUNDLE.
     */
    rows = page.query_selector_all(
        ".variants-table .table-row, "
        ".variants-table tr"
    )

    for row in rows:

        try:
            text = row.inner_text().upper()

        except Exception:
            continue

        if "BUNDLE" not in text:
            continue

        link = row.query_selector(
            "a[href*='apk-download']"
        )

        if link:
            return link.get_attribute("href")

    /*
     * Strategy 2:
     * Broader search for apk-download links whose surrounding
     * element contains BUNDLE.
     */
    links = page.query_selector_all(
        "a[href*='apk-download']"
    )

    for link in links:

        try:
            parent_text = link.evaluate(
                """
                el => (
                    el.closest(
                        '.table-row, tr, .list-widget'
                    ) || el.parentElement
                ).textContent || ''
                """
            )

        except Exception:
            continue

        if "BUNDLE" in parent_text.upper():
            return link.get_attribute("href")

    return None


# ---------------------------------------------------------------------------
# Download APKM
# ---------------------------------------------------------------------------

def download_apkm(
    release_url: str,
    variant_url: str | None,
    output_dir: str,
) -> Path:

    output_path = Path(output_dir)

    output_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    with sync_playwright() as playwright:

        # ------------------------------------------------------------------
        # Browser setup
        # ------------------------------------------------------------------

        browser = playwright.chromium.launch(
            headless=True,
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 "
                "Safari/537.36"
            ),
            viewport={
                "width": 1920,
                "height": 1080,
            },
            accept_downloads=True,
        )

        page = context.new_page()

        # ------------------------------------------------------------------
        # Network-level ad blocking
        # ------------------------------------------------------------------

        def block_ads(route):
            route.abort()

        for domain in AD_DOMAIN_KEYWORDS:

            page.route(
                f"**/*{domain}*",
                block_ads,
            )

        log(
            "Ad blocker active (network-level)"
        )

        # ==================================================================
        # STEP 1
        # Release page
        # ==================================================================

        log(
            f"Step 1: Loading release page: "
            f"{release_url}"
        )

        page.goto(
            release_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        wait_for_cloudflare(page)

        try:
            page.wait_for_load_state(
                "load",
                timeout=30000,
            )

        except PwTimeout:
            log(
                "  WARN: Full page load timed out; "
                "continuing"
            )

        nuke_ads(page)

        screenshot(
            page,
            output_path,
            "01_release_page",
        )

        log(
            f"  Page title: {page.title()}"
        )

        # ==================================================================
        # STEP 2
        # Find APK Bundle variant
        # ==================================================================

        log(
            "Step 2: Looking for APK Bundle variant..."
        )

        already_on_variant = bool(
            page.query_selector(
                "a.downloadButton[href*='key=']"
            )
        )

        if already_on_variant:

            log(
                "  Already on variant page "
                "(download button present)"
            )

        else:

            bundle_href = find_bundle_variant_url(
                page
            )

            if not bundle_href:

                screenshot(
                    page,
                    output_path,
                    "02_no_bundle_found",
                )

                if variant_url:

                    log(
                        "  Bundle variant not found; "
                        "falling back to supplied "
                        "variant URL"
                    )

                    bundle_href = variant_url

                else:

                    log(
                        "ERROR: Could not find "
                        "APK Bundle variant on "
                        "release page"
                    )

                    browser.close()
                    sys.exit(1)

            if bundle_href.startswith("/"):
                bundle_href = (
                    BASE + bundle_href
                )

            log(
                f"  Navigating to variant page: "
                f"{bundle_href}"
            )

            page.goto(
                bundle_href,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            wait_for_cloudflare(page)

            try:
                page.wait_for_load_state(
                    "load",
                    timeout=30000,
                )

            except PwTimeout:
                log(
                    "  WARN: Full variant page "
                    "load timed out; continuing"
                )

            nuke_ads(page)

            screenshot(
                page,
                output_path,
                "03_variant_page",
            )

            log(
                f"  Page title: "
                f"{page.title()}"
            )

        # ==================================================================
        # STEP 3
        # Find Download APK Bundle button
        # ==================================================================

        log(
            "Step 3: Finding download button..."
        )

        btn_selector = "a.downloadButton"

        try:

            page.wait_for_selector(
                btn_selector,
                timeout=15000,
            )

        except PwTimeout:

            screenshot(
                page,
                output_path,
                "04_no_download_btn",
            )

            try:
                links = page.evaluate(
                    """
                    () =>
                        Array.from(
                            document.querySelectorAll('a')
                        )
                        .slice(0, 30)
                        .map(a => ({
                            class: a.className,
                            href: a.href,
                            text:
                                a.textContent
                                .trim()
                                .substring(0, 80)
                        }))
                    """
                )

                log(
                    f"  Page has these links: "
                    f"{links}"
                )

            except Exception:
                pass

            log(
                "ERROR: Download button not found "
                "(a.downloadButton)"
            )

            browser.close()
            sys.exit(1)

        btn_info = page.evaluate(
            """
            () => {
                const btn =
                    document.querySelector(
                        'a.downloadButton'
                    );

                if (!btn) {
                    return null;
                }

                return {
                    href: btn.href,
                    text:
                        btn.textContent.trim(),
                    classes:
                        btn.className
                };
            }
            """
        )

        log(
            f"  Found button: {btn_info}"
        )

        # ==================================================================
        # STEP 4
        # Navigate to APKMirror trigger page
        # ==================================================================

        log(
            "Step 4: Navigating to download "
            "trigger page via JS..."
        )

        key_href = page.evaluate(
            """
            () => {
                const btn =
                    document.querySelector(
                        'a.downloadButton'
                    );

                return btn
                    ? btn.href
                    : null;
            }
            """
        )

        if not key_href:

            log(
                "ERROR: Could not extract "
                "download button href"
            )

            browser.close()
            sys.exit(1)

        log(
            f"  Navigating to: {key_href}"
        )

        page.goto(
            key_href,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        wait_for_cloudflare(page)

        nuke_ads(page)

        screenshot(
            page,
            output_path,
            "05_trigger_page",
        )

        log(
            f"  Trigger page title: "
            f"{page.title()}"
        )

        # ==================================================================
        # STEP 5
        # Capture actual browser download
        # ==================================================================

        log(
            "Step 5: Looking for download link..."
        )

        dl_link_selector = "a#download-link"

        try:

            page.wait_for_selector(
                dl_link_selector,
                timeout=10000,
            )

            dl_href = page.evaluate(
                """
                () => {
                    const link =
                        document.querySelector(
                            'a#download-link'
                        );

                    return link
                        ? link.href
                        : null;
                }
                """
            )

            log(
                f"  Found #download-link: "
                f"{dl_href}"
            )

            nuke_ads(page)

            /*
             * IMPORTANT:
             *
             * The old implementation used:
             *
             *     page.on("download", ...)
             *
             * followed by:
             *
             *     while download_event is None:
             *         time.sleep(...)
             *
             * With Playwright's synchronous API that polling loop can
             * prevent the queued Playwright download event from being
             * dispatched until another Playwright API call occurs.
             *
             * expect_download() solves the race by arming the download
             * listener before clicking and waiting on Playwright itself.
             */
            with page.expect_download(
                timeout=120000
            ) as download_info:

                page.click(
                    dl_link_selector
                )

            download_event = (
                download_info.value
            )

            log(
                "  >> Download event received!"
            )

            log(
                "  Clicked #download-link"
            )

        except PwTimeout:

            screenshot(
                page,
                output_path,
                "06_download_timeout",
            )

            debug_html = (
                output_path
                / "debug_trigger_page.html"
            )

            try:
                debug_html.write_text(
                    page.content()
                )

                log(
                    f"  Trigger page HTML "
                    f"saved to {debug_html}"
                )

            except Exception:
                pass

            log(
                "ERROR: Download did not start "
                "within 120 seconds"
            )

            browser.close()
            sys.exit(1)

        # ==================================================================
        # STEP 6
        # Save downloaded APKM
        # ==================================================================

        filename = (
            download_event.suggested_filename
            or "f1tv-android-tv.apkm"
        )

        save_path = (
            output_path / filename
        )

        try:

            download_event.save_as(
                str(save_path)
            )

        except Exception as exc:

            log(
                f"ERROR: Could not save "
                f"download: {exc}"
            )

            browser.close()
            sys.exit(1)

        if not save_path.exists():

            log(
                "ERROR: Playwright reported a "
                "download but the saved file "
                f"does not exist: {save_path}"
            )

            browser.close()
            sys.exit(1)

        file_size = (
            save_path.stat().st_size
        )

        if file_size <= 0:

            log(
                "ERROR: Downloaded file "
                "is empty"
            )

            browser.close()
            sys.exit(1)

        size_mb = (
            file_size
            / (1024 * 1024)
        )

        log(
            f"  Saved: {filename} "
            f"({size_mb:.1f} MB)"
        )

        browser.close()

        return save_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Download F1 TV Android TV "
            "APKM from APKMirror"
        )
    )

    parser.add_argument(
        "release_url",
        help=(
            "APKMirror release page URL"
        ),
    )

    parser.add_argument(
        "--variant-url",
        default=None,
        help=(
            "Direct APKMirror variant URL "
            "used as a fallback if a bundle "
            "cannot be identified automatically"
        ),
    )

    parser.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help=(
            "Directory in which to save "
            "the downloaded APKM "
            "(default: current directory)"
        ),
    )

    args = parser.parse_args()

    path = download_apkm(
        args.release_url,
        args.variant_url,
        args.output_dir,
    )

    # stdout is intentionally reserved for the downloaded path so
    # shell/CI callers can consume it.
    print(str(path))


if __name__ == "__main__":
    main()
