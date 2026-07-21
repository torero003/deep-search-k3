from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.api import search, query, history, status
from app.storage.sqlite_store import init_db
from app.cache import cleanup_stale, get_cache_stats

# Force UTF-8 safe logging (Windows console defaults to GBK)
import logging
class UTF8SafeFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = record.msg.encode('utf-8', 'ignore').decode('utf-8', 'ignore')
        return True
logging.root.addFilter(UTF8SafeFilter())
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')

app = FastAPI(title="本地智能搜索平台", version="0.1.0")


def _sanitize(obj):
    """Recursively remove surrogate pairs from strings in dicts/lists."""
    if isinstance(obj, str):
        return obj.encode('utf-8', 'ignore').decode('utf-8', errors='ignore')
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


@app.middleware("http")
async def sanitize_response(request, call_next):
    """Strip surrogate pairs from JSON responses to prevent encoding errors.

    Surrogates (U+D800 to U+DFFF) can't be encoded to UTF-8 per the Unicode spec.
    Python's json.dumps() outputs them as UTF-16 encoded characters, which creates
    invalid JSON. This middleware strips them at the byte level.
    """
    response = await call_next(request)
    if response.__class__ is JSONResponse:
        try:
            import re
            # UTF-8 encoding of U+D800-U+DFFF: \xED[\xA0-\xBF][\x80-\xBF]
            response.body = re.sub(b'\xED[\xA0-\xBF][\x80-\xBF]', b'', response.body)
        except Exception:
            pass
    return response

app.include_router(search.router)
app.include_router(query.router)
app.include_router(history.router)
app.include_router(status.router)


@app.on_event("startup")
def startup():
    init_db()
    cleanup_stale()


@app.get("/health")
def health():
    return {"status": "ok", "cache": get_cache_stats()}
