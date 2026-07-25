"""Cookie management for API-based sources (zhihu, xueqiu).

Extracts cookies from Edge CDP logged-in tabs once, persists to disk,
and serves them for direct HTTP API calls without needing browser tabs.
"""
import asyncio
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

COOKIE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "../../data/cookies.json",
)


async def extract_cookies_from_cdp(domain: str) -> dict[str, str] | None:
    """Extract cookies for a domain from an existing Edge CDP login tab.

    Uses CDP Network.getCookies — requires the user to have the site open and logged in.
    Returns {name: value} dict, or None on failure.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://127.0.0.1:9222/json")
            tabs = resp.json()
    except Exception as e:
        logger.warning(f"cookie_manager: cannot list CDP tabs: {e}")
        return None

    # Find matching tab
    target_tab = None
    for t in tabs:
        url = (t.get("url") or "").lower()
        if domain in url and "webSocketDebuggerUrl" in t:
            target_tab = t
            break

    if not target_tab:
        logger.warning(f"cookie_manager: no tab found for {domain}")
        return None

    try:
        import websockets

        ws_url = target_tab["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
            req_id = 1
            msg = json.dumps({
                "id": req_id,
                "method": "Network.getCookies",
                "params": {"urls": [f"https://www.{domain}"]},
            })
            await ws.send(msg)
            resp_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            data = json.loads(resp_raw)

            cookies = data.get("result", {}).get("cookies", [])
            result = {}
            for c in cookies:
                result[c["name"]] = c["value"]
            logger.info(f"cookie_manager: extracted {len(result)} cookies for {domain}")
            return result
    except Exception as e:
        logger.warning(f"cookie_manager: CDP getCookies failed for {domain}: {e}")
        return None


def _load_all() -> dict:
    """Load all persisted cookies from file."""
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"cookie_manager: load error: {e}")
    return {}


def _save_all(data: dict):
    """Persist all cookies to file."""
    try:
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"cookie_manager: saved cookies for {list(data.keys())}")
    except Exception as e:
        logger.warning(f"cookie_manager: save error: {e}")


async def get_cookies(domain: str) -> dict[str, str] | None:
    """Get cookies for a domain — from cache or CDP extraction.

    Returns {name: value} dict, or None if unavailable.
    """
    # Check cached first
    all_cookies = _load_all()
    cached = all_cookies.get(domain)
    if cached and isinstance(cached, dict):
        logger.debug(f"cookie_manager: using cached cookies for {domain}")
        return cached

    # Extract via CDP
    logger.info(f"cookie_manager: no cached cookies for {domain}, extracting from CDP...")
    fresh = await extract_cookies_from_cdp(domain)
    if fresh:
        all_cookies[domain] = fresh
        _save_all(all_cookies)
        return fresh

    logger.warning(f"cookie_manager: cannot obtain cookies for {domain}")
    return None


# Cookie whitelist per domain. Only these cookies are sent in the Cookie header.
#
# Xueqiu: tracking/anti-crawler cookies (Hm_*, __utm*, HMACCOUNT, smidV2,
# ssxmod_itna*, cookiesu, .thumbcache_*) extracted from the browser become
# stale quickly, and the xueqiu gateway answers such requests with a bogus
# HTTP 404 (Spring-style error body) instead of the real API response.
# Sending only the login-essential cookies avoids the false 404.
_COOKIE_WHITELIST: dict[str, set[str] | None] = {
    "xueqiu.com": {
        "xq_a_token", "xq_r_token", "xq_id_token", "xqat",
        "u", "bid", "device_id", "xq_is_login", "s",
    },
}


def build_cookie_header(domain: str, cookie_dict: dict[str, str]) -> str:
    """Build Cookie header string from cookie dict.

    Cookies are filtered through _COOKIE_WHITELIST for domains that have
    one; other domains send all cookies (unchanged behaviour).
    """
    whitelist = _COOKIE_WHITELIST.get(domain)
    parts = []
    for name, value in cookie_dict.items():
        if whitelist is not None and name not in whitelist:
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def clear_cookies(domain: str):
    """Clear cached cookies for a domain (force re-login next time)."""
    all_cookies = _load_all()
    all_cookies.pop(domain, None)
    _save_all(all_cookies)
    logger.info(f"cookie_manager: cleared cookies for {domain}")


# ---------------------------------------------------------------------------
# Domain-aware rate limiter
#
# Zhihu: 2-3s between requests, rest 1-2min every 50-100 requests
# Xueqiu: even more sensitive, min 3-5s between requests, "40001" = rate limited
# ---------------------------------------------------------------------------

import asyncio
import time
from collections import defaultdict

_RATE_LIMIT_DOMAINS: dict[str, float] = {}        # domain -> last_request_time
_REQUEST_COUNTS: dict[str, int] = defaultdict(int)  # domain -> request count in current batch
_RATE_CONFIG: dict[str, dict] = {
    "zhihu.com": {
        "min_interval": 2.5,        # seconds between requests
        "batch_reset_after": 75,    # requests before reset counter
        "rest_duration": 90.0,      # seconds to rest after batch limit
        "safer_interval": 4.0,      # conservative interval for stable running
    },
    "xueqiu.com": {
        "min_interval": 4.0,
        "batch_reset_after": 60,
        "rest_duration": 120.0,
        "safer_interval": 6.0,
    },
}
# Default config for unlisted domains
_DEFAULT_RATE_CONFIG = {
    "min_interval": 3.0,
    "batch_reset_after": 80,
    "rest_duration": 90.0,
    "safer_interval": 5.0,
}


async def wait_for_rate_limit(domain: str, safe_mode: bool = False):
    """Wait appropriate duration since last request to avoid rate limiting.

    Args:
        domain: Domain string like 'zhihu.com' or 'xueqiu.com'.
        safe_mode: If True, use safer_interval (conservative). Default False.
    """
    config = _RATE_CONFIG.get(domain, _DEFAULT_RATE_CONFIG)

    # Batch counter: long rest after N requests
    _REQUEST_COUNTS[domain] += 1
    count = _REQUEST_COUNTS[domain]
    if count >= config["batch_reset_after"]:
        logger.info(
            f"rate_limit[{domain}]: {count} requests, resting "
            f"{config['rest_duration']:.0f}s..."
        )
        await asyncio.sleep(config["rest_duration"])
        _REQUEST_COUNTS[domain] = 0
        return  # sleep done, no need for per-request delay

    # Per-request delay using monotonic clock
    interval = config["safer_interval"] if safe_mode else config["min_interval"]
    last = _RATE_LIMIT_DOMAINS.get(domain, 0.0)
    now = time.monotonic()
    elapsed = now - last
    if elapsed < interval:
        await asyncio.sleep(interval - elapsed)
        now = time.monotonic()

    _RATE_LIMIT_DOMAINS[domain] = now


def reset_rate_limit(domain: str):
    """Reset rate limit counters for a domain (e.g., after a 40001 error)."""
    _RATE_LIMIT_DOMAINS.pop(domain, None)
    _REQUEST_COUNTS[domain] = 0
    logger.info(f"rate_limit[{domain}]: counters reset")
