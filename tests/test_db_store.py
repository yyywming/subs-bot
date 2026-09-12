"""db.py 单连接改造验证：性能 + 并发正确性。跑完自删数据。"""
import asyncio
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "x")

from db import Store

FAIL = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAIL.append(label)


async def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "t.db")
    store = Store(path)
    await store.init()

    print("=== 1. 基本读写 ===")
    sub = await store.add_sub(1, "测试订阅", "https://example.com/s")
    check(sub["name"] == "测试订阅", "add_sub 返回正确")
    got = await store.get_sub(1, int(sub["id"]))
    check(got is not None and got["url"] == "https://example.com/s", "get_sub 读回一致")
    upd = await store.update_sub(1, int(sub["id"]), name="改名了")
    check(upd is not None and upd["name"] == "改名了", "update_sub 生效")

    print("=== 2. init() 幂等 ===")
    await store.init()
    await store.init()
    subs = await store.list_subs(1)
    check(len(subs) == 1, "重复 init 不丢数据")

    print("=== 3. WAL 已启用 ===")
    async with store.connection() as db:
        cur = await db.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
    check(str(mode).lower() == "wal", f"journal_mode=WAL (实际 {mode})")

    print("=== 4. 并发 claim_share 不超发 (max_views=3, 20 并发) ===")
    code = await store.create_share(1, "sender", "secret-content", max_views=3)
    results = await asyncio.gather(*[store.claim_share(code, 1000 + i) for i in range(20)])
    ok_count = sum(1 for r in results if r[0])
    share = await store.get_share(code)
    check(ok_count == 3, f"精确 3 人领到 (实际 {ok_count})")
    check(int(share["claimed_count"]) == 3, f"claimed_count==3 (实际 {share['claimed_count']})")

    print("=== 5. 同一用户重复领取不扣份数 ===")
    code2 = await store.create_share(1, "s", "c", max_views=2)
    r1 = await store.claim_share(code2, 7)
    rs = await asyncio.gather(*[store.claim_share(code2, 7) for _ in range(5)])
    sh2 = await store.get_share(code2)
    check(r1[0] and all(r[0] for r in rs), "重复查看均返回成功")
    check(int(sh2["claimed_count"]) == 1, f"份数仍为 1 (实际 {sh2['claimed_count']})")

    print("=== 6. 并发混合读写无异常 ===")
    try:
        await asyncio.gather(
            *[store.add_sub(2, f"s{i}", f"https://e.com/{i}") for i in range(15)],
            *[store.list_subs(2) for _ in range(15)],
        )
        n = len(await store.list_subs(2))
        check(n == 15, f"15 条并发插入全部落库 (实际 {n})")
    except Exception as e:
        check(False, f"并发混合读写抛异常: {e}")

    print("=== 7. 性能对比 ===")
    N = 60
    t0 = time.perf_counter()
    for _ in range(N):
        await store.list_subs(1)
    new_ms = (time.perf_counter() - t0) * 1000

    import aiosqlite
    from db import SCHEMA
    t0 = time.perf_counter()
    for _ in range(N):
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        try:
            await db.execute("ALTER TABLE shares ADD COLUMN inline_message_id TEXT")
        except Exception:
            pass
        await db.commit()
        cur = await db.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY id ASC", (1,))
        await cur.fetchall()
        await db.close()
    old_ms = (time.perf_counter() - t0) * 1000
    print(f"  新实现 x{N}: {new_ms:.0f}ms  avg {new_ms/N:.2f}ms")
    print(f"  旧实现 x{N}: {old_ms:.0f}ms  avg {old_ms/N:.2f}ms")
    print(f"  提速: {old_ms/new_ms:.1f}x")
    check(new_ms < old_ms, "新实现更快")

    print("=== 8. close() 后可重新自愈 ===")
    await store.close()
    n = len(await store.list_subs(1))
    check(n == 1, "close 后再查询自动重连")
    await store.close()

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")


asyncio.run(main())
