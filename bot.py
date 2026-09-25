from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from contextlib import suppress
from typing import Any
from urllib.parse import quote_plus, urlparse

import aiohttp
from aiohttp import web
from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
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

from config import (
    ALLOWED_USER_IDS,
    BOT_TOKEN,
    BOT_USERNAME,
    GITHUB_TOKEN,
    HTTP_HOST,
    HTTP_PORT,
    MAX_DOCUMENT_BYTES,
    MAX_IMPORTED_NODES,
    MAX_IMPORTED_SUBSCRIPTIONS,
    PUBLIC_BASE_URL,
    UPDATE_CONCURRENCY,
)
from convert import (
    apply_path_maps,
    close_session,
    extract_share_links,
    fetch_subscription,
    format_bytes_gb,
    format_expire,
    node_name_from_url,
    parse_nodes_from_text,
    remain_text,
    to_base64_sub,
    to_clash,
    to_qx,
    to_share_links,
    to_singbox,
    to_surge,
)
from db import Store, nodes_of

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


def _decode_document(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📦 订阅列表", "🟠 临期列表"],
        ["📤 导出链接", "🔄 更新所有"],
        ["🔢 重置编号", "♻️ 撤销删除"],
        ["🧭 路径对应", "❓ 帮助菜单"],
    ],
    resize_keyboard=True,
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
    "名称优先使用订阅返回的配置名称\n\n"
    "<b>临时节点</b>\n"
    "发送 ss/vmess/vless/... 链接加入临时列表\n"
    "/temp - 查看临时节点\n"
    "/temp clear - 清空临时节点\n\n"
    "<b>路径对应</b>\n"
    "格式：路径关键词 空格 配置名称\n"
    "例如：liangxinyun 良心云\n"
    "/path clear - 清空全部\n\n"
    "发送数字编号可查看订阅详情。"
)


def allowed(user_id: int | None) -> bool:
    return bool(user_id) and (not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS)


async def deny(update: Update) -> bool:
    if update.effective_user and allowed(update.effective_user.id):
        return False
    text = "此 Bot 需要授权使用\n授权请联系管理员"
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    return True


def sub_token_url(token: str, kind: str = "clash") -> str:
    return f"{PUBLIC_BASE_URL}/sub/{token}/{kind}"


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
        days = "未知"
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


def detail_text(sub: dict[str, Any], idx: int) -> str:
    nodes = nodes_of(sub)
    used = sub.get("traffic_used")
    total = sub.get("traffic_total")
    remain_traffic = None
    if used is not None and total is not None:
        remain_traffic = max(total - used, 0)

    # 采用可展开的引用块 (expandable blockquote)，直接展示全量节点，客户端可原生点击下拉展开
    # 为防极端大库（例如上千节点）撑爆 Telegram 单消息 4096 字符硬限制，安全展示前 60 个
    display_limit = 60
    display_nodes = nodes[:display_limit]
    node_lines = [
        f"• {html.escape(str(n.get('name') or '未命名'))}" for n in display_nodes
    ]
    if not node_lines:
        node_preview = "未解析到节点"
    else:
        if len(nodes) > display_limit:
            node_lines.append(f"\n<i>（前 {display_limit} 个已展示，点击下方按钮可翻页浏览全量 {len(nodes)} 个节点）</i>")
        node_preview = "\n".join(node_lines)

    err = sub.get("last_error")
    err_line = f"\n⚠️ 刷新错误: {html.escape(err)}" if err else ""
    return (
        f"配置名称: {html.escape(sub['name'])}\n"
        f"订阅链接: {html.escape(sub['url'])}\n"
        f"流量详情: {html.escape(format_bytes_gb(used))} / {html.escape(format_bytes_gb(total))}\n"
        f"使用进度: {html.escape(progress_bar(used, total))}\n"
        f"剩余可用: {html.escape(format_bytes_gb(remain_traffic))}\n"
        f"过期时间: {html.escape(format_expire(sub.get('expire_at')))}\n"
        f"剩余时间: {html.escape(remain_text(sub.get('expire_at')))}\n"
        f"📡 节点列表 ({len(nodes)} 个，支持下拉展开):\n<blockquote expandable>{node_preview}</blockquote>"
        f"{err_line}"
    )


def detail_keyboard(sub_id: int, page: int = 0, node_count: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Clash Meta", callback_data=f"sub:fmt:{sub_id}:clash"),
            InlineKeyboardButton("Sing-box", callback_data=f"sub:fmt:{sub_id}:singbox"),
        ],
        [
            InlineKeyboardButton("Base64", callback_data=f"sub:fmt:{sub_id}:base64"),
            InlineKeyboardButton("Surge", callback_data=f"sub:fmt:{sub_id}:surge"),
        ],
        [InlineKeyboardButton("QX", callback_data=f"sub:fmt:{sub_id}:qx")],
        [
            InlineKeyboardButton("🔄 刷新订阅", callback_data=f"sub:refresh:{sub_id}:{page}"),
            InlineKeyboardButton("✏️ 重命名", callback_data=f"sub:rename:{sub_id}:{page}"),
        ],
        [
            InlineKeyboardButton("📦 导出节点", callback_data=f"sub:nodes:{sub_id}"),
            InlineKeyboardButton("🔗 生成短链", callback_data=f"sub:short:{sub_id}"),
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


def list_keyboard(page: int, total: int, page_size: int = 8, items: list[tuple[int, dict[str, Any]]] | None = None) -> InlineKeyboardMarkup:
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
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"list:page:{page+1}"))
    rows.append(nav)
    rows.append([
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


async def render_list(user_id: int, page: int = 0, sort_mode: str = "默认") -> tuple[str, InlineKeyboardMarkup]:
    subs = await store.list_subs_meta(user_id)
    if sort_mode == "流量":
        subs.sort(key=lambda s: (s.get("traffic_total") or 0) - (s.get("traffic_used") or 0), reverse=True)
    elif sort_mode == "到期":
        subs.sort(key=lambda s: s.get("expire_at") or 99999999999)
    elif sort_mode == "名称":
        subs.sort(key=lambda s: str(s.get("name") or ""))
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
    items = [(start + i, sub) for i, sub in enumerate(chunk, start=1)]
    text = f"📁 <b>订阅列表 共{len(subs)}个 | 🟠{soon_count}个临期 | 第{page + 1}/{total_pages}页</b>"
    return text, list_keyboard(page, len(subs), page_size, items)


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
    await update.effective_message.reply_text(
        f"👋 你好，{name}！\n\n"
        "🤖 订阅管理机器人\n"
        "发送订阅链接，或发送文本 / 上传 .txt、.log、.yaml、.yml 文件，"
        "可进行订阅查询、节点解析、导出节点和短链生成。\n"
        "更多内容请查看帮助菜单。",
        reply_markup=MAIN_KB,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    await update.effective_message.reply_html(HELP_TEXT, reply_markup=MAIN_KB, disable_web_page_preview=True)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text, kb = await render_list(update.effective_user.id, 0)
    await update.effective_message.reply_html(text, reply_markup=kb, disable_web_page_preview=True)


async def cmd_expire(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    subs = await store.list_subs_meta(update.effective_user.id)
    now = int(time.time())
    soon = []
    for i, sub in enumerate(subs, start=1):
        exp = sub.get("expire_at")
        if not exp:
            continue
        if 0 <= exp - now <= 14 * 86400:
            soon.append((i, sub))
    if not soon:
        nav = [[
            InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
            InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
        ]]
        await update.effective_message.reply_text(
            "🟠 临期列表为空，当前没有 14 天内到期的订阅。",
            reply_markup=InlineKeyboardMarkup(nav),
        )
        return
    text = f"🟠 <b>临期列表 共{len(soon)}个 | 第1/1页</b>"
    kb = list_keyboard(0, len(soon), 8, soon)
    await update.effective_message.reply_html(text, reply_markup=kb)


async def cmd_update_all_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    user_id = update.effective_user.id
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
    user_id = update.effective_user.id
    subs = await store.list_subs_meta(user_id)
    if not subs:
        await context.bot.send_message(chat_id=user_id, text="暂无订阅可更新。", reply_markup=MAIN_KB)
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
                results[idx] = await refresh_sub(
                    user_id, sub, rename=True, meta_only=True
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
        await context.bot.send_document(chat_id=user_id, document=doc)
    stats = f"有效:{len(valid)} | 耗尽:{len(drained)} | 过期:{len(expired)} | 失败:{len(failed)}"
    if failed:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑 删除失效订阅", callback_data="upd:delfailed"),
                InlineKeyboardButton("↩️ 暂不删除", callback_data="upd:keepfailed"),
            ],
            [
                InlineKeyboardButton("📦 订阅列表", callback_data="list:home"),
                InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
            ],
        ])
        await context.bot.send_message(
            chat_id=user_id,
            text=f"{stats}\n\n检测到 {len(failed)} 条失效订阅，是否从订阅列表中删除？",
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(chat_id=user_id, text=stats, reply_markup=MAIN_KB)
        text_list, kb_list = await render_list(user_id, 0)
        await context.bot.send_message(chat_id=user_id, text=text_list, reply_markup=kb_list, parse_mode=ParseMode.HTML)


async def cmd_recycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    rows = await store.list_deleted(update.effective_user.id, days=30)
    nav = [[
        InlineKeyboardButton("📦 订阅列表", callback_data="list:page:0"),
        InlineKeyboardButton("🏠 主菜单", callback_data="list:home"),
    ]]
    if not rows:
        await update.effective_message.reply_text(
            "♻️ 订阅回收站为空，最近30天内没有可恢复的订阅。",
            reply_markup=InlineKeyboardMarkup(nav),
        )
        return
    lines = [f"♻️ <b>订阅回收站</b>（{len(rows)}）\n"]
    keyboard = []
    for i, row in enumerate(rows[:12], start=1):
        lines.append(f"#{i} {html.escape(row['name'])}")
        keyboard.append([
            InlineKeyboardButton(
                f"恢复 #{i} {row['name']}"[:64],
                callback_data=f"recycle:restore:{int(row['id'])}",
            )
        ])
    keyboard.append([InlineKeyboardButton("🏠 主菜单", callback_data="list:home")])
    await update.effective_message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    user_id = update.effective_user.id
    subs = await store.list_subs(user_id)
    maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(user_id)}
    all_nodes: list[dict[str, Any]] = []
    for sub in subs:
        nodes = nodes_of(sub)
        if not nodes:
            sub = await refresh_sub(user_id, sub)
            nodes = nodes_of(sub)
        all_nodes.extend(apply_path_maps(nodes, maps))
    temp = await store.list_temp(user_id)
    for t in temp:
        all_nodes.append({"name": t["name"], "share": t["url"], "type": "temp"})
    if not all_nodes:
        await update.effective_message.reply_text("暂无可导出节点。", reply_markup=MAIN_KB)
        return
    code = await store.create_short(user_id, "aggregate://all")
    clash_url = f"{PUBLIC_BASE_URL}/agg/{code}/clash"
    base64_url = f"{PUBLIC_BASE_URL}/agg/{code}/base64"
    await update.effective_message.reply_html(
        f"📦 <b>导出完成</b>\n节点数: {len(all_nodes)}\n\n"
        f"Clash:\n<code>{html.escape(clash_url)}</code>\n"
        f"Base64:\n<code>{html.escape(base64_url)}</code>\n\n"
        f"短链码: <code>{html.escape(code)}</code>",
        reply_markup=MAIN_KB,
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


def path_keyboard(maps: list[dict[str, Any]] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in maps or []:
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
    for row in maps:
        lines.append(f"• <code>{html.escape(row['node_name'])}</code> → {html.escape(row['remark'])}")
    return "\n".join(lines), path_keyboard(maps)


async def cmd_path(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    args = context.args or []
    user_id = update.effective_user.id
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
    n = await store.renumber(update.effective_user.id)
    await update.effective_message.reply_text(f"已重置编号，当前 {n} 条订阅。", reply_markup=MAIN_KB)


async def cmd_delete_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip().lower()
    if not q:
        await update.effective_message.reply_text("用法：/d 关键词或订阅链接", reply_markup=MAIN_KB)
        return
    subs = await store.list_subs_meta(update.effective_user.id)
    hits = [s for s in subs if q in f"{s['name']} {s['url']}".lower()]
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    for sub in hits:
        await store.delete_sub(update.effective_user.id, int(sub["id"]))
    await update.effective_message.reply_text(f"✅ 已删除 {len(hits)} 个匹配订阅。", reply_markup=MAIN_KB)


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
    if context.user_data is not None:
        context.user_data["await"] = "inline_sort"
    current = (context.user_data or {}).get("sort_mode", "默认")
    await update.effective_message.reply_text(
        f"当前排序：{current}\n请发送新的排序方式（默认 / 流量 / 到期 / 名称）。",
        reply_markup=MAIN_KB,
    )


async def cmd_api_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    if context.user_data is not None:
        context.user_data["await"] = "api_test"
    await update.effective_message.reply_text(
        "🧪 API 批量测试\n请发送要测试的 API 地址和 Key，每行一组：\n地址|Key",
        reply_markup=MAIN_KB,
    )


async def cmd_search_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip().lower()
    # 先用瘦行筛选，再只回取命中的完整节点数据。否则搜一个普通订阅也会先把
    # 其它巨型订阅的几十 MB nodes_json 全搬进内存。
    subs = await store.list_subs_meta(update.effective_user.id)
    hits = [s for s in subs if not q or q in f"{s['name']} {s['url']}".lower()]
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    nodes = []
    for hit in hits:
        sub = await store.get_sub(update.effective_user.id, int(hit["id"]))
        if sub is not None:
            nodes.extend(nodes_of(sub))
    if not nodes:
        await update.effective_message.reply_text("匹配订阅暂无可导出节点。", reply_markup=MAIN_KB)
        return
    code = await store.create_short(
        update.effective_user.id,
        "aggregate://ids/" + ",".join(str(int(hit["id"])) for hit in hits),
    )
    await update.effective_message.reply_text(
        f"📦 搜索导出完成：{len(hits)} 个订阅，{len(nodes)} 个节点\n"
        f"{PUBLIC_BASE_URL}/agg/{code}/clash",
        reply_markup=MAIN_KB,
    )



async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    q = " ".join(context.args or []).strip()
    if not q:
        await update.effective_message.reply_text("用法：/s 关键词", reply_markup=MAIN_KB)
        return
    subs = await store.list_subs_meta(update.effective_user.id)
    hits = []
    for i, sub in enumerate(subs, start=1):
        blob = f"{sub['name']} {sub['url']}".lower()
        if q.lower() in blob:
            hits.append(sub_summary_line(i, sub))
    if not hits:
        await update.effective_message.reply_text("没有匹配订阅。", reply_markup=MAIN_KB)
        return
    await update.effective_message.reply_html("🔍 搜索结果\n\n" + "\n\n".join(hits), reply_markup=MAIN_KB)


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
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            # 1. 优先搜索高赞开源仓库 (免 Token 100% 可用)
            repo_url = f"https://api.github.com/search/repositories?q={quote_plus(query)}&sort=stars&order=desc&per_page=5"
            try:
                async with session.get(repo_url) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        repos_results = payload.get("items") or []
            except Exception as e:
                log.warning("GitHub repo search failed: %s", e)

            # 2. 搜索公开代码 (有 Token 优先，无 Token 带扩展名)
            code_query = query
            if not GITHUB_TOKEN and " " not in query:
                code_query = f"{query} extension:yaml extension:txt"
            code_url = f"https://api.github.com/search/code?q={quote_plus(code_query)}&per_page=5"
            try:
                async with session.get(code_url) as resp:
                    if resp.status == 200:
                        payload = await resp.json(content_type=None)
                        code_results = payload.get("items") or []
            except Exception as e:
                log.warning("GitHub code search failed: %s", e)

            # 3. 如果代码为空，尝试备用 grep.app
            if not code_results:
                try:
                    async with session.get(
                        f"https://grep.app/api/search?q={quote_plus(query)}&regexp=false"
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
            html_url = r.get("html_url") or ""
            stars = r.get("stargazers_count", 0)
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


async def cmd_temp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    args = context.args or []
    user_id = update.effective_user.id
    if args and args[0].lower() == "clear":
        n = await store.clear_temp(user_id)
        await update.effective_message.reply_text(f"已清空临时节点 {n} 条。", reply_markup=MAIN_KB)
        return
    rows = await store.list_temp(user_id)
    if not rows:
        await update.effective_message.reply_text("临时列表为空。", reply_markup=MAIN_KB)
        return
    lines = [f"🧪 临时节点 ({len(rows)})\n"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"#{i} {html.escape(r['name'])}\n<code>{html.escape(r['url'])}</code>")
    await update.effective_message.reply_html("\n\n".join(lines), reply_markup=MAIN_KB)


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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny(update):
        return
    text = (update.effective_message.text or "").strip()
    user_id = update.effective_user.id
    if not text:
        return

    # ── 交互状态机 ─────────────────────────────────
    state = context.user_data.get("await") if context.user_data else None
    # ReplyKeyboard 菜单按钮优先级最高：点击菜单时取消正在等待的输入。
    menu_texts = {
        "📦 订阅列表", "📋 订阅列表", "订阅列表", "⏳ 临期列表", "🟠 临期列表",
        "临期列表", "🔗 短链", "短链", "📦 导出", "📤 导出链接", "导出", "导出链接",
        "🔄 更新所有", "更新所有", "🔢 重置编号", "重置编号", "♻️ 撤销删除", "撤销删除",
        "🧭 路径对应", "路径对应", "❓ 帮助菜单", "帮助菜单", "帮助",
    }
    if state and text in menu_texts and context.user_data is not None:
        context.user_data.pop("await", None)
        state = None

    if text.lower() in ("取消", "/cancel", "cancel", "退出"):
        if state and context.user_data is not None:
            context.user_data.pop("await", None)
            await update.effective_message.reply_text("已取消当前操作。", reply_markup=MAIN_KB)
            return

    # ── 订阅重命名状态机 ─────────────────────────
    if state == "sub_rename":
        sub_id = (context.user_data or {}).pop("rename_sub_id", None)
        page = (context.user_data or {}).pop("rename_page", 0)
        context.user_data.pop("await", None)
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

    # ── 页码跳转状态机 ──────────────────────────────
    if state == "list_jump":
        context.user_data.pop("await", None)
        if not text.isdigit():
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
        text_out, kb = await render_list(user_id, page)
        await update.effective_message.reply_html(text_out, reply_markup=kb, disable_web_page_preview=True)
        return

    # ── 路径对应新增状态机 ─────────────────────────
    if state == "path_add":
        rule = parse_path_rule(text)
        if rule is None:
            # 保留等待状态，输错后可直接重试，不用重新点“添加对应”。
            await update.effective_message.reply_text(PATH_ADD_PROMPT)
            return
        context.user_data.pop("await", None)
        await store.upsert_path_map(user_id, *rule)
        page_text, page_kb = await render_path_page(user_id)
        await update.effective_message.reply_html(
            f"✅ 已添加路径对应：<code>{html.escape(rule[0])}</code> → {html.escape(rule[1])}\n\n{page_text}",
            reply_markup=page_kb,
        )
        return

    # ── /ai API 测试状态机 ─────────────────────────
    if state == "api_test":
        context.user_data.pop("await", None)
        results = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                api_url, _, key = line.partition("|")
            else:
                parts = line.split()
                api_url, key = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            api_url = api_url.strip().rstrip("/")
            key = key.strip()
            if not re.match(r"^https?://", api_url) or not key:
                results.append("⚠️ 格式错误：请用 API地址|Key（每行一组）")
                continue
            # 兼容 OpenAI 兼容服务：优先探测 /v1/models，根地址则自动补路径。
            probe_url = api_url if api_url.rstrip("/").endswith(("/models", "/chat/completions")) else api_url + "/v1/models"
            t0 = time.time()
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        probe_url,
                        headers={"Authorization": f"Bearer {key}", "X-API-Key": key},
                    ) as resp:
                        elapsed = time.time() - t0
                        status = resp.status
                        body = (await resp.text())[:120].replace("\n", " ")
                # 不回显 Key；只展示状态、延迟和有限响应摘要。
                mark = "✅" if 200 <= status < 300 else "❌"
                results.append(f"{mark} {api_url} → HTTP {status} ({elapsed:.1f}s) {body}")
            except Exception as exc:
                elapsed = time.time() - t0
                results.append(f"❌ {api_url} → {type(exc).__name__} ({elapsed:.1f}s)")
        if not results:
            await update.effective_message.reply_text("未解析到任何 API 地址和 Key。", reply_markup=MAIN_KB)
            return
        await update.effective_message.reply_text("\n".join(results[:20]), reply_markup=MAIN_KB)
        return

    # ── 内联排序状态机 ─────────────────────────────
    if state == "inline_sort":
        context.user_data.pop("await", None)
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
    if text in ("🧭 路径对应", "路径对应"):
        return await cmd_path(update, context)
    if text in ("❓ 帮助菜单", "帮助菜单", "帮助"):
        return await cmd_help(update, context)

    if text.isdigit():
        return await show_detail(update, user_id, int(text))

    if text.startswith("短链 ") or text.lower().startswith("short "):
        target = text.split(" ", 1)[1].strip()
        if not re.match(r"^https?://", target):
            await update.effective_message.reply_text("短链目标必须是 http/https 链接。")
            return
        code = await store.create_short(user_id, target)
        await update.effective_message.reply_html(
            f"✅ 短链已生成\n<code>{html.escape(PUBLIC_BASE_URL + '/s/' + code)}</code>",
            reply_markup=MAIN_KB,
        )
        return

    if "=" in text and not text.lower().startswith("http") and "://" not in text.split("=", 1)[0]:
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
        if re.match(r"^https?://", url):
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
                    f"🔄 <b>订阅已存在，已为您自动更新！</b>\n\n#{idx} <b>{html.escape(sub['name'])}</b>\n节点数: {sub['node_count']}",
                    reply_markup=MAIN_KB,
                )
                return

            sub = await store.add_sub(user_id, name or urlparse(url).netloc or "订阅", url)
            sub = await refresh_sub(
                user_id, sub, rename=not bool(name), meta_only=True
            )
            await update.effective_message.reply_html(
                f"✅ 已添加订阅 <b>{html.escape(sub['name'])}</b>\n节点: {sub['node_count']}",
                reply_markup=MAIN_KB,
            )
            return

    # subscription urls
    urls = re.findall(r"https?://\S+", text)
    if urls and not extract_share_links(text):
        added = []
        updated_list = []
        existing_subs = await store.list_subs_meta(user_id)
        existing_url_map = {s["url"].rstrip("/"): s for s in existing_subs}

        for url in urls:
            clean_url = url.rstrip("/")
            if clean_url in existing_url_map:
                old_sub = existing_url_map[clean_url]
                refreshed = await refresh_sub(
                    user_id, old_sub, rename=True, meta_only=True
                )
                updated_list.append(f"{refreshed['name']} ({refreshed['node_count']} 节点)")
            else:
                name = urlparse(url).netloc or "订阅"
                sub = await store.add_sub(user_id, name, url)
                sub = await refresh_sub(user_id, sub, rename=True, meta_only=True)
                added.append(f"{sub['name']} ({sub['node_count']} 节点)")

        msg_lines = []
        if added:
            msg_lines.append("✅ 已添加新增订阅:\n" + "\n".join(added))
        if updated_list:
            msg_lines.append("🔄 检测到重复订阅，已为您自动更新:\n" + "\n".join(updated_list))
        await update.effective_message.reply_text("\n\n".join(msg_lines), reply_markup=MAIN_KB)
        return

    # node share links
    links = extract_share_links(text)
    if links:
        for link in links:
            await store.add_temp(user_id, link, node_name_from_url(link))
        await update.effective_message.reply_text(
            f"✅ 已加入临时列表 {len(links)} 条。发送 /temp 查看。",
            reply_markup=MAIN_KB,
        )
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
        text = _decode_document(raw)
    except Exception:
        log.exception("document download failed")
        await status.edit_text("文件读取失败，请稍后重试。")
        return

    user_id = update.effective_user.id
    existing_urls = {sub["url"] for sub in await store.list_subs_meta(user_id)}
    parsed_nodes = parse_nodes_from_text(text)
    structured = bool(parsed_nodes and ("proxies:" in text or "outbounds" in text))
    created: list[tuple[str, int]] = []
    failures: list[str] = []
    skipped = 0
    temp_count = 0

    if structured:
        name = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0][:64] or "导入配置"
        # 结构化配置去重：如果已有同名本地配置，直接更新其节点，避免重复创建
        existing_imported = [s for s in await store.list_subs_meta(user_id) if s.get("name") == name and s.get("url", "").startswith("uploaded://")]
        if existing_imported:
            old_sub = existing_imported[0]
            await store.update_sub(user_id, int(old_sub["id"]), nodes_json=json.dumps(parsed_nodes[:MAX_IMPORTED_NODES], ensure_ascii=False))
            created.append((f"{name} (已覆盖更新)", len(parsed_nodes[:MAX_IMPORTED_NODES])))
        else:
            local = await store.add_imported_sub(user_id, name, parsed_nodes[:MAX_IMPORTED_NODES])
            created.append((str(local["name"]), len(parsed_nodes[:MAX_IMPORTED_NODES])))
    else:
        existing_url_norm = {sub["url"].rstrip("/"): sub for sub in await store.list_subs_meta(user_id)}
        urls = _unique(value.rstrip(".,;:!?)]}>\"\'") for value in re.findall(r"https?://[^\s<>\"]+", text))
        for url in urls[:MAX_IMPORTED_SUBSCRIPTIONS]:
            clean_url = url.rstrip("/")
            if clean_url in existing_url_norm:
                old_sub = existing_url_norm[clean_url]
                try:
                    updated = await refresh_sub(
                        user_id, old_sub, rename=True, meta_only=True
                    )
                    created.append((f"{updated['name']} (已更新)", updated['node_count']))
                except Exception:
                    skipped += 1
                continue
            name = urlparse(url).netloc or "订阅"
            try:
                sub = await store.add_sub(user_id, name, url)
                updated = await refresh_sub(
                    user_id, sub, rename=True, meta_only=True
                )
                if updated.get("last_error"):
                    failures.append(f"{name}: {updated['last_error']}")
                else:
                    created.append((str(updated.get("name") or name), updated['node_count']))
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        links = _unique(extract_share_links(text))[:MAX_IMPORTED_NODES]
        for link in links:
            await store.add_temp(user_id, link, node_name_from_url(link))
        temp_count = len(links)

    lines = [f"✅ 已处理文件：{html.escape(filename)}"]
    for name, count in created:
        lines.append(f"已添加：{html.escape(name)}（{count} 节点）")
    if temp_count:
        lines.append(f"已加入临时节点：{temp_count} 条")
    if skipped:
        lines.append(f"已跳过重复订阅：{skipped} 条")
    if failures:
        lines.append("失败：" + "；".join(html.escape(item) for item in failures[:8]))
    if not created and not temp_count and not failures and not skipped:
        lines.append("未识别到订阅或节点内容。")
    await status.edit_text("\n".join(lines), reply_markup=MAIN_KB, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    user_id = update.effective_user.id if update.effective_user else 0

    # ── 公开私密分享领取：任何人均可参与，不受管理员白名单拦截 ─────────────────────
    if data.startswith("share:c:"):
        code = data.split(":")[-1]
        ok, msg, share = await store.claim_share(code, user_id)
        if not ok:
            await q.answer(msg, show_alert=True)
            if share and (share["claimed_count"] >= share["max_views"] or share["expire_at"] < int(time.time())):
                with suppress(Exception):
                    await q.edit_message_text(
                        f"🔒 <b>私密分享已失效</b>\n\n原因: {msg}\n来自: {html.escape(share.get('sender_name') or '用户')}",
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

        # 截断防超过 Telegram 弹窗字数限制
        await q.answer(alert_msg[:190], show_alert=True)

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

    await q.answer()
    if data == "noop":
        return
    if data == "list:home":
        await q.message.reply_text("🏠 主菜单", reply_markup=MAIN_KB)
        return
    if data == "list:jump":
        if context.user_data is not None:
            context.user_data["await"] = "list_jump"
        await q.message.reply_text("🔢 请输入要跳转的页码数字（如 2）。")
        return
    if data == "path:add":
        if context.user_data is not None:
            context.user_data["await"] = "path_add"
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
    if data.startswith("list:page:"):
        page = int(data.split(":")[-1])
        text, kb = await render_list(user_id, page)
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
            sub = await refresh_sub(user_id, sub, rename=True)
        subs = await store.list_subs_meta(user_id)
        idx = next((i for i, s in enumerate(subs, 1) if int(s["id"]) == sub_id), sub_id)
        await q.edit_message_text(
            detail_text(sub, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=detail_keyboard(sub_id, page, len(nodes_of(sub))),
            disable_web_page_preview=True,
        )
        return
    if data.startswith("recycle:restore:"):
        deleted_id = int(data.split(":")[-1])
        restored = await store.restore_deleted(user_id, deleted_id)
        if restored:
            await q.edit_message_text(f"✅ 已恢复：{restored['name']}")
        else:
            await q.edit_message_text("恢复失败，记录可能已过期。")
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
            f"<i>共 {len(nodes)} 个节点 ｜ 第 {node_page + 1}/{total_pages} 页</i>\n",
        ]
        for i, n in enumerate(chunk_nodes, start=start_idx + 1):
            n_type = html.escape(str(n.get("type") or "node")).upper()
            n_name = html.escape(str(n.get("name") or "未命名"))
            lines.append(f"<code>#{i:02d}</code> [{n_type}] <b>{n_name}</b>")

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
            InlineKeyboardButton("⬅️ 返回订阅详情", callback_data=f"sub:open:{sub_id}:{list_page}")
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
        text, kb = await render_list(user_id, page)
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
            sub = await refresh_sub(user_id, sub, rename=True)
        target = sub_token_url(sub["token"], kind)
        code = await store.create_short(user_id, target)
        short = f"{PUBLIC_BASE_URL}/s/{code}"
        title = {
            "clash": "Clash Meta",
            "singbox": "Sing-box",
            "base64": "Base64",
            "surge": "Surge",
            "qx": "QX",
        }.get(kind, kind)
        await q.message.reply_html(
            f"🔗 <b>{html.escape(title)}</b>\n<code>{html.escape(short)}</code>",
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
        sub = await refresh_sub(user_id, sub, rename=True)
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
        if context.user_data is not None:
            context.user_data["await"] = "sub_rename"
            context.user_data["rename_sub_id"] = sub_id
            context.user_data["rename_page"] = page
        await q.message.reply_text(
            f"✏️ 请发送订阅「<b>{html.escape(sub['name'])}</b>」的新名称：\n<i>（输入 取消 可退出）</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KB,
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
        text, kb = await render_list(user_id, page)
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
        code = await store.create_short(user_id, target)
        short = f"{PUBLIC_BASE_URL}/s/{code}"
        await q.message.reply_html(
            "🔗 <b>短链生成成功</b>\n"
            f"🏷️ 原始机场名称：{html.escape(sub['name'])}\n"
            f"🌐 原始订阅链接：{html.escape(sub['url'])}\n"
            f"🌟 生成短链地址：{html.escape(short)}\n"
            "📦 此短链会代理原始订阅，可直接导入客户端\n"
            "⚠️ 注意部分机场无法代理访问，请先自行尝试",
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
        doc = InputFile(content.encode("utf-8"), filename="Base64.txt")
        await q.message.reply_document(document=doc, caption="📦 节点导出完成")
        return


async def mapped_nodes_for_user(user_id: int, sub_ids: list[int] | None = None) -> list[dict[str, Any]]:
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
        for t in await store.list_temp(user_id):
            nodes.append({"name": t["name"], "type": "temp", "share": t["url"]})
    return nodes



async def http_sub(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    kind = request.match_info.get("kind", "clash").lower()
    sub = await store.get_sub_by_token(token)
    if not sub:
        return web.Response(status=404, text="not found")
    maps = {m["node_name"]: m["remark"] for m in await store.list_path_maps(int(sub["user_id"]))}
    nodes = apply_path_maps(nodes_of(sub), maps)
    if kind in ("base64", "v2ray", "mixed"):
        return web.Response(text=to_base64_sub(nodes), content_type="text/plain")
    if kind in ("singbox", "sing-box"):
        return web.Response(text=to_singbox(nodes), content_type="application/json")
    if kind == "surge":
        return web.Response(text=to_surge(nodes), content_type="text/plain")
    if kind in ("qx", "quantumult", "quantumultx"):
        return web.Response(text=to_qx(nodes), content_type="text/plain")
    return web.Response(text=to_clash(nodes), content_type="text/yaml")


async def http_agg(request: web.Request) -> web.Response:
    code = request.match_info["code"]
    row = await store.get_short(code)
    if not row:
        return web.Response(status=404, text="not found")
    target = str(row["target_url"])
    user_id = int(row["user_id"])
    if target == "aggregate://all":
        nodes = await mapped_nodes_for_user(user_id)
    elif target.startswith("aggregate://ids/"):
        raw_ids = target.removeprefix("aggregate://ids/")
        try:
            sub_ids = [int(value) for value in raw_ids.split(",") if value]
        except ValueError:
            return web.Response(status=400, text="invalid aggregate target")
        if not sub_ids:
            return web.Response(status=404, text="not found")
        nodes = await mapped_nodes_for_user(user_id, sub_ids)
    else:
        return web.Response(status=404, text="not found")
    kind = request.match_info.get("kind", "clash").lower()
    if kind in ("base64", "v2ray", "mixed"):
        return web.Response(text=to_base64_sub(nodes), content_type="text/plain")
    if kind in ("singbox", "sing-box"):
        return web.Response(text=to_singbox(nodes), content_type="application/json")
    if kind == "surge":
        return web.Response(text=to_surge(nodes), content_type="text/plain")
    if kind in ("qx", "quantumult", "quantumultx"):
        return web.Response(text=to_qx(nodes), content_type="text/plain")
    return web.Response(text=to_clash(nodes), content_type="text/yaml")




async def http_short(request: web.Request) -> web.Response:
    code = request.match_info["code"]
    row = await store.get_short(code)
    if not row:
        return web.Response(status=404, text="not found")
    target = row["target_url"]
    if target == "aggregate://all" or target.startswith("aggregate://ids/"):
        raise web.HTTPFound(f"/agg/{code}/clash")
    if not target.startswith(("http://", "https://")):
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
        if lower.startswith("x") and lower[1:].isdigit():
            max_views = max(1, min(100, int(lower[1:])))
        elif lower.startswith("id") and lower[2:].isdigit():
            target_user_id = int(lower[2:])
        elif lower.startswith("s") and lower[1:].isdigit():
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
    if not allowed(user_id):
        await iq.answer(
            [],
            cache_time=5,
            switch_pm_text="此 Bot 需要授权使用",
            switch_pm_parameter="denied",
        )
        return

    raw_query = (iq.query or "").strip()
    share_parsed = parse_share_query(raw_query)
    if share_parsed:
        max_views, target_user_id, duration_minutes, content = share_parsed
        subs = await store.list_subs_meta(user_id)
        results: list[InlineQueryResultArticle] = []

        # 1. 寻找可能匹配的订阅列表 (候选池)
        candidate_subs: list[tuple[int, dict[str, Any]]] = []
        clean_num = content.lstrip("#").strip()
        if clean_num.isdigit():
            idx = int(clean_num)
            if 1 <= idx <= len(subs):
                candidate_subs.append((idx, subs[idx - 1]))

        content_lower = content.lower()
        for i, s in enumerate(subs, 1):
            s_name_lower = str(s.get("name") or "").lower()
            if content_lower in s_name_lower or s_name_lower in content_lower:
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
            card_desc = f"原始链接: {sub['url'][:50]}…"
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
    sort_mode = (context.user_data or {}).get("sort_mode", "默认")
    subs = await store.list_subs_meta(user_id)
    if sort_mode == "流量":
        subs.sort(key=lambda s: (s.get("traffic_total") or 0) - (s.get("traffic_used") or 0), reverse=True)
    elif sort_mode == "到期":
        subs.sort(key=lambda s: s.get("expire_at") or 99999999999)
    elif sort_mode == "名称":
        subs.sort(key=lambda s: str(s.get("name") or ""))
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
        title = f"#{int(sub['id'])} {sub['name']} [{remain_s}] {days}"
        results.append(
            InlineQueryResultArticle(
                id=f"s{int(sub['id'])}",
                title=title[:64],
                description=sub["url"][:100],
                input_message_content=InputTextMessageContent(sub["url"]),
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
        await store.bind_inline_message_id(code, cir.inline_message_id)


def build_app() -> Application:
    # 全局关掉链接预览：这个 bot 发的几乎全是订阅链接/节点列表/GitHub 链接，
    # Telegram 自动拽出来的预览卡纯属噪音。在 Defaults 里设一次，省掉逐个调用点传参
    # （已显式传 disable_web_page_preview 的 18 处照旧生效，互斥检查在调用层先跑，不冲突）。
    # 注意：绝不在这里设全局 parse_mode —— 大量 reply_text 发的是纯文本提示，
    # 一旦被当 HTML 解析，用户输入里带个 < 或 & 就会让整条消息发送失败。
    defaults = Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()
    app.add_handler(CommandHandler("start", cmd_start))
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


async def amain() -> None:
    app = build_app()
    # 先把建表/迁移一次性做完，之后所有查询共享同一条连接
    await store.init()
    runner = await start_http(app)
    try:
        log.info("Bot starting...")
        await app.initialize()
        await app.start()

        # 启动后从官方自动校准真实 Bot Username，彻底杜绝跳错频道
        try:
            me = await app.bot.get_me()
            if me and me.username:
                get_bot_username(me)
                log.info("Bot username calibrated: @%s", CURRENT_BOT_USERNAME)
        except Exception as e:
            log.warning("Failed to calibrate bot username from API: %s", e)

        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        # run forever
        while True:
            await asyncio.sleep(3600)
    finally:
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
