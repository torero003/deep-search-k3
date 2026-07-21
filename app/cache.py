"""Search result cache with per-source TTL.

Stores raw search results per (query, source) in SQLite.
Cache key: normalized query + source name.
TTL: search engines 5 min, community 10 min, investment 15 min.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from app.config import DATA_DIR
from app.storage.sqlite_store import get_connection

logger = logging.getLogger(__name__)

CACHE_DB_PATH = str(DATA_DIR / "cache.db") if hasattr(DATA_DIR, '__truediv__') else str(DATA_DIR) + "/cache.db"

# TTL in seconds per source type
TTL_CONFIG = {
    # Search engines: fast-changing, short TTL
    "google": 300,
    "bing": 300,
    "yandex": 300,
    # Community: moderate TTL
    "zhihu": 600,
    "v2ex": 600,
    "twitter": 300,
    # Investment: prices change but not every minute
    "trendforce": 900,
    "eastmoney": 300,
    "xueqiu": 600,
    # Others
    "github": 600,
    "sogou_wechat": 600,
    "stats_gov": 900,
    # API sources
    "hacker_news": 300,
    "github_trending": 600,
    "rsshub": 300,
    "yahoo_finance": 300,
    "coingecko": 300,
    "binance": 120,
    "fear_greed": 3600,
    "world_bank": 3600,
    "sec_edgar": 600,
    "cninfo": 300,
    "sina_finance": 60,
    "sogou_wechat": 300,
}
DEFAULT_TTL = 300


def _init_cache_db():
    """Create cache table if not exists."""
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            ttl INTEGER NOT NULL DEFAULT 300,
            created_at REAL NOT NULL,
            results_json TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON search_cache(created_at)")
    conn.commit()
    conn.close()


def _make_cache_key(query: str, source: str) -> str:
    """Deterministic key from query + source."""
    raw = f"{query.lower().strip()}|{source}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_cached_results(query: str, source: str) -> list[dict] | None:
    """Return cached results if fresh, else None."""
    try:
        conn = sqlite3.connect(CACHE_DB_PATH)
        conn.row_factory = sqlite3.Row
        ttl = TTL_CONFIG.get(source, DEFAULT_TTL)
        key = _make_cache_key(query, source)
        now = time.time()

        row = conn.execute(
            "SELECT results_json, created_at, ttl FROM search_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()

        if row is None:
            conn.close()
            return None

        age = now - row["created_at"]
        if age > row["ttl"]:
            # Stale — delete and return None
            conn.execute("DELETE FROM search_cache WHERE cache_key = ?", (key,))
            conn.commit()
            conn.close()
            return None

        results = json.loads(row["results_json"])
        conn.close()
        return results

    except Exception as e:
        logger.warning(f"Cache read error: {e}")
        return None


def _sanitize_text(obj):
    """Remove surrogate pairs that can't be encoded to UTF-8."""
    if isinstance(obj, str):
        return obj.encode('utf-8', 'ignore').decode('utf-8')
    if isinstance(obj, dict):
        return {k: _sanitize_text(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_text(v) for v in obj]
    return obj


def put_cached_results(query: str, source: str, results: list[dict]):
    """Store results in cache with source-specific TTL."""
    try:
        _init_cache_db()
        conn = sqlite3.connect(CACHE_DB_PATH)
        ttl = TTL_CONFIG.get(source, DEFAULT_TTL)
        key = _make_cache_key(query, source)
        clean = _sanitize_text(results)
        conn.execute(
            "INSERT OR REPLACE INTO search_cache (cache_key, source, ttl, created_at, results_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, source, ttl, time.time(), json.dumps(clean, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Cache write error: {e}")


def cleanup_stale():
    """Delete all expired entries. Call periodically."""
    try:
        _init_cache_db()
        conn = sqlite3.connect(CACHE_DB_PATH)
        now = time.time()
        deleted = conn.execute(
            "DELETE FROM search_cache WHERE created_at + ttl < ?",
            (now,),
        ).rowcount
        conn.commit()
        conn.close()
        if deleted:
            logger.info(f"Cache cleanup: removed {deleted} stale entries")
    except Exception as e:
        logger.warning(f"Cache cleanup error: {e}")


def get_cache_stats() -> dict:
    """Return cache size, hit rate info."""
    try:
        _init_cache_db()
        conn = sqlite3.connect(CACHE_DB_PATH)
        total = conn.execute("SELECT COUNT(*) as c FROM search_cache").fetchone()["c"]
        now = time.time()
        fresh = conn.execute(
            "SELECT COUNT(*) as c FROM search_cache WHERE created_at + ttl > ?",
            (now,),
        ).fetchone()["c"]
        conn.close()
        return {"total_entries": total, "fresh_entries": fresh, "stale_entries": total - fresh}
    except Exception:
        return {"total_entries": 0, "fresh_entries": 0, "stale_entries": 0}
