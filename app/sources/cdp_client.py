import asyncio
import json
import logging
import re
import websockets
import websockets.exceptions
import httpx

logger = logging.getLogger(__name__)
CDP_HOST = "ws://127.0.0.1:9222"
SEND_TIMEOUT = 15.0  # seconds

# Map subdomains to their canonical domain
DOMAIN_ALIASES = {"so.eastmoney.com": "eastmoney.com", "google.com.hk": "google.com",
                  "www.google.com.hk": "google.com", "www.zhihu.com": "zhihu.com",
                  "www.eastmoney.com": "eastmoney.com", "www.google.com": "google.com",
                  "www.bing.com": "bing.com",
                  "www.github.com": "github.com", "www.v2ex.com": "v2ex.com",
                  "www.sov2ex.com": "sov2ex.com", "www.youtube.com": "youtube.com",
                  "www.bilibili.com": "bilibili.com", "search.bilibili.com": "bilibili.com"}


class CDPConnection:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.ws = None
        self.id_counter = 0
        self.pending = {}

    async def connect(self):
        self.ws = await websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=10,
        )

    def is_connected(self):
        """Check if the WebSocket is still open (websockets 16.0 compatible)."""
        if self.ws is None:
            return False
        try:
            return self.ws.state.is_connected()
        except AttributeError:
            return not getattr(self.ws, 'closed', False)

    async def close(self):
        if self.is_connected():
            try:
                await self.ws.close()
            except websockets.exceptions.ConnectionClosed:
                pass

    async def send(self, method, params=None):
        self.id_counter += 1
        msg = {"id": self.id_counter, "method": method, "params": params or {}}
        future = asyncio.get_running_loop().create_future()
        self.pending[self.id_counter] = future
        await self.ws.send(json.dumps(msg))
        try:
            resp = await asyncio.wait_for(future, timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError:
            self.pending.pop(self.id_counter, None)
            raise Exception(f"CDP send timeout ({SEND_TIMEOUT}s) for {method}")
        return resp

    async def handler(self):
        try:
            async for message in self.ws:
                try:
                    resp = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"CDP: malformed message from browser: {message[:100]}")
                    continue
                msg_id = resp.get("id")
                if msg_id in self.pending:
                    fut = self.pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(resp)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            logger.exception("CDP handler crashed")


_cdp = None
_cdp_lock = asyncio.Lock()
_handler_task = None
_current_tab_id = None  # Track which tab we're connected to
# Map of site domain -> tab info (for reusing logged-in tabs)
_site_tabs = {}


def _handler_done_callback(task):
    """Callback when handler task finishes — invalidates CDP singleton."""
    global _cdp, _handler_task
    try:
        exc = task.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            logger.warning(f"CDP handler task ended with exception: {exc}")
    except asyncio.CancelledError:
        pass  # Normal on shutdown
    _handler_task = None
    # Don't set _cdp to None here — let _get_cdp check is_connected()


# Sources that should use their own site tab (not neutral tabs)
# Sources requiring actual user login (tabs are NEVER closed after search)
LOGIN_SITES = {
    "zhihu": "zhihu.com",
    "xueqiu": "xueqiu.com",
    "twitter": "x.com",
    "eastmoney": "eastmoney.com",
}

# Sources needing dedicated tabs for isolation (tabs ARE closed after search)
# These are NOT in LOGIN_SITES but still need their own tab to avoid sharing
DEDICATED_SITES = {
    "google": "google.com",
    "bing": "bing.com",
    "github": "github.com",
    "v2ex": "sov2ex.com",
    "youtube": "youtube.com",
    "bilibili": "bilibili.com",
}

# Combined: all sources with tab isolation (used by tab-routing logic)
_ALL_ISOLATED_SITES = {**LOGIN_SITES, **DEDICATED_SITES}

# Homepage to open when no existing tab found for a login site
SITE_HOME_URLS = {
    "zhihu": "https://www.zhihu.com",
    "xueqiu": "https://xueqiu.com",
    "twitter": "https://x.com",
    "eastmoney": "https://www.eastmoney.com",
    "google": "https://www.google.com",
    "bing": "https://www.bing.com",
    "github": "https://github.com",
    "v2ex": "https://www.sov2ex.com",
    "youtube": "https://www.youtube.com",
    "bilibili": "https://www.bilibili.com",
}

# Sources that are SPAs requiring longer render wait times
SPA_WAIT_TIMES = {
    "v2ex": 10.0,   # sov2ex.com is a SPA, needs JS rendering
    "youtube": 8.0, # YouTube search is heavy JS SPA
    "bilibili": 8.0,# Bilibili search is SPA with login wall
}

# Login URLs for each source (from sources.yml)
LOGIN_URLS = {
    "zhihu": "https://www.zhihu.com/login",
    "xueqiu": "https://xueqiu.com/login",
    "twitter": "https://x.com/login",
}


async def _get_cdp(source_name: str = None):
    global _cdp, _handler_task, _current_tab_id
    async with _cdp_lock:
        # Refresh site -> tab mapping on each call
        await _refresh_site_tabs()

        # Check if we need to switch tabs for this source
        need_new_tab = False
        target_tab = None

        if _cdp is not None:
            # If handler died, reconnect
            if _handler_task is None or not _cdp.is_connected():
                logger.info("CDP: handler dead or disconnected, reconnecting")
                await _cdp.close()
                _cdp = None
                _handler_task = None
                _current_tab_id = None
                need_new_tab = True
            elif source_name in _ALL_ISOLATED_SITES:
                # Source with dedicated tab: find the tab for this site
                domain = _ALL_ISOLATED_SITES[source_name]
                candidate = _site_tabs.get(domain)
                if candidate and candidate.get("id") != _current_tab_id:
                    need_new_tab = True
                    target_tab = candidate
            elif _current_tab_id:
                # Non-isolated source: check if we're on an isolated site tab
                current_is_isolated = any(
                    t.get("id") == _current_tab_id
                    for t in _site_tabs.values()
                )
                if current_is_isolated:
                    # Switch back to neutral tab
                    need_new_tab = True

        if _cdp is None or need_new_tab:
            tab = target_tab or await _find_debuggable_tab(source_name=source_name)
            if not tab:
                raise Exception("No browser tabs found on port 9222")
            ws_url = tab.get("webSocketDebuggerUrl") or f"{CDP_HOST}/devtools/page/{tab['id']}"
            _cdp = CDPConnection(ws_url)
            await _cdp.connect()
            _handler_task = asyncio.ensure_future(_cdp.handler())
            _handler_task.add_done_callback(_handler_done_callback)
            _current_tab_id = tab.get("id")
            logger.info(f"CDP: connected to tab {_current_tab_id} url={tab.get('url', '?')[:80]}")
            await asyncio.sleep(0.1)
    return _cdp


async def _refresh_site_tabs():
    """Refresh the mapping of login site domains to their browser tabs."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://127.0.0.1:9222/json")
            resp.raise_for_status()
            tabs = resp.json()
    except Exception:
        return

    # DOMAIN_ALIASES moved to module level for shared access

    def _extract_hostname(url):
        """Extract hostname from URL properly."""
        url = url.lower().split("?")[0].split("#")[0].split("/")[2] if url.count("/") >= 2 else ""
        # Remove port
        if ":" in url:
            url = url.split(":")[0]
        return url

    def _tab_domain(tab):
        url = (tab.get("url") or "").lower()
        if url.startswith("chrome-extension://") or url.startswith("edge://"):
            return None
        hostname = _extract_hostname(url)
        # Exact match first, then subdomain match
        canonical = DOMAIN_ALIASES.get(hostname, hostname)
        for site_key, domain in _ALL_ISOLATED_SITES.items():
            if hostname == domain or canonical == domain:
                return domain
        return None

    _site_tabs.clear()
    for tab in tabs:
        domain = _tab_domain(tab)
        if not domain or "webSocketDebuggerUrl" not in tab:
            continue
        tab_type = tab.get("type", "page")
        # Skip service workers and other non-page types
        if tab_type not in ("page", "iframe"):
            continue
        if domain in _site_tabs:
            existing_type = _site_tabs[domain].get("type", "page")
            # Prefer "page" over "iframe"
            if existing_type == "page" and tab_type == "iframe":
                continue
        _site_tabs[domain] = tab


async def _switch_to_tab(tab_id: str):
    """Switch CDP connection to a different tab (for login-required sources)."""
    cdp = await _get_cdp()
    try:
        await cdp.send("Target.activateTarget", {"targetId": tab_id})
        logger.info(f"CDP: switched to tab {tab_id}")
        return True
    except Exception as e:
        logger.warning(f"CDP: failed to switch to tab {tab_id}: {e}")
        return False


async def _create_new_tab(url: str = "about:blank", cdp: CDPConnection = None) -> dict | None:
    """Create a new browser tab using CDP Target.createTarget (works when /json/new returns 405).
    Returns tab info dict with webSocketDebuggerUrl, or None on failure.
    """
    try:
        # Try /json/new first (works on Chrome, not on some Edge configs)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://127.0.0.1:9222/json/new")
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass

    # Fallback: use Target.createTarget via CDP
    try:
        if cdp is None:
            # Connect to any existing tab to use Target domain
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("http://127.0.0.1:9222/json")
                tabs = resp.json()
            for tab in tabs:
                if "webSocketDebuggerUrl" in tab:
                    cdp = CDPConnection(tab["webSocketDebuggerUrl"])
                    await cdp.connect()
                    break
            if cdp is None:
                return None

        result = await cdp.send("Target.createTarget", {"url": url})
        target_id = result.get("targetId")
        if not target_id:
            # Close temp CDP if we created one
            if tab.get("id") != cdp.ws_url:
                await cdp.close()
            return None

        # Wait for the new tab to appear
        for _ in range(20):  # up to 2 seconds
            await asyncio.sleep(0.1)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("http://127.0.0.1:9222/json")
                for t in resp.json():
                    if t.get("id") == target_id or t.get("url") == url:
                        await cdp.close()
                        return t

        await cdp.close()
        return None
    except Exception as e:
        logger.warning(f"CDP: Target.createTarget failed: {e}")
        return None


async def _find_debuggable_tab(source_name: str = None):
    await _refresh_site_tabs()

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get("http://127.0.0.1:9222/json")
        resp.raise_for_status()
        tabs = resp.json()

    # If source has a dedicated tab, use it from _site_tabs (already has correct domain mapping)
    if source_name in _ALL_ISOLATED_SITES:
        domain = _ALL_ISOLATED_SITES[source_name]
        tab = _site_tabs.get(domain)
        if tab and "webSocketDebuggerUrl" in tab:
            logger.info(f"CDP: using existing tab for {source_name} ({domain})")
            return tab
        # No existing tab — try to open site homepage
        home = SITE_HOME_URLS.get(source_name, f"https://{domain}")
        logger.info(f"CDP: no tab for {source_name}, trying to open {home}")
        try:
            new_tab = await _create_new_tab(home)
            if new_tab:
                return new_tab
        except Exception as e:
            logger.warning(f"CDP: failed to open {home}: {e}")
        # Fallback: use any existing isolated site tab (borrow it)
        for site_key, site_tab in _site_tabs.items():
            if "webSocketDebuggerUrl" in site_tab:
                logger.info(f"CDP: borrowing {site_key} tab for {source_name} as fallback")
                return site_tab
        # Fall through: try any available tab

    # For non-isolated sources: find a neutral tab (not on isolated sites)
    for tab in tabs:
        if "webSocketDebuggerUrl" not in tab:
            continue
        # Skip service workers and other non-page types
        if tab.get("type") not in ("page", "iframe"):
            continue
        url = (tab.get("url") or "").lower()
        if url.startswith("chrome-extension://") or url.startswith("edge://"):
            continue
        # Skip tabs that belong to isolated sites
        hostname = url.split("?")[0].split("#")[0].split("/")[2] if url.count("/") >= 2 else ""
        if ":" in hostname:
            hostname = hostname.split(":")[0]
        canonical = DOMAIN_ALIASES.get(hostname, hostname)
        is_isolated = False
        for domain in _ALL_ISOLATED_SITES.values():
            if hostname == domain or canonical == domain or hostname.endswith("." + domain):
                is_isolated = True
                break
        if is_isolated:
            continue
        return tab

    # If all tabs are on login sites, open a new page
    new_tab = await _create_new_tab("about:blank")
    if new_tab:
        return new_tab

    # Last resort: use first available non-extension tab
    for tab in tabs:
        url = (tab.get("url") or "").lower()
        if url.startswith("chrome-extension://") or url.startswith("edge://"):
            continue
        if "webSocketDebuggerUrl" in tab:
            return tab
    return tabs[0] if tabs else None


async def create_parallel_connection(source_name: str = None):
    """Create an independent CDP connection for parallel search.

    - Login sources: reuse existing logged-in tab, NEVER close after search
    - Dedicated sources: use dedicated tab, close after search
    - Other sources: try to create new tab, close if created
    Returns (cdp, handler_task, tab_id, should_close_tab, original_url)
    """
    await _refresh_site_tabs()
    tab = await _find_debuggable_tab(source_name=source_name)
    if not tab:
        raise Exception("No browser tabs found on port 9222")

    is_login = source_name in LOGIN_SITES
    is_dedicated = source_name in DEDICATED_SITES
    original_url = None
    should_close = False  # whether to close the tab when done

    if is_login:
        # Login sites: reuse tab, never close
        original_url = tab.get("url")
        should_close = False
    elif is_dedicated:
        # Dedicated sites: close after search to prevent tab accumulation
        should_close = True
    else:
        # Other sources: try to create a fresh tab
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("http://127.0.0.1:9222/json/new")
                if resp.status_code == 200:
                    tab = resp.json()
                    should_close = True
                    logger.info(f"CDP parallel: created new tab {tab.get('id')}")
        except Exception as e:
            logger.warning(f"CDP: failed to create new tab: {e}")
        # If /json/new failed, try Target.createTarget via CDP as fallback
        if not should_close:
            try:
                new_tab = await _create_new_tab("about:blank")
                if new_tab:
                    tab = new_tab
                    should_close = True
                    logger.info(f"CDP parallel: created new tab via Target.createTarget {tab.get('id')}")
            except Exception as e2:
                logger.warning(f"CDP: Target.createTarget also failed: {e2}")
        # If both fail, reuse the neutral tab from _find_debuggable_tab (don't close)

    ws_url = tab.get("webSocketDebuggerUrl") or f"{CDP_HOST}/devtools/page/{tab['id']}"
    cdp = CDPConnection(ws_url)
    await cdp.connect()
    handler_task = asyncio.ensure_future(cdp.handler())
    logger.info(f"CDP parallel: connected to tab {tab.get('id')} (close={should_close}, login={is_login}, dedicated={is_dedicated})")
    return (cdp, handler_task, tab.get("id"), should_close, original_url)


async def close_parallel_connection(conn_info):
    """Close a parallel CDP connection and clean up the tab.

    IMPORTANT: Do NOT use _get_cdp() singleton here — it holds a global lock
    that conflicts with other parallel connections still in flight.
    """
    cdp, handler_task, tab_id, should_close, original_url = conn_info
    handler_task.cancel()
    try:
        await cdp.close()
    except Exception:
        pass
    if should_close:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(f"http://127.0.0.1:9222/json/close/{tab_id}")
            logger.info(f"CDP parallel: closed tab {tab_id}")
        except Exception:
            pass
    # Login sources: just leave the tab as-is (user has locked tabs).
    # Do NOT try to restore original_url via _get_cdp() — it races with
    # other parallel connections on the singleton lock.
    if not should_close and original_url:
        logger.debug(f"CDP parallel: leaving login tab {tab_id} at current URL (user-managed)")


async def navigate(url: str, wait: float = 3.0, source_name: str = None, cdp: CDPConnection = None) -> dict:
    """Navigate to URL and return page content dict {title, url, text}."""
    if cdp is None:
        cdp = await _get_cdp(source_name=source_name)
    await cdp.send("Page.enable")
    await cdp.send("Page.navigate", {"url": url})
    await asyncio.sleep(wait)
    return await _fetch_page_text(cdp=cdp)


async def get_content(source_name: str = None, cdp: CDPConnection = None) -> dict:
    """Get current page content dict {title, url, text}."""
    if cdp is None:
        cdp = await _get_cdp(source_name=source_name)
    await cdp.send("Page.enable")
    return await _fetch_page_text(cdp=cdp)


async def _fetch_page_text(source_name: str = None, cdp: CDPConnection = None):
    if cdp is None:
        cdp = await _get_cdp(source_name=source_name)
    result = await cdp.send("Runtime.evaluate", {
        "expression": """(function() {
            try {
                return JSON.stringify({
                    title: document.title,
                    url: window.location.href,
                    text: document.body ? document.body.innerText.substring(0, 5000) : ''
                });
            } catch(e) {
                return JSON.stringify({title: 'error', url: location.href, text: e.message});
            }
        })()""",
        "returnByValue": True,
    })
    rval = result.get("result", {}).get("result", {})
    if rval.get("subtype") == "error" or "value" not in rval:
        raise Exception(f"Runtime.evaluate error: {rval.get('description', str(rval))}")
    try:
        return json.loads(rval["value"])
    except json.JSONDecodeError as e:
        raise Exception(f"Browser returned invalid JSON: {e}, value={rval['value'][:200]}")



async def search_url(url: str, wait: float = 3.0, source_name: str = None,
                      cdp: CDPConnection = None) -> list[dict]:
    """Navigate to a search engine URL and extract structured results.
    Returns list of {title, url, snippet} dicts.
    Uses engine-specific extraction strategies.
    If source_name is a login-required source (zhihu/xueqiu/twitter),
    connects to the already-logged-in tab for that site.
    If cdp is provided, uses it directly (for parallel search).
    """
    if cdp is None:
        cdp = await _get_cdp(source_name=source_name)
    await cdp.send("Page.enable")
    await cdp.send("Page.navigate", {"url": url})
    # Login-required SPAs need longer wait; SPA sources (sov2ex) also need longer wait
    spa_wait = SPA_WAIT_TIMES.get(source_name, 0.0)
    actual_wait = max(wait, 5.0 if source_name in LOGIN_SITES else wait, spa_wait)
    await asyncio.sleep(actual_wait)

    # Get page URL to detect search engine (keep same tab as navigation)
    page = await _fetch_page_text(cdp=cdp)
    page_url = page.get("url", "").lower()
    logger.info(f"search_url: source={source_name} page_url={page_url[:80]}")

    # Engine-specific extraction
    logger.info(f"search_url: source={source_name} page_url={page_url[:80]} text_len={len(page.get('text',''))}")
    if "google.com" in page_url:
        js = _js_google_extract()
    elif "bing.com" in page_url:
        js = _js_bing_extract()
    elif "yandex.com" in page_url:
        js = _js_yandex_extract()
    elif "github.com" in page_url:
        js = _js_github_extract()
    elif "trendforce.com" in page_url:
        js = _js_trendforce_extract()
    elif "xueqiu.com" in page_url:
        js = _js_xueqiu_extract()
    elif "zhihu.com" in page_url:
        js = _js_zhihu_extract()
    elif "eastmoney.com" in page_url or "so.eastmoney.com" in page_url:
        js = _js_eastmoney_extract()
    elif "v2ex.com" in page_url or "sov2ex.com" in page_url:
        js = _js_sov2ex_extract()
    elif "x.com" in page_url or "twitter.com" in page_url:
        js = _js_twitter_extract()
    elif "youtube.com" in page_url:
        js = _js_youtube_extract()
    elif "bilibili.com" in page_url:
        js = _js_bilibili_extract()
    elif "stats.gov.cn" in page_url:
        js = _js_stats_gov_extract()
    else:
        js = _js_generic_extract()

    result = await cdp.send("Runtime.evaluate", {
        "expression": js,
        "returnByValue": True,
    })
    rval = result.get("result", {}).get("result", {})
    if rval.get("subtype") == "error" or "value" not in rval:
        return []
    try:
        results = json.loads(rval["value"])
    except json.JSONDecodeError as e:
        logger.warning(f"search_url: invalid JSON from browser: {e}, value={rval['value'][:200]}")
        return []

    logger.info(f"search_url: source={source_name} extracted {len(results)} results")

    # Post-process: clean titles for community sites
    if "xueqiu.com" in page_url:
        # New DOM extractor returns clean "author: content" format — no extra cleanup needed
        pass

    # 归一化 JS 提取的原始日期为契约字段 published_date，原始 date 一并保留
    from app.sources.edge_mcp_source import _normalize_cdp_date
    for r in results:
        if isinstance(r, dict):
            r["published_date"] = _normalize_cdp_date(r.get("date", ""))

    return results


def _js_google_extract() -> str:
    """Extract Google search results - handles both old /url?q= and direct link formats."""
    return """(function() {
        var results = [];
        var seen = {};

        // Strategy 1: Direct external links (new Google format - no /url?q= redirect)
        var anchors = document.querySelectorAll('a[href]');
        for (var i = 0; i < anchors.length && results.length < 15; i++) {
            var a = anchors[i];
            var href = a.getAttribute('href') || '';
            if (!href.startsWith('http')) continue;
            if (href.indexOf('google') !== -1 || href.indexOf('google.') !== -1) continue;
            if (href.indexOf('/url') !== -1) continue; // skip old-format redirect links
            if (href.indexOf('#') !== -1) continue; // skip anchor/in-page links
            if (href.indexOf('youtube.com/embed') !== -1) continue; // skip video embeds
            if (href.indexOf('youtube.com') !== -1) continue; // skip youtube links
            if (href.indexOf('facebook.com') !== -1) continue; // skip social links
            if (href.indexOf('instagram.com') !== -1) continue; // skip instagram
            if (href.indexOf('reddit.com') !== -1) continue; // skip reddit
            if (href.indexOf('tiktok.com') !== -1) continue; // skip tiktok

            var text = (a.textContent || '').trim();
            if (!text || text.length < 3) continue;

            // Clean title: remove trailing domain, breadcrumb artifacts
            var title = text.replace(/https?:\\/\\/[^\\s]+$/g, '').trim();
            title = title.replace(/[›>]\s*(blog|news|article|insights|resources)\s*[›>]/gi, '').trim();
            title = title.replace(/https?:\/\/[^\s]+/g, '').trim(); // strip any remaining URLs
            title = title.replace(/\s+/g, ' ').trim();
            // Skip titles that are just timestamps (e.g. "0:34", "1:38")
            if (/^\d+:\d+$/.test(title)) continue;
            if (!title || title.length < 3) continue;

            // Dedup by base URL (without query params) to avoid same page different anchors
            var baseHref = href.split('#')[0].split('?')[0];
            if (seen[baseHref]) continue;
            seen[baseHref] = true;

            // Snippet: look for sibling text after the link
            var snippet = '';
            var parent = a.parentElement;
            if (parent) {
                var siblings = parent.children;
                var foundLink = false;
                for (var j = 0; j < siblings.length; j++) {
                    if (siblings[j] === a) foundLink = true;
                    else if (foundLink) {
                        var st = (siblings[j].innerText || '').trim();
                        if (st.length > 30 && st.length < 500) { snippet = st; break; }
                    }
                }
                // Fallback: get parent's full text minus the link text
                if (!snippet) {
                    var pt = (parent.innerText || '').trim();
                    if (pt.length > title.length + 10) snippet = pt.substring(title.length).trim().substring(0, 400);
                }
            }

            results.push({title: title.substring(0, 120), url: href.substring(0, 200), snippet: snippet.substring(0, 400)});
        }

        // Strategy 2: Old format with /url?q= (fallback)
        var cards = document.querySelectorAll('div[role="listitem"], g-unit-mosaic');
        for (var k = 0; k < cards.length && results.length < 15; k++) {
            var card = cards[k];
            var h3 = card.querySelector('h3');
            if (!h3) continue;
            var titleEl = h3.querySelector('a');
            if (!titleEl) continue;
            var href2 = titleEl.getAttribute('href') || '';
            var m = href2.match(/\\/url\\?q=(https?:\\/\\/[^&]+)/);
            if (!m) continue;
            var realUrl = m[1];
            if (seen[realUrl]) continue;
            seen[realUrl] = true;
            var title2 = h3.textContent || '';
            if (!title2 || title2.length < 3) continue;

            var snippet2 = '';
            var allDivs = card.querySelectorAll('div');
            for (var l = 0; l < allDivs.length; l++) {
                var d = allDivs[l];
                var t = (d.innerText || '').trim();
                if (t.length > 50 && t.length < 500 && t !== title2) { snippet2 = t; break; }
            }
            results.push({title: title2.substring(0, 120), url: realUrl, snippet: snippet2.substring(0, 400)});
        }

        return JSON.stringify(results);
    })()"""


def _js_bing_extract() -> str:
    """Extract Bing search results - handles both old q=URL and new ck/a redirect formats."""
    return """(function() {
        var results = [];
        var seen = {};
        var items = document.querySelectorAll('li.b_algo, [data-testid="organic"]');
        for (var i = 0; i < items.length && results.length < 15; i++) {
            var item = items[i];
            var titleA = item.querySelector('h2 a, h3 a');
            if (!titleA) continue;
            var title = titleA.textContent || '';
            if (!title || title.length < 3) continue;

            var href = titleA.getAttribute('href') || '';
            var realUrl = '';

            // Old format: /search?q=URL
            var m = href.match(/q=(https?:\\/\\/[^&]+)/);
            if (m) { realUrl = decodeURIComponent(m[1]); }

            // New format: /ck/a?!&&p=... (redirect URL, use as-is)
            if (!realUrl && href.indexOf('/ck/a') !== -1) { realUrl = href; }

            if (!realUrl || seen[realUrl]) continue;
            seen[realUrl] = true;

            var snippet = '';
            var children = item.children;
            var foundTitle = false;
            for (var j = 0; j < children.length; j++) {
                if (children[j].tagName === 'H2' || children[j].tagName === 'H3') foundTitle = true;
                else if (foundTitle && children[j].tagName !== 'A' && children[j].tagName !== 'H2') {
                    snippet = (children[j].innerText || '').trim();
                    break;
                }
            }
            results.push({title: title.substring(0,120), url: realUrl.substring(0,200), snippet: snippet.substring(0,400)});
        }
        return JSON.stringify(results);
    })()"""


def _js_yandex_extract() -> str:
    """Extract Yandex search results — handles current page structure."""
    return """(function() {
        var results = [];
        var seen = {};

        // Verify we're on a search page
        if (window.location.href.indexOf('/search') === -1 && window.location.href.indexOf('yandex') === -1) {
            return JSON.stringify([]);
        }

        // Strategy 1: Organic result blocks (various Yandex class names over time)
        var selectors = '.OrganicVisible, .serp-item, .userplus__snippet, .search-result, ' +
                        '.serp-item__passage, [data-show-passages], .link-organic, .page-serp';
        var items = document.querySelectorAll(selectors);
        for (var i = 0; i < items.length && results.length < 15; i++) {
            var item = items[i];
            var titleA = item.querySelector('a.link-organic, a.snippet__host, a.title__link, h2 a, h3 a');
            if (!titleA) {
                // Fallback: first external link
                var allA = item.querySelectorAll('a[href]');
                for (var j = 0; j < allA.length; j++) {
                    var h = allA[j].getAttribute('href') || '';
                    if (h.startsWith('http') && h.indexOf('yandex') === -1) {
                        titleA = allA[j];
                        break;
                    }
                }
            }
            if (!titleA) continue;
            var title = (titleA.textContent || '').trim();
            var href = titleA.getAttribute('href') || '';
            if (!href.startsWith('http')) continue;
            if (href.indexOf('yandex') !== -1) continue;
            if (seen[href] || !title || title.length < 3) continue;
            seen[href] = true;

            var snippet = '';
            var allText = (item.innerText || '').trim();
            var idx = allText.indexOf(title);
            if (idx !== -1) snippet = allText.substring(idx + title.length).trim().substring(0, 400);

            results.push({title: title.substring(0,120), url: href.substring(0,200), snippet: snippet});
        }

        // Strategy 2: Generic link scan — find external links on search page
        if (results.length < 3) {
            var anchors = document.querySelectorAll('a[href]');
            for (var k = 0; k < anchors.length && results.length < 20; k++) {
                var a = anchors[k];
                var h = a.getAttribute('href') || '';
                if (!h.startsWith('http')) continue;
                if (h.indexOf('yandex') !== -1) continue;
                if (seen[h]) continue;
                var t = (a.textContent || '').trim();
                if (!t || t.length < 5 || t.length > 150) continue;
                seen[h] = true;
                results.push({title: t.substring(0, 120), url: h.substring(0, 200), snippet: ''});
            }
        }

        return JSON.stringify(results);
    })()"""


def _js_github_extract() -> str:
    """Extract GitHub search results — handles code, repo, and issue search pages."""
    return """(function() {
        var results = [];
        var seen = {};

        // Verify we're on a search page
        if (window.location.href.indexOf('/search') === -1) {
            return JSON.stringify([]);
        }

        // Strategy 1: article elements (code search results)
        var articles = document.querySelectorAll('article');
        for (var i = 0; i < articles.length && results.length < 15; i++) {
            var art = articles[i];
            var h3 = art.querySelector('h3');
            if (!h3) continue;
            var titleA = h3.querySelector('a');
            if (!titleA) continue;
            var title = (titleA.textContent || '').trim();
            var href = titleA.getAttribute('href') || '';
            if (!href || seen[href]) continue;
            if (href.indexOf('http') !== 0) href = 'https://github.com' + href;
            seen[href] = true;
            if (!title || title.length < 2) continue;

            // Snippet: look for code match text or description paragraph
            var snippet = '';
            var codeBlock = art.querySelector('.BorderGrid-row, .search-code-highlight, code, .search-match');
            if (codeBlock) {
                snippet = (codeBlock.textContent || '').trim().substring(0, 400);
            }
            if (!snippet) {
                var p = art.querySelector('p');
                if (p) snippet = (p.textContent || '').trim().substring(0, 400);
            }
            // Fallback: get text from the article minus the title
            if (!snippet) {
                var artText = (art.innerText || '').trim();
                var idx = artText.indexOf(title);
                if (idx !== -1) snippet = artText.substring(idx + title.length).trim().substring(0, 400);
            }

            results.push({title: title.substring(0, 120), url: href.substring(0, 200), snippet: snippet.substring(0, 400)});
        }

        // Strategy 2: repo-result elements (repo search)
        var repos = document.querySelectorAll('.repo-result');
        for (var j = 0; j < repos.length && results.length < 15; j++) {
            var repo = repos[j];
            var repoLink = repo.querySelector('h3 a');
            if (!repoLink) continue;
            var repoTitle = (repoLink.textContent || '').trim();
            var repoHref = repoLink.getAttribute('href') || '';
            if (!repoHref || seen[repoHref]) continue;
            if (repoHref.indexOf('http') !== 0) repoHref = 'https://github.com' + repoHref;
            seen[repoHref] = true;

            var repoSnippet = '';
            var desc = repo.querySelector('p');
            if (desc) repoSnippet = (desc.textContent || '').trim().substring(0, 400);

            results.push({title: repoTitle.substring(0, 120), url: repoHref.substring(0, 200), snippet: repoSnippet});
        }

        // Strategy 3: generic link scan (fallback for any search type)
        if (results.length < 3) {
            var anchors = document.querySelectorAll('a[href]');
            for (var k = 0; k < anchors.length && results.length < 15; k++) {
                var a = anchors[k];
                var h = a.getAttribute('href') || '';
                if (!h || h.indexOf('/search') !== -1) continue;
                if (h.indexOf('http') !== 0) h = 'https://github.com' + h;
                if (seen[h]) continue;
                var t = (a.textContent || '').trim();
                if (!t || t.length < 3 || t.length > 150) continue;
                // Skip nav items
                if (t.indexOf('Sign in') !== -1 || t.indexOf('Sign up') !== -1) continue;
                seen[h] = true;
                results.push({title: t.substring(0, 120), url: h.substring(0, 200), snippet: ''});
            }
        }

        return JSON.stringify(results);
    })()"""


def _js_stats_gov_extract() -> str:
    """Extract stats.gov.cn search results.
    The search page loads results via AJAX into .work-list or .draw-searchResult.
    We wait for the content to be populated and extract title+url+snippet.
    """
    return """(function() {
        var results = [];
        var seen = {};

        // Strategy 1: Result items in the search results area
        var items = document.querySelectorAll('.work-item, .search-result-item, .result-item, .draw-searchResult > div, .main-region-list > div');
        for (var i = 0; i < items.length && results.length < 20; i++) {
            var item = items[i];
            var links = item.querySelectorAll('a[href]');
            if (!links.length) continue;
            var link = links[0];
            var href = link.getAttribute('href') || '';
            if (href.indexOf('http') !== 0) href = 'https://www.stats.gov.cn' + href;
            if (href.indexOf('stats.gov.cn') === -1 || seen[href]) continue;
            var title = (link.textContent || '').trim();
            if (!title || title.length < 3) continue;
            seen[href] = true;

            // Snippet: sibling text or next element
            var snippet = '';
            var nextEl = item.nextElementSibling;
            if (nextEl) snippet = (nextEl.textContent || '').trim().substring(0, 300);
            if (!snippet) {
                var itemText = (item.innerText || '').trim();
                var tIdx = itemText.indexOf(title);
                if (tIdx !== -1) snippet = itemText.substring(tIdx + title.length).trim().substring(0, 300);
            }

            results.push({title: title.substring(0, 120), url: href.substring(0, 200), snippet: snippet});
        }

        // Strategy 2: Generic link scan from the results area
        if (results.length < 3) {
            var resultArea = document.querySelector('.work-list, .draw-searchResult, .main-region-list, #result');
            var container = resultArea || document.body;
            var anchors = container.querySelectorAll('a[href]');
            for (var j = 0; j < anchors.length && results.length < 20; j++) {
                var h = anchors[j].getAttribute('href') || '';
                if (h.indexOf('http') !== 0) h = 'https://www.stats.gov.cn' + h;
                if (h.indexOf('stats.gov.cn') === -1 || seen[h]) continue;
                if (h.indexOf('/search') !== -1) continue;
                var t = (anchors[j].textContent || '').trim();
                if (!t || t.length < 3 || t.length > 150) continue;
                seen[h] = true;
                results.push({title: t.substring(0, 120), url: h.substring(0, 200), snippet: ''});
            }
        }

        return JSON.stringify(results);
    })()"""


def _js_generic_extract() -> str:
    """Generic extraction for unknown search engines / community sites."""
    return """(function() {
        var results = [];
        var seen = {};
        var anchors = document.querySelectorAll('a[href]');
        for (var i = 0; i < anchors.length && results.length < 20; i++) {
            var a = anchors[i];
            var href = a.getAttribute('href') || '';
            var title = (a.innerText || a.textContent || '').trim();
            if (!title || title.length < 3) continue;
            var realUrl = href;
            if (href.indexOf('/url?q=') !== -1) {
                var m = href.match(/\\/url\\?q=(https?:\\/\\/[^&]+)/);
                if (m) realUrl = m[1]; else continue;
            } else if (!href.startsWith('http')) continue;
            if (realUrl.length < 15 || seen[realUrl]) continue;
            seen[realUrl] = true;

            var snippet = '';
            var parent = a.parentElement;
            if (parent) {
                var next = parent.nextElementSibling;
                if (next) snippet = (next.innerText || '').trim();
                if (!snippet) {
                    var p = a.closest('div, li, article, tr');
                    if (p) {
                        var t = (p.innerText || '').trim();
                        if (t.length > title.length) snippet = t.substring(title.length).trim();
                    }
                }
            }
            results.push({title: title.substring(0,120), url: realUrl.substring(0,200), snippet: snippet.substring(0,400)});
        }
        return JSON.stringify(results);
    })()"""


def _js_xueqiu_extract() -> str:
    """Extract Xueqiu (xueqiu.com) search results via DOM selectors.

    Only runs on search pages (URL contains /k?). Each result is
    article.timeline__item with author, post URL, and content.
    """
    return """(function() {
        // Verify we're on a search page
        if (window.location.href.indexOf('/k?') === -1 && window.location.href.indexOf('/search') === -1) {
            return JSON.stringify([]);
        }

        var results = [];
        var seen = {};

        // 1) Extract discussion posts from article.timeline__item
        var articles = document.querySelectorAll('article.timeline__item');
        for (var i = 0; i < articles.length && results.length < 15; i++) {
            var art = articles[i];

            // Author: a.user-name
            var authorA = art.querySelector('a.user-name');
            var author = authorA ? (authorA.textContent || '').trim() : '';

            // Post URL: a.date-and-source (href like /2931099276/391718028)
            var dateA = art.querySelector('a.date-and-source');
            var href = dateA ? (dateA.getAttribute('href') || '') : '';
            if (href && href.indexOf('http') === 0) {
                // keep as-is
            } else if (href && href.indexOf('/') === 0) {
                href = 'https://xueqiu.com' + href;
            } else if (href && href.indexOf('//') === 0) {
                href = 'https:' + href;
            }
            if (!href || seen[href]) continue;

            // Skip if URL looks like a non-post link (write page, settings, etc)
            if (href.indexOf('/write') !== -1 || href.indexOf('/settings') !== -1 ||
                href.indexOf('/mp/') !== -1 || href.indexOf('position=') !== -1) continue;

            seen[href] = true;

            // Content: div.content.content--description or div.timeline__item__content
            var contentDiv = art.querySelector('div.content.content--description') ||
                             art.querySelector('div.timeline__item__content');
            var body = contentDiv ? (contentDiv.textContent || '').trim() : '';
            if (!body || body.length < 5) continue;

            // 发布日期（best-effort）：a.date-and-source 文本形如 "07-18 10:23 · 来自..."，抓不到置空
            var dateText = '';
            try {
                if (dateA) {
                    dateText = (dateA.textContent || '').trim();
                    var dotIdx = dateText.indexOf('·');
                    if (dotIdx !== -1) dateText = dateText.substring(0, dotIdx).trim();
                }
            } catch (e) { dateText = ''; }

            // Title: first 80 chars of body, author as prefix
            var title = (author ? author + ': ' : '') + body.substring(0, 80);
            var snippet = body.substring(0, 400);

            results.push({title: title.substring(0, 120), url: href, snippet: snippet, date: dateText});
        }

        // 2) Extract stock results from links with /S/CODE
        var anchors = document.querySelectorAll('a[href]');
        for (var j = 0; j < anchors.length && results.length < 20; j++) {
            var h = anchors[j].getAttribute('href') || '';
            if (!h.match(/^\/S\/[A-Z0-9]+$/)) continue;
            var stockUrl = 'https://xueqiu.com' + h;
            if (seen[stockUrl]) continue;
            seen[stockUrl] = true;
            var stockName = (anchors[j].textContent || '').trim();
            if (!stockName || stockName.length < 2) continue;

            var snippet = '';
            var row = anchors[j].closest('tr, div');
            if (row) snippet = (row.textContent || '').trim().substring(0, 200);

            results.push({title: stockName, url: stockUrl, snippet: snippet, date: ''});
        }

        return JSON.stringify(results);
    })()"""




def _js_zhihu_extract() -> str:
    """Extract Zhihu (zhihu.com) search results via DOM selectors.

    Only runs on search pages (URL contains /search). Each result card has
    class 'ContentItem' with a link to zhuanlan.zhihu.com or /question/.
    """
    return """(function() {
        // Verify we're on a search page
        if (window.location.href.indexOf('/search') === -1) {
            return JSON.stringify([]);
        }

        var results = [];
        var seen = {};
        var cards = document.querySelectorAll('.ContentItem');

        // Navigation URLs to skip
        var skipPatterns = ['ring-feeds', '/columns', '/people', '/topic/',
            '创作中心', '热榜', '推荐', '关注', '圈子', '专栏', '故事',
            '付费咨询', '电子书', '话题', '视频', 'AI 搜索', '论文', '直答',
            '帮助中心', 'settings', 'notification'];

        for (var i = 0; i < cards.length && results.length < 15; i++) {
            var card = cards[i];
            var links = card.querySelectorAll('a[href]');
            var titleLink = null;

            for (var j = 0; j < links.length; j++) {
                var h = links[j].getAttribute('href') || '';
                if (h.indexOf('zhuanlan.zhihu.com') !== -1 ||
                    h.match(/^\/(?:question|answer|article|p)\//)) {
                    // Skip navigation links
                    var skip = false;
                    for (var k = 0; k < skipPatterns.length; k++) {
                        if (h.indexOf(skipPatterns[k]) !== -1) { skip = true; break; }
                    }
                    if (!skip) {
                        titleLink = links[j];
                        break;
                    }
                }
            }
            if (!titleLink) continue;

            var href = titleLink.getAttribute('href');
            var fullUrl = href;
            if (href.indexOf('http') === 0) {
                fullUrl = href;
            } else if (href.indexOf('//') === 0) {
                fullUrl = 'https:' + href;
            } else {
                fullUrl = 'https://www.zhihu.com' + href;
            }
            if (seen[fullUrl]) continue;

            var title = (titleLink.textContent || '').trim().replace(/\s+/g, ' ');
            if (title.length < 5 || title.length > 120) continue;

            // Skip if title looks like navigation text
            var titleSkip = true;
            for (var m = 0; m < skipPatterns.length; m++) {
                if (title.indexOf(skipPatterns[m]) !== -1) { titleSkip = false; break; }
            }
            // Actually: skip IF any pattern matches
            var titleSkip = false;
            for (var m = 0; m < skipPatterns.length; m++) {
                if (title.indexOf(skipPatterns[m]) !== -1) { titleSkip = true; break; }
            }
            if (titleSkip) continue;

            // Snippet: card text minus title
            var cardText = (card.innerText || '').trim().replace(/\s+/g, ' ');
            var snippet = '';
            var idx = cardText.indexOf(title);
            if (idx !== -1) {
                snippet = cardText.substring(idx + title.length).trim();
            }
            if (snippet.indexOf('阅读全文') !== -1) {
                snippet = snippet.substring(0, snippet.indexOf('阅读全文')).trim();
            }
            snippet = snippet.replace(/赞同\s*\d+/g, '').replace(/评论/g, '').replace(/添加评论/g, '').trim();

            // 发布日期（best-effort）：卡片 data-tooltip 或文本里的完整日期，抓不到置空
            var dateText = '';
            try {
                var dm = null;
                var tmEl = card.querySelector('[data-tooltip]');
                if (tmEl) dm = (tmEl.getAttribute('data-tooltip') || '').match(/\d{4}-\d{2}-\d{2}/);
                if (!dm) dm = cardText.match(/\d{4}-\d{2}-\d{2}/);
                if (dm) dateText = dm[0];
            } catch (e) { dateText = ''; }

            seen[fullUrl] = true;
            results.push({
                title: title.substring(0, 120),
                url: fullUrl.substring(0, 200),
                snippet: snippet.substring(0, 400),
                date: dateText
            });
        }
        return JSON.stringify(results);
    })()"""




def _js_eastmoney_extract() -> str:
    """Extract Eastmoney (eastmoney.com) search results.
    Page has sections: 相关板块/个股/题材/公告/研报/资讯/股吧.
    Strategy: collect all links, filter for news/article URLs, use link text as title.
    """
    return """(function() {
        var results = [];
        var seen = {};
        var anchors = document.querySelectorAll('a[href]');
        var links = [];

        // Collect all links with text
        for (var i = 0; i < anchors.length; i++) {
            var h = (anchors[i].getAttribute('href') || '').trim();
            var t = (anchors[i].textContent || '').replace(/\\s+/g, ' ').trim();
            if (h && t && h !== '#' && h !== 'javascript:;' && h.indexOf('javascript') !== 0) {
                // 发布日期（best-effort）：优先同 li 内 span.zixun_time（MM-DD），其次文章URL里的日期，抓不到置空
                var d = '';
                try {
                    var li = anchors[i].closest('li');
                    var timeEl = li ? li.querySelector('.zixun_time') : null;
                    if (timeEl) d = (timeEl.textContent || '').trim();
                    if (!d) {
                        var um = h.match(/\/a\/(\\d{4})(\\d{2})(\\d{2})\\d*\.html/);
                        if (um) d = um[1] + '-' + um[2] + '-' + um[3];
                    }
                } catch (e) { d = ''; }
                links.push({url: h, text: t, date: d});
            }
        }

        // Filter for news/article links
        for (var j = 0; j < links.length && results.length < 20; j++) {
            var url = links[j].url;
            var text = links[j].text;

            // Skip stock quotes, navigation, and short noise
            if (url.indexOf('/quote/') !== -1 || url.indexOf('/data/') !== -1) continue;
            if (url.indexOf('guba.eastmoney') !== -1) continue;
            if (text.length < 4 || text.length > 200) continue;
            if (/^[0-9.,%\\-+\\./（）]+$/.test(text)) continue;
            if (text.match(/^(查看|更多|详情|全部|首页|下一页|登录|注册|下载|开户|申购|行情|股吧|数据)/)) continue;

            // Match news/article URLs
            var isArticle = (
                url.indexOf('/a/') !== -1 ||
                url.indexOf('/news/') !== -1 ||
                url.indexOf('/article/') !== -1 ||
                url.indexOf('finance.eastmoney.com') !== -1 ||
                url.indexOf('stock.eastmoney.com') !== -1 ||
                url.indexOf('research.eastmoney.com') !== -1 ||
                (url.indexOf('eastmoney.com/a/') !== -1)
            );

            if (!isArticle) continue;

            // Normalize URL
            if (url.indexOf('http') !== 0) {
                url = 'https:' + url;
            }
            if (seen[url]) continue;
            seen[url] = true;

            results.push({
                title: text.substring(0, 120),
                url: url,
                snippet: text.substring(0, 300),
                date: links[j].date || ''
            });
        }

        // Fallback: if few results, try text-based parsing
        if (results.length < 3) {
            var text = document.body ? document.body.innerText : '';
            var lines = text.split('\\n').map(function(l) { return l.trim(); }).filter(function(l) { return l; });
            var dateRe = /([0-9]{2}-[0-9]{2})$/;

            for (var k = 0; k < lines.length && results.length < 15; k++) {
                var line = lines[k];
                if (line.length < 10 || line.length > 200) continue;
                var m = line.match(dateRe);
                if (m && k + 1 < lines.length) {
                    var nextLine = lines[k + 1];
                    var nextUrl = null;
                    for (var l = 0; l < links.length; l++) {
                        if (links[l].text.indexOf(line.substring(0, 15)) !== -1) {
                            nextUrl = links[l].url;
                            break;
                        }
                    }
                    if (nextUrl && !seen[nextUrl]) {
                        if (nextUrl.indexOf('http') !== 0) nextUrl = 'https:' + nextUrl;
                        seen[nextUrl] = true;
                        results.push({
                            title: line.replace(dateRe, '').trim().substring(0, 120),
                            url: nextUrl,
                            snippet: line.substring(0, 300),
                            date: m[0]
                        });
                    }
                }
            }
        }

        return JSON.stringify(results);
    })()"""


def _js_twitter_extract() -> str:
    """Extract Twitter/X (x.com) search results.
    Tweets in [data-testid="tweet"] with text, author, time.
    """
    return """(function() {
        var results = [];
        var seen = {};

        var tweets = document.querySelectorAll('[data-testid="tweet"], article');
        for (var i = 0; i < tweets.length && results.length < 15; i++) {
            var tweet = tweets[i];

            // Author
            var authorEl = tweet.querySelector('[data-testid="User-Name"] a, [data-screen-name]');
            var author = authorEl ? (authorEl.textContent || '').trim() : '';

            // Tweet text
            var textEl = tweet.querySelector('[data-testid="tweetText"]');
            if (!textEl) textEl = tweet;
            var text = (textEl ? (textEl.innerText || '').trim() : '');
            if (!text || text.length < 3) continue;

            // Build title
            var title = (author ? author + ': ' : '') + text.substring(0, 80);

            // URL: from link containing /status/
            var href = '';
            var permA = tweet.querySelector('a[href*="/status/"]');
            if (permA) {
                href = permA.getAttribute('href') || '';
                if (!href.startsWith('http')) href = 'https://x.com' + href;
            }
            if (!href || seen[href]) continue;
            seen[href] = true;

            results.push({title: title.substring(0,120), url: href.substring(0,200), snippet: text.substring(0,400)});
        }
        return JSON.stringify(results);
    })()"""


def _js_sov2ex_extract() -> str:
    """Extract sov2ex.com search results (SPA-based v2ex search engine).
    sov2ex.com renders search results via JS. Each result is a .resultcard
    with a topic link a[href*="/t/"] pointing to v2ex.com/t/ID.
    """
    return """(function() {
        var results = [];
        var seen = {};

        // Strategy 1: .resultcard elements
        var cards = document.querySelectorAll('.resultcard');
        for (var i = 0; i < cards.length && results.length < 15; i++) {
            var card = cards[i];
            var titleA = card.querySelector('a[href*="/t/"]');
            if (!titleA) continue;
            var title = (titleA.textContent || '').trim();
            var href = titleA.getAttribute('href') || '';
            if (!href || seen[href]) continue;
            if (href.indexOf('http') !== 0) href = 'https://www.v2ex.com' + href;
            seen[href] = true;
            if (!title || title.length < 3) continue;

            var snippet = '';
            var cardText = (card.innerText || '').trim();
            var idx = cardText.indexOf(title);
            if (idx !== -1) snippet = cardText.substring(idx + title.length).trim().substring(0, 400);

            results.push({title: title.substring(0, 120), url: href.substring(0, 200), snippet: snippet});
        }

        // Strategy 2: Topic link scan (fallback)
        if (results.length < 3) {
            var anchors = document.querySelectorAll('a[href*="/t/"]');
            for (var j = 0; j < anchors.length && results.length < 15; j++) {
                var a = anchors[j];
                var h = a.getAttribute('href') || '';
                if (!h || seen[h]) continue;
                if (h.indexOf('http') !== 0) h = 'https://www.v2ex.com' + h;
                seen[h] = true;
                var t = (a.textContent || '').trim();
                if (!t || t.length < 3) continue;
                results.push({title: t.substring(0, 120), url: h.substring(0, 200), snippet: ''});
            }
        }

        return JSON.stringify(results);
    })()"""


def _js_trendforce_extract() -> str:
    """Extract TrendForce search results via text parsing.
    TrendForce search results show article title, category, date, and snippet.
    """
    return """(function() {
        var results = [];
        var seen = {};
        var anchors = document.querySelectorAll('a[href]');

        for (var i = 0; i < anchors.length && results.length < 15; i++) {
            var a = anchors[i];
            var href = a.getAttribute('href') || '';
            if (!href.startsWith('http')) href = 'https://www.trendforce.com' + href;

            // Content links: /tech/insights/..., /commodity-prices/...
            if (href.indexOf('/tech/') === -1 && href.indexOf('/commodity-prices/') === -1 &&
                href.indexOf('/news/') === -1 && href.indexOf('/reports/') === -1) continue;
            if (href.indexOf('/search') !== -1) continue;

            if (seen[href]) continue;

            var title = (a.textContent || '').trim().replace(/\\s+/g, ' ');
            if (!title || title.length < 3) continue;

            seen[href] = true;

            var snippet = '';
            var card = a.closest('div, li, article');
            if (card) {
                var cardText = (card.innerText || '').trim().replace(/\\s+/g, ' ');
                if (cardText.length > title.length) {
                    snippet = cardText.substring(title.length, title.length + 400).trim();
                }
            }

            results.push({title: title.substring(0, 120), url: href.substring(0, 200), snippet: snippet.substring(0, 400)});
        }
        return JSON.stringify(results);
    })()"""


async def navigate_to_login(source_name: str) -> dict:
    """Navigate to the login page for a source. Returns {url, title, logged_in}.
    Used for the /login/{source_name} endpoint."""
    login_url = LOGIN_URLS.get(source_name)
    if not login_url:
        return {"error": f"No login URL configured for {source_name}"}

    check = None
    from app.sources.edge_mcp_source import LOGIN_CHECKS
    check = LOGIN_CHECKS.get(source_name)

    # Navigate to login URL
    page = await navigate(login_url, wait=3.0, source_name=source_name)

    return {
        "source": source_name,
        "login_url": login_url,
        "current_url": page.get("url", ""),
        "title": page.get("title", ""),
        "message": f"请在浏览器中登录 {source_name}，登录完成后访问 GET /status/login 检查状态",
    }


def _js_youtube_extract() -> str:
    """Extract YouTube search results (video title, URL, channel, views, duration).
    YouTube search page uses yt-lockup components with video metadata.
    Also attempts to extract chapter descriptions as content preview.
    """
    return """(function() {
        var results = [];
        var seen = {};

        if (window.location.href.indexOf('/results') === -1) {
            return JSON.stringify([]);
        }

        // Strategy 1: yt-lockup video results
        var lockups = document.querySelectorAll('ytd-video-renderer, yt-lockup');
        for (var i = 0; i < lockups.length && results.length < 15; i++) {
            var lock = lockups[i];

            // Title: h3 a
            var titleA = lock.querySelector('h3 a');
            if (!titleA) continue;
            var title = (titleA.textContent || '').trim();
            if (!title || title.length < 3) continue;

            // URL: extract video ID from href
            var href = titleA.getAttribute('href') || '';
            var videoId = '';
            var m = href.match(/[?&]v=([A-Za-z0-9_-]{11})/);
            if (m) videoId = m[1];
            if (!videoId || seen[videoId]) continue;
            seen[videoId] = true;

            // Channel: a.yt-simple-endpoint with channel link
            var channel = '';
            var channelA = lock.querySelector('a[href*="/@"], a[href*="/channel/"]');
            if (channelA) channel = (channelA.textContent || '').trim();

            // Metadata: view count, time ago, duration
            var metaText = '';
            var metaEls = lock.querySelectorAll('#meta .metadata-line-text, .metadata-line-text');
            for (var j = 0; j < metaEls.length; j++) {
                metaText += ' ' + (metaEls[j].textContent || '').trim();
            }

            // Duration
            var duration = '';
            var durEl = lock.querySelector('#text_container #text');
            if (durEl) duration = (durEl.textContent || '').trim();

            // Description: chapter list or video description
            var desc = '';
            var descEl = lock.querySelector('#description, .description-content, #expanded-description-content');
            if (descEl) desc = (descEl.textContent || '').trim().substring(0, 400);

            // Chapters: look for section-list-renderer
            var chapters = '';
            var chEl = lock.querySelector('#sections');
            if (chEl) chapters = (chEl.textContent || '').trim().substring(0, 300);

            var snippet = [channel, metaText, duration, desc, chapters].filter(function(x) { return x; }).join(' | ');

            results.push({
                title: title.substring(0, 120),
                url: 'https://www.youtube.com/watch?v=' + videoId,
                snippet: snippet.substring(0, 400)
            });
        }

        // Strategy 2: Generic link scan for /watch?v= links
        if (results.length < 3) {
            var anchors = document.querySelectorAll('a[href*="/watch?v="]');
            for (var k = 0; k < anchors.length && results.length < 15; k++) {
                var h = anchors[k].getAttribute('href') || '';
                var vm = h.match(/[?&]v=([A-Za-z0-9_-]{11})/);
                if (!vm || seen[vm[1]]) continue;
                seen[vm[1]] = true;
                var t = (anchors[k].textContent || '').trim();
                if (!t || t.length < 3) continue;
                results.push({
                    title: t.substring(0, 120),
                    url: 'https://www.youtube.com/watch?v=' + vm[1],
                    snippet: ''
                });
            }
        }

        return JSON.stringify(results);
    })()"""


async def cleanup_unused_tabs():
    """Close all non-essential tabs after search completes.

    Keeps:
    - Login site tabs (zhihu, xueqiu, twitter, eastmoney) — needed for auth
    - 1 neutral tab — to prevent Edge from closing

    Closes everything else (dedicated sites, random pages, about:blank).
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://127.0.0.1:9222/json")
            resp.raise_for_status()
            tabs = resp.json()
    except Exception:
        logger.warning("cleanup_unused_tabs: failed to list tabs")
        return

    # Identify which tabs belong to login sites (keep these)
    login_domains = set(LOGIN_SITES.values())

    def _extract_hostname(url):
        url = url.lower().split("?")[0].split("#")[0].split("/")[2] if url.count("/") >= 2 else ""
        if ":" in url:
            url = url.split(":")[0]
        return url

    def _tab_is_login(tab):
        hostname = _extract_hostname(tab.get("url") or "")
        canonical = DOMAIN_ALIASES.get(hostname, hostname)
        for domain in login_domains:
            if hostname == domain or canonical == domain or hostname.endswith("." + domain):
                return True
        return False

    def _tab_is_closable(tab):
        url = (tab.get("url") or "").lower()
        if url.startswith("chrome-extension://") or url.startswith("edge://"):
            return False
        if tab.get("type") not in ("page", "iframe"):
            return False
        if _tab_is_login(tab):
            return False
        return True

    # Collect tabs: login tabs to keep, others are candidates for closing
    login_tabs = [t for t in tabs if _tab_is_login(t)]
    closable = [t for t in tabs if _tab_is_closable(t) and "webSocketDebuggerUrl" in t]

    # Keep 1 neutral tab, close the rest
    to_close = closable[1:] if len(closable) > 1 else []

    closed = 0
    for tab in to_close:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(f"http://127.0.0.1:9222/json/close/{tab['id']}")
            closed += 1
        except Exception:
            pass

    if closed:
        logger.info(f"cleanup: closed {closed} tabs, kept {len(login_tabs)} login + {max(1, len(closable) - closed)} neutral")


def _js_bilibili_extract() -> str:
    """Extract Bilibili search results from search.bilibili.com/video page.
    Only extracts from main search results area (.search-body .video-list).
    Filters out sidebar noise (稍后再看, 收藏, etc).
    """
    return """(function() {
        var results = [];
        var seen = {};

        if (window.location.href.indexOf('search.bilibili.com') === -1) {
            return JSON.stringify([]);
        }

        // Noise prefixes to skip
        var noisePrefixes = ['稍后再看', '收藏', '分享', '点赞', '投币', '关注'];

        function isNoise(title) {
            for (var i = 0; i < noisePrefixes.length; i++) {
                if (title.indexOf(noisePrefixes[i]) === 0) return true;
            }
            return false;
        }

        // Strategy 1: Only .video-item inside main search results (.search-body or .video-list)
        var mainArea = document.querySelector('.search-body, .video-list, #result, .main-container');
        var container = mainArea || document.body;
        var items = container.querySelectorAll('.video-item');

        for (var i = 0; i < items.length && results.length < 15; i++) {
            var item = items[i];

            // Title: .title a (specific selector for search results)
            var titleA = item.querySelector('.title a');
            if (!titleA) continue;
            var title = (titleA.textContent || '').trim();
            if (!title || title.length < 5 || isNoise(title)) continue;

            // URL
            var href = titleA.getAttribute('href') || '';
            if (!href || seen[href]) continue;
            if (href.indexOf('http') !== 0) {
                if (href.indexOf('//') === 0) href = 'https:' + href;
                else href = 'https:' + href;
            }
            seen[href] = true;

            // Author
            var author = '';
            var authorA = item.querySelector('.author .author-name, .author .name');
            if (authorA) author = (authorA.textContent || '').trim();

            // Duration
            var duration = '';
            var durEl = item.querySelector('.duration');
            if (durEl) duration = (durEl.textContent || '').trim();

            // Play count + date from data items
            var stats = '';
            var dataEls = item.querySelectorAll('.data-item span');
            for (var j = 0; j < dataEls.length; j++) {
                stats += ' ' + (dataEls[j].textContent || '').trim();
            }

            var snippet = [author, duration, stats].filter(function(x) { return x; }).join(' | ');

            // 发布日期（best-effort）：.so-time 或 stats 文本里的完整日期，抓不到置空
            var dateText = '';
            try {
                var tm = null;
                var timeEl = item.querySelector('.so-time, span.date, [class*="time"], .bili-video-card__info--date');
                if (timeEl) tm = (timeEl.textContent || '').match(/\\d{4}-\\d{1,2}-\\d{1,2}/);
                if (!tm) tm = stats.match(/\\d{4}-\\d{1,2}-\\d{1,2}/);
                if (tm) dateText = tm[0];
            } catch (e) { dateText = ''; }

            results.push({
                title: title.substring(0, 120),
                url: href.substring(0, 200),
                snippet: snippet.substring(0, 400),
                date: dateText
            });
        }

        // Strategy 2: Link scan for bilibili.com/video/ URLs (fallback, with noise filter)
        if (results.length < 3) {
            var anchors = document.querySelectorAll('a[href*="bilibili.com/video/"]');
            for (var k = 0; k < anchors.length && results.length < 15; k++) {
                var h = anchors[k].getAttribute('href') || '';
                if (!h || seen[h]) continue;
                if (h.indexOf('http') !== 0) h = 'https:' + h;
                seen[h] = true;
                var t = (anchors[k].textContent || '').trim();
                if (!t || t.length < 5 || isNoise(t)) continue;
                // 发布日期（best-effort）：卡片底部 span.bili-video-card__info--date，抓不到置空
                var dt2 = '';
                try {
                    var card = anchors[k].closest('.bili-video-card');
                    var dateEl = card ? card.querySelector('.bili-video-card__info--date') : null;
                    if (dateEl) dt2 = (dateEl.textContent || '').trim();
                } catch (e) { dt2 = ''; }
                results.push({
                    title: t.substring(0, 120),
                    url: h.substring(0, 200),
                    snippet: '',
                    date: dt2
                });
            }
        }

        return JSON.stringify(results);
    })()"""
