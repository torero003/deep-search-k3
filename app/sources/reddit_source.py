"""Reddit search — RSS + .json API, no authentication required.

Adapted from last30days reddit_keyless/reddit_rss modules.
RSS is the primary path (stable, no auth), .json API as bonus attempt.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx

from app.sources.base import BaseSource, SearchResult

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ATOM = "{http://www.w3.org/2005/Atom}"

FEED_TIMEOUT = 15

# ---------------------------------------------------------------------------
# RSS search
# ---------------------------------------------------------------------------


async def _fetch_rss(url: str) -> str:
    """Fetch an RSS feed, return XML text or empty string on failure."""
    try:
        async with httpx.AsyncClient(timeout=FEED_TIMEOUT) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        logger.debug(f"Reddit RSS fetch failed: {url[:60]}... {e}")
        return ""


def _parse_rss_feed(xml_text: str, query: str = "") -> list[dict]:
    """Parse Atom feed XML into normalized post dicts."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    posts = []
    for entry in root.iter(f"{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link")
        url = link_el.get("href", "").strip() if link_el is not None else ""
        if not url or "/comments/" not in url:
            continue

        title_el = entry.find(f"{ATOM}title")
        title = (title_el.text or "").strip() if title_el is not None else ""

        author = ""
        author_el = entry.find(f"{ATOM}author/{ATOM}name")
        if author_el is not None and author_el.text:
            author = author_el.text.strip().removeprefix("/u/").removeprefix("u/")
        if author in ("[deleted]", "[removed]", ""):
            author = "[deleted]"

        cat_el = entry.find(f"{ATOM}category")
        category = cat_el.get("term", "").strip() if cat_el is not None else ""
        subreddit = _subreddit_from(category, url)

        updated_el = entry.find(f"{ATOM}updated")
        updated = (updated_el.text or "").strip() if updated_el is not None else ""

        content_el = entry.find(f"{ATOM}content")
        selftext = ""
        if content_el is not None and content_el.text:
            selftext = re.sub(r"<[^>]+>", " ", content_el.text)
            selftext = re.sub(r"\s+", " ", selftext).strip()[:500]

        posts.append({
            "title": title,
            "url": url,
            "subreddit": subreddit,
            "author": author,
            "selftext": selftext,
            "date": _iso_to_date(updated),
            "relevance": _token_overlap(query, title),
        })

    return posts


def _subreddit_from(category: str, url: str) -> str:
    if category:
        return category
    parts = url.split("/r/", 1)
    if len(parts) == 2:
        return parts[1].split("/", 1)[0]
    return ""


def _iso_to_date(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.strip())
        return dt.date().isoformat()
    except (ValueError, TypeError):
        return ""


def _token_overlap(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    q_tokens = set(re.findall(r"\b\w+\b", query.lower()))
    t_tokens = set(re.findall(r"\b\w+\b", text.lower()))
    if not q_tokens or not t_tokens:
        return 0.0
    return len(q_tokens & t_tokens) / len(q_tokens)


async def _search_rss(query: str) -> list[dict]:
    """Search Reddit via RSS feeds — global search + top subreddits."""
    q = quote_plus(query)

    # Global search RSS
    urls = [f"https://www.reddit.com/search.rss?q={q}&sort=relevance&t=year"]

    # Top subreddits relevant to the query
    top_subs = ["all", "technology", "programming", "science", "worldnews", "business", "gaming"]
    for sub in top_subs:
        urls.append(f"https://www.reddit.com/r/{sub}/search.rss?q={q}&sort=relevance&t=year")

    # Fetch all feeds in parallel
    tasks = [_fetch_rss(url) for url in urls]
    feeds = await asyncio.gather(*tasks, return_exceptions=True)

    all_posts = []
    for feed in feeds:
        if isinstance(feed, Exception):
            continue
        if isinstance(feed, str):
            posts = _parse_rss_feed(feed, query)
            all_posts.extend(posts)

    # Dedup by URL
    seen = set()
    unique = []
    for p in all_posts:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)

    # Sort by relevance then date
    unique.sort(key=lambda p: (-p["relevance"], p["date"] or ""))
    return unique


# ---------------------------------------------------------------------------
# .json API search (bonus, works on residential IPs)
# ---------------------------------------------------------------------------


async def _search_json(query: str) -> list[dict]:
    """Search Reddit via public .json API — may 403 on datacenter IPs."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://www.reddit.com/search.json?q={quote_plus(query)}&sort=relevance&t=month&limit=25",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        children = data.get("data", {}).get("children", [])
        posts = []
        for child in children:
            if child.get("kind") != "t3":
                continue
            post = child.get("data", {})
            permalink = str(post.get("permalink", "")).strip()
            if "/comments/" not in permalink:
                continue

            score = int(post.get("score", 0) or 0)
            num_comments = int(post.get("num_comments", 0) or 0)
            selftext = str(post.get("selftext", ""))
            author = str(post.get("author", "[deleted]"))
            subreddit = str(post.get("subreddit", ""))
            title = str(post.get("title", ""))
            url = str(post.get("url", ""))
            if not url or url.startswith("/r/"):
                url = f"https://www.reddit.com{permalink}"

            created_utc = post.get("created_utc")
            date_str = ""
            if created_utc:
                try:
                    date_str = datetime.fromtimestamp(created_utc, tz=timezone.utc).date().isoformat()
                except (ValueError, TypeError, OSError):
                    pass

            posts.append({
                "title": title,
                "url": url,
                "subreddit": subreddit,
                "author": author,
                "selftext": selftext[:500],
                "date": date_str,
                "score": score,
                "num_comments": num_comments,
                "relevance": _token_overlap(query, title),
            })
        return posts
    except Exception as e:
        logger.debug(f"Reddit .json search failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_reddit(query: str, max_results: int = 15) -> list[SearchResult]:
    """Search Reddit via RSS + .json API, merge and dedup results."""
    # Run both paths in parallel
    rss_posts, json_posts = await asyncio.gather(
        _search_rss(query),
        _search_json(query),
        return_exceptions=True,
    )
    if isinstance(rss_posts, Exception):
        rss_posts = []
    if isinstance(json_posts, Exception):
        json_posts = []

    # Merge: .json results first (have engagement data), then RSS
    seen = set()
    merged = []
    for p in json_posts + rss_posts:
        if p["url"] not in seen:
            seen.add(p["url"])
            merged.append(p)
        if len(merged) >= max_results * 2:
            break

    # Sort by relevance, then score (if available), then date
    merged.sort(key=lambda p: (-p.get("relevance", 0), -p.get("score", 0), p.get("date", "") or ""))

    results = []
    for p in merged[:max_results]:
        content = f"{p.get('selftext', '')[:400]}".strip()
        results.append(SearchResult(
            title=p["title"],
            url=p["url"],
            content=content,
            source="reddit",
            score=p.get("score", 0),
            published_date=p.get("date", ""),
            engagement={
                "upvotes": p.get("score", 0),
                "comments": p.get("num_comments", 0),
            } if p.get("score") else {},
        ))

    logger.info(f"reddit: {len(results)} results (RSS={len(rss_posts)}, JSON={len(json_posts)})")
    return results


class RedditSource(BaseSource):
    async def search(self, query: str, max_results: int = 15) -> list[SearchResult]:
        return await search_reddit(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "Reddit RSS + API ready"}
