"""Shared NSE session with cookie handling, rate limiting, and retry logic."""

import time
import logging
import requests
from config import (
    NSE_BASE_URL, NSE_HEADERS,
    COOKIE_TTL_SECONDS, REQUEST_RATE_LIMIT_SECONDS, REQUEST_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


class NSESession:
    """
    Singleton session handler for NSE India.
    NSE requires:
    1. First hit the main page to get cookies (nseappid, nsit, bm_sv, etc.)
    2. Use those cookies + proper headers for subsequent API calls
    3. Rate limit: minimum 1 second between requests
    4. Refresh cookies every 5 minutes (they expire)
    """

    BASE_URL = NSE_BASE_URL

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(NSE_HEADERS)
        self.last_request_time: float = 0.0
        self.cookie_time: float = 0.0

    def _refresh_cookies(self, force: bool = False):
        """Visit NSE homepage to get fresh cookies."""
        now = time.time()
        if not force and (now - self.cookie_time) < COOKIE_TTL_SECONDS:
            return
        logger.debug("Refreshing NSE cookies...")
        try:
            self.session.get(self.BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
            self.cookie_time = time.time()
            logger.debug("Cookies refreshed successfully.")
        except requests.RequestException as e:
            logger.warning("Cookie refresh failed: %s", e)

    def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < REQUEST_RATE_LIMIT_SECONDS:
            time.sleep(REQUEST_RATE_LIMIT_SECONDS - elapsed)

    def get(self, url: str, referer: str = None, max_retries: int = 3):
        """Make a rate-limited, cookie-authenticated GET request with retry."""
        self._refresh_cookies()
        self._rate_limit()

        if referer:
            self.session.headers["Referer"] = referer
        else:
            self.session.headers["Referer"] = self.BASE_URL

        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                self.last_request_time = time.time()

                if resp.status_code in (401, 403):
                    logger.warning(
                        "Got %s on attempt %d — refreshing cookies and retrying in 3s",
                        resp.status_code, attempt
                    )
                    time.sleep(3)
                    self._refresh_cookies(force=True)
                    continue

                if resp.status_code == 429:
                    wait = 30
                    logger.warning("Rate limited (429) — waiting %ds before retry %d", wait, attempt)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout as e:
                last_exc = e
                logger.warning("Timeout on attempt %d for %s — retrying in 5s", attempt, url)
                time.sleep(5)
            except requests.exceptions.JSONDecodeError as e:
                logger.error(
                    "JSON decode error for %s — raw response (first 500 chars): %s",
                    url, resp.text[:500] if 'resp' in dir() else "N/A"
                )
                raise
            except requests.RequestException as e:
                last_exc = e
                logger.warning("Request error on attempt %d: %s", attempt, e)
                time.sleep(5)

        raise RuntimeError(f"All {max_retries} attempts failed for {url}: {last_exc}")


# Module-level singleton
nse = NSESession()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    logger.info("Testing NSE session...")
    # Hit the homepage first
    nse._refresh_cookies(force=True)
    logger.info("Cookies: %s", dict(nse.session.cookies))
    print("NSE session OK — cookies loaded.")
