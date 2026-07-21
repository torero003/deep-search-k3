"""LLM client helper — handles UD model reasoning_content + content."""
import asyncio
import logging
import requests
import re
import json as _json

from app.config import LLM_URL

logger = logging.getLogger(__name__)
MODEL = "Qwen3.6-27B-UD-Q6_K_XL.gguf"
DEFAULT_TIMEOUT = 600  # UD model needs time for reasoning + answer


def _post_llm(messages, max_tokens, temperature, stream=False):
    """POST to LLM API with status code check. Returns response."""
    resp = requests.post(
        f"{LLM_URL}/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.error(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
        raise Exception(f"LLM returned HTTP {resp.status_code}")
    return resp


def _parse_llm_response(resp, include_reasoning: bool = False) -> str:
    """Parse LLM response, return content string."""
    data = resp.json()
    msg = data["choices"][0]["message"]
    content = (msg.get("content", "") or "").strip()
    reasoning = (msg.get("reasoning_content", "") or "").strip()
    if include_reasoning and reasoning and content:
        return f"[思考过程]\n{reasoning}\n\n[回答]\n{content}"
    return content or reasoning


def chat(messages: list[dict], max_tokens: int = 3000, temperature: float = 0.3, include_reasoning: bool = False) -> str:
    """Call LLM and return content."""
    try:
        resp = _post_llm(messages, max_tokens, temperature)
        return _parse_llm_response(resp, include_reasoning)
    except Exception as e:
        logger.error(f"chat() failed: {e}")
        return f"(LLM 调用失败: {str(e)})"


async def achat(messages: list[dict], max_tokens: int = 3000, temperature: float = 0.3, include_reasoning: bool = False) -> str:
    """Async version of chat — runs in executor to not block event loop."""
    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, lambda: _post_llm(messages, max_tokens, temperature))
        return _parse_llm_response(resp, include_reasoning)
    except Exception as e:
        logger.error(f"achat() failed: {e}")
        return f"(LLM 调用失败: {str(e)})"


def chat_json(messages: list[dict], max_tokens: int = 2000) -> list[dict] | dict | None:
    """Call LLM and parse JSON from response."""
    try:
        resp = _post_llm(messages, max_tokens, 0.1)
        return _parse_llm_json(resp)
    except Exception as e:
        logger.error(f"chat_json() failed: {e}")
        return None


async def achat_json(messages: list[dict], max_tokens: int = 2000) -> list[dict] | dict | None:
    """Async version of chat_json."""
    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, lambda: _post_llm(messages, max_tokens, 0.1))
        return _parse_llm_json(resp)
    except Exception as e:
        logger.error(f"achat_json() failed: {e}")
        return None


def _parse_llm_json(resp) -> list[dict] | dict | None:
    """Parse JSON from LLM response. Uses non-greedy regex to find the first valid JSON."""
    data = resp.json()
    msg = data["choices"][0]["message"]
    for field in ("reasoning_content", "content"):
        text = msg.get(field, "") or ""
        # Find all [...] blocks and try each one (non-greedy)
        for m in re.finditer(r'\[[^\]]*(?:\[[^\]]*\][^\]]*)*\]', text):
            try:
                parsed = _json.loads(m.group())
                if isinstance(parsed, (list, dict)):
                    return parsed
            except _json.JSONDecodeError:
                continue
        # Fallback: find all {...} blocks
        for m in re.finditer(r'\{[^}]*(?:\{[^}]*\}[^}]*)*\}', text):
            try:
                parsed = _json.loads(m.group())
                if isinstance(parsed, (list, dict)):
                    return parsed
            except _json.JSONDecodeError:
                continue
    return None
