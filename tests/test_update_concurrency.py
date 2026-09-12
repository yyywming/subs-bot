"""离线验证 run_update_all 并发化 + 共享 session。不碰任何真实网络/真实 bot。"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time

os.environ.setdefault("BOT_TOKEN", "0:TEST")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = os.path.join(_tmp, "t.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as B  # noqa: E402
import convert as C  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' | ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


# ── 假 telegram 对象 ──────────────────────────────────────────────────────────
class FakeMsg:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.stamps: list[float] = []

    async def edit_text(self, text: str, **kw):
        self.edits.append(text)
        self.stamps.append(time.monotonic())


class FakeCbq:
    def __init__(self, msg): self.message = msg


class FakeBot:
    def __init__(self) -> None:
        self.docs: list[str] = []
        self.doc_body: dict[str, str] = {}
        self.msgs: list[str] = []

    async def send_document(self, chat_id, document, **kw):
        name = getattr(document, "filename", "?")
        self.docs.append(name)
        # InputFile 把内容存在 .input_file_content (bytes)
        raw = getattr(document, "input_file_content", b"") or b""
        if isinstance(raw, (bytes, bytearray)):
            self.doc_body[name] = bytes(raw).decode("utf-8", "ignore")

    async def send_message(self, chat_id, text="", **kw):
        self.msgs.append(text)


class FakeUser:
    id = 1


class FakeUpdate:
    def __init__(self, cbq): self.callback_query = cbq; self.effective_user = FakeUser()


class FakeCtx:
    def __init__(self, bot): self.bot = bot


async def main() -> None:
    await B.store.init()

    # ── 造 12 条订阅 ──────────────────────────────────────────────────────────
    N = 12
    for i in range(N):
        await B.store.add_sub(1, f"sub{i:02d}", f"http://example.invalid/{i}")
    subs = await B.store.list_subs(1)
    check("准备数据", len(subs) == N, f"{len(subs)} 条")

    # ── 1. 并发度 + 保序 + 墙钟提速 ──────────────────────────────────────────
    print("=== 1. 并发度 / 保序 / 墙钟 ===")
    # 故意让靠后的订阅延迟更短 —— 完成顺序会跟输入顺序相反，
    # 这样才能真正验证"结果按 index 回填保序"而不是碰巧顺序对。
    def _delay_of(i: int) -> float:
        return 0.40 - i * 0.025

    cur = 0
    peak = 0
    order_seen: list[str] = []

    async def fake_refresh(user_id, sub, rename=False):
        nonlocal cur, peak
        cur += 1
        peak = max(peak, cur)
        try:
            idx = int(sub["name"][-2:])
            await asyncio.sleep(_delay_of(idx))
            order_seen.append(sub["name"])
            # 偶数条标成失效，奇数条正常 —— 用于验分类保序
            if idx % 2 == 0:
                return {**sub, "last_error": "boom"}
            return {**sub, "last_error": None}
        finally:
            cur -= 1

    B.refresh_sub = fake_refresh
    msg = FakeMsg()
    bot = FakeBot()
    upd = FakeUpdate(FakeCbq(msg))
    ctx = FakeCtx(bot)

    t0 = time.monotonic()
    await B.run_update_all(upd, ctx)
    wall = time.monotonic() - t0

    serial = sum(_delay_of(i) for i in range(N))
    limit = B.UPDATE_CONCURRENCY
    check("并发峰值不超过 Semaphore 上限", peak <= limit, f"peak={peak} limit={limit}")
    check("确实并发了(峰值>1)", peak > 1, f"peak={peak}")
    check("墙钟远快于串行", wall < serial * 0.6, f"wall={wall:.2f}s 串行={serial:.2f}s")
    check("三个结果文件都发了", len(bot.docs) == 3, str(bot.docs))

    # 前提：完成顺序必须真的被打乱了，否则"保序"验的是空气
    scrambled = order_seen != sorted(order_seen)
    check("完成顺序已打乱(乱序前提成立)", scrambled, f"完成序={order_seen[:4]}…")

    # 保序本体：结果文件里 id 必须升序，跟完成顺序无关
    def _ids_in(name: str) -> list[int]:
        body = bot.doc_body.get(name, "")
        return [int(m) for m in re.findall(r"^#(\d+) ", body, re.M)]

    failed_ids = _ids_in("failed_subs.txt")
    valid_ids = _ids_in("valid_subs.txt")
    check("failed 文件 id 升序(按输入序回填)", failed_ids == sorted(failed_ids), f"{failed_ids}")
    check("valid 文件 id 升序(按输入序回填)", valid_ids == sorted(valid_ids), f"{valid_ids}")
    check("失效/有效条数各半(偶数条标失效)", len(failed_ids) == N // 2 and len(valid_ids) == N // 2,
          f"failed={len(failed_ids)} valid={len(valid_ids)}")
    check("failed/valid 无交集且合计=N",
          not (set(failed_ids) & set(valid_ids)) and len(failed_ids) + len(valid_ids) == N,
          f"合计={len(failed_ids) + len(valid_ids)}")

    # ── 2. 异常隔离 ──────────────────────────────────────────────────────────
    print("=== 2. 单条抛异常不拖垮整批 ===")
    async def boom_refresh(user_id, sub, rename=False):
        if int(sub["name"][-2:]) == 5:
            raise RuntimeError("network exploded")
        return {**sub, "last_error": None}

    B.refresh_sub = boom_refresh
    msg2, bot2 = FakeMsg(), FakeBot()
    await B.run_update_all(FakeUpdate(FakeCbq(msg2)), FakeCtx(bot2))
    check("异常条数不为 0 时整批仍完成", len(bot2.docs) == 3, str(bot2.docs))
    done_txt = [e for e in msg2.edits if "更新完成" in e]
    check("末条消息是更新完成", bool(done_txt), done_txt[-1] if done_txt else "无")

    # ── 3. 进度条节流 ────────────────────────────────────────────────────────
    print("=== 3. 进度条节流 (>=1.5s 间隔) ===")
    gaps = [msg.stamps[i + 1] - msg.stamps[i] for i in range(len(msg.stamps) - 1)]
    # 首尾各有一次 force，中间的普通刷新必须被节流；总编辑次数应远小于 N
    check("编辑次数远小于订阅数(被节流)", len(msg.edits) < N, f"edits={len(msg.edits)} N={N}")
    check("进度条含方块字符", any("▓" in e or "░" in e for e in msg.edits), msg.edits[0] if msg.edits else "无")

    # ── 4. 空订阅早退 ────────────────────────────────────────────────────────
    print("=== 4. 空订阅早退 ===")
    for s in await B.store.list_subs(1):
        await B.store.delete_sub(1, int(s["id"]))
    msg3, bot3 = FakeMsg(), FakeBot()
    await B.run_update_all(FakeUpdate(FakeCbq(msg3)), FakeCtx(bot3))
    check("空订阅不发文件", len(bot3.docs) == 0, str(bot3.docs))
    check("空订阅有提示", any("暂无订阅" in m for m in bot3.msgs), str(bot3.msgs))

    # ── 5. 共享 session 复用 ─────────────────────────────────────────────────
    print("=== 5. 共享 aiohttp session ===")
    s1 = await C.get_session()
    s2 = await C.get_session()
    check("两次 get_session 返回同一对象", s1 is s2, f"{id(s1)} vs {id(s2)}")
    check("session 未关闭", not s1.closed)
    check("limit_per_host 已设", s1.connector.limit_per_host == 8, str(s1.connector.limit_per_host))
    await C.close_session()
    check("close_session 生效", s1.closed)
    s3 = await C.get_session()
    check("关闭后自愈成新 session", s3 is not s1 and not s3.closed)
    await C.close_session()

    # ── 6. 并发抓取时 session 不被并发创建成多个 ──────────────────────────────
    print("=== 6. 并发下 session 单例 ===")
    got = await asyncio.gather(*(C.get_session() for _ in range(20)))
    check("20 并发只拿到一个 session", len({id(x) for x in got}) == 1, f"{len({id(x) for x in got})} 个")
    await C.close_session()

    await B.store.close()
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
