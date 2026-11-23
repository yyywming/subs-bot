from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import signal
import time
from contextlib import suppress
from typing import Any
from urllib.parse import quote, quote_plus, urlparse

import aiohttp
from aiohttp import web
from telegram import (
    BotCommand,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    LinkPreviewOptions,
    ReplyKeyboardRemove,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import config
from config import (
    ALERT_EXPIRE_DAYS,
    ALERT_TRAFFIC_GB,
    AUTO_REFRESH_HOURS,
    BOT_TOKEN,
    BOT_USERNAME,
    GITHUB_TOKEN,
    HTTP_HOST,
    HTTP_PORT,
    MAX_DOCUMENT_BYTES,
    MAX_IMPORTED_NODES,
    MAX_IMPORTED_SUBSCRIPTIONS,
    PUBLIC_BASE_URL,
    SHORTLINK_API_URL,
    SHORTLINK_DOMAIN,
    SHORTLINK_TOKEN,
    UPDATE_CONCURRENCY,
    parse_tg_user_id,
)
from convert import (
    apply_path_maps,
    close_session,
    decode_subscription_bytes,
    deduplicate_nodes,
    extract_share_links,
    fetch_subscription,
    format_bytes_gb,
    format_expire,
    format_subscription_userinfo,
    get_session,
    node_name_from_url,
    node_to_share_link,
    parse_nodes_from_text,
    parse_share_link,
    remain_text,
    to_base64_sub,
    to_clash,
    to_qx,
    to_share_links,
    to_singbox,
    to_surge,
    _check_public_url,
)
from db import Store, nodes_of
from apitest import (
    MAX_API_PAIRS,
    format_probe_result,
    looks_like_api_test,
    parse_api_pairs,
    parse_api_specs,
    probe_api,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx/httpcore 在 INFO 级会把每次请求的完整 URL 打进日志，而 Telegram 的 URL
# 里嵌着 bot token —— 长轮询每 10 秒一次，等于把凭据反复写进 journal（实测 24h
# 8440 行、journal 涨到 249MB）。降到 WARNING：出错照样记，正常请求不再刷。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("subs-bot")

store = Store()

CURRENT_BOT_USERNAME = str(BOT_USERNAME or "").lstrip("@")


def get_bot_username(bot: Any = None) -> str:
    global CURRENT_BOT_USERNAME
    if bot and getattr(bot, "username", None):
        CURRENT_BOT_USERNAME = str(bot.username).lstrip("@")
    return CURRENT_BOT_USERNAME or "bot"


ALLOWED_DOCUMENT_SUFFIXES = frozenset({".txt", ".log", ".yaml", ".yml", ".json"})


def _document_suffix(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


# Telegram 硬限制
TG_MESSAGE_LIMIT = 4096
TG_CALLBACK_ALERT_LIMIT = 200
TG_COPY_TEXT_LIMIT = 256
# 正文预算：给标题/按钮说明等固定文案留出余量
TG_SAFE_BODY = 3800

_URL_TRAILING_PUNCT = ".,;:!?)]}>\"'，。；：！？）】》」』、"


def _utf16_len(text: str) -> int:
    """Telegram 按 UTF-16 码元计长度；emoji 等补充平面字符算 2。"""
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, limit: int, ellipsis: str = "…") -> str:
    """按 UTF-16 长度截断，保证不切开代理对，也不超过 Telegram 的硬上限。"""
    if _utf16_len(text) <= limit:
        return text
    budget = max(0, limit - _utf16_len(ellipsis))
    out: list[str] = []
    used = 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out) + ellipsis


def copy_button(label: str, text: str) -> InlineKeyboardButton | None:
    """生成 CopyTextButton；内容为空或超过 256 字符（Telegram 硬限制）时返回 None，由调用方跳过。"""
    text = str(text or "")
    if not text or _utf16_len(text) > TG_COPY_TEXT_LIMIT:
        return None
    return InlineKeyboardButton(label, copy_text=CopyTextButton(text=text))


def _copy_rows(*rows: list[InlineKeyboardButton | None]) -> list[list[InlineKeyboardButton]]:
    """过滤掉超限而返回 None 的复制按钮，并丢弃因此变空的行。"""
    out: list[list[InlineKeyboardButton]] = []
    for row in rows:
        kept = [btn for btn in row if btn is not None]
        if kept:
            out.append(kept)
    return out


def is_ascii_digits(text: str) -> bool:
    """只认 ASCII 0-9。str.isdigit() 对 '²'、'①' 也返回 True，随后 int() 会直接抛 ValueError。"""
    return bool(text) and text.isascii() and text.isdigit()


def clean_url_token(url: str) -> str:
    """去掉从聊天文本里抠出来的 URL 尾随标点（含中文全角标点）。"""
    return url.rstrip(_URL_TRAILING_PUNCT)


def is_valid_http_url(url: str, max_len: int = 2048) -> bool:
    """单个、无空白/控制字符的 http(s) URL。用于短链目标与更换链接。

    带换行的目标存进 short_links 后，/s/ 跳转时 aiohttp 会因 Location 头含 CRLF 抛异常（500）。
    """
    if not url or len(url) > max_len:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    if not re.fullmatch(r"https?://\S+", url, re.I):
        return False
    try:
        return bool(urlparse(url).hostname)
    except ValueError:
        return False


# 主菜单采用贴合消息气泡底部的两列对称内联键盘（避免底部 ReplyKeyboard 占满输入区）。
HOME_INLINE_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📦 订阅列表", callback_data="menu:list"),
        InlineKeyboardButton("🟠 临期列表", callback_data="menu:expire"),
    ],
    [
        InlineKeyboardButton("📤 导出链接", callback_data="menu:export"),
        InlineKeyboardButton("🔄 更新所有", callback_data="menu:updateall"),
    ],
    [
        InlineKeyboardButton("🔢 重置编号", callback_data="menu:renumber"),
        InlineKeyboardButton("♻️ 撤销删除", callback_data="menu:recycle"),
    ],
    [
        InlineKeyboardButton("🧭 路径对应", callback_data="menu:path"),
        InlineKeyboardButton("🧪 临时节点", callback_data="menu:temp"),
    ],
    [
        InlineKeyboardButton("🤖 API 测试", callback_data="menu:ai"),
        InlineKeyboardButton("❓ 帮助菜单", callback_data="menu:help"),
    ],
])

# 普通回执与提示消息附带轻量单行内联导航，不再弹出底部 ReplyKeyboard。
MAIN_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📦 订阅列表", callback_data="menu:list"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ]
])


def render_home_text(user_name: str | None = None) -> str:
    safe_name = html.escape(str(user_name or "用户").strip() or "用户")
    return (
        f"👋 你好，{safe_name}！\n\n"
        "🤖 订阅管理机器人\n"
        "发送订阅链接，或发送文本 / 上传 <code>.txt</code>、<code>.log</code>、<code>.yaml</code>、<code>.yml</code> 文件，\n"
        "可进行订阅查询、节点解析、导出节点、短链生成与 API 测试。\n"
        "更多内容请查看帮助菜单。"
    )

HELP_TEXT = (
    "📌 支持直接发送任意数量的订阅链接\n"
    "📄 支持上传 <code>.txt</code> / <code>.log</code> / <code>.yaml</code> / <code>.yml</code> 文本文档自动识别订阅链接和节点\n"
    "♻️ 支持在回收站中恢复最近 30 天删除的订阅\n"
    "🤖 支持直接发送 <code>API 地址 + KEY</code> 测试 OpenAI 兼容接口可用性\n\n"
    "/s &lt;关键词&gt; 搜索订阅\n"
    "/g &lt;关键词或链接&gt; 搜索 GitHub 公开代码\n"
    "/o &lt;关键词&gt; 导出搜索结果\n"
    "/d &lt;关键词或链接&gt; 删除名称或链接匹配的订阅\n"
    "/i &lt;排序方式&gt; 切换内联排序\n"
    "/ai 批量测试 API 地址和 Key\n\n"
    f"内联发送订阅：输入 @{BOT_USERNAME} 或 @{BOT_USERNAME} 订阅名或链接内容\n"
    f"内联私密分享：输入 @{BOT_USERNAME} Share [x份数] [id用户ID] [s分钟] 分享内容\n\n"
    "<b>添加订阅</b>\n"
    "直接发送订阅链接（http/https）\n"
    "名称优先使用订阅返回的配置名称\n"
    "TXT/Base64 节点列表与 YAML 配置可上传为订阅；同名文件重新上传覆盖更新。\n\n"
    "<b>临时节点</b>\n"
    "发送 ss/vmess/vless/... 链接加入临时列表\n"
    "/temp - 查看临时节点\n"
    "/temp clear - 清空临时节点\n\n"
    "<b>路径对应</b>\n"
    "格式：路径关键词 空格 配置名称\n"
    "例如：liangxinyun 良心云\n"
    "/path clear - 清空全部\n\n"
    "<b>跨会话管理</b>\n"
    "按用户 ID 管理个人订阅；同一用户群聊与私聊互通，不同用户独立。\n"
    "无关群消息不回复。可用 /list、列表按钮、独立链接，或 @机器人用户名 输入。\n"
    "群内编号详情请点击按钮，或提及/回复 Bot 后发送编号。\n\n"
    "<b>授权管理（仅管理员私聊）</b>\n"
    "/auth list - 查看授权\n"
    "/auth add 用户ID - 添加授权\n"
    "/auth remove 用户ID - 撤销授权\n"
    "授权变更立即生效并持久化；授权用户不能再授权他人。\n\n"
    "私聊发送数字编号可查看订阅详情。"
)


async def allowed(user_id: int | None) -> bool:
    """当前用户是否可用 Bot。每次都读配置和数据库覆盖项，授权立刻生效。

    管理员始终允许。显式拒绝优先于环境白名单，也优先于公开模式。
    ALLOWED_USER_IDS 为空是公开模式（显式拒绝仍然生效）。没有第一用户自动升权。
    """
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    if user_id in config.ADMIN_USER_IDS:
        return True
    try:
        override = await store.get_auth_override(user_id)
    except ValueError:
        return False
    if override is False:
        return False
    if override is True:
        return True
    allowed_ids = config.ALLOWED_USER_IDS
    if not allowed_ids:
        return True
    return user_id in allowed_ids


async def deny(update: Update, quiet: bool = False) -> bool:
    """未授权返回 True。quiet=True 时不回复，供群里的普通消息使用。

    命令和回调保持默认 quiet=False，继续明确拒绝。判断的是 effective_user，
    不使用会话 chat id 判断用户权限。
    """
    user = update.effective_user
    if user is not None and await allowed(user.id):
        return False
    if quiet:
        return True
    text = "此 Bot 需要授权使用\n授权请联系管理员"
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    return True


_AUTH_USAGE = (
    "用法：\n"
    "/auth list — 查看授权\n"
    "/auth add <用户ID> — 授权，或回复对方消息后发送 /auth add\n"
    "/auth remove <用户ID> — 取消授权，或回复对方消息后发送 /auth remove\n"
    "用户 ID 为 1 到 9223372036854775807 的 ASCII 正整数，不能有前导 0。\n"
    "动态授权可以使用 Bot，但不能再授权他人。管理员来自环境变量，不能在这里撤销。"
)

_AUTH_DISABLED = (
    "授权管理未启用：当前没有配置管理员，也不会把第一个使用者自动升为管理员。\n"
    "请在服务器 .env 设置 ADMIN_USER_IDS 后重启。"
    "未设置该变量时，管理员等于 ALLOWED_USER_IDS。"
)


def _format_auth_list(overrides: list[tuple[int, bool]]) -> str:
    allowed_ids = set(config.ALLOWED_USER_IDS)
    admin_ids = set(config.ADMIN_USER_IDS)
    denied = {uid for uid, ok in overrides if not ok}
    granted = [uid for uid, ok in overrides if ok]
    if allowed_ids:
        lines = ["授权模式：白名单"]
    else:
        lines = ["授权模式：公开（未配置 ALLOWED_USER_IDS，除显式拒绝外都可使用）"]
    if admin_ids:
        lines.append("管理员（环境变量，不能在 Bot 内撤销）：")
        lines.extend(f"• {uid}" for uid in sorted(admin_ids))
    else:
        lines.append("管理员：未配置")
    env_users = sorted(allowed_ids - admin_ids)
    if env_users:
        lines.append("环境白名单（非管理员）：")
        for uid in env_users:
            suffix = "（已撤销）" if uid in denied else ""
            lines.append(f"• {uid}{suffix}")
    if granted:
        lines.append("动态授权：")
        lines.extend(f"• {uid}" for uid in sorted(granted))
    if denied:
        lines.append("显式拒绝：")
        lines.extend(f"• {uid}" for uid in sorted(denied))
    if not allowed_ids and not granted and not denied:
        lines.append("当前没有动态授权或显式拒绝。")
    lines.append("动态授权可以使用 Bot，但不能添加或删除授权。")
    return "\n".join(lines)


def _auth_target(update: Update, args: list[str]) -> tuple[int | None, str | None]:
    if args:
        if len(args) != 1:
            return None, _AUTH_USAGE
        parsed = parse_tg_user_id(args[0])
        if parsed is None:
            return None, (
                "用户 ID 必须是 1 到 9223372036854775807 之间的 ASCII 正整数，不能有前导 0。"
            )
        return parsed, None
    message = update.effective_message
    reply = getattr(message, "reply_to_message", None) if message is not None else None
    replied_user = getattr(reply, "from_user", None) if reply is not None else None
    target_id = getattr(replied_user, "id", None) if replied_user is not None else None
    if isinstance(target_id, bool) or not isinstance(target_id, int):
        return None, "请提供用户 ID，或回复要操作的那条用户消息。\n" + _AUTH_USAGE
    parsed = parse_tg_user_id(str(target_id))
    if parsed is None:
        return None, "回复的用户 ID 不在 Telegram 允许的范围内。"
    return parsed, None


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """私聊中的授权管理。群里不改名单，也不展示名单。"""
    if await deny(update):
        return
    message = update.effective_message
    if message is None:
        return
    chat = update.effective_chat
    if chat is None or chat.type != "private":
        await message.reply_text("授权管理只能在私聊中使用，以免暴露授权名单。")
        return
    actor = update.effective_user
    if actor is None:
        return
    admin_ids = config.ADMIN_USER_IDS
    if not admin_ids:
        await message.reply_text(_AUTH_DISABLED)
        return
    if actor.id not in admin_ids:
        await message.reply_text("只有管理员可以管理授权。")
        return
    args = [str(part) for part in (getattr(context, "args", None) or [])]
    action = args[0].lower() if args else "list"
    rest = args[1:] if args else []
    if action in {"list", "ls"}:
        if rest:
            await message.reply_text(_AUTH_USAGE)
            return
        await message.reply_text(_format_auth_list(await store.list_auth_overrides()))
        return
    if action not in {"add", "remove", "del", "rm"}:
        await message.reply_text(_AUTH_USAGE)
        return
    target, error = _auth_target(update, rest)
    if error or target is None:
        await message.reply_text(error or _AUTH_USAGE)
        return
    if action == "add":
        if target in admin_ids:
            await message.reply_text("该用户已是管理员，无需再授权。")
            return
        await store.set_auth_override(target, True)
        await message.reply_text(f"已授权 {target}。该用户可以使用 Bot，但不能管理授权。")
        return
    if target in admin_ids:
        await message.reply_text(
            "不能通过 Bot 撤销管理员。请修改服务器上的 ADMIN_USER_IDS"
            "（未单独配置时等同 ALLOWED_USER_IDS）后重启。"
        )
        return
    await store.set_auth_override(target, False)
    await message.reply_text(f"已取消 {target} 的授权。重启后仍然有效，不会被环境白名单重新加回。")


def sub_token_url(token: str, kind: str = "clash") -> str:
    return f"{PUBLIC_BASE_URL}/sub/{token}/{kind}"


async def external_short_url(target: str) -> str | None:
    """Ask the configured external shortener to wrap ``target``.

    Returns None when the service is not configured, unreachable, rejects the
    request, or returns a link outside the configured domain. Callers then use
    the local link, so the bot never depends on the third party.
    """
    if not (SHORTLINK_TOKEN and SHORTLINK_DOMAIN and SHORTLINK_API_URL.startswith("https://")):
        return None
    try:
        session = await get_session()
        async with session.post(
            SHORTLINK_API_URL,
            json={"url": target, "domain": SHORTLINK_DOMAIN},
            headers={"Authorization": f"Bearer {SHORTLINK_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as resp:
            status = resp.status
            payload = await resp.json(content_type=None) if 200 <= status < 300 else None
    except Exception as exc:
        log.warning("external shortener unavailable: %s: %s", type(exc).__name__, exc)
        return None
    data = payload.get("data") if isinstance(payload, dict) and payload.get("ok") is True else None
    short = data.get("short_url") if isinstance(data, dict) else None
    parsed = urlparse(short) if isinstance(short, str) else None
    if parsed is None or parsed.scheme != "https" or parsed.hostname != SHORTLINK_DOMAIN:
        log.warning("external shortener rejected or returned an invalid link: HTTP %s", status)
        return None
    return short


async def make_short_url(user_id: int, target: str) -> str:
    """Create a short link for ``target``.

    The local /s/ code is always created first. The external shortener only
    sees that local link, so deleting the local code also revokes the external
    link's access to the subscription.
    """
    code = await store.create_short(user_id, target)
    local = f"{PUBLIC_BASE_URL}/s/{code}"
    return await external_short_url(local) or local


def scope_owner(update: Update) -> int:
    """管理数据归当前用户：同一个人在群、超群和私聊里共用订阅。"""
    return update.effective_user.id


def interaction_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """重命名/翻页/路径/API 等待按用户+会话隔离。sort_mode 留在 user_data，供个人内联查询。"""
    data = context.user_data
    if data is None:
        data = {}
        context.user_data = data
    chat_id = update.effective_chat.id
    scopes = data.get("interaction")
    if not isinstance(scopes, dict):
        scopes = {}
        data["interaction"] = scopes
    bucket = scopes.get(chat_id)
    if not isinstance(bucket, dict):
        bucket = {}
        scopes[chat_id] = bucket
    return bucket


def progress_bar(used: float | None, total: float | None) -> str:
    if used is None or total is None or total <= 0:
        return "未知"
    pct = max(0, min(100, int(used * 100 / total)))
    filled = pct // 10
    return f"[{'█' * filled}{'░' * (10 - filled)}] {pct}%"


def remain_traffic_gb(sub: dict[str, Any]) -> float | None:
    used = sub.get("traffic_used")
    total = sub.get("traffic_total")
    if used is None or total is None:
        return None
    return max(float(total) - float(used), 0.0)


def list_button_label(idx: int, sub: dict[str, Any]) -> str:
    remain = remain_traffic_gb(sub)
    remain_s = f"{remain:.2f} GB" if remain is not None else "? GB"
    exp = sub.get("expire_at")
    now = int(time.time())
    if exp is None or exp <= 0:
        days = "长期"
        is_expired = False
        is_soon = False
    else:
        delta = exp - now
        if delta < 0:
            days = "已过期"
            is_expired = True
            is_soon = False
        elif delta > 10 * 365 * 86400:
            days = "长期"
            is_expired = False
            is_soon = False
        else:
            days_num = max(0, delta // 86400)
            days = f"{days_num}天"
            is_expired = False
            is_soon = 0 <= delta <= 14 * 86400

    is_drained = remain is not None and remain <= 0.001
    if is_expired or is_drained:
        status_icon = "🔴"
    elif is_soon:
        status_icon = "🟠"
    else:
        status_icon = "🟢"

    name = str(sub.get("name") or "未命名").strip()
    label = f"{status_icon}#{idx} {name} [{remain_s}] {days}"
    return label[:64]


def sub_summary_line(idx: int, sub: dict[str, Any]) -> str:
    remain = remain_traffic_gb(sub)
    remain_s = format_bytes_gb(remain)
    expire = remain_text(sub.get("expire_at"))
    return (
        f"#{idx} <b>{html.escape(sub['name'])}</b>\n"
        f"剩余流量 {html.escape(remain_s)} · {html.escape(expire)}"
    )


def _sub_status_badge(sub: dict[str, Any], remain_traffic: float | None) -> str:
    now = int(time.time())
    exp = sub.get("expire_at")
    url = str(sub.get("url") or "")
    if sub.get("last_error"):
        return "⚠️ 刷新异常"
    if url.startswith("uploaded://"):
        return "📄 本地导入配置"
    if exp is not None and 0 < int(exp) < now:
        return "🔴 已过期"
    if remain_traffic is not None and remain_traffic <= 0.001:
        return "🔴 流量耗尽"
    if exp is not None and 0 <= int(exp) - now <= 14 * 86400:
        return "🟠 即将到期"
    return "🟢 状态正常"


def _protocol_summary(nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return "无"
    counts: dict[str, int] = {}
    for n in nodes:
        t = str(n.get("type") or "unknown").upper()
        if t == "SHADOWSOCKS":
            t = "SS"
        elif t == "HY2":
            t = "HYSTERIA2"
        counts[t] = counts.get(t, 0) + 1
    parts = [f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
    return " · ".join(parts)


def detail_text(sub: dict[str, Any], idx: int) -> str:
    nodes = nodes_of(sub)
    traffic_used = sub.get("traffic_used")
    total = sub.get("traffic_total")
    remain_traffic = None
    if traffic_used is not None and total is not None:
        remain_traffic = max(total - traffic_used, 0)

    status_badge = _sub_status_badge(sub, remain_traffic)
    proto_line = _protocol_summary(nodes)
    updated_ts = sub.get("updated_at")
    updated_str = format_expire(int(updated_ts)) if updated_ts else "未知"

    # 采用可展开的引用块 (expandable blockquote)，直接展示节点，客户端可原生点击下拉展开。
    # Telegram 单消息 4096 字符硬限制：除了最多 60 个，还按字符预算截断（长节点名 / 长订阅链接
    # 叠加时 60 行也可能超限，导致 editMessageText 直接 BadRequest、详情页打不开）。
    display_limit = 60
    err = sub.get("last_error")
    err_text = truncate_utf16(str(err), 300) if err else ""
    url_text = truncate_utf16(str(sub.get("url") or ""), 600)
    fixed_cost = 760 + _utf16_len(html.escape(str(sub.get("name") or ""))) + _utf16_len(html.escape(url_text)) + _utf16_len(html.escape(err_text))
    node_budget = max(300, TG_SAFE_BODY - fixed_cost)
    node_lines: list[str] = []
    used_chars = 0
    for n in nodes[:display_limit]:
        line = f"• {html.escape(truncate_utf16(str(n.get('name') or '未命名'), 80))}"
        cost = _utf16_len(line) + 1
        if used_chars + cost > node_budget:
            break
        node_lines.append(line)
        used_chars += cost
    shown = len(node_lines)
    if not node_lines:
        node_preview = "未解析到节点"
    else:
        if len(nodes) > shown:
            node_lines.append(f"\n<i>（前 {shown} 个已展示，点击下方按钮可翻页浏览全量 {len(nodes)} 个节点）</i>")
        node_preview = "\n".join(node_lines)

    summary_lines = [
        f"流量详情: {html.escape(format_bytes_gb(traffic_used))} / {html.escape(format_bytes_gb(total))}",
        f"使用进度: {html.escape(progress_bar(traffic_used, total))}",
        f"剩余可用: {html.escape(format_bytes_gb(remain_traffic))}",
        f"过期时间: {html.escape(format_expire(sub.get('expire_at')))}",
        f"剩余时间: {html.escape(remain_text(sub.get('expire_at')))}",
        f"协议分布: {html.escape(proto_line)}",
        f"更新时间: {html.escape(updated_str)}",
    ]
    if err:
        summary_lines.append(f"⚠️ 刷新错误: {html.escape(err_text)}")

    return (
        f"📦 <b>{html.escape(sub['name'])}</b>  ({status_badge})\n"
        f"订阅链接: <code>{html.escape(url_text)}</code>\n"
        f"<blockquote>{chr(10).join(summary_lines)}</blockquote>\n"
        f"<blockquote expandable>📡 节点列表 ({len(nodes)} 个，支持下拉展开):\n{node_preview}</blockquote>"
    )


def detail_keyboard(sub_id: int, page: int = 0, node_count: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Clash Meta", callback_data=f"sub:fmt:{sub_id}:clash"),
            InlineKeyboardButton("Sing-box", callback_data=f"sub:fmt:{sub_id}:singbox"),
            InlineKeyboardButton("Base64", callback_data=f"sub:fmt:{sub_id}:base64"),
        ],
        [
            InlineKeyboardButton("Surge", callback_data=f"sub:fmt:{sub_id}:surge"),
            InlineKeyboardButton("QX", callback_data=f"sub:fmt:{sub_id}:qx"),
            InlineKeyboardButton("🔗 生成短链", callback_data=f"sub:short:{sub_id}"),
        ],
        [
            InlineKeyboardButton("🔄 刷新订阅", callback_data=f"sub:refresh:{sub_id}:{page}"),
            InlineKeyboardButton("✏️ 重命名", callback_data=f"sub:rename:{sub_id}:{page}"),
            InlineKeyboardButton("🔗 更换链接", callback_data=f"sub:editurl:{sub_id}:{page}"),
        ],
        [
            InlineKeyboardButton("📦 导出节点", callback_data=f"sub:nodes:{sub_id}"),
            InlineKeyboardButton("🔑 重置Token", callback_data=f"sub:retoken:{sub_id}:{page}"),
        ],
    ]
    if node_count > 0:
        rows.append([
            InlineKeyboardButton(f"📡 浏览全部节点 ({node_count}个)", callback_data=f"sub:vn:{sub_id}:{page}:0")
        ])
    rows.append([
        InlineKeyboardButton("🗑 删除订阅", callback_data=f"sub:delask:{sub_id}:{page}"),
        InlineKeyboardButton("⬅️ 返回列表", callback_data=f"sub:back:{page}"),
    ])
    return InlineKeyboardMarkup(rows)


SORT_MODES = ("默认", "流量", "到期", "名称")


def _current_sort_mode(context: ContextTypes.DEFAULT_TYPE | Any) -> str:
    data = getattr(context, "user_data", None)
    mode = data.get("sort_mode", "默认") if isinstance(data, dict) else "默认"
    return mode if mode in SORT_MODES else "默认"


def list_keyboard(
    page: int,
    total: int,
    page_size: int = 8,
    items: list[tuple[int, dict[str, Any]]] | None = None,
    sort_mode: str = "默认",
) -> InlineKeyboardMarkup:
    pages = max(1, (total + page_size - 1) // page_size)
    rows: list[list[InlineKeyboardButton]] = []
    if items:
        for display_idx, sub in items:
            rows.append([
                InlineKeyboardButton(
                    list_button_label(display_idx, sub),
                    callback_data=f"sub:open:{int(sub['id'])}:{page}",
                )
            ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"list:page:{page-1}"))
    nav.append(
        InlineKeyboardButton(
            f"📄 {page+1}/{pages}",
            callback_data="list:jump" if pages > 1 else "noop",
        )
    )
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"list:page:{page+1}"))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton(f"🔀 排序:{sort_mode}", callback_data=f"list:sort:{page}"),
        InlineKeyboardButton("🔢 跳转页", callback_data="list:jump"),
        InlineKeyboardButton("🔄 更新所有", callback_data="list:updateall"),
    ])
    rows.append([InlineKeyboardButton("🏠 主菜单", callback_data="list:home")])
    return InlineKeyboardMarkup(rows)


def looks_like_host_name(name: str | None) -> bool:
    if not name:
        return True
    n = name.strip().lower()
    if n in {"订阅", "sub", "subscription"}:
        return True
    if "." in n and " " not in n and not any("\u4e00" <= ch <= "\u9fff" for ch in n):
        return True
    return False


async def refresh_sub(
    user_id: int,
    sub: dict[str, Any],
    rename: bool = False,
    meta_only: bool = False,
) -> dict[str, Any]:
    """抓订阅并落库。

    meta_only=True 时返回瘦行（不含 nodes_json，节点数在 node_count 键）。
    默认行为会把刚写进去的 nodes_json 再读回来，巨型订阅下等于白搬几十 MB；
    只需要节点数的调用方应该开这个开关，然后读 node_count 而不是 nodes_of()。

    抓取失败或解析出 0 个节点时，只写 last_error，不覆盖已缓存的 nodes_json。
    """
    if sub["url"].startswith("uploaded://"):
        # 静态文件只在重新上传时更新；不向不存在的远程地址发请求。
        return await store.update_sub(user_id, int(sub["id"]), return_meta=meta_only, last_error=None) or sub
    nodes, meta, err = await fetch_subscription(sub["url"])
    fields: dict[str, Any] = {
        "last_error": err,
        "traffic_used": meta.get("traffic_used"),
        "traffic_total": meta.get("traffic_total"),
        "expire_at": meta.get("expire_at"),
    }
    if err is None and nodes:
        fields["nodes_json"] = json.dumps(nodes, ensure_ascii=False)
        fields["node_count"] = len(nodes)
        profile_name = (meta.get("profile_name") or "").strip()
        if profile_name and (rename or looks_like_host_name(sub.get("name"))):
            fields["name"] = profile_name
    elif err is None:
        fields["last_error"] = "订阅中未解析出任何节点"
    updated = await store.update_sub(
        user_id, int(sub["id"]), return_meta=meta_only, **fields
    )
    if updated is None:
        fallback = dict(sub)
        if "node_count" in fields:
            fallback["node_count"] = fields["node_count"]
        fallback["last_error"] = fields["last_error"]
        return fallback
    return updated


def sort_subs(subs: list[dict[str, Any]], sort_mode: str) -> list[dict[str, Any]]:
    """按排序模式返回新列表（不改入参）。列表页与内联查询共用，保证两处顺序一致。"""
    out = list(subs)
    if sort_mode == "流量":
        out.sort(key=lambda s: (s.get("traffic_total") or 0) - (s.get("traffic_used") or 0), reverse=True)
    elif sort_mode == "到期":
        out.sort(key=lambda s: s.get("expire_at") or 99999999999)
    elif sort_mode == "名称":
        out.sort(key=lambda s: str(s.get("name") or "").casefold())
    return out


def stable_index_map(subs: list[dict[str, Any]]) -> dict[int, int]:
    """订阅 id → 编号（按 id 升序 1 起）。

    私聊发送数字、详情页 #编号、内联分享 #编号 都按 id 顺序；排序模式只改变展示顺序，
    不能改变编号，否则「列表上看到 #3，发 3 却打开另一条」。
    """
    ordered = sorted(int(s["id"]) for s in subs)
    return {sid: i for i, sid in enumerate(ordered, start=1)}


async def render_list(user_id: int, page: int = 0, sort_mode: str = "默认") -> tuple[str, InlineKeyboardMarkup]:
    raw_subs = await store.list_subs_meta(user_id)
    index_of = stable_index_map(raw_subs)
    subs = sort_subs(raw_subs, sort_mode)
    page_size = 8
    now = int(time.time())
    soon_count = sum(
        1 for s in subs
        if s.get("expire_at") and 0 <= s["expire_at"] - now <= 14 * 86400
    )
    if not subs:
        return "📁 <b>订阅列表为空</b>\n\n直接发送订阅链接即可添加。", InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 主菜单", callback_data="list:home")]]
        )
    total_pages = max(1, (len(subs) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    chunk = subs[start : start + page_size]
    items = [(index_of.get(int(sub["id"]), start + i), sub) for i, sub in enumerate(chunk, start=1)]
    total_nodes = sum(int(s.get("node_count") or 0) for s in subs)
    bad_count = sum(
        1 for s in subs
        if s.get("last_error")
        or (s.get("expire_at") and 0 < int(s["expire_at"]) < now)
        or (remain_traffic_gb(s) is not None and (remain_traffic_gb(s) or 0) <= 0.001)
    )
    summary_parts = [f"📊 总节点: <b>{total_nodes}</b>", f"排序: <b>{html.escape(sort_mode)}</b>"]
    if bad_count > 0:
        summary_parts.append(f"🔴异常/耗尽: <b>{bad_count}</b>")
    text = (
        f"📁 <b>订阅列表 共{len(subs)}个 | 🟠{soon_count}个临期 | 第{page + 1}/{total_pages}页</b>\n"
        f"<i>{' ｜ '.join(summary_parts)}</i>"
    )
    return text, list_keyboard(page, len(subs), page_size, items, sort_mode=sort_mode)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ── 检查是否有私密分享单次临时授权参数 (claim_{code}) ────────────────────────────
    args = context.args or []
    if args and args[0].startswith("claim_"):
        code = args[0][6:]
        user_id = update.effective_user.id if update.effective_user else 0
        ok, msg, share = await store.claim_share(code, user_id)
        if not ok:
            await update.effective_message.reply_html(
                f"🔒 <b>私密分享领取失败</b>\n\n原因: <code>{html.escape(msg)}</code>\n\n"
                "<i>此分享可能已被领完、已过期或限制了指定接收人。</i>"
            )
            return

        sender = html.escape(share.get("sender_name") or "用户")
        claimed = share.get("claimed_count", 1)
        max_v = share.get("max_views", 1)
        split_body = format_split_share_content(share["content"])
        resp_text = (
            "🎁 <b>您已成功领取私密分享：</b>\n\n"
            f"👤 来自: {sender}\n"
            f"📦 领取进度: <code>{claimed}/{max_v}</code>\n\n"
            f"{split_body}\n\n"
            "💡 <b>轻触上方灰底内容即可直接复制。</b>\n"
            "<i>（本次为单次私密提取临时授权，不包含 Bot 管理员权限）</i>"
        )
        await update.effective_message.reply_html(
            resp_text,
            disable_web_page_preview=True,
        )

        # ── 同步更新群聊里的原卡片消息 (若已捕获 inline_message_id) ───────────────────
        imid = share.get("inline_message_id")
        if imid:
            bot_username = get_bot_username(context.bot)
            target_uid = share.get("target_user_id")
            if claimed >= max_v:
                new_card_text = (
                    "🔒 <b>私密分享 (已领完)</b>\n\n"
                    f"👤 来自: {sender}\n"
                    f"📦 份数: <code>{claimed}/{max_v}</code> (已满)\n"
                    "<i>此分享已全部被领取。</i>"
                )
                with suppress(Exception):
                    await context.bot.edit_message_text(
                        inline_message_id=imid,
                        text=new_card_text,
                        parse_mode=ParseMode.HTML,
                    )
            else:
                new_card_text = (
                    "🔒 <b>私密分享</b>\n\n"
                    f"👤 来自: {sender}\n"
                    f"📦 份数: <code>{claimed}/{max_v}</code>\n"
                )
                if target_uid:
                    new_card_text += f"🎯 指定接收人: <code>{target_uid}</code>\n"
                new_card_text += "\n<i>点击下方按钮即可进入 Bot 领取内容并一键复制。</i>"
                btn_lbl = f"🎁 领取 ({claimed}/{max_v})" if max_v > 1 else "🎁 领取"
                with suppress(Exception):
                    await context.bot.edit_message_text(
                        inline_message_id=imid,
                        text=new_card_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(btn_lbl, url=f"https://t.me/{bot_username}?start=claim_{code}")]
                        ]),
                    )

        return

    # 无单次授权参数时，严格执行管理员白名单检查
    if await deny(update):
        return

    name = update.effective_user.first_name or "用户"
    # 清除历史上可能残留在客户端底部的旧 ReplyKeyboard，再发送图一同款内联主菜单卡片
    cleanup_msg = None
    with suppress(Exception):
        cleanup_msg = await update.effective_message.reply_text(
            "🔄 正在加载主菜单…",
            reply_markup=ReplyKeyboardRemove(),
        )
    await update.effective_message.reply_html(
        render_home_text(name),
        reply_markup=HOME_INLINE_KB,
        disable_web_page_preview=True,
    )
    if cleanup_msg is not None and callable(getattr(cleanup_msg, "delete", None)):
        with suppress(Exception):
            await cleanup_msg.delete()


def render_help_text(bot: Any = None) -> str:
    uname = html.escape(get_bot_username(bot))
    return (
        "📖 <b>使用帮助</b>\n"
        "<blockquote>"
        "📌 直接发订阅链接（支持多条并发导入）或 <code>名称|链接</code>\n"
        "📄 上传 <code>.txt</code> / <code>.log</code> / <code>.yaml</code> / <code>.yml</code> / <code>.json</code> 自动导入\n"
        "🤖 直接发 <code>API地址 + Key [+ 模型名]</code> 实测 OpenAI 兼容接口\n"
        "♻️ 删除后 30 天内可在回收站一键恢复"
        "</blockquote>\n"
        "<blockquote>"
        "<b>常用指令</b>\n"
        "• /list — 订阅列表（支持流量/到期/名称排序）\n"
        "• /expire — 查看 14 天内临期订阅\n"
        "• /export — 全部订阅与临时节点聚合去重导出\n"
        "• /temp — 管理临时节点（单删 / 一键转正式订阅）\n"
        "• /path 关键词 名称 — 节点路径映射（<code>/path clear</code> 清空）\n"
        "• /s 关键词 — 搜索订阅并直达详情\n"
        "• /o 关键词 — 聚合导出搜索结果\n"
        "• /d 关键词 — 删除匹配订阅（可回收站恢复）\n"
        "• /ai [地址 Key 模型] — 批量测试 API 可用性\n"
        "• /g 关键词 — 搜索 GitHub 仓库与公开代码\n"
        "• /i 排序 — 切换排序（默认 / 流量 / 到期 / 名称）"
        "</blockquote>\n"
        "<blockquote expandable>"
        "<b>内联与私密分享</b>\n"
        f"• 内联查订阅：<code>@{uname} 关键词</code>\n"
        f"• 私密分享：<code>@{uname} Share [x份数] [id用户ID] [s分钟] 内容</code>\n\n"
        "<b>跨会话与群聊规则</b>\n"
        "按用户 ID 管理个人订阅，同一用户在私聊与群聊共用数据，他人无法操作你的菜单；群内无关聊天静默不打扰。\n\n"
        "<b>授权管理（仅管理员私聊）</b>\n"
        "• /auth list — 查看授权名单\n"
        "• /auth add 用户ID — 添加授权（可回复消息）\n"
        "• /auth remove 用户ID — 撤销授权（可回复消息）\n\n"
        "💡 私聊直接发送数字编号（如 <code>1</code>）可快速打开对应订阅详情。"
        "</blockquote>"
    )


def _help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
            InlineKeyboardButton("🧪 临时节点", callback_data="temp:open"),
        ],
        [
            InlineKeyboardButton("♻️ 回收站", callback_data="recycle:page:0"),
            InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
        ],
    ])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    await update.effective_message.reply_html(
        render_help_text(getattr(context, "bot", None)),
        reply_markup=_help_keyboard(),
        disable_web_page_preview=True,
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text, kb = await render_list(scope_owner(update), 0, sort_mode=_current_sort_mode(context))
    await update.effective_message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)


async def render_expire_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    subs = await store.list_subs_meta(user_id)
    now = int(time.time())
    soon = []
    for i, sub in enumerate(subs, start=1):
        exp = sub.get("expire_at")
        if not exp:
            continue
        if 0 <= exp - now <= 14 * 86400:
            soon.append((i, sub))
    soon.sort(key=lambda pair: int(pair[1].get("expire_at") or 0))
    if not soon:
        nav = [[
            InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
            InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
        ]]
        return (
            "🟠 临期列表为空，当前没有 14 天内到期的订阅。",
            InlineKeyboardMarkup(nav),
        )
    # 临期列表一次性展示：正文最多 20 条摘要，按钮最多 40 个且全部在同一“页”内。
    # 旧实现把全部条目交给 list_keyboard(page_size=8)：超过 8 条时出现指向主列表的
    # ➡️ 翻页（点了跳到普通订阅列表第 2 页），和正文里写死的“第1/1页”自相矛盾。
    shown = soon[:40]
    summary_lines = [sub_summary_line(idx, s) for idx, s in soon[:20]]
    more = f"\n\n<i>（共 {len(soon)} 条，正文仅展示前 20 条）</i>" if len(soon) > 20 else ""
    text = (
        f"🟠 <b>临期列表 共{len(soon)}个</b>\n\n"
        + "\n\n".join(summary_lines)
        + more
    )
    kb = list_keyboard(0, len(shown), max(1, len(shown)), shown)
    return text, kb


async def cmd_expire(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text, kb = await render_expire_page(scope_owner(update))
    await update.effective_message.reply_html(text, reply_markup=kb)


async def cmd_update_all_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    user_id = scope_owner(update)
    n = await store.count_subs(user_id)
    if not n:
        await update.effective_message.reply_text("暂无订阅可更新。", reply_markup=MAIN_KB)
        return
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认更新", callback_data="upd:run"),
            InlineKeyboardButton("❌ 取消", callback_data="upd:cancel"),
        ]
    ])
    await update.effective_message.reply_text(
        f"您是否要更新 {n} 条订阅？",
        reply_markup=keyboard,
    )


async def run_update_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """并发批量更新，实时刷进度，发送结果文件并统计失效订阅。

    旧实现是串行 for 循环：fetch_subscription 默认 25s 超时，20 条订阅里只要
    有 3 条死链就得干等 75s+，而这期间界面上一个字都不动，用户只会以为 bot 死了。
    现在用 Semaphore(UPDATE_CONCURRENCY) 限流并发，墙钟时间从"累加"变成"最慢那条"。
    """
    user_id = scope_owner(update)
    deliver_to = update.effective_chat.id
    subs = await store.list_subs_meta(user_id)
    if not subs:
        await context.bot.send_message(chat_id=deliver_to, text="暂无订阅可更新。", reply_markup=MAIN_KB)
        return

    total = len(subs)
    started = time.monotonic()

    # 进度条挂在触发这次更新的那条消息上（upd:run 回调刚编辑过它）
    progress_msg = None
    if update.callback_query is not None:
        progress_msg = update.callback_query.message

    done = 0
    last_edit = 0.0
    last_text = ""

    async def _flush_progress(force: bool = False) -> None:
        """刷新进度条。节流 1.5s，避免撞 Telegram 的 editMessageText 限频。"""
        nonlocal last_edit, last_text
        if progress_msg is None:
            return
        now = time.monotonic()
        if not force and now - last_edit < 1.5:
            return
        filled = int(done / total * 10) if total else 10
        bar = "▓" * filled + "░" * (10 - filled)
        text = f"🔄 正在更新订阅…\n\n{bar} {done}/{total}"
        if text == last_text:
            return
        last_edit, last_text = now, text
        with suppress(Exception):  # 限频/消息未变更都不该中断更新
            await progress_msg.edit_text(text)

    sem = asyncio.Semaphore(max(1, UPDATE_CONCURRENCY))
    results: list[dict[str, Any] | None] = [None] * total

    async def _worker(idx: int, sub: dict[str, Any]) -> None:
        nonlocal done
        try:
            async with sem:
                # rename=False：批量更新不能把用户手动改过的名称覆盖回机场的 profile-title；
                # 仍是域名/默认名的订阅会由 refresh_sub 内的 looks_like_host_name 自动改名。
                results[idx] = await refresh_sub(
                    user_id, sub, rename=False, meta_only=True
                )
        except Exception as exc:
            # 单条炸掉不能拖垮整批，标成失败继续
            log.warning("refresh_sub failed for #%s: %s", sub.get("id"), exc)
            results[idx] = {**sub, "last_error": str(exc)}
        finally:
            done += 1
        await _flush_progress()

    await _flush_progress(force=True)
    await asyncio.gather(*(_worker(i, s) for i, s in enumerate(subs)))
    await _flush_progress(force=True)

    elapsed = time.monotonic() - started

    valid, drained, expired, failed = [], [], [], []
    for updated in results:
        if updated is None:
            continue
        if updated.get("last_error"):
            failed.append(updated)
        elif updated.get("traffic_total") and remain_traffic_gb(updated) == 0:
            drained.append(updated)
        elif updated.get("expire_at") and int(updated["expire_at"]) < int(time.time()):
            expired.append(updated)
        else:
            valid.append(updated)

    if progress_msg is not None:
        with suppress(Exception):
            await progress_msg.edit_text(f"✅ 更新完成，共 {total} 条，耗时 {elapsed:.1f}s")

    # 生成结果文件
    def _fmt_rows(rows: list[dict[str, Any]]) -> str:
        out = []
        for s in rows:
            out.append(f"#{s['id']} {s['name']} {s['url']}")
        return "\n".join(out) or "(无)"
    files = [
        ("valid_subs.txt", f"有效订阅 ({len(valid)})\n{_fmt_rows(valid)}"),
        ("failed_subs.txt", f"失效订阅 ({len(failed)})\n{_fmt_rows(failed)}"),
        ("update_report.txt", (
            f"更新完成\n有效: {len(valid)}\n耗尽: {len(drained)}\n"
            f"过期: {len(expired)}\n失败: {len(failed)}"
        )),
    ]
    for name, content in files:
        doc = InputFile(content.encode("utf-8"), filename=name)
        await context.bot.send_document(chat_id=deliver_to, document=doc)
    stats = f"有效:{len(valid)} | 耗尽:{len(drained)} | 过期:{len(expired)} | 失败:{len(failed)}"
    detail_items: list[str] = []
    for s in failed[:15]:
        err_s = truncate_utf16(str(s.get("last_error") or "未知错误"), 60)
        detail_items.append(f"❌ <b>{html.escape(str(s['name']))}</b>：{html.escape(err_s)}")
    for s in drained[:10]:
        detail_items.append(f"🟠 <b>{html.escape(str(s['name']))}</b>：流量已耗尽")
    for s in expired[:10]:
        detail_items.append(f"⏳ <b>{html.escape(str(s['name']))}</b>：已过期")
    card_html = (
        f"📊 <b>批量更新汇总</b>（耗时 {elapsed:.1f}s）\n"
        f"<blockquote>✅ 有效: {len(valid)} ｜ 🟠 耗尽: {len(drained)}\n"
        f"⏳ 过期: {len(expired)} ｜ ❌ 失败: {len(failed)}</blockquote>"
    )
    if detail_items:
        card_html += "\n<blockquote expandable>⚠️ <b>异常与过期明细（点击展开）</b>\n" + "\n".join(detail_items) + "</blockquote>"

    if failed:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑 删除失效订阅", callback_data="upd:delfailed"),
                InlineKeyboardButton("↩️ 暂不删除", callback_data="upd:keepfailed"),
            ],
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ],
        ])
        await context.bot.send_message(
            chat_id=deliver_to,
            text=f"{card_html}\n\n检测到 {len(failed)} 条失效订阅，是否从订阅列表中删除？",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    else:
        await context.bot.send_message(
            chat_id=deliver_to,
            text=card_html,
            reply_markup=MAIN_KB,
            parse_mode=ParseMode.HTML,
        )
        text_list, kb_list = await render_list(user_id, 0, sort_mode=_current_sort_mode(context))
        await context.bot.send_message(chat_id=deliver_to, text=text_list, reply_markup=kb_list, parse_mode=ParseMode.HTML)


async def render_recycle_page(user_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    rows = await store.list_deleted(user_id, days=30)
    nav_empty = [[
        InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ]]
    if not rows:
        return "♻️ 订阅回收站为空，最近30天内没有可恢复的订阅。", InlineKeyboardMarkup(nav_empty)

    page_size = 8
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    chunk = rows[start : start + page_size]

    lines = [f"♻️ <b>订阅回收站</b>（共 {len(rows)} 条 ｜ 第 {page + 1}/{total_pages} 页）\n"]
    keyboard: list[list[InlineKeyboardButton]] = []
    now = int(time.time())
    for i, row in enumerate(chunk, start=start + 1):
        nc = int(row.get("node_count") or 0)
        del_ts = int(row.get("deleted_at") or now)
        left_days = max(1, 30 - max(0, (now - del_ts) // 86400))
        lines.append(
            f"#{i} <b>{html.escape(row['name'])}</b> · <code>{nc} 节点</code> · <i>剩 {left_days} 天</i>"
        )
        keyboard.append([
            InlineKeyboardButton(
                f"↩️ 恢复 #{i} {row['name']} ({nc}节点)"[:64],
                callback_data=f"recycle:restore:{int(row['id'])}",
            )
        ])

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"recycle:page:{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))
        if page + 1 < total_pages:
            nav_row.append(InlineKeyboardButton("下页 ➡️", callback_data=f"recycle:page:{page + 1}"))
        keyboard.append(nav_row)

    keyboard.append([
        InlineKeyboardButton("♻️ 全部恢复", callback_data="recycle:restoreall"),
        InlineKeyboardButton("🧹 清空回收站", callback_data="recycle:clear"),
    ])
    keyboard.append([
        InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def cmd_recycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text, kb = await render_recycle_page(scope_owner(update), 0)
    await update.effective_message.reply_html(text, reply_markup=kb)


def _export_copy_keyboard(code: str) -> InlineKeyboardMarkup:
    clash_url = f"{PUBLIC_BASE_URL}/agg/{code}/clash"
    singbox_url = f"{PUBLIC_BASE_URL}/agg/{code}/singbox"
    base64_url = f"{PUBLIC_BASE_URL}/agg/{code}/base64"
    surge_url = f"{PUBLIC_BASE_URL}/agg/{code}/surge"
    qx_url = f"{PUBLIC_BASE_URL}/agg/{code}/qx"
    # PUBLIC_BASE_URL 很长时单个 URL 可能超过 CopyTextButton 的 256 字符上限，超限按钮直接省略
    rows = _copy_rows(
        [copy_button("📋 复制 Clash", clash_url), copy_button("📋 复制 Sing-box", singbox_url)],
        [
            copy_button("📋 复制 Base64", base64_url),
            copy_button("📋 复制 Surge", surge_url),
            copy_button("📋 复制 QX", qx_url),
        ],
    )
    rows.append([
        InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ])
    return InlineKeyboardMarkup(rows)


async def render_export_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    subs = await store.list_subs(user_id)
    for sub in subs:
        if not nodes_of(sub):
            await refresh_sub(user_id, sub)
    all_nodes = await mapped_nodes_for_user(user_id)
    if not all_nodes:
        return "暂无可导出节点。", MAIN_KB
    code = await store.create_short(user_id, "aggregate://all")
    clash_url = f"{PUBLIC_BASE_URL}/agg/{code}/clash"
    singbox_url = f"{PUBLIC_BASE_URL}/agg/{code}/singbox"
    base64_url = f"{PUBLIC_BASE_URL}/agg/{code}/base64"
    surge_url = f"{PUBLIC_BASE_URL}/agg/{code}/surge"
    qx_url = f"{PUBLIC_BASE_URL}/agg/{code}/qx"
    text = (
        f"📦 <b>导出完成</b>\n"
        f"节点数: <b>{len(all_nodes)}</b>（已自动去重）｜ 协议: <code>{html.escape(_protocol_summary(all_nodes))}</code>\n\n"
        f"Clash Meta:\n<code>{html.escape(clash_url)}</code>\n"
        f"Sing-box:\n<code>{html.escape(singbox_url)}</code>\n"
        f"Base64:\n<code>{html.escape(base64_url)}</code>\n"
        f"Surge:\n<code>{html.escape(surge_url)}</code>\n"
        f"QX:\n<code>{html.escape(qx_url)}</code>\n\n"
        f"短链码: <code>{html.escape(code)}</code>"
    )
    return text, _export_copy_keyboard(code)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text, kb = await render_export_page(scope_owner(update))
    await update.effective_message.reply_html(
        text,
        reply_markup=kb,
        disable_web_page_preview=True,
    )



async def cmd_short(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    await update.effective_message.reply_text(
        "🔗 短链用法：\n发送：短链 https://example.com/xxx\n或先发链接后点「短链」按钮。",
        reply_markup=MAIN_KB,
    )


PATH_ADD_PROMPT = "请输入 路径关键词 空格 配置名称\n例如：liangxinyun 良心云"


def parse_path_rule(raw: str) -> tuple[str, str] | None:
    """Parse target syntax ``keyword name`` while retaining ``keyword=name`` compatibility."""
    raw = " ".join(raw.split()).strip()
    if not raw:
        return None
    if "=" in raw and not raw.lower().startswith(("http://", "https://")):
        keyword, remark = raw.split("=", 1)
    else:
        parts = raw.split(None, 1)
        if len(parts) != 2:
            return None
        keyword, remark = parts
    keyword = keyword.strip()
    remark = remark.strip()
    if not keyword or not remark or len(keyword) > 128 or len(remark) > 128:
        return None
    return keyword, remark


PATH_BUTTON_LIMIT = 40


def path_keyboard(maps: list[dict[str, Any]] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Telegram 单条消息最多 100 个内联按钮；规则很多时只给前 40 条提供删除按钮
    for row in (maps or [])[:PATH_BUTTON_LIMIT]:
        label = f"🗑 {row['node_name']} → {row['remark']}"[:64]
        rows.append([InlineKeyboardButton(label, callback_data=f"path:delete:{int(row['id'])}")])
    rows.append([InlineKeyboardButton("➕ 添加对应", callback_data="path:add")])
    rows.append([InlineKeyboardButton("🏠 主菜单", callback_data="list:home")])
    return InlineKeyboardMarkup(rows)


async def render_path_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    maps = await store.list_path_maps(user_id)
    if not maps:
        return (
            "🧭 <b>路径对应为空</b>\n\n点击下方按钮即可新增一条路径对应规则。",
            path_keyboard(),
        )
    lines = [f"🧭 <b>路径对应</b>（{len(maps)}）\n"]
    used = _utf16_len(lines[0])
    shown = 0
    for row in maps:
        line = f"• <code>{html.escape(row['node_name'])}</code> → {html.escape(row['remark'])}"
        if used + _utf16_len(line) + 1 > TG_SAFE_BODY - 300:
            break
        lines.append(line)
        used += _utf16_len(line) + 1
        shown += 1
    if shown < len(maps):
        lines.append(f"\n<i>（仅展示前 {shown} 条，共 {len(maps)} 条；/path clear 可清空）</i>")
    elif len(maps) > PATH_BUTTON_LIMIT:
        lines.append(f"\n<i>（仅前 {PATH_BUTTON_LIMIT} 条提供删除按钮）</i>")
    return "\n".join(lines), path_keyboard(maps)


async def cmd_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    args = context.args or []
    user_id = scope_owner(update)
    if args and args[0].lower() == "clear":
        n = await store.clear_path_maps(user_id)
        text, kb = await render_path_page(user_id)
        await update.effective_message.reply_html(f"✅ 已清空 {n} 条路径对应。\n\n{text}", reply_markup=kb)
        return
    if args:
        rule = parse_path_rule(" ".join(args))
        if rule is None:
            await update.effective_message.reply_text(PATH_ADD_PROMPT)
            return
        await store.upsert_path_map(user_id, *rule)
    text, kb = await render_path_page(user_id)
    await update.effective_message.reply_html(text, reply_markup=kb)


async def cmd_renumber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    n = await store.renumber(scope_owner(update))
    await update.effective_message.reply_text(f"已重置编号，当前 {n} 条订阅。", reply_markup=MAIN_KB)


async def cmd_delete_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip().lower()
    if not q:
        await update.effective_message.reply_text("用法：/d 关键词或订阅链接", reply_markup=MAIN_KB)
        return
    subs = await store.list_subs_meta(scope_owner(update))
    hits = [s for s in subs if q in f"{s['name']} {s['url']}".lower()]
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    for sub in hits:
        await store.delete_sub(scope_owner(update), int(sub["id"]))
    names = "\n".join(f"• {truncate_utf16(str(s['name']), 40)}" for s in hits[:20])
    more = f"\n… 等共 {len(hits)} 个" if len(hits) > 20 else ""
    await update.effective_message.reply_text(
        f"✅ 已删除 {len(hits)} 个匹配订阅：\n{names}{more}\n\n误删可在「♻️ 撤销删除」回收站中 30 天内恢复。",
        reply_markup=MAIN_KB,
    )


async def cmd_inline_sort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    args = " ".join(context.args or []).strip()
    if args:
        valid_modes = {"默认", "流量", "到期", "名称"}
        if args not in valid_modes:
            await update.effective_message.reply_text(
                f"❌ 不支持的排序方式：{args}\n可选：{' / '.join(sorted(valid_modes))}", reply_markup=MAIN_KB
            )
            return
        if context.user_data is not None:
            context.user_data["sort_mode"] = args
        await update.effective_message.reply_text(f"✅ 内联排序已切换为：{args}", reply_markup=MAIN_KB)
        return
    interaction_data(update, context)["await"] = "inline_sort"
    current = (context.user_data or {}).get("sort_mode", "默认")
    await update.effective_message.reply_text(
        f"当前排序：{current}\n请发送新的排序方式（默认 / 流量 / 到期 / 名称）。",
        reply_markup=MAIN_KB,
    )


API_TEST_USAGE = (
    "请发送要测试的 API 地址和 Key，支持以下写法（多组可连续发送，最多 "
    f"{MAX_API_PAIRS} 组）：\n"
    "• 地址 和 Key 分两行（可加第三行 model=模型名）\n"
    "• 地址 Key [指定模型名]\n"
    "• 地址|Key[|指定模型名]\n"
    "也可以直接发送：/ai 地址 Key [模型名]\n"
    "地址可写到域名、/v1 或 /v1/chat/completions，会自动规范化。"
)


async def run_api_tests(
    update: Update,
    pairs: list[tuple[str, str]] | list[tuple[str, str, str | None]],
    *,
    is_group: bool,
    stray: int = 0,
) -> None:
    """并发检测多组「地址 + Key（+ 可选指定模型）」，每组一条结果消息。群聊里 Key 打码。"""
    message = update.effective_message
    specs: list[tuple[str, str, str | None]] = [
        (item[0], item[1], item[2] if len(item) >= 3 else None)
        for item in pairs[:MAX_API_PAIRS]
    ]
    status_msg = await message.reply_text(f"🧪 正在测试 {len(specs)} 组 API，请稍候…")
    sem = asyncio.Semaphore(5)

    async def _one(api_url: str, key: str, model_name: str | None) -> dict[str, Any]:
        async with sem:
            kwargs: dict[str, Any] = {
                "get_session": get_session,
                "check_url": _check_public_url,
            }
            if model_name:
                kwargs["model"] = model_name
            return await probe_api(api_url, key, **kwargs)

    results = await asyncio.gather(*(_one(u, k, m) for u, k, m in specs))

    rendered: list[tuple[str, InlineKeyboardMarkup | None]] = []
    for res in results:
        text = format_probe_result(res, show_full_key=not is_group)
        rows = _copy_rows(
            [
                copy_button("📋 复制地址", res["base"]),
                copy_button("📋 复制 Key", res["key"]) if not is_group else None,
            ],
            [copy_button("📋 复制测试模型", res["model"]) if res.get("model") else None],
        )
        rendered.append((text, InlineKeyboardMarkup(rows) if rows else None))

    ok_count = sum(1 for r in results if r.get("ok"))
    summary = f"🧪 测试完成：共 {len(results)} 组，✅ {ok_count} 组可用，❌ {len(results) - ok_count} 组失败"
    if stray:
        summary += f"\n⚠️ 另有 {stray} 段内容无法配对成「地址 + Key」，已忽略"

    edit = getattr(status_msg, "edit_text", None)
    if len(rendered) == 1 and not stray and callable(edit):
        text, kb = rendered[0]
        try:
            await edit(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
            return
        except Exception as exc:  # 编辑失败（如消息已删除）时退回新消息
            log.warning("edit api test status failed: %s", exc)
    elif callable(edit):
        with suppress(Exception):
            await edit(summary)
    for text, kb in rendered:
        await message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)


async def cmd_api_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    raw_args = " ".join(context.args or []).strip()
    if raw_args:
        specs, stray = parse_api_specs(raw_args)
        if specs:
            is_group = getattr(update.effective_chat, "type", None) in {"group", "supergroup"}
            await run_api_tests(update, specs, is_group=is_group, stray=stray)
            return
        await update.effective_message.reply_text(
            "⚠️ 格式错误：未识别到「API 地址 + Key」。\n" + API_TEST_USAGE,
            reply_markup=MAIN_KB,
        )
        return
    interaction_data(update, context)["await"] = "api_test"
    await update.effective_message.reply_text(
        "🧪 API 批量测试\n" + API_TEST_USAGE,
        reply_markup=MAIN_KB,
    )


async def cmd_search_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip().lower()
    # 先用瘦行筛选，再只回取命中的完整节点数据。否则搜一个普通订阅也会先把
    # 其它巨型订阅的几十 MB nodes_json 全搬进内存。
    subs = await store.list_subs_meta(scope_owner(update))
    hits = [s for s in subs if not q or q in f"{s['name']} {s['url']}".lower()]
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    nodes = []
    for hit in hits:
        sub = await store.get_sub(scope_owner(update), int(hit["id"]))
        if sub is not None:
            nodes.extend(nodes_of(sub))
    if not nodes:
        await update.effective_message.reply_text("匹配订阅暂无可导出节点。", reply_markup=MAIN_KB)
        return
    code = await store.create_short(
        scope_owner(update),
        "aggregate://ids/" + ",".join(str(int(hit["id"])) for hit in hits),
    )
    clash_url = f"{PUBLIC_BASE_URL}/agg/{code}/clash"
    singbox_url = f"{PUBLIC_BASE_URL}/agg/{code}/singbox"
    base64_url = f"{PUBLIC_BASE_URL}/agg/{code}/base64"
    await update.effective_message.reply_html(
        f"📦 <b>搜索导出完成</b>：{len(hits)} 个订阅，{len(nodes)} 个节点\n\n"
        f"Clash Meta:\n<code>{html.escape(clash_url)}</code>\n"
        f"Sing-box:\n<code>{html.escape(singbox_url)}</code>\n"
        f"Base64:\n<code>{html.escape(base64_url)}</code>",
        reply_markup=_export_copy_keyboard(code),
        disable_web_page_preview=True,
    )



async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.effective_message.reply_text("用法：/s 关键词", reply_markup=MAIN_KB)
        return
    subs = await store.list_subs_meta(scope_owner(update))
    hits: list[str] = []
    kb_rows: list[list[InlineKeyboardButton]] = []
    for i, sub in enumerate(subs, start=1):
        blob = f"{sub['name']} {sub['url']}".lower()
        if q.lower() in blob:
            hits.append(sub_summary_line(i, sub))
            if len(kb_rows) < 8:
                kb_rows.append([
                    InlineKeyboardButton(
                        list_button_label(i, sub),
                        callback_data=f"sub:open:{int(sub['id'])}:0",
                    )
                ])
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    kb_rows.append([
        InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ])
    await update.effective_message.reply_html(
        f"🔍 <b>搜索结果</b>（共 {len(hits)} 条）\n\n" + "\n\n".join(hits[:15]),
        reply_markup=InlineKeyboardMarkup(kb_rows),
        disable_web_page_preview=True,
    )


async def cmd_github_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """高可用 GitHub 搜索：并发检索代码片段与热门开源仓库，并提供网页直达按钮。"""
    if await deny(update):
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await update.effective_message.reply_text("用法：/g 关键词或开源项目名称\n例如：/g clash 或 /g subs-bot", reply_markup=MAIN_KB)
        return

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    repos_results: list[dict[str, Any]] = []
    code_results: list[dict[str, Any]] = []
    source = "GitHub"

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        session = await get_session()
        repo_url = f"https://api.github.com/search/repositories?q={quote_plus(query)}&sort=stars&order=desc&per_page=5"
        code_query = query
        if not GITHUB_TOKEN and " " not in query:
            code_query = f"{query} extension:yaml extension:txt"
        code_url = f"https://api.github.com/search/code?q={quote_plus(code_query)}&per_page=5"

        async def _fetch_repos() -> list[dict[str, Any]]:
            try:
                async with session.get(repo_url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        return payload.get("items") or []
            except Exception as e:
                log.warning("GitHub repo search failed: %s", e)
            return []

        async def _fetch_code() -> list[dict[str, Any]]:
            try:
                async with session.get(code_url, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        return payload.get("items") or []
            except Exception as e:
                log.warning("GitHub code search failed: %s", e)
            return []

        repos_results, code_results = await asyncio.gather(_fetch_repos(), _fetch_code())

        # 如果代码为空，尝试备用 grep.app
        if not code_results:
            try:
                async with session.get(
                    f"https://grep.app/api/search?q={quote_plus(query)}&regexp=false",
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        grep_items = payload.get("hits", {}).get("hits", [])
                        if grep_items:
                            source = "grep.app"
                            code_results = grep_items[:5]
            except Exception:
                pass

    except Exception as exc:
        log.warning("GitHub multi-search error: %s", exc)

    if not repos_results and not code_results:
        # 即使接口完全触发官方限流，依然提供一键直达网页结果的按钮
        web_search_url = f"https://github.com/search?q={quote_plus(query)}&type=code"
        btn_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 点击在 GitHub 网页查看代码结果", url=web_search_url)]
        ])
        await update.effective_message.reply_html(
            f"🔎 <b>GitHub 搜索</b>\n关键词：<code>{html.escape(query)}</code>\n\n"
            "⚠️ 当前官方搜索 API 暂受频控限制，已为您生成网页端专属查询直链：",
            reply_markup=btn_kb,
            disable_web_page_preview=True,
        )
        return

    lines = [f"🔎 <b>GitHub 搜索结果</b>\n关键词：<code>{html.escape(query)}</code>\n"]

    # 展示热门开源项目
    if repos_results:
        lines.append("📦 <b>热门开源仓库：</b>")
        for r in repos_results:
            full_name = html.escape(str(r.get("full_name") or "unknown"))
            html_url = str(r.get("html_url") or "")
            try:
                stars = int(r.get("stargazers_count") or 0)
            except (TypeError, ValueError):
                stars = 0
            star_str = f"⭐ {stars / 1000:.1f}k" if stars >= 1000 else f"⭐ {stars}"
            desc = html.escape(str(r.get("description") or "无项目描述").strip())
            desc_short = desc[:45] + "…" if len(desc) > 45 else desc
            lines.append(f"• <a href=\"{html.escape(html_url, quote=True)}\"><b>{full_name}</b></a> ({star_str})\n  <i>{desc_short}</i>")
        lines.append("")

    # 展示代码搜索匹配项
    if code_results:
        lines.append(f"📄 <b>相关公开代码（{source}）：</b>")
        for item in code_results:
            if source == "GitHub":
                repo = item.get("repository") or {}
                repo_name = repo.get("full_name") or "unknown"
                path = item.get("path") or ""
                html_url = item.get("html_url") or ""
            else:
                repo = item.get("repo") or {}
                repo_name = repo.get("raw") or repo.get("name") or "unknown"
                path = item.get("path") or ""
                html_url = f"https://github.com/{repo_name}/blob/HEAD/{path}" if path else ""
            title = f"{repo_name}/{path}" if path else repo_name
            if html_url:
                lines.append(f"• <a href=\"{html.escape(html_url, quote=True)}\">{html.escape(title)}</a>")
            else:
                lines.append(f"• {html.escape(title)}")

    web_url = f"https://github.com/search?q={quote_plus(query)}&type=code"
    action_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 在 GitHub 查看更多代码结果", url=web_url)]
    ])
    await update.effective_message.reply_html(
        "\n".join(lines),
        reply_markup=action_kb,
        disable_web_page_preview=True,
    )


async def render_temp_page(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = await store.list_temp(user_id)
    if not rows:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        return "🧪 <b>临时列表为空</b>\n\n直接发送 ss/vmess/vless/trojan/hy2/tuic 分享链接即可加入临时列表。", kb

    lines = [f"🧪 <b>临时节点</b> ({len(rows)})\n"]
    del_btns: list[InlineKeyboardButton] = []
    # vmess 等分享链接单条可达数百字符，20 条全文展示会超过 4096 字符硬限制导致整页发不出去。
    used = _utf16_len(lines[0])
    shown = 0
    for i, r in enumerate(rows[:20], start=1):
        scheme = (urlparse(str(r.get("url") or "")).scheme or "node").upper()
        entry = (
            f"#{i} [{html.escape(scheme)}] <b>{html.escape(truncate_utf16(str(r['name']), 60))}</b>\n"
            f"<code>{html.escape(truncate_utf16(str(r['url']), 900))}</code>"
        )
        if shown and used + _utf16_len(entry) + 2 > TG_SAFE_BODY - 300:
            break
        lines.append(entry)
        used += _utf16_len(entry) + 2
        shown += 1
        del_btns.append(
            InlineKeyboardButton(
                f"🗑 #{i} {r['name']}"[:32],
                callback_data=f"temp:del:{int(r['id'])}",
            )
        )
    if len(rows) > shown:
        lines.append(f"<i>（仅展示前 {shown} 条，共 {len(rows)} 条）</i>")

    kb_rows: list[list[InlineKeyboardButton]] = [
        del_btns[idx : idx + 2] for idx in range(0, min(len(del_btns), 12), 2)
    ]
    all_urls = "\n".join(str(r["url"]) for r in rows[:15])
    action_row = [
        InlineKeyboardButton("📦 转为正式订阅", callback_data="temp:tosub"),
        InlineKeyboardButton("🧹 清空全部", callback_data="temp:clear"),
    ]
    copy_all = copy_button("📋 一键复制全部链接", all_urls) if len(rows) <= 15 else None
    if copy_all is not None:
        kb_rows.append([copy_all])
    kb_rows.append(action_row)
    kb_rows.append([
        InlineKeyboardButton("📤 聚合导出", callback_data="temp:export"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ])
    return "\n\n".join(lines), InlineKeyboardMarkup(kb_rows)


async def cmd_temp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    args = context.args or []
    user_id = scope_owner(update)
    if args and args[0].lower() == "clear":
        n = await store.clear_temp(user_id)
        await update.effective_message.reply_text(f"已清空临时节点 {n} 条。", reply_markup=MAIN_KB)
        return
    rows = await store.list_temp(user_id)
    if not rows:
        await update.effective_message.reply_text("临时列表为空。", reply_markup=MAIN_KB)
        return
    text, kb = await render_temp_page(user_id)
    await update.effective_message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)


async def show_detail(update: Update, user_id: int, idx: int, page: int = 0) -> None:
    # 编号定位只需要元数据；完整 nodes_json 只取被点中的那一条。
    subs = await store.list_subs_meta(user_id)
    if idx < 1 or idx > len(subs):
        await update.effective_message.reply_text("编号不存在。", reply_markup=MAIN_KB)
        return
    sub = await store.get_sub(user_id, int(subs[idx - 1]["id"]))
    if not sub:
        await update.effective_message.reply_text("订阅不存在或已被删除。", reply_markup=MAIN_KB)
        return
    if not nodes_of(sub):
        sub = await refresh_sub(user_id, sub)
    nodes = nodes_of(sub)
    await update.effective_message.reply_html(
        detail_text(sub, idx),
        reply_markup=detail_keyboard(int(sub["id"]), page, len(nodes)),
        disable_web_page_preview=True,
    )


def group_input(text: str) -> bool:
    """Recognize deliberate imports, not links or assignments embedded in chat."""
    if re.fullmatch(r"[^\n|]+\|\s*https?://\S+", text):
        return True
    if text.startswith("短链 ") or text.lower().startswith("short "):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(
        not re.search(r"\s", line)
        and (re.fullmatch(r"https?://\S+", line) or extract_share_links(line))
        for line in lines
    )


def _sub_brief_meta(sub: dict[str, Any]) -> str:
    """简短单行状态摘要：节点数 · 剩余流量 · 剩余时间（或报错）。"""
    err = sub.get("last_error")
    if err:
        return f"⚠️ {truncate_utf16(str(err), 60)}"
    remain = remain_traffic_gb(sub)
    parts = [f"{int(sub.get('node_count') or 0)} 节点"]
    if remain is not None:
        parts.append(f"剩 {remain:.2f} GB")
    exp_s = remain_text(sub.get("expire_at"))
    parts.append("长期" if exp_s == "长期有效" else exp_s)
    return " · ".join(parts)


def _sub_receipt_keyboard(subs: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if len(subs) == 1:
        sid = int(subs[0]["id"])
        rows.append([
            InlineKeyboardButton("📂 查看详情", callback_data=f"sub:open:{sid}:0"),
            InlineKeyboardButton("⚡ Clash 短链", callback_data=f"sub:fmt:{sid}:clash"),
        ])
    elif subs:
        btns = [
            InlineKeyboardButton(
                f"📂 {truncate_utf16(str(s.get('name') or '订阅'), 20)}",
                callback_data=f"sub:open:{int(s['id'])}:0",
            )
            for s in subs[:6]
        ]
        rows.extend(btns[i : i + 2] for i in range(0, len(btns), 2))
    rows.append([
        InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ])
    return InlineKeyboardMarkup(rows)


def _format_single_sub_receipt(title: str, sub: dict[str, Any], idx: int | None = None) -> str:
    remain = remain_traffic_gb(sub)
    badge = _sub_status_badge(sub, remain)
    prefix = f"#{idx} " if idx else ""
    lines = [
        f"状态: {badge}",
        f"节点数: {int(sub.get('node_count') or 0)}",
        f"剩余流量: {html.escape(format_bytes_gb(remain))}",
        f"剩余时间: {html.escape(remain_text(sub.get('expire_at')))}",
    ]
    if sub.get("last_error"):
        lines.append(f"⚠️ 错误: {html.escape(truncate_utf16(str(sub['last_error']), 120))}")
    return (
        f"{title}\n"
        f"{prefix}<b>{html.escape(str(sub.get('name') or '未命名'))}</b>\n"
        f"<blockquote>{chr(10).join(lines)}</blockquote>"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    text = (update.effective_message.text or "").strip()
    user_id = scope_owner(update)
    if not text:
        return

    # ── 交互状态机 ─────────────────────────────────
    scoped = interaction_data(update, context)
    state = scoped.get("await")
    # ReplyKeyboard 菜单按钮优先级最高：点击菜单时取消正在等待的输入。
    menu_texts = {
        "📦 订阅列表", "📋 订阅列表", "订阅列表", "⏳ 临期列表", "🟠 临期列表",
        "临期列表", "🔗 短链", "短链", "📦 导出", "📤 导出链接", "导出", "导出链接",
        "🔄 更新所有", "更新所有", "🔢 重置编号", "重置编号", "♻️ 撤销删除", "撤销删除",
        "🧪 临时节点", "临时节点", "🧭 路径对应", "路径对应", "❓ 帮助菜单", "帮助菜单", "帮助",
    }
    is_group = update.effective_chat.type in {"group", "supergroup"}
    addressed = False
    if is_group:
        username = get_bot_username(context.bot)
        mention = re.match(rf"^@{re.escape(username)}(?:\s+|$)", text, re.I)
        if mention:
            text = text[mention.end():].strip()
            addressed = True
        reply = update.effective_message.reply_to_message
        if reply and reply.from_user and reply.from_user.id == context.bot.id:
            addressed = True
        if not state and not addressed and text not in menu_texts and not group_input(text):
            return
    if await deny(update):
        return
    if state and text in menu_texts and context.user_data is not None:
        scoped.pop("await", None)
        state = None

    if text.lower() in ("取消", "/cancel", "cancel", "退出"):
        if state and context.user_data is not None:
            scoped.pop("await", None)
            await update.effective_message.reply_text("已取消当前操作。", reply_markup=MAIN_KB)
            return

    # ── 订阅重命名状态机 ─────────────────────────
    if state == "sub_rename":
        sub_id = scoped.pop("rename_sub_id", None)
        page = scoped.pop("rename_page", 0)
        scoped.pop("await", None)
        new_name = text.strip()
        if not new_name or len(new_name) > 64:
            await update.effective_message.reply_text("❌ 名称不能为空且不能超过 64 个字符，重命名已取消。", reply_markup=MAIN_KB)
            return
        updated = await store.update_sub(user_id, sub_id, name=new_name)
        if not updated:
            await update.effective_message.reply_text("❌ 订阅不存在或已被删除。", reply_markup=MAIN_KB)
            return
        subs = await store.list_subs_meta(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        await update.effective_message.reply_html(
            f"✅ 订阅名称已更新为：<b>{html.escape(new_name)}</b>\n\n" + detail_text(updated, idx),
            reply_markup=detail_keyboard(sub_id, page, len(nodes_of(updated))),
            disable_web_page_preview=True,
        )
        return

    # ── 订阅更换链接状态机 ─────────────────────────
    if state == "sub_editurl":
        sub_id = scoped.pop("editurl_sub_id", None)
        page = scoped.pop("editurl_page", 0)
        scoped.pop("await", None)
        new_url = clean_url_token(text.strip())
        if not is_valid_http_url(new_url):
            await update.effective_message.reply_text("❌ 订阅链接格式无效（须为 http/https 链接），已取消更换。", reply_markup=MAIN_KB)
            return
        updated = await store.update_sub(user_id, sub_id, url=new_url)
        if not updated:
            await update.effective_message.reply_text("❌ 订阅不存在或已被删除。", reply_markup=MAIN_KB)
            return
        updated = await refresh_sub(user_id, updated, rename=False)
        subs = await store.list_subs_meta(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        await update.effective_message.reply_html(
            f"✅ <b>订阅链接已更换并完成刷新</b>（原专属短链与 Token 保持不变）\n\n" + detail_text(updated, idx),
            reply_markup=detail_keyboard(sub_id, page, len(nodes_of(updated))),
            disable_web_page_preview=True,
        )
        return

    # ── 页码跳转状态机 ──────────────────────────────
    if state == "list_jump":
        scoped.pop("await", None)
        if not is_ascii_digits(text) or len(text) > 6:
            await update.effective_message.reply_text("❌ 页码必须是数字，已取消跳转。", reply_markup=MAIN_KB)
            return
        page = int(text) - 1
        if page < 0:
            page = 0
        subs_total = await store.count_subs(user_id)
        max_page = max(0, (subs_total - 1) // 8)
        if page > max_page:
            await update.effective_message.reply_text(
                f"❌ 页码超出范围（最大 {max_page + 1}），已取消。", reply_markup=MAIN_KB
            )
            return
        text_out, kb = await render_list(user_id, page, sort_mode=_current_sort_mode(context))
        await update.effective_message.reply_html(text_out, reply_markup=kb, disable_web_page_preview=True)
        return

    # ── 路径对应新增状态机 ─────────────────────────
    if state == "path_add":
        rule = parse_path_rule(text)
        if rule is None:
            # 保留等待状态，输错后可直接重试，不用重新点“添加对应”。
            await update.effective_message.reply_text(PATH_ADD_PROMPT)
            return
        scoped.pop("await", None)
        await store.upsert_path_map(user_id, *rule)
        page_text, page_kb = await render_path_page(user_id)
        await update.effective_message.reply_html(
            f"✅ 已添加路径对应：<code>{html.escape(rule[0])}</code> → {html.escape(rule[1])}\n\n{page_text}",
            reply_markup=page_kb,
        )
        return

    # ── /ai API 测试状态机 ─────────────────────────
    if state == "api_test":
        scoped.pop("await", None)
        specs, stray = parse_api_specs(text)
        if not specs:
            await update.effective_message.reply_text(
                "⚠️ 格式错误：未识别到「API 地址 + Key」。\n" + API_TEST_USAGE, reply_markup=MAIN_KB
            )
            return
        await run_api_tests(update, specs, is_group=is_group, stray=stray)
        return

    # ── 内联排序状态机 ─────────────────────────────
    if state == "inline_sort":
        scoped.pop("await", None)
        mode = text.strip()
        valid_modes = {"默认", "流量", "到期", "名称"}
        if mode not in valid_modes:
            await update.effective_message.reply_text(
                f"❌ 不支持的排序方式：{mode}\n可选：{' / '.join(sorted(valid_modes))}", reply_markup=MAIN_KB
            )
            return
        context.user_data["sort_mode"] = mode
        await update.effective_message.reply_text(f"✅ 内联排序已切换为：{mode}", reply_markup=MAIN_KB)
        return

    if text in ("📦 订阅列表", "📋 订阅列表", "订阅列表"):
        return await cmd_list(update, context)
    if text in ("⏳ 临期列表", "🟠 临期列表", "临期列表"):
        return await cmd_expire(update, context)
    if text in ("🔗 短链", "短链"):
        return await cmd_short(update, context)
    if text in ("📦 导出", "📤 导出链接", "导出", "导出链接"):
        return await cmd_export(update, context)
    if text in ("🔄 更新所有", "更新所有"):
        return await cmd_update_all_ask(update, context)
    if text in ("🔢 重置编号", "重置编号"):
        return await cmd_renumber(update, context)
    if text in ("♻️ 撤销删除", "撤销删除"):
        return await cmd_recycle(update, context)
    if text in ("🧪 临时节点", "临时节点"):
        return await cmd_temp(update, context)
    if text in ("🧭 路径对应", "路径对应"):
        return await cmd_path(update, context)
    if text in ("❓ 帮助菜单", "帮助菜单", "帮助"):
        return await cmd_help(update, context)

    if is_ascii_digits(text):
        if len(text) > 9:
            await update.effective_message.reply_text("编号不存在。", reply_markup=MAIN_KB)
            return
        return await show_detail(update, user_id, int(text))

    if text.startswith("短链 ") or text.lower().startswith("short "):
        target = clean_url_token(text.split(" ", 1)[1].strip())
        if not is_valid_http_url(target):
            await update.effective_message.reply_text("短链目标必须是单个 http/https 链接（不能包含空格或换行）。")
            return
        short = await make_short_url(user_id, target)
        copy_rows = _copy_rows([copy_button("📋 一键复制短链", short)])
        copy_kb = InlineKeyboardMarkup(copy_rows) if copy_rows else None
        await update.effective_message.reply_html(
            f"✅ 短链已生成\n<code>{html.escape(short)}</code>",
            reply_markup=copy_kb,
        )
        return

    # 直接发送「API 地址 + Key（+ 可选模型名）」：必须在「名称|链接」和订阅链接识别之前判断，
    # 否则地址会被当成订阅链接加进列表（Key 以 == 结尾也不能落进路径规则）。
    if (not is_group or addressed) and looks_like_api_test(text):
        specs, _stray = parse_api_specs(text)
        await run_api_tests(update, specs, is_group=is_group)
        return

    if (not is_group or addressed) and "=" in text and not text.lower().startswith("http") and "://" not in text.split("=", 1)[0]:
        rule = parse_path_rule(text)
        if rule is None:
            await update.effective_message.reply_text(PATH_ADD_PROMPT)
            return
        await store.upsert_path_map(user_id, *rule)
        page_text, page_kb = await render_path_page(user_id)
        await update.effective_message.reply_html(
            f"✅ 已设置路径对应：<code>{html.escape(rule[0])}</code> → {html.escape(rule[1])}\n\n{page_text}",
            reply_markup=page_kb,
        )
        return

    # name|url
    if "|" in text and re.search(r"https?://", text):
        name, url = [x.strip() for x in text.split("|", 1)]
        name = name[:64].strip()
        url = clean_url_token(url)
        if is_valid_http_url(url):
            clean_url = url.rstrip("/")
            existing = [s for s in await store.list_subs_meta(user_id) if s["url"].rstrip("/") == clean_url]
            if existing:
                sub = existing[0]
                if name:
                    await store.update_sub(user_id, int(sub["id"]), name=name)
                sub = await refresh_sub(
                    user_id, sub, rename=not bool(name), meta_only=True
                )
                subs = await store.list_subs_meta(user_id)
                idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == int(sub["id"])), 1)
                await update.effective_message.reply_html(
                    _format_single_sub_receipt("🔄 <b>订阅已存在，已为您自动更新</b>", sub, idx),
                    reply_markup=_sub_receipt_keyboard([sub]),
                    disable_web_page_preview=True,
                )
                return

            sub = await store.add_sub(user_id, name or urlparse(url).netloc or "订阅", url)
            sub = await refresh_sub(
                user_id, sub, rename=not bool(name), meta_only=True
            )
            subs = await store.list_subs_meta(user_id)
            idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == int(sub["id"])), None)
            await update.effective_message.reply_html(
                _format_single_sub_receipt("✅ <b>已添加订阅</b>", sub, idx),
                reply_markup=_sub_receipt_keyboard([sub]),
                disable_web_page_preview=True,
            )
            return

    # subscription urls
    urls = _unique([clean_url_token(u) for u in re.findall(r"https?://\S+", text)])
    urls = [u for u in urls if is_valid_http_url(u)]
    if urls and not extract_share_links(text):
        existing_subs = await store.list_subs_meta(user_id)
        existing_url_map = {s["url"].rstrip("/"): s for s in existing_subs}
        tasks_spec: list[tuple[bool, dict[str, Any]]] = []
        # 单条消息新增订阅数与文件导入共用上限，防止一次粘贴几十条把机场并发打爆
        new_budget = MAX_IMPORTED_SUBSCRIPTIONS
        skipped_new = 0

        for url in urls:
            clean_url = url.rstrip("/")
            if clean_url in existing_url_map:
                tasks_spec.append((False, existing_url_map[clean_url]))
            else:
                if new_budget <= 0:
                    skipped_new += 1
                    continue
                new_budget -= 1
                name = urlparse(url).netloc or "订阅"
                sub = await store.add_sub(user_id, name, url)
                existing_url_map[clean_url] = sub
                tasks_spec.append((True, sub))

        sem = asyncio.Semaphore(max(1, UPDATE_CONCURRENCY))

        async def _refresh_one(is_new: bool, sub_row: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                # 只有新增订阅采用机场返回的名称；重复发送已有链接不覆盖用户手动改过的名字
                return await refresh_sub(user_id, sub_row, rename=is_new, meta_only=True)

        refreshed_rows = await asyncio.gather(*(_refresh_one(n, s) for n, s in tasks_spec))
        if len(refreshed_rows) == 1 and not skipped_new:
            is_new = tasks_spec[0][0]
            sub = refreshed_rows[0]
            subs_all = await store.list_subs_meta(user_id)
            idx = next((i for i, s in enumerate(subs_all, 1) if int(s["id"]) == int(sub["id"])), None)
            title = "✅ <b>已添加订阅</b>" if is_new else "🔄 <b>检测到重复订阅，已为您自动更新</b>"
            await update.effective_message.reply_html(
                _format_single_sub_receipt(title, sub, idx),
                reply_markup=_sub_receipt_keyboard([sub]),
                disable_web_page_preview=True,
            )
            return

        added_lines: list[str] = []
        updated_lines: list[str] = []
        for (is_new, _), refreshed in zip(tasks_spec, refreshed_rows):
            line_str = f"• <b>{html.escape(str(refreshed['name']))}</b> ({html.escape(_sub_brief_meta(refreshed))})"
            if is_new:
                added_lines.append(line_str)
            else:
                updated_lines.append(line_str)

        blocks: list[str] = []
        if added_lines:
            blocks.append(f"✅ <b>已添加新增订阅 ({len(added_lines)})</b>\n<blockquote>" + "\n".join(added_lines[:20]) + "</blockquote>")
        if updated_lines:
            blocks.append(f"🔄 <b>检测到重复订阅，已自动更新 ({len(updated_lines)})</b>\n<blockquote>" + "\n".join(updated_lines[:20]) + "</blockquote>")
        if skipped_new:
            blocks.append(f"⚠️ 单次最多新增 {MAX_IMPORTED_SUBSCRIPTIONS} 条订阅，另有 {skipped_new} 条未处理，请分批发送。")
        await update.effective_message.reply_html(
            "\n\n".join(blocks),
            reply_markup=_sub_receipt_keyboard(list(refreshed_rows)),
            disable_web_page_preview=True,
        )
        return

    # node share links
    links = extract_share_links(text)
    if links:
        names: list[str] = []
        for link in links:
            n_name = node_name_from_url(link)
            await store.add_temp(user_id, link, n_name)
            names.append(n_name)
        preview = "\n".join(f"• {html.escape(truncate_utf16(n, 48))}" for n in names[:6])
        if len(names) > 6:
            preview += f"\n<i>…等共 {len(names)} 条</i>"
        temp_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🧪 查看临时节点", callback_data="temp:open"),
                InlineKeyboardButton("📦 转为正式订阅", callback_data="temp:tosub"),
            ],
            [InlineKeyboardButton("🏠 主菜单", callback_data="list:home")],
        ])
        await update.effective_message.reply_html(
            f"✅ <b>已加入临时列表 {len(links)} 条</b>\n<blockquote>{preview}</blockquote>",
            reply_markup=temp_kb,
            disable_web_page_preview=True,
        )
        return

    if is_group and not addressed:
        return

    await update.effective_message.reply_text(
        "未识别内容。可发送订阅链接、节点链接，或点击帮助菜单。",
        reply_markup=MAIN_KB,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    message = update.effective_message
    document = message.document
    filename = (document.file_name or "upload.txt").strip() or "upload.txt"
    suffix = _document_suffix(filename)
    if suffix not in ALLOWED_DOCUMENT_SUFFIXES:
        await message.reply_text("不支持的文件类型。请上传 .txt、.log、.yaml、.yml 或 .json 文件。", reply_markup=MAIN_KB)
        return
    if document.file_size and document.file_size > MAX_DOCUMENT_BYTES:
        await message.reply_text(f"文件过大，最大支持 {MAX_DOCUMENT_BYTES // 1024 // 1024} MB。", reply_markup=MAIN_KB)
        return
    status = await message.reply_text("正在读取文件…")
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        raw = bytes(await telegram_file.download_as_bytearray())
        if len(raw) > MAX_DOCUMENT_BYTES:
            await status.edit_text("文件过大，下载后超过大小限制。")
            return
        text = decode_subscription_bytes(raw)
    except Exception:
        log.exception("document download failed")
        await status.edit_text("文件读取失败，请稍后重试。")
        return

    user_id = scope_owner(update)
    parsed_nodes = parse_nodes_from_text(text)
    created: list[tuple[str, int]] = []
    imported_subs: list[dict[str, Any]] = []
    failures: list[str] = []
    skipped = 0

    if parsed_nodes:
        name = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0][:64] or "导入配置"
        # 节点文件按名称更新同一导入订阅，TXT 与 YAML 使用相同列表入口。
        existing_imported = [s for s in await store.list_subs_meta(user_id) if s.get("name") == name and s.get("url", "").startswith("uploaded://")]
        if existing_imported:
            old_sub = existing_imported[0]
            upd_sub = await store.update_sub(user_id, int(old_sub["id"]), return_meta=True, nodes_json=json.dumps(parsed_nodes[:MAX_IMPORTED_NODES], ensure_ascii=False), last_error=None)
            created.append((f"{name} (已覆盖更新)", len(parsed_nodes[:MAX_IMPORTED_NODES])))
            if upd_sub:
                imported_subs.append(upd_sub)
        else:
            local = await store.add_imported_sub(user_id, name, parsed_nodes[:MAX_IMPORTED_NODES])
            created.append((str(local["name"]), len(parsed_nodes[:MAX_IMPORTED_NODES])))
            imported_subs.append(local)
    if not parsed_nodes or all(node.get("share") for node in parsed_nodes):
        existing_url_norm = {sub["url"].rstrip("/"): sub for sub in await store.list_subs_meta(user_id)}
        urls = _unique(value.rstrip(".,;:!?)]}>\"\'") for value in re.findall(r"https?://[^\s<>\"]+", text))
        doc_specs: list[tuple[bool, str, dict[str, Any]]] = []
        for url in urls[:MAX_IMPORTED_SUBSCRIPTIONS]:
            clean_url = url.rstrip("/")
            if clean_url in existing_url_norm:
                doc_specs.append((False, clean_url, existing_url_norm[clean_url]))
                continue
            name = urlparse(url).netloc or "订阅"
            try:
                sub = await store.add_sub(user_id, name, url)
                existing_url_norm[clean_url] = sub
                doc_specs.append((True, name, sub))
            except Exception as exc:
                failures.append(f"{name}: {exc}")

        if doc_specs:
            sem = asyncio.Semaphore(max(1, UPDATE_CONCURRENCY))

            async def _doc_refresh(is_new: bool, fallback_name: str, sub_row: dict[str, Any]) -> tuple[str, Any, dict[str, Any] | None]:
                try:
                    async with sem:
                        updated = await refresh_sub(
                            user_id, sub_row, rename=is_new, meta_only=True
                        )
                    if not is_new:
                        return ("updated", (f"{updated['name']} (已更新)", updated["node_count"]), updated)
                    if updated.get("last_error"):
                        return ("failed", f"{fallback_name}: {updated['last_error']}", None)
                    return ("created", (str(updated.get("name") or fallback_name), updated["node_count"]), updated)
                except Exception as exc:
                    if not is_new:
                        return ("skipped", 1, None)
                    return ("failed", f"{fallback_name}: {exc}", None)

            outcomes = await asyncio.gather(
                *(_doc_refresh(is_new, fname, srow) for is_new, fname, srow in doc_specs)
            )
            for kind_tag, val, sub_obj in outcomes:
                if kind_tag in ("created", "updated"):
                    created.append(val)
                    if sub_obj:
                        imported_subs.append(sub_obj)
                elif kind_tag == "skipped":
                    skipped += 1
                elif kind_tag == "failed":
                    failures.append(str(val))

    body_lines: list[str] = []
    for name, count in created[:20]:
        body_lines.append(f"• <b>{html.escape(name)}</b>（{count} 节点）")
    if len(created) > 20:
        body_lines.append(f"<i>…其余 {len(created) - 20} 条已省略</i>")
    if skipped:
        body_lines.append(f"已跳过重复订阅：{skipped} 条")
    if failures:
        body_lines.append("⚠️ 失败：" + "；".join(html.escape(truncate_utf16(item, 80)) for item in failures[:8]))
    if not body_lines:
        body_lines.append("未识别到订阅或节点内容。")
    # editMessageText 只支持 InlineKeyboardMarkup，不能传 ReplyKeyboardMarkup(MAIN_KB)
    await status.edit_text(
        f"✅ <b>已处理文件</b>：{html.escape(filename)}\n<blockquote>" + "\n".join(body_lines) + "</blockquote>",
        reply_markup=_sub_receipt_keyboard(imported_subs),
        parse_mode=ParseMode.HTML,
    )


def _callback_menu_owner(update: Update) -> int | None:
    """群聊回调所属菜单的发起人 ID；无法判定（私聊、无引用、引用的是 Bot）时返回 None。"""
    chat = getattr(update, "effective_chat", None)
    if getattr(chat, "type", None) not in ("group", "supergroup"):
        return None
    q = getattr(update, "callback_query", None)
    message = getattr(q, "message", None) if q is not None else None
    reply = getattr(message, "reply_to_message", None) if message is not None else None
    origin = getattr(reply, "from_user", None) if reply is not None else None
    if origin is None or getattr(origin, "is_bot", False) is True:
        return None
    owner_id = getattr(origin, "id", None)
    if isinstance(owner_id, bool) or not isinstance(owner_id, int):
        return None
    return owner_id


async def _edit_or_reply_html(
    q: Any,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """优先原地编辑内联卡片消息，失败时自动退回发送新消息。"""
    try:
        await q.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        msg = getattr(q, "message", None)
        if msg is not None and callable(getattr(msg, "reply_html", None)):
            await msg.reply_html(
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    user_id = update.effective_user.id if update.effective_user else 0

    # ── 公开私密分享领取：任何人均可参与，不受管理员白名单拦截 ─────────────────────
    if data.startswith("share:c:"):
        code = data.split(":")[-1]
        # 内联卡片的回调自带 inline_message_id：即使 BotFather 未开启 inline feedback
        # （收不到 ChosenInlineResult），首次点击时也能补绑，后续 /start 领取才能同步更新卡片，
        # 且已发出的卡片不会再被 purge_unsent_share_drafts 当作“未发出草稿”误删。
        imid = getattr(q, "inline_message_id", None)
        if isinstance(imid, str) and imid:
            with suppress(Exception):
                existing = await store.get_share(code)
                if existing and not existing.get("inline_message_id"):
                    await store.bind_inline_message_id(code, imid)
        ok, msg, share = await store.claim_share(code, user_id)
        if not ok:
            await q.answer(truncate_utf16(msg, TG_CALLBACK_ALERT_LIMIT), show_alert=True)
            if share and (share["claimed_count"] >= share["max_views"] or share["expire_at"] < int(time.time())):
                with suppress(Exception):
                    await q.edit_message_text(
                        f"🔒 <b>私密分享已失效</b>\n\n原因: {html.escape(msg)}\n来自: {html.escape(share.get('sender_name') or '用户')}",
                        parse_mode=ParseMode.HTML,
                    )
            return

        # 成功：尝试通过私聊把内容发给他 (各字段独立隔离灰框，轻触即可单独复制)
        sent_pm = False
        pm_err = ""
        split_body = format_split_share_content(share["content"])
        pm_text = (
            "🎁 <b>您领取的私密分享内容如下：</b>\n"
            f"👤 来自: {html.escape(share.get('sender_name') or '用户')}\n\n"
            f"{split_body}\n\n"
            "<i>💡 轻触上方灰底内容即可直接复制。</i>"
        )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=pm_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent_pm = True
        except Exception as e:
            pm_err = str(e)

        bot_user = get_bot_username(context.bot)
        if sent_pm:
            alert_msg = (
                f"🎁 领取成功！\n\n"
                f"内容已私信送达 @{bot_user}，轻触私信灰底框即可一键复制！\n\n"
                f"预览：\n{share['content']}"
            )
        else:
            alert_msg = (
                f"🎁 领取成功！\n\n"
                f"{share['content']}\n\n"
                f"💡 建议先去 @{bot_user} 发送 /start 启用私聊，以后领取自动送达私聊可直接复制！"
            )

        # Telegram 回调弹窗上限 200 字符（按 UTF-16 计，emoji 占 2）；按码点切 190 仍可能超限报错
        await q.answer(truncate_utf16(alert_msg, TG_CALLBACK_ALERT_LIMIT), show_alert=True)

        # 原地更新卡片进度
        claimed = share["claimed_count"]
        max_v = share["max_views"]
        sender = share.get("sender_name") or "用户"
        target_uid = share.get("target_user_id")

        if claimed >= max_v:
            new_text = (
                "🔒 <b>私密分享 (已领完)</b>\n\n"
                f"👤 来自: {html.escape(sender)}\n"
                f"📦 份数: <code>{claimed}/{max_v}</code> (已满)\n"
                "<i>此分享已全部被领取。</i>"
            )
            with suppress(Exception):
                await q.edit_message_text(
                    new_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔒 已领完", callback_data="noop")]
                    ]),
                )
        else:
            new_text = (
                "🔒 <b>私密分享</b>\n\n"
                f"👤 来自: {html.escape(sender)}\n"
                f"📦 份数: <code>{claimed}/{max_v}</code>\n"
            )
            if target_uid:
                new_text += f"🎯 指定接收人: <code>{target_uid}</code>\n"
            new_text += "\n<i>点击下方按钮即可进入 Bot 领取。</i>"
            with suppress(Exception):
                await q.edit_message_text(
                    new_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"🎁 领取 ({claimed}/{max_v})", callback_data=f"share:c:{code}")]
                    ]),
                )
        return

    # 管理员白名单拦截（其余管理指令如删除、刷新、导出必须经过授权）
    if await deny(update):
        return
    user_id = scope_owner(update)

    # 群聊里的菜单消息归发起人所有：Bot 在群里回复时会引用发起人的消息。其他成员点击
    # 别人的按钮时，旧实现会用点击者自己的数据 edit 这条消息——要么把别人的菜单改成
    # “订阅不存在”，要么把点击者自己的订阅列表直接公开贴到群里。这里改为只弹提示、不改消息。
    menu_owner = _callback_menu_owner(update)
    if menu_owner is not None and menu_owner != user_id:
        await q.answer("这是其他成员的菜单，请自己发送 /list 打开。", show_alert=True)
        return

    await q.answer()
    if data == "noop":
        return
    if data in ("list:home", "menu:cancel"):
        interaction_data(update, context).pop("await", None)
        user_name = getattr(update.effective_user, "first_name", None) or "用户"
        await _edit_or_reply_html(q, render_home_text(user_name), HOME_INLINE_KB)
        return
    if data == "menu:list":
        text, kb = await render_list(user_id, 0, sort_mode=_current_sort_mode(context))
        await _edit_or_reply_html(q, text, kb)
        return
    if data == "menu:expire":
        text, kb = await render_expire_page(user_id)
        await _edit_or_reply_html(q, text, kb)
        return
    if data == "menu:export":
        with suppress(Exception):
            await q.edit_message_text("📤 正在汇总节点并生成导出链接…")
        text, kb = await render_export_page(user_id)
        await _edit_or_reply_html(q, text, kb)
        return
    if data == "menu:updateall":
        n = await store.count_subs(user_id)
        if not n:
            await _edit_or_reply_html(q, "暂无订阅可更新。", MAIN_KB)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 确认更新", callback_data="upd:run"),
                InlineKeyboardButton("❌ 取消", callback_data="upd:cancel"),
            ]
        ])
        await _edit_or_reply_html(q, f"🔄 确认要批量更新 <b>{n}</b> 条订阅吗？", keyboard)
        return
    if data == "menu:renumber":
        n = await store.renumber(user_id)
        await _edit_or_reply_html(q, f"🔢 已核对订阅编号，当前共 <b>{n}</b> 条订阅。", MAIN_KB)
        return
    if data == "menu:recycle":
        text, kb = await render_recycle_page(user_id, 0)
        await _edit_or_reply_html(q, text, kb)
        return
    if data == "menu:path":
        text, kb = await render_path_page(user_id)
        await _edit_or_reply_html(q, text, kb)
        return
    if data == "menu:temp":
        text, kb = await render_temp_page(user_id)
        await _edit_or_reply_html(q, text, kb)
        return
    if data == "menu:ai":
        interaction_data(update, context)["await"] = "api_test"
        ai_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❌ 取消", callback_data="menu:cancel"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        await _edit_or_reply_html(
            q,
            f"🧪 <b>API 批量测试</b>\n<blockquote>{html.escape(API_TEST_USAGE)}</blockquote>",
            ai_kb,
        )
        return
    if data == "menu:help":
        await _edit_or_reply_html(q, render_help_text(getattr(context, "bot", None)), _help_keyboard())
        return
    if data == "list:jump":
        interaction_data(update, context)["await"] = "list_jump"
        await q.message.reply_text("🔢 请输入要跳转的页码数字（如 2）。")
        return
    if data == "path:add":
        interaction_data(update, context)["await"] = "path_add"
        await q.message.reply_text(PATH_ADD_PROMPT)
        return
    if data.startswith("path:delete:"):
        map_id = int(data.split(":")[-1])
        deleted = await store.delete_path_map(user_id, map_id)
        page_text, page_kb = await render_path_page(user_id)
        prefix = "✅ 已删除路径对应。\n\n" if deleted else "❌ 路径对应不存在。\n\n"
        with suppress(Exception):
            await q.edit_message_text(prefix + page_text, parse_mode=ParseMode.HTML, reply_markup=page_kb)
        return
    if data == "list:updateall":
        n = await store.count_subs(user_id)
        if not n:
            await q.message.reply_text("暂无订阅可更新。", reply_markup=MAIN_KB)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 确认更新", callback_data="upd:run"),
                InlineKeyboardButton("❌ 取消", callback_data="upd:cancel"),
            ]
        ])
        await q.message.reply_text(f"您是否要更新 {n} 条订阅？", reply_markup=keyboard)
        return
    if data == "upd:cancel":
        cancel_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        with suppress(Exception):
            await q.edit_message_text("已取消更新。", reply_markup=cancel_kb)
        return
    if data == "upd:run":
        await q.edit_message_text("🔄 开始更新所有订阅…")
        await run_update_all(update, context)
        return
    if data == "upd:keepfailed":
        keep_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        with suppress(Exception):
            await q.edit_message_text("已保留失效订阅。", reply_markup=keep_kb)
        return
    if data == "upd:delfailed":
        subs = await store.list_subs_meta(user_id)
        removed = 0
        for sub in subs:
            if sub.get("last_error"):
                if await store.delete_sub(user_id, int(sub["id"])):
                    removed += 1
        done_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        with suppress(Exception):
            await q.edit_message_text(f"✅ 已删除 {removed} 条失效订阅。", reply_markup=done_kb)
        return
    if data.startswith("list:sort:"):
        page = int(data.split(":")[-1])
        cur_mode = _current_sort_mode(context)
        next_mode = SORT_MODES[(SORT_MODES.index(cur_mode) + 1) % len(SORT_MODES)]
        if context.user_data is None:
            context.user_data = {}
        context.user_data["sort_mode"] = next_mode
        text, kb = await render_list(user_id, page, sort_mode=next_mode)
        with suppress(Exception):
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("list:page:"):
        page = int(data.split(":")[-1])
        text, kb = await render_list(user_id, page, sort_mode=_current_sort_mode(context))
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("sub:open:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        if not nodes_of(sub):
            sub = await refresh_sub(user_id, sub, rename=False)
        subs = await store.list_subs_meta(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        await q.edit_message_text(
            detail_text(sub, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=detail_keyboard(sub_id, page, len(nodes_of(sub))),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("recycle:page:"):
        page = int(data.split(":")[-1])
        text, kb = await render_recycle_page(user_id, page)
        with suppress(Exception):
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return
    if data == "recycle:restoreall":
        count = await store.restore_all_deleted(user_id)
        done_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        with suppress(Exception):
            await q.edit_message_text(
                f"✅ 已全部恢复（共 {count} 条订阅）。" if count else "♻️ 回收站中没有可恢复的订阅。",
                reply_markup=done_kb,
            )
        return
    if data == "recycle:clear":
        count = await store.clear_deleted(user_id)
        done_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        with suppress(Exception):
            await q.edit_message_text(f"🧹 已清空回收站（共清理 {count} 条记录）。", reply_markup=done_kb)
        return
    if data.startswith("recycle:restore:"):
        deleted_id = int(data.split(":")[-1])
        restored = await store.restore_deleted(user_id, deleted_id)
        if restored:
            restored_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📂 打开该订阅", callback_data=f"sub:open:{int(restored['id'])}:0"),
                    InlineKeyboardButton("♻️ 返回回收站", callback_data="recycle:page:0"),
                ],
                [
                    InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                    InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
                ],
            ])
            await q.edit_message_text(f"✅ 已恢复：{restored['name']}", reply_markup=restored_kb)
        else:
            await q.edit_message_text("恢复失败，记录可能已过期。")
        return
    if data == "temp:open":
        page_text, page_kb = await render_temp_page(user_id)
        with suppress(Exception):
            await q.edit_message_text(
                page_text,
                parse_mode=ParseMode.HTML,
                reply_markup=page_kb,
                disable_web_page_preview=True,
            )
        return
    if data.startswith("temp:del:"):
        temp_id = int(data.split(":")[-1])
        deleted = await store.delete_temp(user_id, temp_id)
        page_text, page_kb = await render_temp_page(user_id)
        prefix = "✅ 已删除该临时节点。\n\n" if deleted else "❌ 临时节点不存在。\n\n"
        with suppress(Exception):
            await q.edit_message_text(
                prefix + page_text,
                parse_mode=ParseMode.HTML,
                reply_markup=page_kb,
                disable_web_page_preview=True,
            )
        return
    if data == "temp:clear":
        n = await store.clear_temp(user_id)
        done_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ]
        ])
        with suppress(Exception):
            await q.edit_message_text(f"🧹 已清空临时节点 {n} 条。", reply_markup=done_kb)
        return
    if data == "temp:tosub":
        rows = await store.list_temp(user_id)
        if not rows:
            await q.edit_message_text("临时列表为空，无法转为订阅。")
            return
        nodes = [_temp_row_to_node(r) for r in rows]
        sub_name = f"临时节点合集 ({len(nodes)})"
        sub = await store.add_imported_sub(user_id, sub_name, nodes)
        await store.clear_temp(user_id)
        subs = await store.list_subs_meta(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == int(sub["id"])), 1)
        await q.edit_message_text(
            f"✅ <b>已将 {len(nodes)} 条临时节点转为正式订阅！</b>\n\n" + detail_text(sub, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=detail_keyboard(int(sub["id"]), 0, len(nodes)),
            disable_web_page_preview=True,
        )
        return
    if data == "temp:export":
        all_nodes = await mapped_nodes_for_user(user_id)
        if not all_nodes:
            await q.edit_message_text("暂无可导出节点。")
            return
        code = await store.create_short(user_id, "aggregate://all")
        clash_url = f"{PUBLIC_BASE_URL}/agg/{code}/clash"
        base64_url = f"{PUBLIC_BASE_URL}/agg/{code}/base64"
        await q.edit_message_text(
            f"📦 <b>导出完成</b>\n节点数: <b>{len(all_nodes)}</b>（含临时节点，已自动去重）\n\n"
            f"Clash Meta:\n<code>{html.escape(clash_url)}</code>\n"
            f"Base64:\n<code>{html.escape(base64_url)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_export_copy_keyboard(code),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("sub:vn:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        list_page = int(parts[3]) if len(parts) > 3 else 0
        node_page = int(parts[4]) if len(parts) > 4 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        nodes = nodes_of(sub)
        if not nodes:
            await q.answer("该订阅当前未解析到节点", show_alert=True)
            return

        node_page_size = 15
        total_pages = max(1, (len(nodes) + node_page_size - 1) // node_page_size)
        node_page = max(0, min(node_page, total_pages - 1))
        start_idx = node_page * node_page_size
        chunk_nodes = nodes[start_idx : start_idx + node_page_size]

        lines = [
            f"📡 <b>节点浏览：{html.escape(sub['name'])}</b>",
            f"<i>共 {len(nodes)} 个节点 ｜ 协议: {html.escape(_protocol_summary(nodes))} ｜ 第 {node_page + 1}/{total_pages} 页</i>\n",
        ]
        page_links: list[str] = []
        for i, n in enumerate(chunk_nodes, start=start_idx + 1):
            # 先 upper 再转义：反过来会把 &amp; 变成 Telegram 不认识的 &AMP;
            n_type = html.escape(str(n.get("type") or "node").upper())
            n_name = html.escape(truncate_utf16(str(n.get("name") or "未命名"), 60))
            server = truncate_utf16(str(n.get("server") or ""), 80)
            port = n.get("port") or n.get("server_port") or ""
            ep_str = f" <code>({html.escape(server)}:{html.escape(str(port))})</code>" if server and port else ""
            lines.append(f"<code>#{i:02d}</code> [{n_type}] <b>{n_name}</b>{ep_str}")
            link = node_to_share_link(n)
            if link:
                page_links.append(link)

        if page_links:
            # vmess 等链接单条数百字符，8 条全塞进来会超 4096 上限、整页 BadRequest；按剩余预算装填
            header = "\n🔗 <b>本页分享链接（轻触展开复制）：</b>\n<blockquote expandable><code>"
            footer = "</code></blockquote>"
            budget = TG_SAFE_BODY - _utf16_len("\n".join(lines)) - _utf16_len(header) - _utf16_len(footer)
            picked: list[str] = []
            for l in page_links[:8]:
                piece = html.escape(l)
                cost = _utf16_len(piece) + 1
                if cost > budget:
                    break
                picked.append(piece)
                budget -= cost
            if picked:
                omitted = len(page_links) - len(picked)
                tail = f"\n<i>（另有 {omitted} 条未展示，可点「导出全部节点文件」获取）</i>" if omitted > 0 else ""
                lines.append(f"{header}{chr(10).join(picked)}{footer}{tail}")

        nav_row: list[InlineKeyboardButton] = []
        if node_page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"sub:vn:{sub_id}:{list_page}:{node_page - 1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {node_page + 1}/{total_pages}", callback_data="noop"))
        if node_page + 1 < total_pages:
            nav_row.append(InlineKeyboardButton("下页 ➡️", callback_data=f"sub:vn:{sub_id}:{list_page}:{node_page + 1}"))

        kb_rows = []
        if total_pages > 1:
            kb_rows.append(nav_row)
        kb_rows.append([
            InlineKeyboardButton("📦 导出全部节点文件", callback_data=f"sub:nodes:{sub_id}"),
            InlineKeyboardButton("⬅️ 返回订阅详情", callback_data=f"sub:open:{sub_id}:{list_page}"),
        ])

        await q.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb_rows),
            disable_web_page_preview=True,
        )
        return

    if data.startswith("sub:back:"):
        page = int(data.split(":")[-1])
        text, kb = await render_list(user_id, page, sort_mode=_current_sort_mode(context))
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("sub:fmt:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        kind = parts[3]
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.message.reply_text("订阅不存在")
            return
        if not nodes_of(sub):
            sub = await refresh_sub(user_id, sub, rename=False)
        target = sub_token_url(sub["token"], kind)
        short = await make_short_url(user_id, target)
        title = {
            "clash": "Clash Meta",
            "singbox": "Sing-box",
            "base64": "Base64",
            "surge": "Surge",
            "qx": "QX",
        }.get(kind, kind)
        copy_rows = _copy_rows([copy_button(f"📋 一键复制 {title} 链接", short)])
        copy_kb = InlineKeyboardMarkup(copy_rows) if copy_rows else None
        await q.message.reply_html(
            f"🔗 <b>{html.escape(title)}</b>（{html.escape(sub['name'])}）\n<code>{html.escape(short)}</code>",
            reply_markup=copy_kb,
            disable_web_page_preview=True,
        )
        return
    if data.startswith("sub:refresh:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        sub = await refresh_sub(user_id, sub, rename=False)
        # find display index
        subs = await store.list_subs_meta(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        await q.edit_message_text(
            detail_text(sub, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=detail_keyboard(sub_id, page, len(nodes_of(sub))),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("sub:rename:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        scoped = interaction_data(update, context)
        scoped["await"] = "sub_rename"
        scoped["rename_sub_id"] = sub_id
        scoped["rename_page"] = page
        await q.message.reply_text(
            f"✏️ 请发送订阅「<b>{html.escape(sub['name'])}</b>」的新名称：\n<i>（输入 取消 可退出）</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KB,
        )
        return
    if data.startswith("sub:editurl:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        scoped = interaction_data(update, context)
        scoped["await"] = "sub_editurl"
        scoped["editurl_sub_id"] = sub_id
        scoped["editurl_page"] = page
        await q.message.reply_text(
            f"🔗 请发送订阅「<b>{html.escape(sub['name'])}</b>」的新订阅链接（http/https）：\n"
            "<i>（将保留该订阅的本地专属短链与 Token 不变，输入 取消 可退出）</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KB,
        )
        return
    if data.startswith("sub:retoken:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        updated = await store.rotate_sub_token(user_id, sub_id)
        if not updated:
            await q.edit_message_text("订阅不存在")
            return
        subs = await store.list_subs_meta(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        new_clash = sub_token_url(updated["token"], "clash")
        await q.edit_message_text(
            f"🔑 <b>已重置专属 Token（旧短链已自动吊销）</b>\n"
            f"新 Clash 订阅地址：<code>{html.escape(new_clash)}</code>\n\n"
            + detail_text(updated, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=detail_keyboard(sub_id, page, len(nodes_of(updated))),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("sub:delask:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.edit_message_text("订阅不存在")
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 确认删除", callback_data=f"sub:del:{sub_id}:{page}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"sub:back:{page}"),
            ]
        ])
        await q.edit_message_text(
            f"确认删除订阅「{html.escape(sub['name'])}」？\n删除后 30 天内可在回收站恢复。",
            reply_markup=keyboard,
        )
        return
    if data.startswith("sub:del:"):
        parts = data.split(":")
        sub_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        ok = await store.delete_sub(user_id, sub_id)
        text, kb = await render_list(user_id, page, sort_mode=_current_sort_mode(context))
        prefix = "✅ 已删除\n\n" if ok else "❌ 删除失败\n\n"
        await q.edit_message_text(prefix + text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return
    if data.startswith("sub:short:"):
        sub_id = int(data.split(":")[-1])
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.message.reply_text("订阅不存在")
            return
        target = sub_token_url(sub["token"], "clash")
        short = await make_short_url(user_id, target)
        short_rows = _copy_rows(
            [copy_button("📋 一键复制短链", short)],
            [copy_button("📋 复制原始订阅链接", str(sub["url"]))]
            if str(sub.get("url") or "").startswith(("http://", "https://"))
            else [],
        )
        await q.message.reply_html(
            "🔗 <b>短链生成成功</b>\n"
            f"🏷️ 原始机场名称：{html.escape(sub['name'])}\n"
            f"🌐 原始订阅链接：<code>{html.escape(truncate_utf16(str(sub['url']), 1500))}</code>\n"
            f"🌟 生成短链地址：<code>{html.escape(short)}</code>\n"
            "📦 此短链会代理原始订阅，可直接导入客户端\n"
            "⚠️ 注意部分机场无法代理访问，请先自行尝试",
            reply_markup=InlineKeyboardMarkup(short_rows) if short_rows else None,
            disable_web_page_preview=True,
        )
        return
    if data.startswith("sub:nodes:"):
        sub_id = int(data.split(":")[-1])
        sub = await store.get_sub(user_id, sub_id)
        if not sub:
            await q.message.reply_text("订阅不存在")
            return
        maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(user_id)}
        nodes = apply_path_maps(nodes_of(sub), maps)
        links = to_share_links(nodes)
        if not links:
            await q.message.reply_text("没有可导出的分享链接（可能来自 YAML 订阅）。")
            return
        content = "\n".join(links)
        # 内容是逐行明文分享链接（不是 Base64 编码），文件名如实标注
        doc = InputFile(content.encode("utf-8"), filename="nodes.txt")
        await q.message.reply_document(document=doc, caption=f"📦 节点导出完成（{len(links)} 条分享链接）")
        return


def _temp_row_to_node(t: dict[str, Any]) -> dict[str, Any]:
    url = str(t.get("url") or "").strip()
    name = str(t.get("name") or "").strip() or node_name_from_url(url)
    parsed = parse_share_link(url)
    if parsed:
        node = dict(parsed)
        node["name"] = name
        node["share"] = url
        return node
    return {"name": name, "type": "temp", "share": url}


async def mapped_nodes_for_user(
    user_id: int, sub_ids: list[int] | None = None, *, dedup: bool = True
) -> list[dict[str, Any]]:
    maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(user_id)}
    if sub_ids is None:
        subs = await store.list_subs(user_id)
    else:
        subs = []
        for sub_id in sub_ids:
            sub = await store.get_sub(user_id, sub_id)
            if sub is not None:
                subs.append(sub)
    nodes: list[dict[str, Any]] = []
    for sub in subs:
        nodes.extend(apply_path_maps(nodes_of(sub), maps))
    if sub_ids is None:
        temp_nodes = [_temp_row_to_node(t) for t in await store.list_temp(user_id)]
        nodes.extend(apply_path_maps(temp_nodes, maps))
    return deduplicate_nodes(nodes) if dedup else nodes


def _render_subscription_response(
    nodes: list[dict[str, Any]],
    kind: str,
    *,
    subs_meta: list[dict[str, Any]] | dict[str, Any] | None = None,
    profile_title: str | None = None,
) -> web.Response:
    headers: dict[str, str] = {"Cache-Control": "no-store, private"}
    if subs_meta is not None:
        userinfo = format_subscription_userinfo(subs_meta)
        if userinfo:
            headers["subscription-userinfo"] = userinfo
    if profile_title:
        clean_title = profile_title.strip()[:64]
        if clean_title:
            b64_title = base64.b64encode(clean_title.encode("utf-8")).decode("ascii")
            headers["profile-title"] = f"base64:{b64_title}"
            ext = ".json" if kind in ("singbox", "sing-box") else (".yaml" if kind == "clash" else ".txt")
            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(clean_title + ext)}"

    if kind in ("base64", "v2ray", "mixed"):
        return web.Response(text=to_base64_sub(nodes), content_type="text/plain", headers=headers)
    if kind in ("singbox", "sing-box"):
        return web.Response(text=to_singbox(nodes), content_type="application/json", headers=headers)
    if kind == "surge":
        return web.Response(text=to_surge(nodes), content_type="text/plain", headers=headers)
    if kind in ("qx", "quantumult", "quantumultx"):
        return web.Response(text=to_qx(nodes), content_type="text/plain", headers=headers)
    return web.Response(text=to_clash(nodes), content_type="text/yaml", headers=headers)


async def http_sub(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    kind = (
        request.match_info.get("kind")
        or request.query.get("format")
        or request.query.get("type")
        or "clash"
    ).lower()
    sub = await store.get_sub_by_token(token)
    if not sub:
        return web.Response(status=404, text="not found")
    maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(int(sub["user_id"]))}
    nodes = apply_path_maps(nodes_of(sub), maps)
    if request.query.get("dedup", "1") != "0":
        nodes = deduplicate_nodes(nodes)
    return _render_subscription_response(
        nodes,
        kind,
        subs_meta=sub,
        profile_title=str(sub.get("name") or "subs-bot"),
    )


async def http_agg(request: web.Request) -> web.Response:
    code = request.match_info["code"]
    row = await store.get_short(code)
    if not row:
        return web.Response(status=404, text="not found")
    target = str(row["target_url"])
    user_id = int(row["user_id"])
    dedup = request.query.get("dedup", "1") != "0"
    if target == "aggregate://all":
        nodes = await mapped_nodes_for_user(user_id, dedup=dedup)
        subs_meta = await store.list_subs_meta(user_id)
    elif target.startswith("aggregate://ids/"):
        raw_ids = target.removeprefix("aggregate://ids/")
        try:
            sub_ids = [int(value) for value in raw_ids.split(",") if value]
        except ValueError:
            return web.Response(status=400, text="invalid aggregate target")
        if not sub_ids:
            return web.Response(status=404, text="not found")
        nodes = await mapped_nodes_for_user(user_id, sub_ids, dedup=dedup)
        subs_meta = [
            meta
            for sid in sub_ids
            if (meta := await store.get_sub_meta(user_id, sid)) is not None
        ]
    else:
        return web.Response(status=404, text="not found")
    kind = (
        request.match_info.get("kind")
        or request.query.get("format")
        or request.query.get("type")
        or "clash"
    ).lower()
    return _render_subscription_response(
        nodes,
        kind,
        subs_meta=subs_meta,
        profile_title="subs-bot 聚合订阅",
    )




async def http_short(request: web.Request) -> web.Response:
    code = request.match_info["code"]
    row = await store.get_short(code)
    if not row:
        return web.Response(status=404, text="not found")
    target = row["target_url"]
    if target == "aggregate://all" or target.startswith("aggregate://ids/"):
        raise web.HTTPFound(f"/agg/{code}/clash")
    if not target.startswith(("http://", "https://")) or not is_valid_http_url(target, max_len=8192):
        # 历史数据里可能存有带换行/空格的目标；直接 HTTPFound 会因 Location 头非法抛 500
        return web.Response(status=400, text="invalid target")
    raise web.HTTPFound(target)



async def http_health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "subs-bot"})


async def start_http(app: Application) -> web.AppRunner:
    api = web.Application()
    api.router.add_get("/health", http_health)
    api.router.add_get("/sub/{token}/{kind}", http_sub)
    api.router.add_get("/sub/{token}", http_sub)
    api.router.add_get("/agg/{code}/{kind}", http_agg)
    api.router.add_get("/agg/{code}", http_agg)
    api.router.add_get("/s/{code}", http_short)
    runner = web.AppRunner(api)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    log.info("HTTP on %s:%s", HTTP_HOST, HTTP_PORT)
    return runner


def format_split_share_content(raw_content: str) -> str:
    """把私密分享内容格式化为彼此隔离的独立代码框，轻触即可单独复制，防止连带全选。"""
    lines = [line.strip() for line in raw_content.splitlines() if line.strip()]
    body_parts: list[str] = []

    # 1. 检测是否为格式化的订阅包（含名称、原链、Clash配置等）
    is_sub_pack = any("原始链接" in l or "Clash 配置" in l for l in lines)
    if is_sub_pack:
        for l in lines:
            if "：" in l:
                label, _, val = l.partition("：")
            elif ":" in l:
                label, _, val = l.partition(":")
            else:
                label, val = "内容", l
            label = label.strip()
            val = val.strip()
            if not val:
                continue

            # 每一项单独使用代码框包裹，并且中间空一行做物理隔离
            body_parts.append(f"<b>{html.escape(label)}：</b>\n<code>{html.escape(val)}</code>")

        return "\n\n".join(body_parts)

    # 2. 检测是否为多行节点或多条链接
    if len(lines) > 1:
        for i, l in enumerate(lines, 1):
            body_parts.append(f"<b>片段 #{i}：</b>\n<code>{html.escape(l)}</code>")
        return "\n\n".join(body_parts)

    # 3. 单条内容：单一隔离框
    single_val = raw_content.strip()
    return f"<b>内容如下：</b>\n<code>{html.escape(single_val)}</code>"


def parse_share_query(raw: str) -> tuple[int, int | None, int, str] | None:
    """Parse ``Share [x份数] [id用户ID] [s分钟] 内容`` syntax."""
    s = raw.strip()
    if not s.lower().startswith("share"):
        return None
    rest = s[5:].strip()
    if not rest:
        return None
    parts = rest.split()
    max_views = 1
    target_user_id = None
    duration_minutes = 10
    content_parts = []
    for p in parts:
        lower = p.lower()
        # 只认 ASCII 数字：'x²'.isdigit() 为 True，但 int('²') 会抛 ValueError，导致整个内联查询崩溃
        if lower.startswith("x") and is_ascii_digits(lower[1:]) and len(lower) <= 8:
            max_views = max(1, min(100, int(lower[1:])))
        elif lower.startswith("id") and is_ascii_digits(lower[2:]):
            parsed_target = parse_tg_user_id(lower[2:])
            if parsed_target is None:
                content_parts.append(p)
            else:
                target_user_id = parsed_target
        elif lower.startswith("s") and is_ascii_digits(lower[1:]) and len(lower) <= 10:
            duration_minutes = max(1, min(10080, int(lower[1:])))
        else:
            content_parts.append(p)
    content = " ".join(content_parts).strip()
    if not content:
        return None
    return max_views, target_user_id, duration_minutes, content


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """内联查询：@bot 关键词 → 订阅/临时节点；@bot Share ... → 私密分享。"""
    iq = update.inline_query
    if iq is None:
        return
    user_id = iq.from_user.id
    user_name = iq.from_user.first_name or "用户"
    if not await allowed(user_id):
        # PTB 21+ 已移除 switch_pm_text/switch_pm_parameter，旧写法会直接 TypeError，
        # 未授权用户的内联查询因此永远得不到任何响应
        await iq.answer(
            [],
            cache_time=5,
            is_personal=True,
            button=InlineQueryResultsButton(text="此 Bot 需要授权使用", start_parameter="denied"),
        )
        return

    raw_query = (iq.query or "").strip()
    share_parsed = parse_share_query(raw_query)
    if share_parsed:
        max_views, target_user_id, duration_minutes, content = share_parsed
        with suppress(Exception):
            await store.purge_unsent_share_drafts(user_id, within_seconds=3)
        subs = await store.list_subs_meta(user_id)
        results: list[InlineQueryResultArticle] = []

        # 1. 寻找可能匹配的订阅列表 (候选池)
        candidate_subs: list[tuple[int, dict[str, Any]]] = []
        clean_num = content.lstrip("#").strip()
        if is_ascii_digits(clean_num) and len(clean_num) <= 9:
            idx = int(clean_num)
            if 1 <= idx <= len(subs):
                candidate_subs.append((idx, subs[idx - 1]))

        content_lower = content.lower()
        for i, s in enumerate(subs, 1):
            s_name_lower = str(s.get("name") or "").lower()
            if s_name_lower and (content_lower in s_name_lower or s_name_lower in content_lower):
                if not any(c[1]["id"] == s["id"] for c in candidate_subs):
                    candidate_subs.append((i, s))

        bot_username = get_bot_username(context.bot)
        # 为每一个匹配到的订阅生成一个专属私密分享卡片 (最多前 5 个最匹配的)
        target_hint = f" | 指定: {target_user_id}" if target_user_id else ""
        for display_idx, sub in candidate_subs[:5]:
            sub_name = sub["name"]
            sub_clash = sub_token_url(sub["token"], "clash")
            real_content = (
                f"🏷️ 订阅名称：{sub_name}\n"
                f"🌐 原始链接：{sub['url']}\n"
                f"⚡ Clash 配置：{sub_clash}"
            )
            code = await store.create_share(
                sender_id=user_id,
                sender_name=user_name,
                content=real_content,
                max_views=max_views,
                target_user_id=target_user_id,
                duration_minutes=duration_minutes,
            )
            card_title = f"🔒 分享订阅: #{display_idx} {sub_name} (限{max_views}份/{duration_minutes}m{target_hint})"
            remain_val = remain_traffic_gb(sub)
            remain_str = f"{remain_val:.2f} GB" if remain_val is not None else "? GB"
            card_desc = f"节点: {sub.get('node_count', 0)} 个 | 剩余流量: {remain_str}"
            msg_text = (
                "🔒 <b>私密分享</b>\n\n"
                f"👤 来自: {html.escape(user_name)}\n"
                f"🏷️ 订阅项目: <b>{html.escape(sub_name)}</b>\n"
                f"📦 份数: <code>0/{max_views}</code>\n"
                f"⏳ 有效期: <code>{duration_minutes} 分钟</code>\n"
            )
            if target_user_id:
                msg_text += f"🎯 指定接收人: <code>{target_user_id}</code>\n"
            msg_text += "\n<i>点击下方按钮即可进入 Bot 领取。</i>"
            btn_lbl = f"🎁 领取 (0/{max_views})" if max_views > 1 else "🎁 领取"

            results.append(
                InlineQueryResultArticle(
                    id=f"share_sub_{code}",
                    title=card_title[:64],
                    description=card_desc[:100],
                    input_message_content=InputTextMessageContent(msg_text, parse_mode=ParseMode.HTML),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(btn_lbl, callback_data=f"share:c:{code}")],
                    ]),
                )
            )

        # 2. 永远追加一张“按原始文本内容分享”的卡片 (兜底或纯文字分享)
        code_text = await store.create_share(
            sender_id=user_id,
            sender_name=user_name,
            content=content,
            max_views=max_views,
            target_user_id=target_user_id,
            duration_minutes=duration_minutes,
        )
        card_title_text = f"🔒 分享自定义内容 (限{max_views}份/{duration_minutes}m{target_hint})"
        card_desc_text = f"文本: {content[:35]}…" if len(content) > 35 else f"文本: {content}"
        msg_text_custom = (
            "🔒 <b>私密分享</b>\n\n"
            f"👤 来自: {html.escape(user_name)}\n"
            f"📦 份数: <code>0/{max_views}</code>\n"
            f"⏳ 有效期: <code>{duration_minutes} 分钟</code>\n"
        )
        if target_user_id:
            msg_text_custom += f"🎯 指定接收人: <code>{target_user_id}</code>\n"
        msg_text_custom += "\n<i>点击下方按钮即可进入 Bot 领取。</i>"
        btn_lbl_txt = f"🎁 领取 (0/{max_views})" if max_views > 1 else "🎁 领取"

        results.append(
            InlineQueryResultArticle(
                id=f"share_txt_{code_text}",
                title=card_title_text[:64],
                description=card_desc_text[:100],
                input_message_content=InputTextMessageContent(msg_text_custom, parse_mode=ParseMode.HTML),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(btn_lbl_txt, callback_data=f"share:c:{code_text}")],
                ]),
            )
        )

        await iq.answer(results, cache_time=1, is_personal=True)
        return

    q = raw_query.lower()
    sort_mode = _current_sort_mode(context)
    raw_subs = await store.list_subs_meta(user_id)
    index_of = stable_index_map(raw_subs)
    subs = sort_subs(raw_subs, sort_mode)
    results: list[InlineQueryResultArticle] = []
    for sub in subs:
        blob = f"{sub['name']} {sub['url']}".lower()
        if q and q not in blob:
            continue
        remain = remain_traffic_gb(sub)
        remain_s = f"{remain:.2f}GB" if remain is not None else "?GB"
        days = remain_text(sub.get("expire_at"))
        if days == "长期有效":
            days = "长期"
        # 标题编号与列表/私聊数字编号一致（按 id 顺序的序号），而不是数据库自增 id
        title = f"#{index_of.get(int(sub['id']), int(sub['id']))} {sub['name']} [{remain_s}] {days}"
        sub_url = str(sub["url"] or "")
        # 本地导入的订阅没有可用的远程链接（uploaded://…），改为发送 Bot 生成的专属订阅地址
        send_url = sub_token_url(sub["token"], "clash") if sub_url.startswith("uploaded://") else sub_url
        results.append(
            InlineQueryResultArticle(
                id=f"s{int(sub['id'])}",
                title=title[:64],
                description=send_url[:100],
                input_message_content=InputTextMessageContent(truncate_utf16(send_url, TG_MESSAGE_LIMIT)),
            )
        )
    for t in await store.list_temp(user_id):
        blob = f"{t['name']} {t['url']}".lower()
        if q and q not in blob:
            continue
        results.append(
            InlineQueryResultArticle(
                id=f"t{int(t['id'])}",
                title=f"🧪 {t['name']}"[:64],
                description=t["url"][:100],
                input_message_content=InputTextMessageContent(t["url"]),
            )
        )
    await iq.answer(results[:50], cache_time=10, is_personal=True)


async def on_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """当用户把内联私密卡片发送到聊天中时，记录其 inline_message_id，以便核销后原地更新群消息。"""
    cir = update.chosen_inline_result
    if not cir or not cir.inline_message_id:
        return
    res_id = cir.result_id or ""
    # 我们生成的 id 形如 share_sub_{code} 或 share_txt_{code}
    if res_id.startswith("share_sub_") or res_id.startswith("share_txt_"):
        code = res_id.split("_", 2)[-1]
        share = await store.get_share(code)
        sender = getattr(getattr(cir, "from_user", None), "id", None)
        # 只允许分享的创建者本人绑定，且不覆盖已绑定的卡片
        if not share or share.get("sender_id") != sender or share.get("inline_message_id"):
            return
        await store.bind_inline_message_id(code, cir.inline_message_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """全局异常兜底：记录完整堆栈，并尽量给用户一个可见反馈，而不是按钮一直转圈/毫无回应。"""
    log.error("Unhandled error while processing update", exc_info=context.error)
    if not isinstance(update, Update):
        return
    with suppress(Exception):
        if update.callback_query is not None:
            await update.callback_query.answer("⚠️ 操作失败，请稍后重试。", show_alert=True)
        elif update.effective_message is not None and update.effective_chat is not None and update.effective_chat.type == "private":
            await update.effective_message.reply_text("⚠️ 处理出错，请稍后重试。")


def build_app() -> Application:
    # 全局关掉链接预览：这个 bot 发的几乎全是订阅链接/节点列表/GitHub 链接，
    # Telegram 自动拽出来的预览卡纯属噪音。在 Defaults 里设一次，省掉逐个调用点传参
    # （已显式传 disable_web_page_preview 的 18 处照旧生效，互斥检查在调用层先跑，不冲突）。
    # 注意：绝不在这里设全局 parse_mode —— 大量 reply_text 发的是纯文本提示，
    # 一旦被当 HTML 解析，用户输入里带个 < 或 & 就会让整条消息发送失败。
    defaults = Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True))
    # concurrent_updates：默认串行处理所有更新，一次「更新所有」(最慢 25s/条) 或文档导入
    # 会把其他用户的全部交互卡住。数据库层已有全局锁，开启并发是安全的。
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).concurrent_updates(True).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("expire", cmd_expire))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("short", cmd_short))
    app.add_handler(CommandHandler("path", cmd_path))
    app.add_handler(CommandHandler("renumber", cmd_renumber))
    app.add_handler(CommandHandler("s", cmd_search))
    app.add_handler(CommandHandler("g", cmd_github_search))
    app.add_handler(CommandHandler("o", cmd_search_export))
    app.add_handler(CommandHandler("d", cmd_delete_match))
    app.add_handler(CommandHandler("i", cmd_inline_sort))
    app.add_handler(CommandHandler("ai", cmd_api_test))
    app.add_handler(CommandHandler("temp", cmd_temp))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(ChosenInlineResultHandler(on_chosen_inline_result))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def check_and_notify_alerts(
    bot: Any,
    user_id: int,
    subs: list[dict[str, Any]] | None = None,
    *,
    cooldown_seconds: int = 86400,
) -> list[str]:
    """检查用户订阅的临期与低流量状态，24h 内同一订阅同一告警只推送一次。"""
    if subs is None:
        subs = await store.list_subs_meta(user_id)
    now = int(time.time())
    alerts: list[str] = []
    expire_threshold = max(1, int(config.ALERT_EXPIRE_DAYS)) * 86400
    traffic_threshold = float(max(1, int(config.ALERT_TRAFFIC_GB)))

    for sub in subs:
        sub_id = int(sub["id"])
        name = html.escape(str(sub.get("name") or "未命名"))
        exp = sub.get("expire_at")
        if exp is not None and 0 < int(exp) - now <= expire_threshold:
            key = f"alert_exp:{user_id}:{sub_id}"
            last_sent = int(await store.get_meta(key) or "0")
            if now - last_sent >= cooldown_seconds:
                days_left = max(0, (int(exp) - now) // 86400)
                hours_left = max(0, ((int(exp) - now) % 86400) // 3600)
                alerts.append(f"⏳ <b>{name}</b> 即将到期（剩余 {days_left}天{hours_left}小时）")
                await store.set_meta(key, str(now))

        remain = remain_traffic_gb(sub)
        total = sub.get("traffic_total")
        if total is not None and float(total) > 0 and remain is not None and 0 < remain <= traffic_threshold:
            key = f"alert_trf:{user_id}:{sub_id}"
            last_sent = int(await store.get_meta(key) or "0")
            if now - last_sent >= cooldown_seconds:
                alerts.append(f"📉 <b>{name}</b> 剩余流量不足（仅剩 {remain:.2f} GB）")
                await store.set_meta(key, str(now))

    if alerts and bot is not None:
        header = "🔔 <b>订阅巡检提醒</b>\n\n"
        footer = "\n\n<i>发送 /list 或 /expire 查看详情。</i>"
        # 订阅很多时单条提醒可能超过 4096 字符而发送失败（且冷却标记已写入，提醒会静默丢失），按预算分片
        chunks: list[list[str]] = [[]]
        used = 0
        for a in alerts:
            line = f"• {a}"
            cost = _utf16_len(line) + 1
            if chunks[-1] and used + cost > TG_SAFE_BODY - 200:
                chunks.append([])
                used = 0
            chunks[-1].append(line)
            used += cost
        for part in chunks:
            with suppress(Exception):
                await bot.send_message(
                    chat_id=user_id,
                    text=header + "\n".join(part) + footer,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
    return alerts


async def run_scheduled_maintenance(bot: Any = None) -> dict[str, int]:
    """后台定时巡检：清理过期数据、并发刷新活跃用户的网络订阅并推送临期/低流量提醒。"""
    cleaned = await store.cleanup_expired()
    user_ids = await store.list_active_user_ids()
    sem = asyncio.Semaphore(max(1, UPDATE_CONCURRENCY))
    refreshed_count = 0
    alert_count = 0

    for uid in user_ids:
        if not await allowed(uid):
            continue
        subs = await store.list_subs_meta(uid)
        remote_subs = [s for s in subs if not str(s.get("url") or "").startswith("uploaded://")]

        async def _refresh_item(s: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                try:
                    # 后台巡检绝不覆盖用户手动设置的名称（rename=False）
                    return await refresh_sub(uid, s, rename=False, meta_only=True)
                except Exception as exc:
                    log.warning("scheduled refresh failed for sub #%s: %s", s.get("id"), exc)
                    return s

        if remote_subs:
            await asyncio.gather(*(_refresh_item(s) for s in remote_subs))
            refreshed_count += len(remote_subs)

        latest_subs = await store.list_subs_meta(uid)
        triggered = await check_and_notify_alerts(bot, uid, latest_subs)
        alert_count += len(triggered)

    return {
        "cleaned_shares": cleaned.get("shares", 0),
        "cleaned_deleted": cleaned.get("deleted_subs", 0),
        "refreshed_subs": refreshed_count,
        "alerts_sent": alert_count,
    }


async def _maintenance_loop(bot: Any) -> None:
    """后台定时任务循环。每小时清理一次过期分享/回收站，按 AUTO_REFRESH_HOURS 周期巡检订阅。"""
    last_refresh_ts = time.monotonic()
    while True:
        await asyncio.sleep(3600)
        with suppress(Exception):
            await store.cleanup_expired()
        hours = int(config.AUTO_REFRESH_HOURS)
        if hours > 0 and (time.monotonic() - last_refresh_ts) >= hours * 3600:
            last_refresh_ts = time.monotonic()
            try:
                stats = await run_scheduled_maintenance(bot)
                log.info("Scheduled maintenance completed: %s", stats)
            except Exception as exc:
                log.warning("Scheduled maintenance failed: %s", exc)


async def amain() -> None:
    app = build_app()
    # 先把建表/迁移一次性做完，之后所有查询共享同一条连接
    await store.init()
    runner = await start_http(app)
    maint_task: asyncio.Task[None] | None = None
    try:
        log.info("Bot starting...")
        await app.initialize()
        await app.start()

        # 启动后从官方自动校准真实 Bot Username，并注册原生命令菜单
        try:
            me = await app.bot.get_me()
            if me and me.username:
                get_bot_username(me)
                log.info("Bot username calibrated: @%s", CURRENT_BOT_USERNAME)
            await app.bot.set_my_commands([
                BotCommand("list", "📦 查看订阅列表"),
                BotCommand("expire", "🟠 查看临期订阅"),
                BotCommand("export", "📤 聚合导出全部节点"),
                BotCommand("temp", "🧪 管理临时节点"),
                BotCommand("path", "🧭 节点路径名称映射"),
                BotCommand("s", "🔍 搜索订阅 (/s 关键词)"),
                BotCommand("o", "📦 导出搜索结果 (/o 关键词)"),
                BotCommand("d", "🗑 批量删除匹配订阅 (/d 关键词)"),
                BotCommand("i", "🔀 切换排序方式 (默认/流量/到期/名称)"),
                BotCommand("g", "🔎 搜索 GitHub 开源仓库与代码"),
                BotCommand("ai", "🤖 批量测试 OpenAI 兼容 API"),
                BotCommand("auth", "🔐 授权管理 (仅管理员私聊)"),
                BotCommand("help", "❓ 查看帮助菜单"),
            ])
        except Exception as e:
            log.warning("Failed to calibrate bot username or commands from API: %s", e)

        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        maint_task = asyncio.create_task(_maintenance_loop(app.bot))
        # systemd stop 发送 SIGTERM：默认会直接杀进程、跳过下面的 finally（不关 DB/HTTP/会话）。
        # 注册信号处理，收到后优雅退出；Windows 不支持 add_signal_handler，回退为 Ctrl+C。
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            with suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
        log.info("Shutdown signal received, stopping...")
    finally:
        if maint_task is not None:
            maint_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await maint_task
        with suppress(Exception):
            await app.updater.stop()
        with suppress(Exception):
            await app.stop()
        with suppress(Exception):
            await app.shutdown()
        with suppress(Exception):
            await runner.cleanup()
        with suppress(Exception):
            await store.close()
        with suppress(Exception):
            await close_session()


if __name__ == "__main__":
    asyncio.run(amain())
