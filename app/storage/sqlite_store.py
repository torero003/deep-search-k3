import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from app.config import DATA_DIR
from app.storage.schema import SCHEMA

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "search_platform.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_connection()
    conn.executescript(SCHEMA)
    try:
        conn.execute("SELECT count(*) FROM search_results").fetchone()
        # Clean duplicates before adding UNIQUE index
        rows = conn.execute(
            "SELECT query, source, url, MIN(id) as min_id "
            "FROM search_results GROUP BY query, source, url HAVING count(*) > 1"
        ).fetchall()
        for row in rows:
            conn.execute(
                "DELETE FROM search_results WHERE query=? AND source=? AND url=? AND id != ?",
                (row["query"], row["source"], row["url"], row["min_id"]),
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_search_dedup ON search_results(query, source, url)")
        logger.info(f"init_db: cleaned {len(rows)} duplicate rows, added UNIQUE index")
    except sqlite3.OperationalError as e:
        logger.warning(f"init_db: index creation skipped (may already exist): {e}")
    except Exception as e:
        logger.warning(f"init_db: unexpected error during dedup: {e}")
    conn.commit()
    conn.close()


def _save_search_result_sync(query, source, url, title, content, score, category):
    """Sync helper for async wrapper."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO search_results (query, source, url, title, content, score, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (query, source, url, title, content, score, category),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # dedup by UNIQUE(query, source, url)
    finally:
        conn.close()


async def save_search_result(query: str, source: str, url: str, title: str,
                             content: str, score: float, category: str):
    """Async wrapper to avoid blocking event loop."""
    await asyncio.to_thread(
        _save_search_result_sync, query, source, url, title, content, score, category
    )


def _save_timeseries_sync(category, metric, value, value_text, unit, period,
                          source, source_url, raw_text):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO timeseries_data "
            "(category, metric, value, value_text, unit, period, source, source_url, raw_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (category, metric, value, value_text, unit, period, source, source_url, raw_text),
        )
        conn.commit()
    finally:
        conn.close()


async def save_timeseries(category: str, metric: str, value: float | None,
                          value_text: str, unit: str, period: str,
                          source: str, source_url: str, raw_text: str):
    await asyncio.to_thread(
        _save_timeseries_sync, category, metric, value, value_text, unit, period,
        source, source_url, raw_text
    )


def _query_timeseries_sync(category, metric, from_date, to_date):
    conn = get_connection()
    sql = "SELECT * FROM timeseries_data WHERE category = ? AND metric = ?"
    params = [category, metric]
    if from_date:
        sql += " AND extracted_at >= ?"
        params.append(from_date)
    if to_date:
        sql += " AND extracted_at <= ?"
        params.append(to_date)
    sql += " ORDER BY extracted_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def query_timeseries(category: str, metric: str, from_date: str = "",
                           to_date: str = "") -> list[dict]:
    return await asyncio.to_thread(_query_timeseries_sync, category, metric, from_date, to_date)


def _update_source_health_sync(source, success, response_time_ms):
    conn = get_connection()
    now = datetime.now().isoformat()
    try:
        if success:
            conn.execute(
                "INSERT INTO source_health (source, last_success, last_failure, failure_count, avg_response_time_ms) "
                "VALUES (?, ?, NULL, 0, ?) ON CONFLICT(source) DO UPDATE SET "
                "last_success=excluded.last_success, last_failure=NULL, failure_count=0, "
                "avg_response_time_ms=excluded.avg_response_time_ms",
                (source, now, response_time_ms),
            )
        else:
            conn.execute(
                "INSERT INTO source_health (source, last_failure, failure_count) "
                "VALUES (?, ?, 1) ON CONFLICT(source) DO UPDATE SET "
                "last_failure=excluded.last_failure, failure_count=failure_count+1",
                (source, now),
            )
        conn.commit()
    finally:
        conn.close()


async def update_source_health(source: str, success: bool, response_time_ms: int = 0):
    await asyncio.to_thread(_update_source_health_sync, source, success, response_time_ms)


def _get_source_health_sync():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def get_source_health() -> list[dict]:
    return await asyncio.to_thread(_get_source_health_sync)


def _search_results_fts_sync(search_term, limit):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM search_results_fts WHERE search_results_fts MATCH ? LIMIT ?",
            (search_term, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


async def search_results_fts(search_term: str, limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(_search_results_fts_sync, search_term, limit)
