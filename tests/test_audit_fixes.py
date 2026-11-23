"""深度走查修复项的回归测试。

覆盖：
1. convert：畸形端口/字段不再让导出崩溃；vless/trojan/vmess 传输参数解析；hy2 obfs；
   sing-box 导出跳过不支持协议并透传原生字段；Surge 不输出 VLESS；响应头毫秒时间戳/inf；
2. db：重置 Token 只吊销精确匹配的短链（LIKE 的 ``_`` 通配符误删）；
3. bot：UTF-16 截断、非 ASCII 数字、CopyTextButton 长度、短链目标换行、内联未授权按钮、
   群聊他人菜单按钮、消息 4096 字符预算、排序后编号稳定、后台巡检不覆盖手动名称、
   分享领取弹窗长度、临期列表翻页、巡检提醒分片、ChosenInlineResult 归属校验、/s/ 非法目标。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

# 只设置 BOT_TOKEN：config 在进程内只加载一次，本文件按字母序最先被 discover 导入，
# 若在这里设置 ALLOWED_USER_IDS/ADMIN_USER_IDS 会改变其它测试文件依赖的授权配置。
# 本文件的流程测试统一 patch bot.allowed，不依赖环境白名单。
os.environ.setdefault("BOT_TOKEN", "0:TEST")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot as B  # noqa: E402
from convert import (  # noqa: E402
    deduplicate_nodes,
    format_expire,
    node_to_share_link,
    parse_share_link,
    parse_subscription_headers,
    to_clash,
    to_singbox,
    to_surge,
)
from db import Store  # noqa: E402


def _long_vmess(i: int) -> str:
    payload = {
        "v": "2", "ps": f"超长节点名称-{i}-" + "x" * 60, "add": f"node{i}.example.com", "port": 443,
        "id": "11111111-2222-3333-4444-555555555555", "aid": 0, "net": "ws", "type": "none",
        "host": "cdn.example.com", "path": "/" + "p" * 120, "tls": "tls", "sni": "cdn.example.com",
    }
    return "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()


class ConvertFixTests(unittest.TestCase):
    def test_malformed_port_and_utls_do_not_crash_exports(self) -> None:
        hop = {"name": "hop", "type": "hysteria2", "server": "h.example.com", "port": "443-445", "password": "p"}
        self.assertEqual(node_to_share_link(hop), "")
        bad_utls = {
            "name": "v", "type": "vless", "server": "v.example.com", "server_port": 443,
            "uuid": "u-1", "tls": {"enabled": True, "utls": "chrome"},
        }
        self.assertTrue(node_to_share_link(bad_utls).startswith("vless://u-1@v.example.com:443"))
        # 去重与 sing-box 导出遇到坏端口也不能抛异常
        self.assertEqual(len(deduplicate_nodes([hop, dict(hop)])), 1)
        out = json.loads(to_singbox([hop, bad_utls]))
        tags = [o["tag"] for o in out["outbounds"]]
        self.assertNotIn("hop", tags)
        self.assertIn("v", tags)

    def test_vless_trojan_vmess_transport_and_hy2_obfs_are_parsed(self) -> None:
        vless = parse_share_link(
            "vless://u-1@a.example.com:443?security=tls&type=ws&path=%2Fws&host=cdn.example.com&sni=a.example.com#WS"
        )
        self.assertEqual(vless["ws-opts"], {"path": "/ws", "headers": {"Host": "cdn.example.com"}})
        trojan = parse_share_link("trojan://pw@t.example.com:443?type=grpc&serviceName=svc#G")
        self.assertEqual(trojan["network"], "grpc")
        self.assertEqual(trojan["grpc-opts"], {"grpc-service-name": "svc"})
        vmess_payload = {"v": "2", "ps": "g", "add": "m.example.com", "port": 443, "id": "u", "net": "grpc", "path": "gsvc"}
        vmess = parse_share_link("vmess://" + base64.b64encode(json.dumps(vmess_payload).encode()).decode())
        self.assertEqual(vmess["grpc-opts"], {"grpc-service-name": "gsvc"})
        self.assertNotIn("ws-opts", vmess)
        hy2 = parse_share_link("hysteria2://pw@h.example.com:443?obfs=salamander&obfs-password=ob#H")
        self.assertEqual((hy2["obfs"], hy2["obfs-password"]), ("salamander", "ob"))
        # Clash 导出带上 ws-opts，sing-box 导出带上 transport / obfs
        self.assertIn("ws-opts", to_clash([vless]))
        sb = {o["tag"]: o for o in json.loads(to_singbox([vless, hy2]))["outbounds"]}
        self.assertEqual(sb["WS"]["transport"], {"type": "ws", "path": "/ws", "headers": {"Host": "cdn.example.com"}})
        self.assertEqual(sb["H"]["obfs"], {"type": "salamander", "password": "ob"})

    def test_singbox_skips_unsupported_and_passes_native_fields(self) -> None:
        nodes = [
            {"name": "snell", "type": "snell", "server": "s.example.com", "port": 443, "psk": "x"},
            {"name": "ssr", "type": "ssr", "server": "r.example.com", "port": 443},
            {"name": "sock", "type": "socks5", "server": "k.example.com", "port": 1080, "username": "u", "password": "p"},
            {
                "name": "native", "type": "hysteria2", "share": "", "server": "n.example.com", "server_port": 443,
                "password": "pw", "up_mbps": 50, "obfs": {"type": "salamander", "password": "o"},
            },
        ]
        out = {o["tag"]: o for o in json.loads(to_singbox(nodes))["outbounds"]}
        self.assertNotIn("snell", out)
        self.assertNotIn("ssr", out)
        self.assertEqual(out["sock"]["type"], "socks")
        self.assertEqual(out["sock"]["username"], "u")
        self.assertEqual(out["native"]["up_mbps"], 50)
        self.assertEqual(out["native"]["obfs"], {"type": "salamander", "password": "o"})
        self.assertNotIn("name", out["native"])

    def test_surge_skips_vless_and_sanitizes_names(self) -> None:
        nodes = [
            {"name": "VL", "type": "vless", "server": "v.example.com", "port": 443, "uuid": "u"},
            {"name": "HK,01=x", "type": "ss", "server": "1.1.1.1", "port": 8388, "cipher": "aes-256-gcm", "password": "p"},
        ]
        out = to_surge(nodes)
        self.assertNotIn("= vless", out)
        self.assertIn("# VL (Surge 不支持 VLESS): vless://", out)
        self.assertIn("HK 01 x = ss, 1.1.1.1, 8388", out)
        self.assertIn("Proxy = select, HK 01 x", out)

    def test_headers_ms_expire_zero_expire_and_inf_traffic(self) -> None:
        ms = parse_subscription_headers({"subscription-userinfo": "upload=1; download=2; total=1073741824; expire=1893456000000"})
        self.assertEqual(ms["expire_at"], 1893456000)
        zero = parse_subscription_headers({"subscription-userinfo": "upload=0; download=0; total=0; expire=0"})
        self.assertIsNone(zero["expire_at"])
        inf = parse_subscription_headers({"subscription-userinfo": "upload=0; download=inf; total=10"})
        self.assertIsNone(inf["traffic_total"])
        self.assertEqual(format_expire(10**15), "未知")


class BotHelperTests(unittest.TestCase):
    def test_utf16_truncate_digits_copy_button_and_url_validation(self) -> None:
        s = "🎁" * 150
        cut = B.truncate_utf16(s, 200)
        self.assertLessEqual(B._utf16_len(cut), 200)
        self.assertFalse(B.is_ascii_digits("²"))
        self.assertFalse(B.is_ascii_digits("①"))
        self.assertTrue(B.is_ascii_digits("12"))
        self.assertIsNone(B.copy_button("x", "https://e.com/" + "a" * 300))
        self.assertIsNotNone(B.copy_button("x", "https://e.com/a"))
        self.assertFalse(B.is_valid_http_url("https://a.com/x\nSet-Cookie: 1"))
        self.assertFalse(B.is_valid_http_url("https:// a.com"))
        self.assertTrue(B.is_valid_http_url("https://a.com/x?y=1"))
        self.assertEqual(B.clean_url_token("https://a.com/sub，"), "https://a.com/sub")
        self.assertEqual(B.parse_share_query("Share x² hello"), (1, None, 10, "x² hello"))

    def test_detail_text_stays_within_telegram_limit(self) -> None:
        nodes = [{"name": "超长节点名称" * 8 + str(i), "type": "ss"} for i in range(80)]
        sub = {
            "id": 1, "name": "N" * 64, "url": "https://example.com/" + "u" * 500, "token": "t",
            "expire_at": None, "traffic_used": None, "traffic_total": None, "updated_at": int(time.time()),
            "last_error": "E" * 2000, "nodes_json": json.dumps(nodes, ensure_ascii=False),
        }
        text = B.detail_text(sub, 1)
        self.assertLessEqual(B._utf16_len(text), 4096)
        self.assertIn("点击下方按钮可翻页浏览全量 80 个节点", text)


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_store = B.store
        B.store = Store(str(Path(self.tmp.name) / "audit.db"))
        await B.store.init()
        self.allowed = patch.object(B, "allowed", AsyncMock(return_value=True))
        self.allowed.start()

    async def asyncTearDown(self) -> None:
        self.allowed.stop()
        await B.store.close()
        B.store = self.orig_store
        self.tmp.cleanup()

    def _cb(self, data: str, *, uid: int = 1, chat: NS | None = None, message: NS | None = None):
        edits: list[tuple[str, dict]] = []
        answers: list[tuple[str | None, dict]] = []
        msg = message or NS(text="", reply_text=AsyncMock(), reply_html=AsyncMock(), reply_document=AsyncMock())
        q = NS(
            data=data,
            message=msg,
            inline_message_id=None,
            answer=AsyncMock(side_effect=lambda t=None, **kw: answers.append((t, kw))),
            edit_message_text=AsyncMock(side_effect=lambda t, **kw: edits.append((t, kw))),
        )
        upd = NS(
            effective_user=NS(id=uid, first_name="U"),
            effective_chat=chat or NS(id=uid, type="private"),
            effective_message=msg,
            callback_query=q,
        )
        return upd, edits, answers

    def _text(self, text: str, *, uid: int = 1):
        replies: list[tuple[str, dict]] = []
        msg = NS(
            text=text,
            reply_to_message=None,
            reply_text=AsyncMock(side_effect=lambda t, **kw: replies.append((t, kw))),
            reply_html=AsyncMock(side_effect=lambda t, **kw: replies.append((t, kw))),
        )
        upd = NS(
            effective_user=NS(id=uid, first_name="U"),
            effective_chat=NS(id=uid, type="private"),
            effective_message=msg,
            callback_query=None,
        )
        return upd, replies

    async def test_rotate_token_only_revokes_exact_short_links(self) -> None:
        a = await B.store.add_sub(1, "A", "https://a.example.com")
        b = await B.store.add_sub(1, "B", "https://b.example.com")
        await B.store.update_sub(1, int(a["id"]), token="tok_A1")
        await B.store.update_sub(1, int(b["id"]), token="tokXA1")
        code_a = await B.store.create_short(1, B.sub_token_url("tok_A1", "clash"))
        code_b = await B.store.create_short(1, B.sub_token_url("tokXA1", "clash"))
        rotated = await B.store.rotate_sub_token(1, int(a["id"]))
        self.assertNotEqual(rotated["token"], "tok_A1")
        self.assertIsNone(await B.store.get_short(code_a))
        # 旧 LIKE '%/sub/tok_A1%' 会把 tokXA1 的短链一起删掉
        self.assertIsNotNone(await B.store.get_short(code_b))
        self.assertIsNone(await B.store.rotate_sub_token(1, 99999))

    async def test_non_ascii_digit_text_and_multiline_short_target(self) -> None:
        ctx = NS(user_data={}, args=[], bot=NS(id=42, username="subsbot"))
        upd, replies = self._text("²")
        await B.handle_text(upd, ctx)  # 旧实现 int("²") 抛 ValueError
        self.assertTrue(replies)
        upd, replies = self._text("短链 https://a.com/x\nSet-Cookie: evil=1")
        await B.handle_text(upd, ctx)
        self.assertIn("不能包含空格或换行", replies[-1][0])
        # 页码跳转同样只接受 ASCII 数字
        B.interaction_data(upd, ctx)["await"] = "list_jump"
        upd2, replies2 = self._text("①")
        await B.handle_text(upd2, ctx)
        self.assertIn("页码必须是数字", replies2[-1][0])

    async def test_unauthorized_inline_query_uses_results_button(self) -> None:
        iq = NS(from_user=NS(id=777, first_name="X"), query="", answer=AsyncMock())
        with patch.object(B, "allowed", AsyncMock(return_value=False)):
            await B.on_inline_query(NS(inline_query=iq), NS(user_data={}, bot=NS(username="subsbot")))
        kwargs = iq.answer.await_args.kwargs
        self.assertNotIn("switch_pm_text", kwargs)
        self.assertEqual(kwargs["button"].start_parameter, "denied")

    async def test_group_member_cannot_hijack_others_menu(self) -> None:
        await B.store.add_sub(1, "Owner", "https://o.example.com")
        group = NS(id=-1001, type="supergroup")
        owner_cmd = NS(from_user=NS(id=1, is_bot=False))
        menu_msg = NS(text="list", reply_to_message=owner_cmd, reply_text=AsyncMock(), reply_html=AsyncMock())
        upd, edits, answers = self._cb("list:page:0", uid=2, chat=group, message=menu_msg)
        await B.on_callback(upd, NS(user_data={}, args=[], bot=NS(username="subsbot")))
        self.assertEqual(edits, [])
        self.assertIn("其他成员的菜单", answers[-1][0])
        # 发起人自己点击正常翻页
        upd, edits, _ = self._cb("list:page:0", uid=1, chat=group, message=menu_msg)
        await B.on_callback(upd, NS(user_data={}, args=[], bot=NS(username="subsbot")))
        self.assertIn("订阅列表", edits[-1][0])

    async def test_node_browser_and_temp_page_respect_message_limit(self) -> None:
        nodes = [{"name": f"N{i}", "type": "vmess", "server": f"s{i}.example.com", "port": 443, "share": _long_vmess(i)} for i in range(15)]
        sub = await B.store.add_imported_sub(1, "Big", nodes)
        upd, edits, _ = self._cb(f"sub:vn:{int(sub['id'])}:0:0")
        await B.on_callback(upd, NS(user_data={}, args=[], bot=NS(username="subsbot")))
        self.assertLessEqual(B._utf16_len(edits[-1][0]), 4096)
        self.assertIn("本页分享链接", edits[-1][0])
        for i in range(20):
            await B.store.add_temp(1, _long_vmess(i), f"临时{i}")
        text, _kb = await B.render_temp_page(1)
        self.assertLessEqual(B._utf16_len(text), 4096)
        self.assertIn("仅展示前", text)

    async def test_sorted_list_keeps_stable_numbers(self) -> None:
        await B.store.add_sub(1, "Zeta", "https://z.example.com")
        await B.store.add_sub(1, "Alpha", "https://a.example.com")
        _text, kb = await B.render_list(1, 0, sort_mode="名称")
        first_label = kb.inline_keyboard[0][0].text
        # 按名称排序后 Alpha 排第一，但编号仍是它按 id 顺序的 #2，与私聊发送 "2" 打开的是同一条
        self.assertIn("#2 Alpha", first_label)

    async def test_background_refresh_keeps_manual_name(self) -> None:
        custom = await B.store.add_sub(1, "我的机场", "https://custom.example.com/sub")
        host_named = await B.store.add_sub(1, "host.example.com", "https://host.example.com/sub")
        fake = AsyncMock(return_value=(
            [{"name": "n", "type": "ss", "server": "1.1.1.1", "port": 8388, "cipher": "aes-256-gcm", "password": "p"}],
            {"traffic_used": None, "traffic_total": None, "expire_at": None, "profile_name": "Provider Title"},
            None,
        ))
        with patch.object(B, "fetch_subscription", fake):
            await B.run_scheduled_maintenance(None)
        self.assertEqual((await B.store.get_sub_meta(1, int(custom["id"])))["name"], "我的机场")
        self.assertEqual((await B.store.get_sub_meta(1, int(host_named["id"])))["name"], "Provider Title")

    async def test_share_claim_alert_is_utf16_bounded_and_binds_inline_id(self) -> None:
        code = await B.store.create_share(5, "Sender", "🎁" * 150, max_views=2)
        upd, _edits, answers = self._cb(f"share:c:{code}", uid=9)
        upd.callback_query.inline_message_id = "IMID-1"
        ctx = NS(user_data={}, args=[], bot=NS(username="subsbot", send_message=AsyncMock()))
        await B.on_callback(upd, ctx)
        self.assertLessEqual(B._utf16_len(answers[-1][0]), 200)
        self.assertEqual((await B.store.get_share(code))["inline_message_id"], "IMID-1")

    async def test_chosen_inline_result_only_binds_for_sender(self) -> None:
        code = await B.store.create_share(5, "Sender", "secret")
        stranger = NS(chosen_inline_result=NS(inline_message_id="X", result_id=f"share_txt_{code}", from_user=NS(id=6)))
        await B.on_chosen_inline_result(stranger, NS())
        self.assertIsNone((await B.store.get_share(code))["inline_message_id"])
        owner = NS(chosen_inline_result=NS(inline_message_id="Y", result_id=f"share_txt_{code}", from_user=NS(id=5)))
        await B.on_chosen_inline_result(owner, NS())
        self.assertEqual((await B.store.get_share(code))["inline_message_id"], "Y")

    async def test_expire_list_has_no_cross_list_pagination(self) -> None:
        now = int(time.time())
        for i in range(12):
            s = await B.store.add_sub(1, f"S{i}", f"https://s{i}.example.com")
            await B.store.update_sub(1, int(s["id"]), expire_at=now + (i + 1) * 3600)
        upd, replies = self._text("临期列表")
        await B.cmd_expire(upd, NS(user_data={}, args=[]))
        kb = replies[-1][1]["reply_markup"]
        datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertNotIn("list:page:1", datas)
        self.assertEqual(sum(1 for d in datas if d and d.startswith("sub:open:")), 12)

    async def test_alerts_are_split_into_sendable_chunks(self) -> None:
        now = int(time.time())
        for i in range(80):
            s = await B.store.add_sub(1, "很长的订阅名称" * 4 + str(i), f"https://a{i}.example.com")
            await B.store.update_sub(1, int(s["id"]), expire_at=now + 3600, traffic_used=99.0, traffic_total=100.0)
        fake_bot = NS(send_message=AsyncMock())
        alerts = await B.check_and_notify_alerts(fake_bot, 1)
        self.assertEqual(len(alerts), 160)
        self.assertGreater(fake_bot.send_message.await_count, 1)
        for call in fake_bot.send_message.await_args_list:
            self.assertLessEqual(B._utf16_len(call.kwargs["text"]), 4096)

    async def test_http_short_rejects_header_injection_target(self) -> None:
        code = await B.store.create_short(1, "https://a.com/x\r\nSet-Cookie: evil=1")
        resp = await B.http_short(NS(match_info={"code": code}))
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
