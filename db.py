from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

from config import DB_PATH

log = logging.getLogger("subs-bot.db")

# meta 表里的一次性迁移标记位
_BACKFILL_FLAG = "node_count_backfilled_v1"

# 瘦查询列：subscriptions 的全部列减去 nodes_json。
# 显式写死而不是 SELECT * —— 巨型订阅的 nodes_json 单行可达数十 MB，
# 列表页/更新流程只需要元信息，把它读出来纯属浪费（实测 83.6MB / 293ms）。
# 节点数由维护型 node_count 列给出，不再靠解析 JSON。
_META_COLS = (
    "id, user_id, name, url, token, expire_at, traffic_used, "
    "traffic_total, node_count, last_error, created_at, updated_at"
)

# 回收站列表同理：只显示名字，没必要把已删订阅的节点也拖出来
_DELETED_META_COLS = (
    "id, user_id, name, url, token, expire_at, traffic_used, "
    "traffic_total, node_count, last_error, created_at, deleted_at"
)


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
    node_count INTEGER NOT NULL DEFAULT 0,
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
    node_count INTEGER NOT NULL DEFAULT 0,
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


def _count_nodes_json(raw: str | None) -> int:
    """数一段 nodes_json 里的节点数。坏数据算 0，不抛异常。"""
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return 0
    return len(data) if isinstance(data, list) else 0


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
        # 安全迁移：为现有数据库补全后加的列（列已存在时抛错属正常）
        for table, col, decl in (
            ("shares", "inline_message_id", "TEXT"),
            ("subscriptions", "node_count", "INTEGER NOT NULL DEFAULT 0"),
            ("deleted_subs", "node_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception:
                pass
        # WAL 让读写不再互相阻塞；NORMAL 省掉每次提交的 fsync 往返
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.commit()
        await self._backfill_node_count(db)
        self._conn = db
        self._ready = True

    async def _backfill_node_count(self, db: aiosqlite.Connection) -> None:
        """给历史数据回填 node_count。只跑一次，靠 meta 表记标记位。

        注意：必须直接收 db 参数，不能走 self.connection() —— 本方法由 init()
        在锁内调用，再去拿锁会死锁。

        为什么要标记位：node_count 默认 0，单看值无法区分"真的 0 个节点"和
        "老数据还没回填"。用 meta 记一笔，之后每次启动只是一次主键查询。
        """
        cur = await db.execute("SELECT value FROM meta WHERE key=?", (_BACKFILL_FLAG,))
        if await cur.fetchone():
            return

        for table in ("subscriptions", "deleted_subs"):
            try:
                # 走 SQL 一条语句搞定；json_valid 挡住脏数据，避免整批迁移崩掉
                await db.execute(
                    f"""UPDATE {table} SET node_count = CASE
                        WHEN json_valid(nodes_json) THEN json_array_length(nodes_json)
                        ELSE 0 END"""
                )
            except Exception as exc:
                # 这个 SQLite 没编 json1，退回 Python 逐行数（慢但一次性）
                log.warning("json1 unavailable on %s (%s), falling back to Python", table, exc)
                cur = await db.execute(f"SELECT id, nodes_json FROM {table}")
                for row in await cur.fetchall():
                    try:
                        data = json.loads(row["nodes_json"] or "[]")
                        n = len(data) if isinstance(data, list) else 0
                    except Exception:
                        n = 0
                    await db.execute(
                        f"UPDATE {table} SET node_count=? WHERE id=?", (n, row["id"])
                    )

        await db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (_BACKFILL_FLAG, "1")
        )
        await db.commit()
        log.info("node_count backfill completed")

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
        """完整行，含 nodes_json。只在真要用节点数据时调（导出/聚合/详情）。"""
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT * FROM subscriptions WHERE user_id=? ORDER BY id ASC", (user_id,)
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def list_subs_meta(self, user_id: int) -> list[dict[str, Any]]:
        """瘦查询：不含 nodes_json，节点数走 node_count 列。

        列表页、到期统计、批量更新这些场景都不需要节点明细。巨型订阅下
        SELECT * 要搬几十 MB，这里是常数级。返回的字典没有 nodes_json 键，
        误传给 nodes_of() 会直接抛 KeyError（故意的，见 nodes_of 注释）。
        """
        async with self.connection() as db:
            cur = await db.execute(
                f"SELECT {_META_COLS} FROM subscriptions WHERE user_id=? ORDER BY id ASC",
                (user_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_sub_meta(self, user_id: int, sub_id: int) -> dict[str, Any] | None:
        """单条瘦查询，语义同 list_subs_meta。"""
        async with self.connection() as db:
            cur = await db.execute(
                f"SELECT {_META_COLS} FROM subscriptions WHERE user_id=? AND id=?",
                (user_id, sub_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def count_subs(self, user_id: int) -> int:
        """只数行数，不读任何 payload。"""
        async with self.connection() as db:
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM subscriptions WHERE user_id=?", (user_id,)
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

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

    async def update_sub(
        self, user_id: int, sub_id: int, *, return_meta: bool = False, **fields: Any
    ) -> dict[str, Any] | None:
        """更新订阅字段。

        写 nodes_json 时自动同步 node_count —— 放在这里而不是让调用方各自维护，
        是因为漏一处就会让节点数长期显示错值，且很难发现。

        return_meta=True 时回瘦行（不含 nodes_json）。巨型订阅下默认行为等于
        "写完几十 MB 再读回几十 MB"，只想拿回节点数的调用方应该开这个开关。
        """
        if not fields:
            return await (self.get_sub_meta if return_meta else self.get_sub)(user_id, sub_id)
        allowed = {
            "name", "url", "token", "expire_at", "traffic_used",
            "traffic_total", "nodes_json", "node_count", "last_error", "created_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported subscription fields: {sorted(unknown)}")
        if "nodes_json" in fields and "node_count" not in fields:
            # 普通小调用方不传 node_count 时仍自动维护；巨型刷新路径会直接
            # 传入已知的 len(nodes)，避免把刚序列化的几十 MB JSON 再解析一遍。
            fields["node_count"] = _count_nodes_json(fields["nodes_json"])
        if "node_count" in fields:
            fields["node_count"] = max(0, int(fields["node_count"]))
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [user_id, sub_id]
        async with self.connection() as db:
            await db.execute(
                f"UPDATE subscriptions SET {cols} WHERE user_id=? AND id=?", vals
            )
            await db.commit()
        return await (self.get_sub_meta if return_meta else self.get_sub)(user_id, sub_id)

    async def delete_sub(self, user_id: int, sub_id: int) -> bool:
        """软删除：整行搬进 deleted_subs。

        旧实现先 get_sub() 把整行（含几十 MB 的 nodes_json）读进 Python，再原样
        INSERT 回去，等于让巨型 payload 在进程内跑一趟往返。改成 INSERT..SELECT，
        数据全程留在 SQLite 内部。
        """
        now = int(time.time())
        async with self.connection() as db:
            cur = await db.execute(
                """INSERT INTO deleted_subs
                (user_id,name,url,token,expire_at,traffic_used,traffic_total,
                 nodes_json,node_count,last_error,created_at,deleted_at)
                SELECT user_id,name,url,token,expire_at,traffic_used,traffic_total,
                       nodes_json,node_count,last_error,created_at,?
                FROM subscriptions WHERE user_id=? AND id=?""",
                (now, user_id, sub_id),
            )
            if cur.rowcount <= 0:
                return False
            cur = await db.execute(
                "DELETE FROM subscriptions WHERE user_id=? AND id=?", (user_id, sub_id)
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_deleted(self, user_id: int, days: int = 30) -> list[dict[str, Any]]:
        """回收站列表，瘦查询（不含 nodes_json）。

        唯一调用方 cmd_recycle 只显示名字。恢复走 restore_deleted 的纯 SQL
        行拷贝，节点数据全程留在 SQLite 里，不需要经过这里。
        """
        cutoff = int(time.time()) - days * 86400
        async with self.connection() as db:
            cur = await db.execute(
                f"""SELECT {_DELETED_META_COLS} FROM deleted_subs
                    WHERE user_id=? AND deleted_at>=? ORDER BY deleted_at DESC""",
                (user_id, cutoff),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def restore_deleted(self, user_id: int, deleted_id: int) -> dict[str, Any] | None:
        """从回收站恢复。同 delete_sub，用 INSERT..SELECT 避免搬运 payload。

        返回瘦行（不含 nodes_json）：调用方只用来显示名字。
        """
        now = int(time.time())
        fallback_token = secrets.token_urlsafe(12)
        async with self.connection() as db:
            cur = await db.execute(
                """INSERT INTO subscriptions
                (user_id,name,url,token,expire_at,traffic_used,traffic_total,
                 nodes_json,node_count,last_error,created_at,updated_at)
                SELECT user_id,name,url,
                       COALESCE(NULLIF(token,''), ?),
                       expire_at,traffic_used,traffic_total,
                       nodes_json,node_count,last_error,created_at,?
                FROM deleted_subs WHERE user_id=? AND id=?""",
                (fallback_token, now, user_id, deleted_id),
            )
            if cur.rowcount <= 0:
                return None
            # 用 lastrowid 精确定位刚插入的行；旧实现取 list_subs()[-1]，
            # 依赖"最后一条就是刚恢复的"这个并不成立的假设。
            new_id = int(cur.lastrowid)
            await db.execute("DELETE FROM deleted_subs WHERE id=?", (deleted_id,))
            await db.commit()
        return await self.get_sub_meta(user_id, new_id)

    async def renumber(self, user_id: int) -> int:
        # Display numbers are derived from ORDER BY id; never rewrite rows here.
        # Re-inserting would invalidate callback IDs and unnecessarily risk data loss.
        return await self.count_subs(user_id)

    async def add_imported_sub(
        self, user_id: int, name: str, nodes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        now = int(time.time())
        token = secrets.token_urlsafe(12)
        local_url = f"uploaded://{secrets.token_urlsafe(8)}"
        async with self.connection() as db:
            cur = await db.execute(
                """INSERT INTO subscriptions
                (user_id,name,url,token,expire_at,traffic_used,traffic_total,
                 nodes_json,node_count,created_at,updated_at)
                VALUES (?,?,?,?,NULL,NULL,NULL,?,?,?,?)""",
                (
                    user_id, name[:64] or "导入配置", local_url, token,
                    json.dumps(nodes, ensure_ascii=False), len(nodes), now, now,
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
    """解析节点明细。

    故意在缺 nodes_json 键时抛 KeyError：那意味着调用方拿的是 list_subs_meta /
    get_sub_meta 的瘦行，而瘦行是没有节点数据的。旧实现用 .get() 兜底会静默
    返回 []，把"我传错了行类型"这个编程错误伪装成"这条订阅 0 个节点"——
    界面上看到的就是节点数莫名变 0，比直接崩难查得多。

    JSON 本身坏掉是另一回事（数据问题，不是调用错误），仍然容错返回 []。
    只想要节点数量时别用 len(nodes_of(sub))，直接读 sub["node_count"]。
    """
    if "nodes_json" not in sub:
        raise KeyError(
            "nodes_of() 收到不含 nodes_json 的瘦行（来自 list_subs_meta/get_sub_meta）。"
            "需要节点明细请改用 list_subs()/get_sub()；只要数量请读 sub['node_count']。"
        )
    try:
        data = json.loads(sub["nodes_json"] or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []
