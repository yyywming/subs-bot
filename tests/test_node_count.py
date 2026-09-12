"""离线验证 node_count 列：migration 回填、各写入点维护、瘦查询、fail-loud。

不碰任何真实网络。会临时造一个"胖行"来量化瘦查询收益。

胖行规模刻意保守（默认 3000 节点约 1.5MB）：这个测试要能在手机沙箱里跑完，
而 timeit 会把整个 payload 反复物化成 Python dict。4 万节点(20MB)那版直接
把沙箱拖进 swap 死亡螺旋，连 shell 都失去响应。要压更大规模用环境变量：
FAT_NODES=40000 python3 tests/test_node_count.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time

os.environ.setdefault("BOT_TOKEN", "0:TEST")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = os.path.join(_tmp, "t.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Store, nodes_of  # noqa: E402

FAILED: list[str] = []
FAT_NODES = int(os.environ.get("FAT_NODES", "3000"))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' | ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


def make_nodes(n: int, tag: str = "n") -> list[dict]:
    return [
        {"name": f"{tag}{i:06d}", "type": "vmess", "server": f"h{i}.example.invalid",
         "port": 443, "share": f"vmess://fake{i}"}
        for i in range(n)
    ]


# ── 1. migration：老库(无 node_count 列)升级 ────────────────────────────────
async def test_migration() -> None:
    print("=== 1. migration：老库无 node_count 列 ===")
    path = os.path.join(_tmp, "old.db")
    now = int(time.time())
    # 手搓一个"旧版"库：只有旧列，没有 node_count
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            name TEXT NOT NULL, url TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
            expire_at INTEGER, traffic_used REAL, traffic_total REAL,
            nodes_json TEXT NOT NULL DEFAULT '[]', last_error TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    rows = [
        (1, "三节点", "http://a.invalid", "tk1", json.dumps(make_nodes(3))),
        (1, "空订阅", "http://b.invalid", "tk2", "[]"),
        (1, "脏数据", "http://c.invalid", "tk3", "{不是合法json"),
        (1, "十七节点", "http://d.invalid", "tk4", json.dumps(make_nodes(17))),
    ]
    for uid, name, url, tk, nj in rows:
        con.execute(
            "INSERT INTO subscriptions(user_id,name,url,token,nodes_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (uid, name, url, tk, nj, now, now),
        )
    con.commit()
    con.close()

    cols_before = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(subscriptions)")]
    check("升级前确实没有 node_count 列", "node_count" not in cols_before)

    st = Store(path)
    await st.init()
    got = {r["name"]: r["node_count"] for r in await st.list_subs_meta(1)}
    check("回填: 3 节点", got.get("三节点") == 3, str(got.get("三节点")))
    check("回填: 空订阅=0", got.get("空订阅") == 0, str(got.get("空订阅")))
    check("回填: 脏 JSON 不炸且记 0", got.get("脏数据") == 0, str(got.get("脏数据")))
    check("回填: 17 节点", got.get("十七节点") == 17, str(got.get("十七节点")))

    # 标记位应已落库，重启不再重跑
    await st.close()
    st2 = Store(path)
    await st2.init()
    con = sqlite3.connect(path)
    flag = con.execute("SELECT value FROM meta WHERE key='node_count_backfilled_v1'").fetchone()
    con.close()
    check("回填标记位已落库", bool(flag), str(flag))
    # 人为把某行 node_count 改错，再 init 一次：不该被"重新回填"覆盖回去
    async with st2.connection() as db:
        await db.execute("UPDATE subscriptions SET node_count=999 WHERE token='tk1'")
        await db.commit()
    await st2.close()
    st3 = Store(path)
    await st3.init()
    again = {r["name"]: r["node_count"] for r in await st3.list_subs_meta(1)}
    check("回填只跑一次(不重复执行)", again.get("三节点") == 999, str(again.get("三节点")))
    await st3.close()


# ── 2. 各写入点维护 node_count ──────────────────────────────────────────────
async def test_write_paths() -> None:
    print("=== 2. 各写入点维护 node_count ===")
    st = Store(os.path.join(_tmp, "w.db"))
    await st.init()

    sub = await st.add_sub(1, "新订阅", "http://x.invalid")
    check("add_sub 初始 node_count=0", sub["node_count"] == 0, str(sub["node_count"]))

    up = await st.update_sub(1, int(sub["id"]), nodes_json=json.dumps(make_nodes(25)))
    check("update_sub 写 nodes_json 自动算 25", up["node_count"] == 25, str(up["node_count"]))
    check("update_sub 默认仍回完整行", "nodes_json" in up)

    up2 = await st.update_sub(1, int(sub["id"]), nodes_json=json.dumps(make_nodes(7)))
    check("update_sub 覆盖写 -> 7", up2["node_count"] == 7, str(up2["node_count"]))

    up3 = await st.update_sub(1, int(sub["id"]), name="改个名")
    check("只改 name 时 node_count 不变", up3["node_count"] == 7, str(up3["node_count"]))

    meta = await st.update_sub(1, int(sub["id"]), return_meta=True,
                              nodes_json=json.dumps(make_nodes(11)))
    check("return_meta=True 回瘦行(无 nodes_json)", "nodes_json" not in meta)
    check("return_meta=True 仍带正确 node_count", meta["node_count"] == 11, str(meta["node_count"]))

    imp = await st.add_imported_sub(1, "导入的", make_nodes(33))
    check("add_imported_sub node_count=33", imp["node_count"] == 33, str(imp["node_count"]))

    check("count_subs 正确", await st.count_subs(1) == 2, str(await st.count_subs(1)))
    check("renumber 走 COUNT(*) 结果一致", await st.renumber(1) == 2)
    await st.close()


# ── 3. delete → restore 往返不丢节点(纯 SQL 行拷贝) ─────────────────────────
async def test_delete_restore() -> None:
    print("=== 3. delete → restore 往返 (纯 SQL 拷贝) ===")
    st = Store(os.path.join(_tmp, "d.db"))
    await st.init()
    nodes = make_nodes(64, tag="keep")
    sub = await st.add_imported_sub(1, "待删除", nodes)
    sid, token = int(sub["id"]), sub["token"]
    orig_json = sub["nodes_json"]

    ok = await st.delete_sub(1, sid)
    check("delete_sub 成功", ok)
    check("删除后订阅表已无此行", await st.get_sub(1, sid) is None)

    dels = await st.list_deleted(1)
    check("回收站有 1 条", len(dels) == 1, str(len(dels)))
    check("回收站是瘦行(无 nodes_json)", "nodes_json" not in dels[0])
    check("回收站 node_count 已带过来=64", dels[0]["node_count"] == 64, str(dels[0]["node_count"]))

    restored = await st.restore_deleted(1, int(dels[0]["id"]))
    check("restore 返回非空", restored is not None)
    check("restore 返回瘦行", restored is not None and "nodes_json" not in restored)
    check("restore node_count=64", restored and restored["node_count"] == 64,
          str(restored and restored["node_count"]))
    check("restore 保住原 token", restored and restored["token"] == token)

    full = await st.get_sub(1, int(restored["id"]))
    check("节点数据完整无损(JSON 逐字节相同)", full["nodes_json"] == orig_json,
          f"{len(full['nodes_json'])} vs {len(orig_json)}")
    check("解析回来仍是 64 个节点", len(nodes_of(full)) == 64, str(len(nodes_of(full))))
    check("首末节点名一致",
          nodes_of(full)[0]["name"] == nodes[0]["name"]
          and nodes_of(full)[-1]["name"] == nodes[-1]["name"])
    check("恢复后回收站已清空", len(await st.list_deleted(1)) == 0)

    # 恢复不存在的记录应安全返回 None
    check("restore 不存在的 id 回 None", await st.restore_deleted(1, 99999) is None)
    check("delete 不存在的 id 回 False", await st.delete_sub(1, 99999) is False)
    await st.close()


# ── 4. 瘦查询语义 + nodes_of fail-loud ─────────────────────────────────────
async def test_thin_semantics() -> None:
    print("=== 4. 瘦查询语义 + nodes_of fail-loud ===")
    st = Store(os.path.join(_tmp, "t2.db"))
    await st.init()
    await st.add_imported_sub(1, "有节点", make_nodes(9))

    thin = (await st.list_subs_meta(1))[0]
    fat = (await st.list_subs(1))[0]

    check("瘦行无 nodes_json 键", "nodes_json" not in thin)
    check("瘦行有 node_count", thin["node_count"] == 9, str(thin["node_count"]))
    check("瘦行保留列表页所需字段",
          all(k in thin for k in ("name", "url", "traffic_used", "traffic_total",
                                  "expire_at", "last_error", "token")))
    check("胖行仍有 nodes_json", "nodes_json" in fat)
    check("两者 node_count 一致", thin["node_count"] == fat["node_count"])

    # 关键：瘦行误传 nodes_of 必须响亮报错，不能静默回 []
    try:
        nodes_of(thin)
        check("瘦行传 nodes_of 抛 KeyError", False, "没有抛异常！静默返回了")
    except KeyError as e:
        check("瘦行传 nodes_of 抛 KeyError", True, str(e)[:60])
    except Exception as e:
        check("瘦行传 nodes_of 抛 KeyError", False, f"抛了别的: {type(e).__name__}")

    check("胖行传 nodes_of 正常", len(nodes_of(fat)) == 9, str(len(nodes_of(fat))))
    # 坏 JSON 属数据问题，仍容错回 []
    check("坏 JSON 容错回 []", nodes_of({"nodes_json": "{坏"}) == [])
    check("非 list JSON 容错回 []", nodes_of({"nodes_json": '{"a":1}'}) == [])

    single = await st.get_sub_meta(1, int(thin["id"]))
    check("get_sub_meta 也是瘦行", "nodes_json" not in single)
    check("get_sub_meta node_count 正确", single["node_count"] == 9)
    check("get_sub_meta 查不存在回 None", await st.get_sub_meta(1, 99999) is None)
    await st.close()


# ── 5. 胖行下的真实收益 ────────────────────────────────────────────────────
async def test_fat_row_perf() -> None:
    print(f"=== 5. 胖行性能 ({FAT_NODES} 节点) ===")
    st = Store(os.path.join(_tmp, "fat.db"))
    await st.init()
    fat_nodes = make_nodes(FAT_NODES)
    payload = json.dumps(fat_nodes, ensure_ascii=False)
    mb = len(payload.encode()) / 1024 / 1024
    print(f"  构造胖行: {FAT_NODES} 节点 / {mb:.1f} MB")
    await st.add_imported_sub(1, "巨型订阅", fat_nodes)
    for i in range(8):  # 再放几条正常的，模拟真实分布
        await st.add_imported_sub(1, f"普通{i}", make_nodes(30))

    async def timeit(fn, n=5):
        best = 1e9
        for _ in range(n):
            t0 = time.perf_counter()
            await fn()
            best = min(best, (time.perf_counter() - t0) * 1000)
        return best

    t_fat = await timeit(lambda: st.list_subs(1))
    t_thin = await timeit(lambda: st.list_subs_meta(1))
    t_cnt = await timeit(lambda: st.count_subs(1))
    fat_rows = await st.list_subs(1)
    thin_rows = await st.list_subs_meta(1)
    fat_bytes = len(json.dumps(fat_rows, ensure_ascii=False).encode())
    thin_bytes = len(json.dumps(thin_rows, ensure_ascii=False).encode())
    print(f"  list_subs      (SELECT *) : {t_fat:7.1f} ms")
    print(f"  list_subs_meta (瘦查询)    : {t_thin:7.1f} ms")
    print(f"  count_subs     (COUNT(*)) : {t_cnt:7.1f} ms")
    print(f"  Python 返回体积           : {fat_bytes / 1024:.1f}KB -> {thin_bytes / 1024:.1f}KB")
    # 微秒级热缓存计时受宿主负载影响，不能拿“必须快 2 倍”当 CI 门禁。
    # 真正确定性的收益是：nodes_json 根本没有从 SQLite 搬进 Python。
    check("瘦查询绝不返回 nodes_json", all("nodes_json" not in r for r in thin_rows))
    check("瘦查询返回体积至少缩小 10 倍", thin_bytes * 10 < fat_bytes,
          f"{fat_bytes} -> {thin_bytes} bytes")
    check("瘦查询仍报出正确节点数",
          max(r["node_count"] for r in thin_rows) == FAT_NODES)
    check("count_subs 行数正确", await st.count_subs(1) == 9)

    # 删除/恢复巨型行也不该把 payload 搬进 Python
    sid = int((await st.list_subs_meta(1))[0]["id"])
    t0 = time.perf_counter()
    await st.delete_sub(1, sid)
    t_del = (time.perf_counter() - t0) * 1000
    dels = await st.list_deleted(1)
    t0 = time.perf_counter()
    await st.restore_deleted(1, int(dels[0]["id"]))
    t_res = (time.perf_counter() - t0) * 1000
    print(f"  delete_sub  (INSERT..SELECT): {t_del:7.1f} ms")
    print(f"  restore_deleted             : {t_res:7.1f} ms")
    rows = await st.list_subs_meta(1)
    check("巨型行删->恢复后 node_count 仍正确",
          max(r["node_count"] for r in rows) == FAT_NODES,
          str(max(r["node_count"] for r in rows)))
    full = [r for r in await st.list_subs(1) if r["node_count"] == FAT_NODES][0]
    check("巨型行删->恢复后节点数据无损", len(nodes_of(full)) == FAT_NODES)
    await st.close()


async def main() -> None:
    await test_migration()
    await test_write_paths()
    await test_delete_restore()
    await test_thin_semantics()
    await test_fat_row_perf()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
