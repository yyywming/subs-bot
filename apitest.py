"""OpenAI 兼容接口检测：解析「地址 + Key」、拉取模型列表、用一个模型实际调用一次 chat/completions。

与 Telegram 无关的纯逻辑放在这里，bot.py 只负责收消息和渲染，便于离线测试。
安全约束：
- 每个请求前调用方传入的 check_url（即 convert._check_public_url）校验公网地址，
  共享 session 的 PublicResolver 在建连时再拦一次内网地址；
- 一律禁止跟随重定向，响应体按上限读取，避免把服务端变成 SSRF 代理或被超大响应拖垮。
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import aiohttp

# Key 只允许常见字符（sk-xxx、base64、JWT 等），长度 16~512；含 :// 的是分享链接/URL，不当作 Key
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_\-.=+/:~]{16,512}$")
_URL_RE = re.compile(r"^https?://\S+$", re.I)
_VERSION_SUFFIX_RE = re.compile(r"/v\d+[a-z0-9]*$", re.I)

MAX_MODELS_BYTES = 1024 * 1024
MAX_CHAT_BYTES = 64 * 1024
MAX_API_PAIRS = 10

# 不适合用 chat/completions 测试的模型
_NON_CHAT_HINTS = (
    "embed", "embedding", "tts", "whisper", "dall-e", "dalle", "image", "moderation",
    "rerank", "audio", "transcribe", "speech", "midjourney", "mj_", "suno", "video", "sora",
)
# 优先挑便宜/快速的模型做测试
_PREFERRED_HINTS = ("mini", "flash", "haiku", "lite", "turbo", "nano", "small", "chat")


def is_api_key_like(token: str) -> bool:
    token = (token or "").strip()
    return bool(token) and "://" not in token and bool(_API_KEY_RE.fullmatch(token))


def _is_url(token: str) -> bool:
    if not _URL_RE.fullmatch(token or ""):
        return False
    try:
        return bool(urlparse(token).hostname)
    except ValueError:
        return False


def parse_api_pairs(text: str) -> tuple[list[tuple[str, str]], int]:
    """把文本解析成 [(地址, Key)]，返回 (pairs, 无法配对的片段数)。

    支持：同一行「地址 Key」「地址|Key」，或地址、Key 分两行；多组可连续书写。
    """
    tokens = [t for t in re.split(r"[\s|，,]+", (text or "").strip()) if t]
    pairs: list[tuple[str, str]] = []
    stray = 0
    i = 0
    while i < len(tokens):
        tok = tokens[i].rstrip("。；;")
        if _is_url(tok) and i + 1 < len(tokens) and is_api_key_like(tokens[i + 1]) and not _is_url(tokens[i + 1]):
            pairs.append((tok, tokens[i + 1]))
            i += 2
            continue
        stray += 1
        i += 1
    return pairs, stray


def looks_like_api_test(text: str) -> bool:
    """未进入 /ai 模式时，判断一条消息是不是「地址 + Key」。

    要求整条消息恰好由若干组「地址 Key」组成，没有多余内容；
    纯订阅链接、「名称|链接」、带说明文字的链接都不会被误判。
    """
    pairs, stray = parse_api_pairs(text)
    return bool(pairs) and stray == 0 and len(pairs) <= MAX_API_PAIRS


def normalize_api_base(url: str) -> str:
    """统一成以 /v1（或其它版本号）结尾的 base；去掉用户误贴的 /models、/chat/completions。"""
    url = (url or "").strip().rstrip("/")
    lowered = url.lower()
    for suffix in ("/chat/completions", "/completions", "/models"):
        if lowered.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            lowered = url.lower()
    path = urlparse(url).path.rstrip("/")
    if not _VERSION_SUFFIX_RE.search(path):
        url = url + "/v1"
    return url


def mask_key(key: str) -> str:
    key = key or ""
    if len(key) <= 12:
        return key[:2] + "****"
    return f"{key[:6]}****{key[-4:]}"


def _extract_error(payload: Any, text: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or err.get("code") or err.get("type")
            if msg:
                return str(msg)
        elif isinstance(err, str) and err:
            return err
        for key in ("message", "msg", "detail", "error_msg"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, (list, dict)) and val:
                return json.dumps(val, ensure_ascii=False)[:200]
    body = (text or "").strip()
    if body.lower().startswith(("<!doctype", "<html")) or "<html" in body[:200].lower():
        return "返回了网页而不是 JSON（地址可能不是 API 接口，或被 WAF/CF 拦截）"
    body = re.sub(r"\s+", " ", body)
    return body[:200] or "无返回内容"


def _model_ids(payload: Any) -> list[str]:
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models")
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
        else:
            mid = item
        if isinstance(mid, str) and mid.strip():
            ids.append(mid.strip())
    return list(dict.fromkeys(ids))


def pick_test_model(models: list[str], preferred: str | None = None) -> str | None:
    if preferred:
        return preferred
    chat_models = [m for m in models if not any(h in m.lower() for h in _NON_CHAT_HINTS)]
    pool = chat_models or models
    if not pool:
        return None
    for hint in _PREFERRED_HINTS:
        for m in pool:
            if hint in m.lower():
                return m
    return pool[0]


async def _read_limited(resp: aiohttp.ClientResponse, limit: int) -> tuple[str, bool]:
    body = bytearray()
    truncated = False
    while True:
        chunk = await resp.content.read(min(64 * 1024, limit + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > limit:
            truncated = True
            del body[limit:]
            break
    try:
        return bytes(body).decode(resp.charset or "utf-8", "replace"), truncated
    except LookupError:
        return bytes(body).decode("utf-8", "replace"), truncated


async def _request_json(
    session: Any, method: str, url: str, *, headers: dict[str, str], limit: int,
    timeout: float, json_body: Any = None,
) -> tuple[int, Any, str]:
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": aiohttp.ClientTimeout(total=timeout),
        "allow_redirects": False,
    }
    if json_body is not None:
        kwargs["json"] = json_body
    async with session.request(method, url, **kwargs) as resp:
        status = resp.status
        text, _ = await _read_limited(resp, limit)
    try:
        payload = json.loads(text) if text.strip() else None
    except ValueError:
        payload = None
    return status, payload, text


def _describe_exc(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "请求超时"
    if isinstance(exc, ValueError):
        return str(exc) or "地址无效"
    if isinstance(exc, aiohttp.ClientConnectorError):
        return f"连接失败（{type(exc).__name__}）"
    if isinstance(exc, aiohttp.ClientError):
        return f"请求失败（{type(exc).__name__}）"
    return f"{type(exc).__name__}: {exc}"[:200]


async def probe_api(
    api_url: str,
    key: str,
    *,
    get_session: Callable[[], Awaitable[Any]],
    check_url: Callable[[str], Awaitable[None]],
    model: str | None = None,
    models_timeout: float = 15,
    chat_timeout: float = 30,
) -> dict[str, Any]:
    """检测一组地址 + Key。不抛异常，失败信息放在返回值里。"""
    base = normalize_api_base(api_url)
    result: dict[str, Any] = {
        "base": base, "key": key, "models": [], "model": None,
        "ok": False, "message": "", "status": None, "elapsed": None, "stage": "models",
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "subs-bot-api-check/1.0",
    }
    try:
        session = await get_session()
        models_url = base + "/models"
        await check_url(models_url)
        status, payload, text = await _request_json(
            session, "GET", models_url, headers=headers, limit=MAX_MODELS_BYTES, timeout=models_timeout,
        )
        result["status"] = status
        if 300 <= status < 400:
            result["message"] = f"接口返回重定向，请填写最终 API 地址（HTTP {status}）"
            return result
        if status != 200:
            result["message"] = f"{_extract_error(payload, text)}（HTTP {status}）"
            return result
        models = _model_ids(payload)
        result["models"] = models
        test_model = pick_test_model(models, model)
        result["model"] = test_model
        if not test_model:
            result["message"] = "接口可访问，但没有返回任何模型"
            return result

        result["stage"] = "chat"
        chat_url = base + "/chat/completions"
        await check_url(chat_url)
        body = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Reply with: ok"}],
            "max_tokens": 16,
            "stream": False,
        }
        t0 = time.monotonic()
        status, payload, text = await _request_json(
            session, "POST", chat_url, headers={**headers, "Content-Type": "application/json"},
            limit=MAX_CHAT_BYTES, timeout=chat_timeout, json_body=body,
        )
        result["elapsed"] = time.monotonic() - t0
        result["status"] = status
        if status != 200:
            result["message"] = f"{_extract_error(payload, text)}（HTTP {status}）"
            return result
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            result["message"] = f"返回格式异常：{_extract_error(payload, text)}"
            return result
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content") or first.get("text") or ""
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        content = re.sub(r"\s+", " ", str(content)).strip()
        result["ok"] = True
        result["message"] = content[:120] if content else "调用成功（返回内容为空）"
        return result
    except Exception as exc:  # noqa: BLE001 — 检测结果要展示给用户，不能把异常抛出去
        result["message"] = _describe_exc(exc)
        return result


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def format_probe_result(result: dict[str, Any], *, show_full_key: bool = True, budget: int = 3800) -> str:
    """渲染成 Telegram HTML：地址/Key + 测试摘要引用块 + 可展开的模型列表。"""
    esc = html.escape
    models: list[str] = list(result.get("models") or [])
    key = str(result.get("key") or "")
    key_text = key if show_full_key else mask_key(key)
    status_line = "✅ 成功" if result.get("ok") else "❌ 失败"
    lines = [
        f"模型数量: {len(models)}",
        f"测试模型: {esc(str(result.get('model') or '未知模型'))}",
        f"测试结果: {status_line}",
    ]
    if result.get("elapsed") is not None:
        lines.append(f"响应耗时: {float(result['elapsed']):.2f}s")
    lines.append(f"返回消息: {esc(str(result.get('message') or '无'))}")

    head = (
        f"API 地址: <code>{esc(str(result.get('base') or ''))}</code>\n"
        f"API Key: <code>{esc(key_text)}</code>\n"
        f"<blockquote>{chr(10).join(lines)}</blockquote>\n"
    )
    title = f"📡 模型列表（共{len(models)}个）："
    remaining = budget - _utf16_len(head) - _utf16_len(title) - 64
    shown: list[str] = []
    for mid in models:
        line = f"<code>{esc(mid)}</code>"
        cost = _utf16_len(line) + 1
        if cost > remaining:
            break
        shown.append(line)
        remaining -= cost
    if len(shown) < len(models):
        shown.append(f"<i>…其余 {len(models) - len(shown)} 个未展示</i>")
    body = title + ("\n" + "\n".join(shown) if shown else "")
    return head + f"<blockquote expandable>{body}</blockquote>"
