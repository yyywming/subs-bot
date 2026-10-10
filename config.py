from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default
    if value < minimum:
        return default
    return min(value, maximum) if maximum is not None else value


TG_USER_ID_MAX = 2**63 - 1


def parse_tg_user_id(raw: str) -> int | None:
    """正整数 Telegram 用户 ID：1..2**63-1 的 ASCII 数字，无符号、无前导 0。

    超出 SQLite 有符号 int64 的值返回 None，避免写入时 OverflowError。
    """
    text = raw.strip()
    if not text or not text.isascii() or not text.isdigit() or text[0] == "0":
        return None
    value = int(text)
    if value > TG_USER_ID_MAX:
        return None
    return value


def parse_user_id_set(raw: str) -> set[int]:
    found: set[int] = set()
    for part in raw.split(","):
        parsed = parse_tg_user_id(part)
        if parsed is not None:
            found.add(parsed)
    return found


def resolve_admin_ids(allowed_ids: set[int], admin_raw: str | None) -> set[int]:
    """未设置 ADMIN_USER_IDS 时管理员等于白名单；显式设置（含空字符串）按原文解析。"""
    if admin_raw is None:
        return set(allowed_ids)
    return parse_user_id_set(admin_raw)



ROOT = Path(__file__).resolve().parent
_load_dotenv(ROOT / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("缺少 BOT_TOKEN：请在 .env 填写 BotFather 下发的 token")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "your_bot_username").lstrip("@")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALLOWED_USER_IDS = parse_user_id_set(os.environ.get("ALLOWED_USER_IDS", ""))
# 不把第一个使用者提升为管理员。管理员集合为空时，Bot 内授权管理关闭。
ADMIN_USER_IDS = resolve_admin_ids(ALLOWED_USER_IDS, os.environ.get("ADMIN_USER_IDS"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = _env_int("HTTP_PORT", 8787, 1, 65535)
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "subs.db")))
MAX_DOCUMENT_BYTES = _env_int("MAX_DOCUMENT_BYTES", 10 * 1024 * 1024, 1, 50 * 1024 * 1024)
MAX_IMPORTED_NODES = _env_int("MAX_IMPORTED_NODES", 500, 1, 5000)
MAX_IMPORTED_SUBSCRIPTIONS = _env_int("MAX_IMPORTED_SUBSCRIPTIONS", 20, 1, 100)
# 批量更新订阅的并发上限。太高会让机场把并发请求当刷探测，8 是实测稳妥值。
UPDATE_CONCURRENCY = _env_int("UPDATE_CONCURRENCY", 8, 1, 32)
DATA_DIR.mkdir(parents=True, exist_ok=True)
