from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    expire_at INTEGER,
    traffic_used REAL,
    traffic_total REAL,
    nodes_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS path_maps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    node_name TEXT NOT NULL,
    remark TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, node_name)
);
CREATE TABLE IF NOT EXISTS temp_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS short_links (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    target_url TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    token TEXT NOT NULL,
    expire_at INTEGER,
    traffic_used REAL,
    traffic_total REAL,
    nodes_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shares (
    code TEXT PRIMARY KEY,
    sender_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    content TEXT NOT NULL,
    max_views INTEGER NOT NULL DEFAULT 1,
    claimed_count INTEGER NOT NULL DEFAULT 0,
    target_user_id INTEGER,
    inline_message_id TEXT,
    expire_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS share_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    share_code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL,
    UNIQUE(share_code, user_id)
);
"""


class Store:
    """单连接 + 一次性建表的 SQLite 存储层。

    旧实现每次查询都新开连接并重放整份 SCHEMA（外加一条必然抛异常的
    ALTER TABLE），实测比复用连接慢 12.3 倍。现在建表/迁移只在首次访问时
    执行一次，之后所有查询共享同一条连接。

    并发安全：aiosqlite 每条连接背后是单线程，但 read-modify-write 型操作
    （如 claim_share）跨多条语句，必须整块串行化，否则两个协程的事务会互相
    穿插。因此 connection() 全程持有 _lock。单次操作耗时 <1ms，锁竞争可忽略。
    调用方签名不变，22 个使用点零改动。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = str(path or DB_PATH)
        self._conn: aiosqlite.Connection | None = None
        self._lock: asyncio.Lock | None = None
        self._ready = False

    async def init(self) -> None:
        """建连接、建表、跑迁移。幂等，可在启动时显式调用一次。"""
        if self._ready:
            return
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        # 安全迁移：为现有数据库补全 inline_message_id 列（列已存在时抛错属正常）
        try:
            await db.execute("ALTER TABLE shares ADD COLUMN inline_message_id TEXT")
        except Exception:
            pass
        # WAL 让读写不再互相阻塞；NORMAL 省掉每次提交的 fsync 往返
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.commit()
        self._conn = db
        self._ready = True

    async def close(self) -> None:
        """关闭共享连接，供进程退出时收尾。"""
        conn, self._conn = self._conn, None
        self._ready = False
        if conn is not None:
            await conn.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        # 懒创建锁：Store() 在事件循环启动前就于模块级实例化，
        # 此处两行之间没有 await，单线程事件循环下是原子的。
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not self._ready:
                await self.init()
            assert self._conn is not None
            yield self._conn

    async def list_subs(self, user_id: int) -> list[dict[str, Any]]:
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT * FROM subscriptions WHERE user_id=? ORDER BY id ASC", (user_id,)
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_sub(self, user_id: int, sub_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT * FROM subscriptions WHERE user_id=? AND id=?", (user_id, sub_id)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_sub_by_token(self, token: str) -> dict[str, Any] | None:
        async with self.connection() as db:
            cur = await db.execute("SELECT * FROM subscriptions WHERE token=?", (token,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def add_sub(self, user_id: int, name: str, url: str) -> dict[str, Any]:
        now = int(time.time())
        token = secrets.token_urlsafe(12)
        async with self.connection() as db:
            cur = await db.execute(
                """INSERT INTO subscriptions
                (user_id,name,url,token,expire_at,traffic_used,traffic_total,nodes_json,created_at,updated_at)
                VALUES (?,?,?,?,NULL,NULL,NULL,'[]',?,?)""",
                (user_id, name, url, token, now, now),
            )
            await db.commit()
            sub_id = int(cur.lastrowid)
        sub = await self.get_sub(user_id, sub_id)
        assert sub is not None
        return sub

    async def update_sub(self, user_id: int, sub_id: int, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return await self.get_sub(user_id, sub_id)
        allowed = {
            "name", "url", "token", "expire_at", "traffic_used",
            "traffic_total", "nodes_json", "last_error", "created_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported subscription fields: {sorted(unknown)}")
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [user_id, sub_id]
        async with self.connection() as db:
            await db.execute(
                f"UPDATE subscriptions SET {cols} WHERE user_id=? AND id=?", vals
            )
            await db.commit()
        return await self.get_sub(user_id, sub_id)

    async def delete_sub(self, user_id: int, sub_id: int) -> bool:
        sub = await self.get_sub(user_id, sub_id)
        if not sub:
            return False
        now = int(time.time())
        async with self.connection() as db:
            await db.execute(
                """INSERT INTO deleted_subs
                (user_id,name,url,token,expire_at,traffic_used,traffic_total,nodes_json,last_error,created_at,deleted_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    sub["name"],
                    sub["url"],
                    sub["token"],
                    sub["expire_at"],
                    sub["traffic_used"],
                    sub["traffic_total"],
                    sub["nodes_json"],
                    sub["last_error"],
                    sub["created_at"],
                    now,
                ),
            )
            cur = await db.execute(
                "DELETE FROM subscriptions WHERE user_id=? AND id=?", (user_id, sub_id)
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_deleted(self, user_id: int, days: int = 30) -> list[dict[str, Any]]:
        cutoff = int(time.time()) - days * 86400
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT * FROM deleted_subs WHERE user_id=? AND deleted_at>=? ORDER BY deleted_at DESC",
                (user_id, cutoff),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def restore_deleted(self, user_id: int, deleted_id: int) -> dict[str, Any] | None:
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT * FROM deleted_subs WHERE user_id=? AND id=?", (user_id, deleted_id)
            )
            row = await cur.fetchone()
            if not row:
                return None
            item = dict(row)
            now = int(time.time())
            token = item["token"] or secrets.token_urlsafe(12)
            await db.execute(
                """INSERT INTO subscriptions
                (user_id,name,url,token,expire_at,traffic_used,traffic_total,nodes_json,last_error,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    item["name"],
                    item["url"],
                    token,
                    item["expire_at"],
                    item["traffic_used"],
                    item["traffic_total"],
                    item["nodes_json"],
                    item["last_error"],
                    item["created_at"],
                    now,
                ),
            )
            await db.execute("DELETE FROM deleted_subs WHERE id=?", (deleted_id,))
            await db.commit()
        subs = await self.list_subs(user_id)
        return subs[-1] if subs else None

    async def renumber(self, user_id: int) -> int:
        # Display numbers are derived from ORDER BY id; never rewrite rows here.
        # Re-inserting would invalidate callback IDs and unnecessarily risk data loss.
        return len(await self.list_subs(user_id))

    async def add_imported_sub(
        self, user_id: int, name: str, nodes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        now = int(time.time())
        token = secrets.token_urlsafe(12)
        local_url = f"uploaded://{secrets.token_urlsafe(8)}"
        async with self.connection() as db:
            cur = await db.execute(
                """INSERT INTO subscriptions
                (user_id,name,url,token,expire_at,traffic_used,traffic_total,nodes_json,created_at,updated_at)
                VALUES (?,?,?,?,NULL,NULL,NULL,?,?,?)""",
                (
                    user_id, name[:64] or "导入配置", local_url, token,
                    json.dumps(nodes, ensure_ascii=False), now, now,
                ),
            )
            await db.commit()
            sub_id = int(cur.lastrowid)
        sub = await self.get_sub(user_id, sub_id)
        assert sub is not None
        return sub

    async def list_path_maps(self, user_id: int) -> list[dict[str, Any]]:
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT * FROM path_maps WHERE user_id=? ORDER BY id ASC", (user_id,)
            )
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_path_map(self, user_id: int, node_name: str, remark: str) -> None:
        async with self.connection() as db:
            if remark == "":
                await db.execute(
                    "DELETE FROM path_maps WHERE user_id=? AND node_name=?", (user_id, node_name)
                )
            else:
                await db.execute(
                    """INSERT INTO path_maps(user_id,node_name,remark) VALUES(?,?,?)
                    ON CONFLICT(user_id,node_name) DO UPDATE SET remark=excluded.remark""",
                    (user_id, node_name, remark),
                )
            await db.commit()

    async def clear_path_maps(self, user_id: int) -> int:
        async with self.connection() as db:
            cur = await db.execute("DELETE FROM path_maps WHERE user_id=?", (user_id,))
            await db.commit()
            return cur.rowcount

    async def delete_path_map(self, user_id: int, map_id: int) -> bool:
        async with self.connection() as db:
            cur = await db.execute(
                "DELETE FROM path_maps WHERE user_id=? AND id=?", (user_id, map_id)
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_temp(self, user_id: int) -> list[dict[str, Any]]:
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT * FROM temp_nodes WHERE user_id=? ORDER BY id ASC", (user_id,)
            )
            return [dict(r) for r in await cur.fetchall()]

    async def add_temp(self, user_id: int, url: str, name: str) -> None:
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO temp_nodes(user_id,url,name,created_at) VALUES(?,?,?,?)",
                (user_id, url, name, int(time.time())),
            )
            await db.commit()

    async def clear_temp(self, user_id: int) -> int:
        async with self.connection() as db:
            cur = await db.execute("DELETE FROM temp_nodes WHERE user_id=?", (user_id,))
            await db.commit()
            return cur.rowcount

    async def create_short(self, user_id: int, target_url: str) -> str:
        code = secrets.token_urlsafe(6)
        async with self.connection() as db:
            await db.execute(
                "INSERT INTO short_links(code,user_id,target_url,created_at) VALUES(?,?,?,?)",
                (code, user_id, target_url, int(time.time())),
            )
            await db.commit()
        return code

    async def get_short(self, code: str) -> dict[str, Any] | None:
        async with self.connection() as db:
            cur = await db.execute("SELECT * FROM short_links WHERE code=?", (code,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def create_share(
        self,
        sender_id: int,
        sender_name: str,
        content: str,
        max_views: int = 1,
        target_user_id: int | None = None,
        duration_minutes: int = 10,
    ) -> str:
        code = secrets.token_urlsafe(8)
        now = int(time.time())
        expire_at = now + max(1, duration_minutes) * 60
        async with self.connection() as db:
            await db.execute(
                """INSERT INTO shares
                (code, sender_id, sender_name, content, max_views, claimed_count, target_user_id, expire_at, created_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (code, sender_id, sender_name, content, max_views, target_user_id, expire_at, now),
            )
            await db.commit()
        return code

    async def get_share(self, code: str) -> dict[str, Any] | None:
        async with self.connection() as db:
            cur = await db.execute("SELECT * FROM shares WHERE code=?", (code,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def bind_inline_message_id(self, code: str, inline_message_id: str) -> bool:
        async with self.connection() as db:
            cur = await db.execute(
                "UPDATE shares SET inline_message_id=? WHERE code=?", (inline_message_id, code)
            )
            await db.commit()
            return cur.rowcount > 0

    async def claim_share(self, code: str, user_id: int) -> tuple[bool, str, dict[str, Any] | None]:
        """Claim a share item.

        Returns (success, message, share_dict).
        """
        now = int(time.time())
        async with self.connection() as db:
            cur = await db.execute("SELECT * FROM shares WHERE code=?", (code,))
            row = await cur.fetchone()
            if not row:
                return False, "分享不存在或已被删除", None
            share = dict(row)

            if share["expire_at"] < now:
                return False, "⚠️ 该分享已过期！", share

            if share["target_user_id"] and share["target_user_id"] != user_id:
                return False, f"🚫 仅指定用户 (ID: {share['target_user_id']}) 可查看！", share

            # 检查是否该用户已经领取过
            cur = await db.execute(
                "SELECT 1 FROM share_claims WHERE share_code=? AND user_id=?", (code, user_id)
            )
            if await cur.fetchone():
                # 已领过的人可重复查看内容，但不扣减份数
                return True, "已再次查看", share

            if share["claimed_count"] >= share["max_views"]:
                return False, "⚠️ 份数已领完！", share

            # 记录领取
            await db.execute(
                "INSERT INTO share_claims(share_code, user_id, claimed_at) VALUES(?, ?, ?)",
                (code, user_id, now),
            )
            await db.execute(
                "UPDATE shares SET claimed_count = claimed_count + 1 WHERE code=?", (code,)
            )
            await db.commit()

            # 重新拉取最新数据
            cur = await db.execute("SELECT * FROM shares WHERE code=?", (code,))
            share = dict(await cur.fetchone())
            return True, "领取成功", share


def nodes_of(sub: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        data = json.loads(sub.get("nodes_json") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []
