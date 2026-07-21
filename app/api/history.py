import asyncio
from fastapi import APIRouter, Query
from app.storage.sqlite_store import query_timeseries, search_results_fts

router = APIRouter(prefix="/history", tags=["history"])


@router.get("")
async def history(
    category: str = Query(..., description="Category: DRAM, NAND, HBM, etc."),
    metric: str = Query(..., description="Metric name"),
    from_date: str = Query("", description="ISO date string"),
    to_date: str = Query("", description="ISO date string"),
):
    """Get time-series data for a specific metric."""
    data = await query_timeseries(category, metric, from_date, to_date)
    return {"category": category, "metric": metric, "data": data}


@router.get("/timeseries")
async def timeseries(
    category: str = Query(..., description="Category"),
    limit: int = Query(50, ge=1, le=500),
):
    """Get all time-series data for a category."""
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _get_timeseries_sync, category, limit)
    return {"category": category, "data": rows}


def _get_timeseries_sync(category, limit):
    from app.storage.sqlite_store import get_connection
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM timeseries_data WHERE category = ? ORDER BY extracted_at DESC LIMIT ?",
            (category, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@router.get("/search")
async def search_history(
    q: str = Query(..., min_length=1, description="Search term"),
    limit: int = Query(50, ge=1, le=500, description="Max results"),
):
    """Full-text search past search results by keyword."""
    data = await search_results_fts(q, limit)
    return {"query": q, "total": len(data), "data": data}
