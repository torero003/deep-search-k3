import asyncio
import logging
import re
import time
from datetime import date, timedelta
from urllib.parse import quote
from app.sources.base import BaseSource, SearchResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engagement extraction from search result text
# ---------------------------------------------------------------------------

def _parse_num(s: str) -> int:
    """Parse a number string like '1.2k', '3.5K', '10万' into an integer."""
    s = s.strip().lower()
    multipliers = {"k": 1000, "m": 1000000, "万": 10000, "亿": 100000000}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            try:
                return int(float(s) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def extract_engagement(source_name: str, text: str) -> dict | None:
    """Extract engagement numbers from search result text (title + snippet).

    Returns dict like {"upvotes": 1234, "comments": 56} or None.
    """
    eng = {}

    if source_name == "twitter":
        m = re.search(r"(\d+\.?\d*[kK]?)\s*Likes?", text, re.I)
        if m:
            eng["likes"] = _parse_num(m.group(1))
        m = re.search(r"(\d+\.?\d*[kK]?)\s*Reposts?", text, re.I)
        if m:
            eng["reposts"] = _parse_num(m.group(1))
        m = re.search(r"(\d+\.?\d*[kK]?)\s*Replies?", text, re.I)
        if m:
            eng["replies"] = _parse_num(m.group(1))
        m = re.search(r"(\d+\.?\d*[kK]?)\s*Quotes?", text, re.I)
        if m:
            eng["quotes"] = _parse_num(m.group(1))

    elif source_name == "v2ex":
        m = re.search(r"(\d+\.?\d*[kK]?)\s*回复", text)
        if m:
            eng["replies"] = _parse_num(m.group(1))

    elif source_name == "github":
        m = re.search(r"(\d+\.?\d*[kK]?)\s*stars?", text, re.I)
        if m:
            eng["stars"] = _parse_num(m.group(1))
        m = re.search(r"(\d+\.?\d*[kK]?)\s*forks?", text, re.I)
        if m:
            eng["forks"] = _parse_num(m.group(1))

    elif source_name == "youtube":
        m = re.search(r"(\d+\.?\d*[kK]?)\s*万次观看", text)
        if not m:
            m = re.search(r"(\d+\.?\d*[kK]?)\s*views?", text, re.I)
        if m:
            eng["views"] = _parse_num(m.group(1))
        m = re.search(r"(\d+\.?\d*[kK]?)\s*点赞", text)
        if m:
            eng["likes"] = _parse_num(m.group(1))

    elif source_name == "bilibili":
        m = re.search(r"(\d+\.?\d*[kK]?)\s*万次播放", text)
        if not m:
            m = re.search(r"(\d+\.?\d*[kK]?)\s*播放", text)
        if m:
            eng["plays"] = _parse_num(m.group(1))
        m = re.search(r"(\d+\.?\d*[kK]?)\s*弹幕", text)
        if m:
            eng["comments"] = _parse_num(m.group(1))

    return eng if eng else None


def _normalize_cdp_date(raw: str) -> str:
    """把 CDP 提取的原始日期文本归一化为 YYYY-MM-DD，认不出来返回 ""。"""
    if not raw:
        return ""
    s = raw.strip()
    today = date.today()

    # 完整日期：2024-07-18 / 2024/07/18 / 2024年7月18日
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""

    # 月-日：07-18 / 7月18日（缺年份按今年算，晚于今天则按去年）
    m = re.search(r"(?<!\d)(\d{1,2})[-/月](\d{1,2})日?(?!\d)", s)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            if d > today:
                d = date(today.year - 1, int(m.group(1)), int(m.group(2)))
            return d.isoformat()
        except ValueError:
            return ""

    # 中文相对时间
    if "今天" in s or "小时前" in s or "分钟前" in s or "刚刚" in s:
        return today.isoformat()
    if "昨天" in s:
        return (today - timedelta(days=1)).isoformat()
    if "前天" in s:
        return (today - timedelta(days=2)).isoformat()
    m = re.search(r"(\d+)\s*天前", s)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*(?:周|星期)前", s)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*个?月前", s)
    if m:
        return (today - timedelta(days=30 * int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*年前", s)
    if m:
        try:
            return today.replace(year=today.year - int(m.group(1))).isoformat()
        except ValueError:
            return ""
    return ""

# Login state cache
LOGIN_STATE = {}
LOGIN_CHECK_TTL = 300  # 5 minutes

# Search URL templates for each source
SEARCH_TEMPLATES = {
    "google":      "https://www.google.com/search?q={query}&hl=zh-CN",
    "bing":        "https://www.bing.com/search?q={query}&setlang=zh-CN",
    "yandex":      "https://yandex.com/search/?text={query}&lr=134",
    "github":      "https://github.com/search?q={query}&type=code",
    "twitter":     "https://x.com/search?q={query}&f=live",
    "xueqiu":      "https://xueqiu.com/k?q={query}",
    "trendforce":  "https://www.trendforce.com/tech/search?q={query}",
    "v2ex":        "https://www.sov2ex.com/?q={query}",
    "stats_gov":   "https://www.stats.gov.cn/search/s?siteCode=bm36000002&tab=&qt={query}",
    "youtube":     "https://www.youtube.com/results?search_query={query}",
    "bilibili":    "https://search.bilibili.com/video?keyword={query}&order=totalrank",
}

# Sources that are currently non-functional due to external issues
# - yandex: CAPTCHA blocks automated requests
# - v2ex: /search redirects to forum node "搜索引擎技术研究"
# - sogou_wechat: CSP blocked by Edge browser
# - stats_gov: CDP unresponsive on search results page
BROKEN_SOURCES = {"yandex", "stats_gov", "trendforce"}  # yandex: CAPTCHA, stats_gov: CDP unresponsive, trendforce: frequent CDP timeout

# Login checks for sources that require authentication
# Uses text indicators (visible text on page) instead of CSS selectors
LOGIN_CHECKS = {
    "twitter": {
        "url": "https://x.com",
        "logged_in_text": ["Home", "Explore", "Profile"],
        "logged_out_text": ["Sign up", "Log in"],
    },
    "github": {
        "url": "https://github.com",
        "logged_in_text": ["Your repositories", "Pull requests", "Settings"],
        "logged_out_text": ["Sign up", "Sign in"],
    },
}


SOURCES_USE_SEARCH_URL = {"google", "bing", "yandex", "github", "twitter", "xueqiu", "v2ex", "stats_gov", "trendforce", "youtube", "bilibili"}


class EdgeMCPSource(BaseSource):
    """Source using direct CDP browser automation for all search engines."""

    def __init__(self, name: str):
        self.name = name

    def _sanitize_text(self, text: str) -> str:
        """Remove surrogate pairs that can't be encoded to UTF-8."""
        if not isinstance(text, str):
            return text
        return text.encode('utf-8', 'ignore').decode('utf-8', errors='ignore')

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if self.name in BROKEN_SOURCES:
            logger.debug(f"{self.name}: source currently non-functional, skipping")
            return []
        if self.name in LOGIN_CHECKS:
            logged_in = await self.check_login(self.name)
            if logged_in is False:
                # Login recovery: clear cached state, navigate to site homepage, recheck
                logger.warning(f"{self.name}: login check failed, attempting recovery")
                self._clear_login_cache()
                recovered = await self._attempt_login_recovery()
                if not recovered:
                    logger.warning(f"{self.name}: login recovery failed, returning empty")
                    return []
                logger.info(f"{self.name}: login recovery successful")
            elif logged_in is None:
                logger.warning(f"{self.name}: login status unknown, proceeding anyway")

        # v2ex needs special tab borrowing logic
        if self.name == "v2ex":
            return (await self._search_v2ex(query))[:max_results]

        url = SEARCH_TEMPLATES.get(self.name, SEARCH_TEMPLATES["google"]).format(query=quote(query))

        if self.name in SOURCES_USE_SEARCH_URL:
            structured = await self._search_structured(url)
            if structured:
                return structured[:max_results]
            if not structured:
                logger.warning(f"{self.name}: structured search returned 0 results from {url[:80]}")

        content = await self._navigate_and_fetch(url)
        return self._parse_content(content, self.name)[:max_results]

    async def _search_v2ex(self, query: str) -> list[SearchResult]:
        """Search v2ex via sov2ex.com, borrowing a tab if needed and restoring it after."""
        from app.sources.cdp_client import _get_cdp, _refresh_site_tabs, _site_tabs, search_url

        # Check if we have a dedicated sov2ex tab
        await _refresh_site_tabs()
        has_own_tab = "sov2ex.com" in _site_tabs
        borrowed_tab_url = None

        if not has_own_tab:
            # We'll borrow a tab — remember its current URL for restoration
            from app.sources.cdp_client import _current_tab_id
            cdp = await _get_cdp(source_name="v2ex")
            # Get current page URL before navigation
            try:
                result = await cdp.send("Runtime.evaluate", {
                    "expression": "document.location.href",
                    "returnByValue": True,
                })
                rval = result.get("result", {}).get("result", {})
                if rval.get("value"):
                    borrowed_tab_url = rval["value"]
                    logger.info(f"v2ex: borrowing tab at {borrowed_tab_url[:80]}")
            except Exception:
                pass

        url = SEARCH_TEMPLATES["v2ex"].format(query=quote(query))
        results = await search_url(url, wait=3.0, source_name="v2ex")

        # Restore borrowed tab
        if borrowed_tab_url:
            try:
                from app.sources.cdp_client import _get_cdp
                cdp = await _get_cdp()
                await cdp.send("Page.navigate", {"url": borrowed_tab_url})
                await asyncio.sleep(1.0)
                logger.info(f"v2ex: restored tab to {borrowed_tab_url[:80]}")
            except Exception as e:
                logger.warning(f"v2ex: failed to restore tab: {e}")

        output = []
        for r in results:
            if not r.get("title") or not r.get("url"):
                continue
            title = self._sanitize_text(r.get("title", ""))
            snippet = self._sanitize_text(r.get("snippet", ""))
            text = title + " " + snippet
            eng = extract_engagement("v2ex", text)
            output.append(SearchResult(
                title=title,
                url=self._sanitize_text(r.get("url", "")),
                content=snippet,
                source="v2ex",
                engagement=eng,
            ))
        return output

    async def _search_structured(self, url: str) -> list[SearchResult]:
        """Use JS-based structured extraction via CDP."""
        from app.sources.cdp_client import search_url
        results = await search_url(url, wait=3.0, source_name=self.name)
        output = []
        for r in results:
            if not r.get("title") or not r.get("url"):
                continue
            title = self._sanitize_text(r.get("title", ""))
            snippet = self._sanitize_text(r.get("snippet", ""))
            text = title + " " + snippet
            eng = extract_engagement(self.name, text)
            output.append(SearchResult(
                title=title,
                url=self._sanitize_text(r.get("url", "")),
                content=snippet,
                source=self.name,
                published_date=_normalize_cdp_date(r.get("date", "")),
                engagement=eng,
            ))
        return output

    async def _navigate_and_fetch(self, url: str) -> str:
        """Navigate via CDP and return page content as text (fallback)."""
        from app.sources.cdp_client import navigate
        page = await navigate(url, wait=3.0, source_name=self.name)
        return f"{page.get('title', '')}\n\n{page.get('text', '')}"

    def _parse_content(self, content: str, source: str) -> list[SearchResult]:
        """Parse search results from plain text (fallback parser)."""
        if source in ("google", "bing", "yandex"):
            return self._parse_search_engine_results(content, source)
        if source == "github":
            return self._parse_github_results(content)
        return self._parse_generic_results(content, source)

    def _parse_search_engine_results(self, content: str, source: str) -> list[SearchResult]:
        """Parse Google/Bing/Yandex search results from text."""
        results = []
        lines = content.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if (line.startswith("http") or "/url?" in line):
                url = ""
                if "/url?q=" in line:
                    url = line.split("/url?q=")[1].split("&")[0]
                elif line.startswith("http"):
                    url = line.split()[0]
                if url and len(url) > 10:
                    title = self._sanitize_text(lines[i + 1].strip() if i + 1 < len(lines) else "")
                    snippet = self._sanitize_text(lines[i + 2].strip() if i + 2 < len(lines) else "")
                    results.append(SearchResult(title=title, url=url, content=snippet, source=source))
            i += 1
        return results[:10]

    def _parse_github_results(self, content: str) -> list[SearchResult]:
        """Parse GitHub search results from text."""
        results = []
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "github.com" in line and line.startswith("http"):
                url = line.split()[0]
                title = self._sanitize_text(lines[i + 1].strip() if i + 1 < len(lines) else "")
                snippet = self._sanitize_text(lines[i + 2].strip() if i + 2 < len(lines) else "")
                if url and title:
                    results.append(SearchResult(title=title, url=url, content=snippet, source="github"))
        return results[:10]

    def _parse_generic_results(self, content: str, source: str) -> list[SearchResult]:
        """Generic text-based parser (fallback)."""
        results = []
        lines = content.strip().split("\n")
        current_url = current_title = current_content = ""
        for line in lines:
            if line.startswith("http") and len(line) > 20:
                if current_url and current_title:
                    results.append(SearchResult(title=self._sanitize_text(current_title), url=current_url, content=self._sanitize_text(current_content), source=source))
                current_url = line.split()[0]
                current_title = current_content = ""
            elif current_url and len(line.strip()) > 10:
                if not current_title:
                    current_title = line.strip()
                else:
                    current_content = line.strip()
        if current_url and current_title:
            results.append(SearchResult(title=self._sanitize_text(current_title), url=current_url, content=self._sanitize_text(current_content), source=source))
        return results

    def _clear_login_cache(self):
        """Clear the cached login state for this source."""
        LOGIN_STATE.pop(self.name, None)

    async def _attempt_login_recovery(self) -> bool:
        """Try to recover login by visiting the site homepage in a temp tab
        (may refresh session cookies) and rechecking.
        Returns True if login is detected after recovery, False otherwise."""
        conn = None
        try:
            from app.sources import cdp_client
            home_url = LOGIN_CHECKS.get(self.name, {}).get("url", "")
            if home_url:
                conn = await cdp_client.create_parallel_connection(self.name)
                await cdp_client.navigate(home_url, wait=3.0, cdp=conn[0])
            logged_in = await self.check_login(self.name)
            return logged_in is True
        except Exception as e:
            logger.error(f"{self.name}: login recovery error: {e}")
            return False
        finally:
            if conn is not None:
                from app.sources import cdp_client
                try:
                    await cdp_client.close_parallel_connection(conn)
                except Exception:
                    pass

    @staticmethod
    async def check_login(source_name: str) -> bool | None:
        """Check if a source is logged in. Returns True/False/None(unknown).
        Uses a fresh temp tab (login state is profile-wide via cookies), so
        the user's own tab is never navigated."""
        now = time.time()
        cached = LOGIN_STATE.get(source_name)
        if cached and now - cached["checked_at"] < LOGIN_CHECK_TTL:
            return cached["logged_in"]

        check = LOGIN_CHECKS.get(source_name)
        if not check:
            return True  # no login check defined, assume OK

        conn = None
        try:
            from app.sources import cdp_client
            conn = await cdp_client.create_parallel_connection(source_name)
            page = await cdp_client.navigate(check["url"], wait=3.0, cdp=conn[0])
            content = f"{page.get('title', '')}\n\n{page.get('text', '')}"

            logged_in_texts = check.get("logged_in_text", [])
            logged_out_texts = check.get("logged_out_text", [])

            logged_in = any(t in content for t in logged_in_texts)
            logged_out = any(t in content for t in logged_out_texts)

            if logged_in:
                status = True
            elif logged_out:
                status = False
            else:
                status = None
        except Exception:
            status = None
        finally:
            if conn is not None:
                from app.sources import cdp_client
                try:
                    await cdp_client.close_parallel_connection(conn)
                except Exception:
                    pass

        LOGIN_STATE[source_name] = {"logged_in": status, "checked_at": now}
        return status

    def health_check(self) -> dict:
        return {"available": True, "message": "Edge MCP source ready"}
