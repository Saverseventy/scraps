import os
import time
import atexit
import threading
from urllib.parse import urlparse

from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURATION
# ============================================================

APP_NAME = "Vidstorm M3U8 Scraper"

PORT = int(os.environ.get("PORT", "5000"))

PAGE_TIMEOUT = int(
    os.environ.get("PAGE_TIMEOUT", "90000")
)

WAIT_AFTER_LOAD = float(
    os.environ.get("WAIT_AFTER_LOAD", "5")
)

# Default allowed hosts.
#
# You can override this in Render Environment Variables:
#
# ALLOWED_HOSTS=vidstorm.ru,www.vidstorm.ru
#
ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.environ.get(
        "ALLOWED_HOSTS",
        "vidstorm.ru,www.vidstorm.ru",
    ).split(",")
    if host.strip()
}

# Optional API key.
#
# If API_KEY is empty, API-key protection is disabled.
#
API_KEY = os.environ.get("API_KEY", "").strip()

# CORS
#
# Default:
# *
#
# Or Render environment variable:
#
# CORS_ORIGINS=https://example.com
#
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "*",
).strip()


# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)

if CORS_ORIGINS == "*":
    CORS(app)
else:
    allowed_origins = [
        origin.strip()
        for origin in CORS_ORIGINS.split(",")
        if origin.strip()
    ]

    CORS(
        app,
        resources={
            r"/*": {
                "origins": allowed_origins
            }
        },
    )


# ============================================================
# PLAYWRIGHT / CHROMIUM
# ============================================================

_playwright = None
_browser = None

_browser_lock = threading.Lock()


def get_browser():
    """
    Start Playwright Chromium lazily.

    Chromium is NOT launched when Gunicorn imports app.py.
    It starts only when /scrape is requested.

    This is more reliable on Render.
    """

    global _playwright
    global _browser

    with _browser_lock:

        if _browser is not None:

            try:
                if _browser.is_connected():
                    return _browser
            except Exception:
                pass

            try:
                _browser.close()
            except Exception:
                pass

            _browser = None

        if _playwright is None:

            _playwright = sync_playwright().start()

        try:

            _browser = _playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-zygote",

                    # Reduce unnecessary Chromium background activity.
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",

                    # Useful for small Render instances.
                    "--disable-features=Translate",
                ],
            )

            print("[BROWSER] Chromium started")

            return _browser

        except Exception as exc:

            print(
                "[BROWSER] Failed to start Chromium:"
                f" {exc}"
            )

            raise


def shutdown_browser():
    """
    Cleanly close Chromium when Gunicorn exits.
    """

    global _playwright
    global _browser

    print("[BROWSER] Shutting down...")

    try:
        if _browser is not None:
            _browser.close()
    except Exception:
        pass

    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception:
        pass

    _browser = None
    _playwright = None


atexit.register(shutdown_browser)


# ============================================================
# API KEY
# ============================================================

def check_api_key():
    """
    API key is optional.

    If API_KEY is configured on Render:

    Header:
        X-API-Key: YOUR_API_KEY

    OR query parameter:
        ?api_key=YOUR_API_KEY
    """

    if not API_KEY:
        return True

    supplied_key = (
        request.headers.get("X-API-Key")
        or request.args.get("api_key")
        or ""
    )

    return supplied_key == API_KEY


# ============================================================
# URL VALIDATION
# ============================================================

def validate_target_url(target_url):
    """
    Validate URL before giving it to Playwright.

    Only domains listed in ALLOWED_HOSTS are accepted.
    """

    try:

        parsed = urlparse(target_url)

        if parsed.scheme not in (
            "http",
            "https",
        ):
            return (
                False,
                "Only HTTP and HTTPS URLs are allowed",
            )

        hostname = (
            parsed.hostname or ""
        ).lower()

        if not hostname:

            return (
                False,
                "Invalid hostname",
            )

        if hostname not in ALLOWED_HOSTS:

            return (
                False,
                "Host not allowed. Allowed hosts: "
                + ", ".join(
                    sorted(ALLOWED_HOSTS)
                ),
            )

        return True, None

    except Exception:

        return (
            False,
            "Invalid URL",
        )


# ============================================================
# M3U8 SCORING
# ============================================================

def score_stream(url):
    """
    Rank captured M3U8 URLs.

    Master/index/playlist URLs are usually more useful
    than individual media playlists.
    """

    value = url.lower()

    score = 0

    if ".m3u8" in value:
        score += 10

    if "master" in value:
        score += 8

    if "index" in value:
        score += 7

    if "playlist" in value:
        score += 6

    if "manifest" in value:
        score += 5

    if "stream" in value:
        score += 2

    if "token" in value:
        score += 1

    return score


# ============================================================
# SCRAPER
# ============================================================

def super_scraper(target_url):

    browser = get_browser()

    streams = set()

    context = None
    page = None

    try:

        # ----------------------------------------------------
        # BROWSER CONTEXT
        # ----------------------------------------------------

        context = browser.new_context(

            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0.0.0 "
                "Safari/537.36"
            ),

            viewport={
                "width": 1280,
                "height": 720,
            },

            java_script_enabled=True,

            ignore_https_errors=False,

        )

        # ----------------------------------------------------
        # PAGE
        # ----------------------------------------------------

        page = context.new_page()

        page.set_default_timeout(15000)

        # ----------------------------------------------------
        # NETWORK RESPONSE LISTENER
        # ----------------------------------------------------

        def handle_response(response):

            try:

                response_url = response.url

                lower_url = response_url.lower()

                # We only care about M3U8 playlists.
                if ".m3u8" not in lower_url:
                    return

                # Ignore HTTP errors.
                if response.status >= 400:
                    return

                if response_url not in streams:

                    streams.add(response_url)

                    print(
                        "[M3U8] Captured:",
                        response_url,
                    )

            except Exception:
                pass

        page.on(
            "response",
            handle_response,
        )

        # ----------------------------------------------------
        # OPEN TARGET
        # ----------------------------------------------------

        print(
            "[SCRAPE] Opening:",
            target_url,
        )

        try:

            page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT,
            )

        except PlaywrightTimeoutError:

            # A page timeout does not necessarily mean
            # that no stream was captured.
            print(
                "[SCRAPE] Navigation timeout."
                " Checking captured streams..."
            )

        # ----------------------------------------------------
        # WAIT FOR PLAYER NETWORK REQUESTS
        # ----------------------------------------------------

        try:

            page.wait_for_timeout(
                int(
                    WAIT_AFTER_LOAD * 1000
                )
            )

        except Exception:
            pass

        # ----------------------------------------------------
        # NETWORK IDLE
        # ----------------------------------------------------

        try:

            page.wait_for_load_state(
                "networkidle",
                timeout=10000,
            )

        except PlaywrightTimeoutError:

            # Streaming pages can intentionally remain
            # network-active.
            pass

        # ----------------------------------------------------
        # FINAL WAIT
        # ----------------------------------------------------

        try:

            page.wait_for_timeout(1500)

        except Exception:
            pass

    except Exception as exc:

        print(
            "[SCRAPE] Error:",
            exc,
        )

        # Re-raise so the API can report the error.
        raise

    finally:

        # ----------------------------------------------------
        # CLEAN PAGE
        # ----------------------------------------------------

        try:

            if page is not None:
                page.close()

        except Exception:
            pass

        # ----------------------------------------------------
        # CLEAN CONTEXT
        # ----------------------------------------------------

        try:

            if context is not None:
                context.close()

        except Exception:
            pass

    # --------------------------------------------------------
    # SORT STREAMS
    # --------------------------------------------------------

    sorted_streams = sorted(
        streams,
        key=score_stream,
        reverse=True,
    )

    best_stream = (
        sorted_streams[0]
        if sorted_streams
        else None
    )

    return {
        "stream": best_stream,
        "streams": sorted_streams,
        "count": len(sorted_streams),
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
def health_check():

    return jsonify(
        {
            "status": "ok",
            "service": APP_NAME,
        }
    ), 200


@app.get("/health")
def health():

    return jsonify(
        {
            "status": "healthy",
        }
    ), 200


# ============================================================
# SCRAPE API
# ============================================================

@app.get("/scrape")
def api_scrape():

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    if not check_api_key():

        return jsonify(
            {
                "success": False,
                "error": "Unauthorized",
            }
        ), 401

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    target_url = (
        request.args.get(
            "url",
            "",
        )
        .strip()
    )

    if not target_url:

        return jsonify(
            {
                "success": False,
                "error": "Missing url parameter",
            }
        ), 400

    # --------------------------------------------------------
    # VALIDATE URL
    # --------------------------------------------------------

    valid, validation_error = (
        validate_target_url(
            target_url
        )
    )

    if not valid:

        return jsonify(
            {
                "success": False,
                "error": validation_error,
            }
        ), 400

    # --------------------------------------------------------
    # SCRAPE
    # --------------------------------------------------------

    started = time.time()

    try:

        data = super_scraper(
            target_url
        )

        elapsed = round(
            time.time() - started,
            2,
        )

        return jsonify(
            {
                "success": True,

                "url": target_url,

                "data": data,

                "elapsed": elapsed,
            }
        ), 200

    except Exception as exc:

        print(
            "[API ERROR]",
            exc,
        )

        return jsonify(
            {
                "success": False,

                "error": "Scraper failed",

                "details": str(exc),
            }
        ), 500


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    print(
        f"{APP_NAME} starting on port {PORT}"
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
    )
