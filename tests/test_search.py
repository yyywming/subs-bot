"""搜索订阅全流程回归测试（多词/排除/状态/节点检索、分页/排序/导出/刷新与内联查询）。"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardMarkup

os.environ.setdefault("BOT_TOKEN", "0:TEST")
os.environ.setdefault("ALLOWED_USER_IDS", "1,2")
os.environ.setdefault("ADMIN_USER_IDS", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot as B  # noqa: E402
from db import Store  # noqa: E402


class SearchEngineUnitTests(unittest.TestCase):
    def test_parse_search_query_modes(self) -> None:
        q1 = B.parse_search_query("香港 vless subs-bot -过期 -备用 -is:failed")
        self.assertEqual(q1.include_terms, ["香港", "vless", "subs-bot"])
        self.assertEqual(q1.exclude_terms, ["过期", "备用"])
        self.assertIn("expired", q1.exclude_statuses)
        self.assertIn("failed", q1.exclude_statuses)
        self.assertIsNone(q1.preset_key)

        q2 = B.parse_search_query("is:valid")
        self.assertEqual(q2.required_statuses, {"valid"})
        self.assertEqual(q2.preset_key, "valid")

        q3 = B.parse_search_query("香港 #soon status:dead")
        self.assertEqual(q3.include_terms, ["香港"])
        self.assertEqual(q3.required_statuses, {"soon", "dead"})
        self.assertIsNone(q3.preset_key)

        q4 = B.parse_search_query("香港 正常 #v2ray")
        self.assertEqual(q4.include_terms, ["香港", "#v2ray"])
        self.assertEqual(q4.soft_status_terms, [("正常", "valid")])


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_store = B.store
        B.store = Store(str(Path(self.tmp.name) / "search.db"))
        await B.store.init()
        self.allowed = patch.object(B, "allowed", AsyncMock(return_value=True))
        self.allowed.start()

    async def asyncTearDown(self) -> None:
        self.allowed.stop()
        await B.store.close()
        B.store = self.orig_store
        self.tmp.cleanup()

    def _ctx(self, *, args: list[str] | None = None, user_data: dict | None = None):
        return NS(
            user_data={} if user_data is None else user_data,
            args=args or [],
            bot=NS(id=9000, username="subsbot"),
        )

    def _msg(
        self,
        text: str = "",
        *,
        uid: int = 1,
        chat: NS | None = None,
        reply_to_message: NS | None = None,
    ):
        replies: list[tuple[str, dict]] = []

        async def _reply(t: str, **kw):
            replies.append((t, kw))
            return NS()

        msg = NS(
            text=text,
            reply_to_message=reply_to_message,
            reply_text=AsyncMock(side_effect=_reply),
            reply_html=AsyncMock(side_effect=_reply),
        )
        upd = NS(
            effective_user=NS(id=uid, first_name="Tester"),
            effective_chat=chat or NS(id=uid, type="private"),
            effective_message=msg,
            callback_query=None,
        )
        return upd, msg, replies

    def _cb(
        self,
        data: str,
        *,
        uid: int = 1,
        chat: NS | None = None,
        reply_to_message: NS | None = None,
    ):
        edits: list[tuple[str, dict]] = []
        replies: list[tuple[str, dict]] = []
        answers: list[tuple[str | None, dict]] = []
        msg = NS(
            text="",
            reply_to_message=reply_to_message,
            reply_text=AsyncMock(side_effect=lambda t, **kw: replies.append((t, kw))),
            reply_html=AsyncMock(side_effect=lambda t, **kw: replies.append((t, kw))),
        )
        q = NS(
            data=data,
            message=msg,
            inline_message_id=None,
            answer=AsyncMock(side_effect=lambda t=None, **kw: answers.append((t, kw))),
            edit_message_text=AsyncMock(side_effect=lambda t, **kw: edits.append((t, kw))),
        )
        upd = NS(
            effective_user=NS(id=uid, first_name="Tester"),
            effective_chat=chat or NS(id=uid, type="private"),
            effective_message=msg,
            callback_query=q,
        )
        return upd, edits, replies, answers

    async def _seed_sub(
        self,
        user_id: int,
        name: str,
        url: str,
        nodes: list[dict],
        *,
        expire_at: int | None = None,
        traffic_used: float | None = 10.0,
        traffic_total: float | None = 100.0,
        last_error: str | None = None,
    ) -> dict:
        if url.startswith("uploaded://"):
            sub = await B.store.add_imported_sub(user_id, name, nodes)
            fields: dict = {"url": url, "expire_at": expire_at, "traffic_used": traffic_used, "traffic_total": traffic_total, "last_error": last_error}
            updated = await B.store.update_sub(user_id, int(sub["id"]), **fields)
            assert updated is not None
            return updated
        sub = await B.store.add_sub(user_id, name, url)
        updated = await B.store.update_sub(
            user_id,
            int(sub["id"]),
            nodes_json=json.dumps(nodes, ensure_ascii=False),
            expire_at=expire_at,
            traffic_used=traffic_used,
            traffic_total=traffic_total,
            last_error=last_error,
        )
        assert updated is not None
        return updated

    async def test_sqlite_node_search_and_user_isolation(self) -> None:
        # ss 协议精确/别名匹配不得误伤 vmess/vless/password
        s_ss = await self._seed_sub(
            1,
            "CloudAlpha",
            "https://alpha.example.com/sub",
            [{"name": "HK IEPL", "type": "ss", "server": "hk1.nodes.example", "port": 443, "password": "secret"}],
        )
        s_hy2 = await self._seed_sub(
            1,
            "CloudBeta",
            "https://beta.example.com/sub",
            [{"name": "JP 东京", "type": "hysteria2", "server": "jp.nodes.example", "port": 443, "password": "pw"}],
        )
        s_vless = await self._seed_sub(
            1,
            "CloudGamma",
            "https://gamma.example.com/sub",
            [{"name": "US 洛杉矶", "type": "vless", "server": "us.nodes.example", "port": 443, "password": "ss-in-password"}],
        )
        # 他人与旧群（负 ID）数据绝不可泄露
        await self._seed_sub(
            2,
            "OtherUser",
            "https://other.example.com/sub",
            [{"name": "HK IEPL", "type": "ss", "server": "hk1.nodes.example"}],
        )
        await B.store.add_sub(-100999, "LegacyGroup", "https://legacy.example.com/sub")

        res = await B.store.search_sub_node_matches(1, ["ss", "hy2", "iepl", "jp.nodes.example"])
        self.assertEqual(res["ss"], {int(s_ss["id"])})
        self.assertNotIn(int(s_vless["id"]), res["ss"])
        self.assertEqual(res["hy2"], {int(s_hy2["id"])})
        self.assertEqual(res["iepl"], {int(s_ss["id"])})
        self.assertEqual(res["jp.nodes.example"], {int(s_hy2["id"])})

    async def test_multi_term_exclude_status_and_soft_alias(self) -> None:
        now = int(time.time())
        s_valid = await self._seed_sub(
            1,
            "良心云",
            "https://liangxin.example.com/sub",
            [{"name": "香港 IEPL-01", "type": "vless", "server": "hk.liangxin.example"}],
            expire_at=now + 60 * 86400,
            traffic_used=20.0,
            traffic_total=200.0,
        )
        s_soon = await self._seed_sub(
            1,
            "临期机场",
            "https://soon.example.com/sub",
            [{"name": "香港 专线", "type": "trojan", "server": "hk.soon.example"}],
            expire_at=now + 3 * 86400,
            traffic_used=10.0,
            traffic_total=50.0,
        )
        s_expired = await self._seed_sub(
            1,
            "香港老牌机场",
            "https://old.example.com/sub",
            [{"name": "香港 老节点", "type": "ss", "server": "old.example.com"}],
            expire_at=now - 2 * 86400,
            traffic_used=10.0,
            traffic_total=50.0,
        )
        s_drained = await self._seed_sub(
            1,
            "用光机场",
            "https://drained.example.com/sub",
            [{"name": "新加坡", "type": "vmess", "server": "sg.example.com"}],
            expire_at=now + 30 * 86400,
            traffic_used=100.0,
            traffic_total=100.0,
        )
        s_failed = await self._seed_sub(
            1,
            "故障机场",
            "https://fail.example.com/sub",
            [{"name": "日本 备用", "type": "hysteria2", "server": "jp.example.com"}],
            last_error="HTTP 502",
        )
        s_uploaded = await self._seed_sub(
            1,
            "本地配置",
            "uploaded://local.yaml",
            [{"name": "本地节点", "type": "vless", "server": "local.example.com"}],
            expire_at=None,
            traffic_used=None,
            traffic_total=None,
        )
        s_named_expired = await self._seed_sub(
            1,
            "过期备用测试",
            "https://backup.example.com/sub",
            [{"name": "日本 正常", "type": "ss", "server": "bk.example.com"}],
            expire_at=now + 100 * 86400,
        )

        # 跨字段多词组合（订阅名 + 节点名）
        hits = await B.search_subs(1, "良心云 香港 vless")
        self.assertEqual([int(s["id"]) for _, s, _ in hits], [int(s_valid["id"])])
        self.assertTrue(hits[0][2])  # 通过节点内容命中

        # 多词 + -排除词 + -过期 状态排除
        hits_hk = await B.search_subs(1, "香港 -过期 -备用")
        self.assertEqual(
            {int(s["id"]) for _, s, _ in hits_hk},
            {int(s_valid["id"]), int(s_soon["id"])},
        )

        # 6 个预设筛选
        self.assertEqual(
            {int(s["id"]) for _, s, _ in await B.search_subs(1, "is:valid")},
            {int(s_valid["id"]), int(s_soon["id"]), int(s_uploaded["id"]), int(s_named_expired["id"])},
        )
        self.assertEqual({int(s["id"]) for _, s, _ in await B.search_subs(1, "is:soon")}, {int(s_soon["id"])})
        self.assertEqual(
            {int(s["id"]) for _, s, _ in await B.search_subs(1, "is:dead")},
            {int(s_expired["id"]), int(s_drained["id"])},
        )
        self.assertEqual({int(s["id"]) for _, s, _ in await B.search_subs(1, "is:failed")}, {int(s_failed["id"])})
        self.assertEqual({int(s["id"]) for _, s, _ in await B.search_subs(1, "is:uploaded")}, {int(s_uploaded["id"])})
        longterm_ids = {int(s["id"]) for _, s, _ in await B.search_subs(1, "is:longterm")}
        self.assertIn(int(s_uploaded["id"]), longterm_ids)
        self.assertIn(int(s_failed["id"]), longterm_ids)

        # 裸中文软状态词：同时命中真实过期（s_expired）与名字含"过期"的（s_named_expired）
        soft_expired_ids = {int(s["id"]) for _, s, _ in await B.search_subs(1, "过期")}
        self.assertEqual(soft_expired_ids, {int(s_expired["id"]), int(s_named_expired["id"])})

    async def test_pagination_sort_open_and_state_machine(self) -> None:
        now = int(time.time())
        created_ids: list[int] = []
        for i in range(10):
            # 编号 1..10，剩余流量故意与 id 逆序方便测试排序
            sub = await self._seed_sub(
                1,
                f"Aero-{10 - i:02d}",
                f"https://aero{i}.example.com/sub",
                [{"name": f"HK-Node-{i}", "type": "vless", "server": f"n{i}.example.com"}],
                expire_at=now + (30 + i) * 86400,
                traffic_used=float(i * 5),
                traffic_total=100.0,
            )
            created_ids.append(int(sub["id"]))

        ctx = self._ctx()

        # 1. 无参 /s 进入 sub_search 状态机并展示 6 个预设筛选
        u_empty, _, replies_empty = self._msg()
        await B.cmd_search(u_empty, ctx)
        self.assertEqual(B.interaction_data(u_empty, ctx).get("await"), "sub_search")
        self.assertIn("搜索订阅", replies_empty[-1][0])
        preset_datas = [
            b.callback_data
            for row in replies_empty[-1][1]["reply_markup"].inline_keyboard
            for b in row
        ]
        for k in B.SEARCH_PRESETS:
            self.assertIn(f"search:preset:{k}", preset_datas)

        # 2. 发送关键词完成搜索（命中 10 条，第 1 页 8 个按钮 + ➡️ 翻页）
        u_text, _, replies_text = self._msg("Aero")
        await B.handle_text(u_text, ctx)
        self.assertIsNone(B.interaction_data(u_text, ctx).get("await"))
        self.assertEqual(B.interaction_data(u_text, ctx).get("search_query"), "Aero")
        body_p0, kw_p0 = replies_text[-1]
        self.assertIn("第 1/2 页", body_p0)
        self.assertIn("<blockquote>", body_p0)
        self.assertIn("命中: <b>10</b> 条", body_p0)
        self.assertLessEqual(B._utf16_len(body_p0), 4096)
        btns_p0 = [b.callback_data for row in kw_p0["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(sum(1 for d in btns_p0 if d and d.startswith("search:open:")), 8)
        self.assertIn("search:page:1", btns_p0)

        # 3. 翻到第 2 页（仅剩 2 条）
        cb_p1, edits_p1, _, _ = self._cb("search:page:1")
        await B.on_callback(cb_p1, ctx)
        body_p1, kw_p1 = edits_p1[-1]
        self.assertIn("第 2/2 页", body_p1)
        btns_p1 = [b.callback_data for row in kw_p1["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(sum(1 for d in btns_p1 if d and d.startswith("search:open:")), 2)
        self.assertIn("search:page:0", btns_p1)

        # 4. 在搜索页切换排序，固定 #编号 不变
        cb_sort, edits_sort, _, _ = self._cb("search:sort:0")
        await B.on_callback(cb_sort, ctx)
        self.assertEqual(ctx.user_data.get("sort_mode"), "流量")
        # 流量最多的是第 1 条（#1 Aero-10），按名称再切前先确认仍在结果中
        self.assertIn("排序: <b>流量</b>", edits_sort[-1][0])
        self.assertIn("#1 <b>Aero-10</b>", edits_sort[-1][0])

        # 5. 从搜索第 2 页打开详情，返回按钮直指 search:page:1
        target_id = created_ids[-1]
        cb_open, edits_open, _, _ = self._cb(f"search:open:{target_id}:1")
        await B.on_callback(cb_open, ctx)
        open_btns = [b.callback_data for row in edits_open[-1][1]["reply_markup"].inline_keyboard for b in row]
        self.assertIn("search:page:1", open_btns)
        open_texts = [b.text for row in edits_open[-1][1]["reply_markup"].inline_keyboard for b in row]
        self.assertIn("⬅️ 返回搜索", open_texts)

        # 6. 点击 preset 后自动清 await；无匹配含"没有匹配订阅"
        cb_ask, _, _, _ = self._cb("search:ask")
        await B.on_callback(cb_ask, ctx)
        self.assertEqual(B.interaction_data(cb_ask, ctx).get("await"), "sub_search")

        cb_preset, edits_preset, _, _ = self._cb("search:preset:dead")
        await B.on_callback(cb_preset, ctx)
        self.assertIsNone(B.interaction_data(cb_preset, ctx).get("await"))
        self.assertIn("没有匹配订阅", edits_preset[-1][0])

    async def test_search_export_refresh_expired_session_and_group_owner(self) -> None:
        now = int(time.time())
        s_bad = await self._seed_sub(
            1,
            "MySubBad",
            "https://bad.example.com/sub",
            [{"name": "HK Node", "type": "vless", "server": "bad.example.com"}],
            last_error="timeout",
        )
        s_still_bad = await self._seed_sub(
            1,
            "MySubStillBad",
            "https://still.example.com/sub",
            [{"name": "HK Node", "type": "vless", "server": "bad.example.com"}],
            last_error="HTTP 500",
        )
        await self._seed_sub(
            1,
            "OtherHealthy",
            "https://healthy.example.com/sub",
            [{"name": "SG Node", "type": "hy2", "server": "sg.example.com"}],
            expire_at=now + 40 * 86400,
        )

        # /o 支持节点关键词、多词与去重导出
        u_o, _, replies_o = self._msg()
        await B.cmd_search_export(u_o, self._ctx(args=["HK", "vless"]))
        text_o, kw_o = replies_o[-1]
        self.assertIn("搜索导出完成", text_o)
        # s_bad 和 s_still_bad 节点完全同源，dedup 后应为 1 个节点
        self.assertIn("导出节点: <b>1</b>", text_o)
        m = re.search(r"/agg/([^/]+)/clash", text_o)
        self.assertIsNotNone(m)

        # /o 无匹配兼容原有断言文案
        u_o_empty, _, replies_o_empty = self._msg()
        await B.cmd_search_export(u_o_empty, self._ctx(args=["NotExistingKeyword"]))
        self.assertIn("没有匹配订阅", replies_o_empty[-1][0])

        # search:preset:failed -> search:export & search:refresh
        ctx = self._ctx()
        cb_failed, _, _, _ = self._cb("search:preset:failed")
        await B.on_callback(cb_failed, ctx)

        cb_exp, edits_exp, _, _ = self._cb("search:export")
        await B.on_callback(cb_exp, ctx)
        self.assertIn("/agg/", edits_exp[-1][0])
        exp_btns = [b.callback_data for row in edits_exp[-1][1]["reply_markup"].inline_keyboard for b in row]
        self.assertIn("search:page:0", exp_btns)

        async def _fake_refresh(uid: int, sub: dict, rename: bool = False, meta_only: bool = False):
            if int(sub["id"]) == int(s_bad["id"]):
                return await B.store.update_sub(uid, int(sub["id"]), return_meta=meta_only, last_error=None)
            return {**sub, "last_error": "HTTP 500"}

        with patch.object(B, "refresh_sub", AsyncMock(side_effect=_fake_refresh)):
            cb_ref, edits_ref, _, _ = self._cb("search:refresh")
            await B.on_callback(cb_ref, ctx)
        final_ref_text = edits_ref[-1][0]
        self.assertIn("已刷新命中的 2 条订阅", final_ref_text)
        self.assertIn("成功 1 ｜ ❌ 失败 1", final_ref_text)
        # 已恢复的 s_bad 不再出现在 is:failed 结果里，仍失败的保留
        self.assertNotIn("MySubBad", final_ref_text)
        self.assertIn("MySubStillBad", final_ref_text)

        # 会话缺失时点 search:page:0 / search:refresh 优雅退回提示页
        fresh_ctx = self._ctx()
        cb_stale, edits_stale, _, _ = self._cb("search:page:2")
        await B.on_callback(cb_stale, fresh_ctx)
        self.assertIn("搜索会话已过期", edits_stale[-1][0])
        self.assertEqual(B.interaction_data(cb_stale, fresh_ctx).get("await"), "sub_search")

        # 群聊中他人点击发起人的搜索翻页按钮被拦截
        owner_msg = NS(from_user=NS(id=1, is_bot=False))
        group_chat = NS(id=-1001, type="supergroup")
        cb_hijack, edits_hijack, _, answers_hijack = self._cb(
            "search:page:0", uid=2, chat=group_chat, reply_to_message=owner_msg
        )
        await B.on_callback(cb_hijack, self._ctx())
        self.assertFalse(edits_hijack)
        self.assertTrue(answers_hijack and answers_hijack[-1][1].get("show_alert"))

    async def test_inline_query_supports_multi_term_exclude_and_nodes(self) -> None:
        now = int(time.time())
        sub_local = await self._seed_sub(
            1,
            "MyLocal",
            "uploaded://abc.yaml",
            [{"name": "香港 HK IEPL", "type": "hysteria2", "server": "hk.local.example"}],
            expire_at=now + 20 * 86400,
        )
        await self._seed_sub(
            1,
            "ExpiredHK",
            "https://exp.example.com/sub",
            [{"name": "香港 Old", "type": "hysteria2", "server": "old.example.com"}],
            expire_at=now - 86400,
        )
        await B.store.add_temp(1, "vless://u@1.1.1.1:443#HK-Temp", "香港 临时 vless")

        answers: list[list] = []

        async def _answer(results, **_kw):
            answers.append(list(results))

        u_iq_user = NS(id=1, first_name="Tester")
        iq_upd = NS(
            effective_user=u_iq_user,
            inline_query=NS(from_user=u_iq_user, query="香港 hy2 -过期", answer=AsyncMock(side_effect=_answer)),
        )
        await B.on_inline_query(iq_upd, self._ctx())
        # "香港" 和 "hy2" 都匹配节点：sub_local 命中（ExpiredHK 因 -过期 被排除）；
        # 且临时节点名称不含 "hy2"，也不误命中。
        self.assertEqual(len(answers[-1]), 1)
        self.assertEqual(answers[-1][0].id, f"s{int(sub_local['id'])}")
        self.assertIn(f"/sub/{sub_local['token']}/clash", answers[-1][0].description)

        # 搜临时节点多词 + 排除；再测带 is:valid 时临时节点不混入
        iq_temp = NS(
            effective_user=u_iq_user,
            inline_query=NS(from_user=u_iq_user, query="香港 vless", answer=AsyncMock(side_effect=_answer)),
        )
        await B.on_inline_query(iq_temp, self._ctx())
        ids = [r.id for r in answers[-1]]
        self.assertTrue(any(i.startswith("t") for i in ids))

        iq_status = NS(
            effective_user=u_iq_user,
            inline_query=NS(from_user=u_iq_user, query="is:valid", answer=AsyncMock(side_effect=_answer)),
        )
        await B.on_inline_query(iq_status, self._ctx())
        self.assertFalse(any(r.id.startswith("t") for r in answers[-1]))

    async def test_long_search_results_utf16_limit_and_cancel(self) -> None:
        now = int(time.time())
        for i in range(8):
            await self._seed_sub(
                1,
                f"🚀超长搜索名称_{i}_" + ("中" * 45),
                f"https://long{i}.example.com/sub/" + ("u" * 60),
                [{"name": "n", "type": "vless", "server": "a.example.com"}],
                expire_at=now + 20 * 86400,
            )
        text, _kb = await B.render_search_page(1, "超长搜索名称", 0)
        self.assertLessEqual(B._utf16_len(text), 4096)

        ctx = self._ctx()
        u_empty, _, _ = self._msg()
        await B.cmd_search(u_empty, ctx)
        self.assertEqual(B.interaction_data(u_empty, ctx).get("await"), "sub_search")

        u_cancel, _, replies_cancel = self._msg("取消")
        await B.handle_text(u_cancel, ctx)
        self.assertIsNone(B.interaction_data(u_cancel, ctx).get("await"))
        self.assertIn("已取消", replies_cancel[-1][0])


if __name__ == "__main__":
    unittest.main()
