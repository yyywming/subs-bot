"""抓取失败不得覆盖已缓存节点。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_TOKEN", "0:TEST")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = os.path.join(_tmp, "t.db")

import bot as B  # noqa: E402
from db import Store, nodes_of  # noqa: E402


async def main() -> None:
    store = Store(os.environ["DB_PATH"])
    B.store = store
    cached = [{"name": "keep", "type": "ss", "server": "1.2.3.4", "port": 8388}]
    sub = await store.add_sub(1, "airport", "https://example.test/sub")
    await store.update_sub(
        1,
        int(sub["id"]),
        nodes_json=json.dumps(cached, ensure_ascii=False),
        node_count=1,
        last_error=None,
    )
    with patch(
        "bot.fetch_subscription",
        new=AsyncMock(return_value=([], {"traffic_used": None, "traffic_total": None, "expire_at": None, "profile_name": None}, "timeout")),
    ):
        updated = await B.refresh_sub(1, await store.get_sub(1, int(sub["id"])))
    fresh = await store.get_sub(1, int(sub["id"]))
    assert nodes_of(fresh) == cached, nodes_of(fresh)
    assert fresh["node_count"] == 1
    assert fresh["last_error"] == "timeout"
    assert updated["last_error"] == "timeout"
    await store.close()
    print("ok refresh preserves cached nodes")


if __name__ == "__main__":
    asyncio.run(main())
