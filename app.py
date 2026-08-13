import os
import time
import atexit
import threading
from urllib.parse import urlparse

from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

APP_NAME = "Vidstorm M3U8 Scraper"

PORT = int(os.environ.get("PORT", "5000"))

PAGE_TIMEOUT = int(os.environ.get("PAGE_TIMEOUT", "90000"))
WAIT_AFTER_LOAD = float(os.environ.get("WAIT_AFTER_LOAD", "5"))

# Only allow these hosts by default.
# Add more hosts with:
# ALLOWED_HOSTS=vixsrc.to,www.vixsrc.to
ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.environ.get(
        "ALLOWED_HOSTS",
        "vixsrc.to,www.vixsrc.to"
    ).split(",")
    if host.strip()
}

# Optional API key.
# If empty, API key protection is disabled.
API_KEY = os.environ.get("API_KEY", "").strip()

# Example:
# CORS_ORIGINS=https://example.com,https://www.example.com
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

if CORS_ORIGINS == "*":
    CORS(app)
else:
    CORS(
        app,
        resources={
            r"/*": {
                "origins": [
                    x.strip()
                    for x in CORS_ORIGINS.split(",")
                    if x.strip()
                ]
            }
        }
    )


# ============================================================
# PLAYWRIGHT
# ============================================================

_playwright = None
_browser = None
_browser_lock = threading.Lock()


def get_browser():
    """
    Lazily create Chromium.

    This is better for Gunicorn/Render than launching Chromium
    during module import.
    """
    global _playwright, _browser

    with _browser_lock:
        if _browser is not None:
            return _browser

        _playwright = sync_playwright().start()

        _browser = _playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
            ]
        )

        return _browser


def shutdown_browser():
    global _browser, _playwright

    try:
        if _browser:
            _browser.close()
    except Exception:
        pass

    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass

    _browser = None
    _playwright = None


atexit.register(shutdown_browser)


# ============================================================
# SECURITY
# ============================================================

def check_api_key():
    """
    API key is optional.

    If API_KEY is configured on Render, clients must send:
        X-API-Key: YOUR_KEY

    or:
        ?api_key=YOUR_KEY
    """

    if not API_KEY:
        return True

    supplied = (
        request.headers.get("X-API-Key")
        or request.args.get("api_key")
        or ""
    )

    return supplied == API_KEY


def validate_target_url(target_url):
    try:
        parsed = urlparse(target_url)

        if parsed.scheme not in ("http", "https"):
            return False, "Only HTTP/HTTPS URLs are allowed"

        hostname = (parsed.hostname or "").lower()

        if not hostname:
            return False, "Invalid hostname"

        if hostname not in ALLOWED_HOSTS:
            return False, (
                "Host not allowed. Allowed hosts: "
                + ", ".join(sorted(ALLOWED_HOSTS))
            )

        return True, None

    except Exception:
        return False, "Invalid URL"


# ============================================================
# SCRAPER
# ============================================================

def score_stream(url):
    """
    Give likely master/index playlists a higher score.
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

    if "stream" in value:
        score += 2

    if "token" in value:
        score += 1

    return score


def super_scraper(target_url):
    browser = get_browser()

    results = set()

    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={
            "width": 1280,
            "height": 720
        },
        java_script_enabled=True,
        ignore_https_errors=False,
    )

    page = context.new_page()

    # Prevent a single page from hanging forever.
    page.set_default_timeout(15000)

    def handle_response(response):
        try:
            url = response.url

            if ".m3u8" not in url.lower():
                return

            # Only collect successful HTTP responses.
            if response.status >= 400:
                return

            results.add(url)

            print(f"[M3U8] {url}")

        except Exception:
            pass

    page.on("response", handle_response)

    try:
        print(f"[SCRAPE] Opening: {target_url}")

        page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        # Give the site's JavaScript/player time to initialize.
        page.wait_for_timeout(int(WAIT_AFTER_LOAD * 1000))

        # A second short wait can catch delayed player requests.
        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=10000
            )
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(1500)

    except PlaywrightTimeoutError:
        print("[SCRAPE] Page timeout; returning captured streams.")

    except Exception as exc:
        print(f"[SCRAPE] Error: {exc}")

    finally:
        try:
            page.close()
        except Exception:
            pass

        try:
            context.close()
        except Exception:
            pass

    streams = sorted(
        results,
        key=lambda x: score_stream(x),
        reverse=True
    )

    best_stream = streams[0] if streams else None

    return {
        "stream": best_stream,
        "streams": streams,
        "count": len(streams)
    }


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def health_check():
    return jsonify({
        "status": "ok",
        "service": APP_NAME
    }), 200


@app.get("/health")
def health():
    return jsonify({
        "status": "healthy"
    }), 200


@app.get("/scrape")
def api_scrape():

    if not check_api_key():
        return jsonify({
            "success": False,
            "error": "Unauthorized"
        }), 401

    target_url = request.args.get("url", "").strip()

    if not target_url:
        return jsonify({
            "success": False,
            "error": "Missing url parameter"
        }), 400

    valid, error = validate_target_url(target_url)

    if not valid:
        return jsonify({
            "success": False,
            "error": error
        }), 400

    started = time.time()

    try:
        data = super_scraper(target_url)

        elapsed = round(time.time() - started, 2)

        return jsonify({
            "success": True,
            "url": target_url,
            "data": data,
            "elapsed": elapsed
        }), 200

    except Exception as exc:
        print(f"[API ERROR] {exc}")

        return jsonify({
            "success": False,
            "error": "Scraper failed",
            "details": str(exc)
        }), 500


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
