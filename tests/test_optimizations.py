"""针对本次全面优化的回归与功能单元测试：
1. YAML/Sing-box 结构化节点逆向生成分享链接与 Base64 导出
2. Sing-box <-> Clash 跨协议字段归一化、同名节点消歧与临时节点完整解析
3. 节点去重、subscription-userinfo / profile-title HTTP 响应头透传
4. SQLite 索引、同目标短链复用、内联分享草稿防抖与过期数据清理
5. 列表一键排序切换与后台定时巡检 / 临期低流量主动提醒（含 24h 冷却去重）
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "0:TEST")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("ADMIN_USER_IDS", "1")

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot as B  # noqa: E402
import config  # noqa: E402
from convert import (  # noqa: E402
    deduplicate_nodes,
    format_subscription_userinfo,
    node_to_share_link,
    parse_nodes_from_text,
    parse_share_link,
    to_base64_sub,
    to_clash,
    to_qx,
    to_singbox,
    to_surge,
)
from db import Store  # noqa: E402


class OptimizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_store = B.store
        self.db_path = str(Path(self.tmp.name) / "opt.db")
        B.store = Store(self.db_path)
        await B.store.init()

    async def asyncTearDown(self) -> None:
        await B.store.close()
        B.store = self.orig_store
        self.tmp.cleanup()

    def test_reverse_share_link_synthesis_from_yaml_and_singbox(self) -> None:
        yaml_doc = """
proxies:
  - name: YAML-SS
    type: ss
    server: ss.example.com
    port: 8388
    cipher: aes-256-gcm
    password: my-password
  - name: YAML-VMess
    type: vmess
    server: vmess.example.com
    port: 443
    uuid: 11111111-1111-1111-1111-111111111111
    alterId: 0
    cipher: auto
    tls: true
    servername: vmess.example.com
    network: ws
    ws-opts:
      path: /ws
      headers:
        Host: vmess.example.com
  - name: YAML-VLESS
    type: vless
    server: vless.example.com
    port: 443
    uuid: 22222222-2222-2222-2222-222222222222
    tls: true
    servername: vless.example.com
    network: ws
    client-fingerprint: chrome
  - name: YAML-Trojan
    type: trojan
    server: tj.example.com
    port: 443
    password: tj-secret
    sni: tj.example.com
    skip-cert-verify: true
  - name: YAML-HY2
    type: hysteria2
    server: hy2.example.com
    port: 8443
    password: hy2-secret
    sni: hy2.example.com
    skip-cert-verify: true
  - name: YAML-TUIC
    type: tuic
    server: tuic.example.com
    port: 8443
    uuid: 33333333-3333-3333-3333-333333333333
    password: tuic-secret
    sni: tuic.example.com
"""
        nodes = parse_nodes_from_text(yaml_doc)
        self.assertEqual(len(nodes), 6)
        # YAML 解析出的节点原始 share 字段保持为空字符串，向后兼容既有测试
        self.assertTrue(all(n["share"] == "" for n in nodes))

        # 逆向生成分享链接并导出 Base64，应能完整还原全部 6 个节点
        b64 = to_base64_sub(nodes)
        reparsed = parse_nodes_from_text(b64)
        self.assertEqual(
            [(n["name"], n["type"], n["server"], n["port"]) for n in reparsed],
            [
                ("YAML-SS", "ss", "ss.example.com", 8388),
                ("YAML-VMess", "vmess", "vmess.example.com", 443),
                ("YAML-VLESS", "vless", "vless.example.com", 443),
                ("YAML-Trojan", "trojan", "tj.example.com", 443),
                ("YAML-HY2", "hysteria2", "hy2.example.com", 8443),
                ("YAML-TUIC", "tuic", "tuic.example.com", 8443),
            ],
        )

    def test_cross_protocol_conversion_and_duplicate_name_disambiguation(self) -> None:
        singbox_doc = json.dumps(
            {
                "outbounds": [
                    {
                        "type": "shadowsocks",
                        "tag": "DupNode",
                        "server": "1.1.1.1",
                        "server_port": 8388,
                        "method": "aes-256-gcm",
                        "password": "p1",
                    },
                    {
                        "type": "shadowsocks",
                        "tag": "DupNode",
                        "server": "2.2.2.2",
                        "server_port": 8388,
                        "method": "chacha20-ietf-poly1305",
                        "password": "p2",
                    },
                ]
            }
        )
        nodes = parse_nodes_from_text(singbox_doc)
        clash_yaml = to_clash(nodes)
        clash_nodes = parse_nodes_from_text(clash_yaml)
        self.assertEqual([n["name"] for n in clash_nodes], ["DupNode", "DupNode #2"])
        self.assertEqual([n["type"] for n in clash_nodes], ["ss", "ss"])
        self.assertEqual([n["port"] for n in clash_nodes], [8388, 8388])
        self.assertEqual([n["cipher"] for n in clash_nodes], ["aes-256-gcm", "chacha20-ietf-poly1305"])

        sb_out = json.loads(to_singbox(clash_nodes))
        real_obs = [o for o in sb_out["outbounds"] if o["type"] == "shadowsocks"]
        self.assertEqual(len(real_obs), 2)
        self.assertEqual(real_obs[0]["server_port"], 8388)
        self.assertEqual(real_obs[0]["method"], "aes-256-gcm")

        surge_out = to_surge(clash_nodes)
        self.assertIn("DupNode = ss, 1.1.1.1, 8388", surge_out)
        self.assertIn("DupNode #2 = ss, 2.2.2.2, 8388", surge_out)

        qx_out = to_qx(clash_nodes)
        self.assertIn("shadowsocks=1.1.1.1:8388", qx_out)

    async def test_temp_node_parsing_and_deduplication_and_http_headers(self) -> None:
        vless_uri = "vless://11111111-1111-1111-1111-111111111111@hk.example.com:443?security=tls&sni=hk.example.com#HKNode"
        sub = await B.store.add_imported_sub(1, "机场A", parse_nodes_from_text(vless_uri))
        exp_ts = int(time.time()) + 7 * 86400
        await B.store.update_sub(
            1, int(sub["id"]), traffic_used=12.5, traffic_total=100.0, expire_at=exp_ts
        )
        # 添加相同端点的临时节点与新端点的临时节点
        await B.store.add_temp(1, vless_uri, "HKNode-Dup")
        await B.store.add_temp(
            1,
            "trojan://secret@jp.example.com:443?sni=jp.example.com#JPNode",
            "JPNode",
        )
        mapped = await B.mapped_nodes_for_user(1, dedup=True)
        self.assertEqual(len(mapped), 2)
        # 临时节点应已被解析为结构化节点，可导出至 Clash
        clash_out = to_clash(mapped)
        reparsed = parse_nodes_from_text(clash_out)
        self.assertEqual({n["server"] for n in reparsed}, {"hk.example.com", "jp.example.com"})

        # 验证 HTTP /sub 与 /agg 透传 subscription-userinfo 和 profile-title
        req_sub = NS(
            match_info={"token": sub["token"], "kind": "clash"},
            query={},
        )
        resp_sub = await B.http_sub(req_sub)
        self.assertEqual(resp_sub.status, 200)
        self.assertIn("subscription-userinfo", resp_sub.headers)
        self.assertIn(f"expire={exp_ts}", resp_sub.headers["subscription-userinfo"])
        self.assertIn("profile-title", resp_sub.headers)

        agg_code = await B.store.create_short(1, "aggregate://all")
        req_agg = NS(
            match_info={"code": agg_code, "kind": "singbox"},
            query={},
        )
        resp_agg = await B.http_agg(req_agg)
        self.assertEqual(resp_agg.status, 200)
        self.assertIn("subscription-userinfo", resp_agg.headers)

    async def test_db_shortlink_reuse_and_share_draft_debounce_and_cleanup(self) -> None:
        c1 = await B.store.create_short(1, "https://example.com/target")
        c2 = await B.store.create_short(1, "https://example.com/target")
        c3 = await B.store.create_short(2, "https://example.com/target")
        self.assertEqual(c1, c2)
        self.assertNotEqual(c1, c3)

        # 相同内容草稿分享复用同一 code
        s1 = await B.store.create_share(1, "Alice", "hello", max_views=2)
        s2 = await B.store.create_share(1, "Alice", "hello", max_views=2)
        self.assertEqual(s1, s2)

        # purge_unsent_share_drafts 可清理刚创建的未发送草稿
        purged = await B.store.purge_unsent_share_drafts(1, within_seconds=5)
        self.assertEqual(purged, 1)

        # 索引已创建
        async with B.store.connection() as db:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            )
            idx_names = {r["name"] for r in await cur.fetchall()}
        self.assertIn("idx_subscriptions_user_id", idx_names)
        self.assertIn("idx_short_links_user_target", idx_names)

    async def test_list_sort_callback_and_scheduled_alerts(self) -> None:
        s1 = await B.store.add_sub(1, "B-Sub", "https://example.com/1")
        s2 = await B.store.add_sub(1, "A-Sub", "https://example.com/2")
        now = int(time.time())
        await B.store.update_sub(
            1, int(s1["id"]), traffic_used=98.0, traffic_total=100.0, expire_at=now + 86400
        )
        await B.store.update_sub(
            1, int(s2["id"]), traffic_used=10.0, traffic_total=100.0, expire_at=now + 20 * 86400
        )

        # 测试列表排序按钮切换
        edits = []
        msg = NS(edit_text=AsyncMock(side_effect=lambda t, **kw: edits.append((t, kw))))
        q = NS(
            data="list:sort:0",
            message=msg,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(side_effect=lambda t, **kw: edits.append((t, kw))),
        )
        upd = NS(
            effective_user=NS(id=1),
            effective_chat=NS(id=1, type="private"),
            effective_message=msg,
            callback_query=q,
        )
        ctx = NS(user_data={}, args=[], bot=NS(send_message=AsyncMock()))
        with patch.object(B, "allowed", AsyncMock(return_value=True)):
            await B.on_callback(upd, ctx)
        self.assertEqual(ctx.user_data.get("sort_mode"), "流量")

        # 测试临期与低流量提醒（含 24h 冷却去重）
        fake_bot = NS(send_message=AsyncMock())
        alerts_first = await B.check_and_notify_alerts(fake_bot, 1)
        self.assertEqual(len(alerts_first), 2)
        self.assertEqual(fake_bot.send_message.await_count, 1)

        # 冷却期内再次检查不重复发送
        alerts_second = await B.check_and_notify_alerts(fake_bot, 1)
        self.assertEqual(alerts_second, [])
        self.assertEqual(fake_bot.send_message.await_count, 1)


if __name__ == "__main__":
    unittest.main()
