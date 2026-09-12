from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default
    if value < minimum:
        return default
    return min(value, maximum) if maximum is not None else value


ROOT = Path(__file__).resolve().parent
_load_dotenv(ROOT / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "your_bot_username").lstrip("@")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8787"))
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "subs.db")))
MAX_DOCUMENT_BYTES = _env_int("MAX_DOCUMENT_BYTES", 10 * 1024 * 1024, 1, 50 * 1024 * 1024)
MAX_IMPORTED_NODES = _env_int("MAX_IMPORTED_NODES", 500, 1, 5000)
MAX_IMPORTED_SUBSCRIPTIONS = _env_int("MAX_IMPORTED_SUBSCRIPTIONS", 20, 1, 100)
# 批量更新订阅的并发上限。太高会让机场把并发请求当刷探测，8 是实测稳妥值。
UPDATE_CONCURRENCY = _env_int("UPDATE_CONCURRENCY", 8, 1, 32)
DATA_DIR.mkdir(parents=True, exist_ok=True)
