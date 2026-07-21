from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from app.sources.edge_mcp_source import EdgeMCPSource, LOGIN_STATE, LOGIN_CHECKS
from app.sources.cdp_client import navigate_to_login, LOGIN_URLS
from app.storage.sqlite_store import get_source_health

router = APIRouter(prefix="/status", tags=["status"])


class LoginCheckRequest(BaseModel):
    sources: list[str]


class LoginCheckResponse(BaseModel):
    source: str
    logged_in: Optional[bool] = None
    message: str = ""


@router.get("/sources")
async def get_sources():
    """Health status of all sources."""
    health = await get_source_health()
    return {"sources": health}


@router.get("/login")
async def get_login_status(source_name: str = None):
    """Login status of all login-required sources.

    If source_name is provided, navigates to that source's login page
    and checks the login state. Useful for re-login flow:
    1. GET /status/login?source_name=zhihu  → navigates to zhihu login
    2. User logs in through the browser
    3. GET /status/login  → checks all source statuses
    """
    if source_name:
        # Single source login flow
        if source_name not in LOGIN_URLS:
            return {"error": f"No login URL configured for {source_name}. Available: {list(LOGIN_URLS.keys())}"}

        result = await navigate_to_login(source_name)
        logged_in = await EdgeMCPSource.check_login(source_name)
        result["logged_in"] = logged_in
        if logged_in is True:
            result["message"] = f"{source_name} 已登录，可以直接搜索"
        else:
            result["message"] = f"请在浏览器中登录 {source_name}，登录完成后 GET /status/login 检查状态"
        return result

    # All sources status
    status = {}
    for source, state in LOGIN_STATE.items():
        logged_in = state.get("logged_in")
        status[source] = {
            "logged_in": logged_in,
            "checked_at": state.get("checked_at", 0),
            "status": "logged_in" if logged_in else ("logged_out" if logged_in is False else "unknown"),
        }
    return {"login_status": status}


@router.post("/login/check")
async def check_login(req: LoginCheckRequest) -> list[LoginCheckResponse]:
    """Manually check login status for specified sources."""
    results = []
    for source in req.sources:
        check = LOGIN_CHECKS.get(source)
        if not check:
            results.append(LoginCheckResponse(
                source=source, logged_in=True, message=f"{source} 无需登录检查"
            ))
            continue

        logged_in = await EdgeMCPSource.check_login(source)
        if logged_in is True:
            results.append(LoginCheckResponse(source=source, logged_in=True, message=f"{source} 已登录"))
        elif logged_in is False:
            results.append(LoginCheckResponse(
                source=source, logged_in=False,
                message=f"{source} 未登录，请在 Edge 浏览器中访问 {check.get('url', '')}/login 登录"
            ))
        else:
            results.append(LoginCheckResponse(
                source=source, logged_in=None,
                message=f"{source} 登录状态无法确定"
            ))
    return results


@router.get("/login/auto")
async def auto_login(source_name: str, timeout: int = 60):
    """Attempt automatic login recovery: navigate to login page, poll for login.
    ?source_name=zhihu&timeout=60
    Returns {success: bool, logged_in: bool, waited: float, message: str}.
    """
    import asyncio
    import time

    if source_name not in LOGIN_URLS:
        return {"error": f"No login URL configured for {source_name}. Available: {list(LOGIN_URLS.keys())}"}

    logged_in = await EdgeMCPSource.check_login(source_name)
    if logged_in is True:
        return {"success": True, "logged_in": True, "waited": 0, "message": f"{source_name} 已登录"}

    await navigate_to_login(source_name)

    start = time.time()
    timeout = min(timeout, 300)
    while time.time() - start < timeout:
        await asyncio.sleep(3)
        logged_in = await EdgeMCPSource.check_login(source_name)
        if logged_in is True:
            return {
                "success": True,
                "logged_in": True,
                "waited": round(time.time() - start, 1),
                "message": f"{source_name} 登录成功，耗时 {time.time() - start:.1f}s",
            }

    elapsed = round(time.time() - start, 1)
    return {
        "success": False,
        "logged_in": False,
        "waited": elapsed,
        "message": f"等待 {elapsed}s 后仍未检测到登录，请手动在浏览器中登录 {source_name}",
        "login_url": LOGIN_URLS.get(source_name),
    }


@router.get("")
def status():
    """Overall platform status."""
    return {
        "platform": "running",
        "edge_mcp": {"available": True, "message": "Edge MCP browser source"},
        "login_state": {s: v.get("logged_in") for s, v in LOGIN_STATE.items()},
    }
