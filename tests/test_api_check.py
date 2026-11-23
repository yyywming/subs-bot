"""API 批量测试：解析、规范化、探测流程与结果渲染。全部离线，不访问网络。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "0:TEST")
os.environ.setdefault("ALLOWED_USER_IDS", "1,2")
os.environ.setdefault("ADMIN_USER_IDS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import apitest as A  # noqa: E402
import bot as B  # noqa: E402
from db import Store  # noqa: E402

KEY = "c2stNWE1ZjgyM2VhMmYwY2QxMjYzODFmOTIzYzBkNmZjZDE0ZTE0OGI5OGEwNWIzZGU3MmM0Njk0ZjcxZTJmN2JmZQ=="
IMAGE_TEXT = f"https://token.1001949.xyz\n{KEY}"


class FakeContent:
    def __init__(self, body: bytes) -> None:
        self.body, self.pos = body, 0

    async def read(self, n: int) -> bytes:
        chunk = self.body[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


class FakeResp:
    def __init__(self, status: int, payload) -> None:
        self.status = status
        self.charset = "utf-8"
        raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        self.content = FakeContent(raw.encode() if isinstance(raw, str) else raw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, routes: dict[tuple[str, str], FakeResp]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.routes[(method, url)]


async def _probe(routes, url="https://token.1001949.xyz", key=KEY, check=None):
    session = FakeSession(routes)
    res = await A.probe_api(
        url, key, get_session=AsyncMock(return_value=session), check_url=check or AsyncMock(),
    )
    return res, session


class ParseTests(unittest.TestCase):
    def test_detects_address_key_pairs_without_false_positives(self) -> None:
        self.assertTrue(A.looks_like_api_test(IMAGE_TEXT))
        self.assertTrue(A.looks_like_api_test(f"https://a.com|sk-{'x' * 32}"))
        self.assertTrue(A.looks_like_api_test(f"https://a.com sk-{'x' * 32}\nhttps://b.com/v1 sk-{'y' * 32}"))
        self.assertEqual(len(A.parse_api_pairs(f"https://a.com sk-{'x' * 32}\nhttps://b.com/v1 sk-{'y' * 32}")[0]), 2)
        # 订阅链接、名称|链接、带说明的链接、节点分享链接都不能被当成 API 测试
        self.assertFalse(A.looks_like_api_test("https://sub.example.com/api/v1/client/subscribe?token=abc"))
        self.assertFalse(A.looks_like_api_test("机场A|https://sub.example.com/sub"))
        self.assertFalse(A.looks_like_api_test("https://sub.example.com/sub 机场A"))
        self.assertFalse(A.looks_like_api_test("https://a.com/1\nhttps://a.com/2"))
        self.assertFalse(A.looks_like_api_test("https://a.com ss://YWVzLTI1Ni1nY206cGFzc0AxLjEuMS4xOjgzODg="))
        self.assertFalse(A.looks_like_api_test("https://a.com short"))

    def test_normalize_base(self) -> None:
        self.assertEqual(A.normalize_api_base("https://token.1001949.xyz"), "https://token.1001949.xyz/v1")
        self.assertEqual(A.normalize_api_base("https://a.com/v1/"), "https://a.com/v1")
        self.assertEqual(A.normalize_api_base("https://a.com/v1/chat/completions"), "https://a.com/v1")
        self.assertEqual(A.normalize_api_base("https://a.com/v1/models"), "https://a.com/v1")
        self.assertEqual(A.normalize_api_base("https://g.com/v1beta"), "https://g.com/v1beta")
        self.assertEqual(A.normalize_api_base("https://a.com/api/v3"), "https://a.com/api/v3")

    def test_pick_model_prefers_cheap_chat_models(self) -> None:
        self.assertEqual(A.pick_test_model(["text-embedding-3-small", "gpt-4o", "gpt-4o-mini"]), "gpt-4o-mini")
        self.assertEqual(A.pick_test_model(["dall-e-3", "claude-3-opus"]), "claude-3-opus")
        self.assertIsNone(A.pick_test_model([]))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_key_matches_screenshot(self) -> None:
        base = "https://token.1001949.xyz/v1"
        res, session = await _probe({
            ("GET", f"{base}/models"): FakeResp(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}}),
        })
        self.assertFalse(res["ok"])
        self.assertEqual(res["message"], "Invalid API key（HTTP 401）")
        self.assertIsNone(res["model"])
        self.assertEqual(session.calls[0][2]["allow_redirects"], False)
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], f"Bearer {KEY}")
        text = A.format_probe_result(res)
        for piece in (
            f"API 地址: <code>{base}</code>",
            f"API Key: <code>{KEY}</code>",
            "模型数量: 0", "测试模型: 未知模型", "测试结果: ❌ 失败",
            "返回消息: Invalid API key（HTTP 401）", "📡 模型列表（共0个）：",
        ):
            self.assertIn(piece, text)
        self.assertIn("<blockquote expandable>", text)

    async def test_success_lists_models_and_calls_chat(self) -> None:
        base = "https://api.example.com/v1"
        res, session = await _probe(
            {
                ("GET", f"{base}/models"): FakeResp(200, {"data": [{"id": "text-embedding-3-small"}, {"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]}),
                ("POST", f"{base}/chat/completions"): FakeResp(200, {"choices": [{"message": {"content": "ok"}}]}),
            },
            url="https://api.example.com/v1/chat/completions",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["model"], "gpt-4o-mini")
        self.assertEqual(res["message"], "ok")
        self.assertEqual(session.calls[1][2]["json"]["model"], "gpt-4o-mini")
        text = A.format_probe_result(res)
        self.assertIn("模型数量: 3", text)
        self.assertIn("测试结果: ✅ 成功", text)
        self.assertIn("响应耗时:", text)
        self.assertIn("<code>gpt-4o</code>", text)

    async def test_chat_failure_html_body_and_redirect(self) -> None:
        base = "https://api.example.com/v1"
        res, _ = await _probe(
            {
                ("GET", f"{base}/models"): FakeResp(200, {"data": [{"id": "gpt-4o-mini"}]}),
                ("POST", f"{base}/chat/completions"): FakeResp(403, {"error": {"message": "quota exceeded"}}),
            },
            url="https://api.example.com",
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["model"], "gpt-4o-mini")
        self.assertEqual(res["message"], "quota exceeded（HTTP 403）")

        res, _ = await _probe({("GET", f"{base}/models"): FakeResp(200, "<html><body>Just a moment</body></html>")}, url="https://api.example.com")
        self.assertIn("没有返回任何模型", res["message"])
        res, _ = await _probe({("GET", f"{base}/models"): FakeResp(403, "<!DOCTYPE html><html>blocked</html>")}, url="https://api.example.com")
        self.assertIn("返回了网页", res["message"])
        res, _ = await _probe({("GET", f"{base}/models"): FakeResp(302, "")}, url="https://api.example.com")
        self.assertIn("重定向", res["message"])

    async def test_private_address_is_rejected_before_request(self) -> None:
        check = AsyncMock(side_effect=ValueError("URL 指向内网/保留地址: 127.0.0.1"))
        res, session = await _probe({}, url="http://127.0.0.1:8080", check=check)
        self.assertFalse(res["ok"])
        self.assertIn("内网", res["message"])
        self.assertEqual(session.calls, [])

    def test_group_masks_key_and_long_model_list_fits_message(self) -> None:
        res = {
            "base": "https://a.com/v1", "key": KEY, "models": [f"model-{i:05d}-" + "x" * 30 for i in range(2000)],
            "model": "model-00000", "ok": True, "message": "<ok & fine>", "elapsed": 0.5,
        }
        text = A.format_probe_result(res, show_full_key=False)
        self.assertNotIn(KEY, text)
        self.assertIn(A.mask_key(KEY), text)
        self.assertIn("&lt;ok &amp; fine&gt;", text)
        self.assertIn("其余", text)
        self.assertLessEqual(B._utf16_len(text), 4096)


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_store = B.store
        B.store = Store(str(Path(self.tmp.name) / "api.db"))
        await B.store.init()
        self.allowed = patch.object(B, "allowed", AsyncMock(return_value=True))
        self.allowed.start()

    async def asyncTearDown(self) -> None:
        self.allowed.stop()
        await B.store.close()
        B.store = self.orig_store
        self.tmp.cleanup()

    def _update(self, text: str, chat_type: str = "private", chat_id: int = 1):
        status = NS(edits=[])

        async def _edit(t, **kw):
            status.edits.append((t, kw))

        status.edit_text = _edit
        replies: list[tuple[str, dict]] = []

        async def _reply_text(t, **kw):
            replies.append((t, kw))
            return status

        async def _reply_html(t, **kw):
            replies.append((t, kw))
            return status

        msg = NS(text=text, reply_to_message=None, reply_text=_reply_text, reply_html=_reply_html)
        upd = NS(
            effective_user=NS(id=1, first_name="U"),
            effective_chat=NS(id=chat_id, type=chat_type),
            effective_message=msg,
            callback_query=None,
        )
        return upd, replies, status

    async def test_direct_send_runs_api_test_instead_of_adding_subscription(self) -> None:
        fake = AsyncMock(return_value={
            "base": "https://token.1001949.xyz/v1", "key": KEY, "models": [], "model": None,
            "ok": False, "message": "Invalid API key（HTTP 401）", "elapsed": None,
        })
        upd, replies, status = self._update(IMAGE_TEXT)
        ctx = NS(user_data={}, args=[], bot=NS(id=42, username="subsbot"))
        with patch.object(B, "probe_api", fake):
            await B.handle_text(upd, ctx)
        fake.assert_awaited_once()
        self.assertEqual(fake.await_args.args[:2], ("https://token.1001949.xyz", KEY))
        self.assertEqual(await B.store.list_subs_meta(1), [])
        self.assertIn("正在测试 1 组", replies[0][0])
        result_text, kw = status.edits[-1]
        self.assertIn("API 地址: <code>https://token.1001949.xyz/v1</code>", result_text)
        self.assertIn(f"<code>{KEY}</code>", result_text)
        labels = [b.text for row in kw["reply_markup"].inline_keyboard for b in row]
        self.assertIn("📋 复制地址", labels)
        self.assertIn("📋 复制 Key", labels)

    async def test_ai_mode_batch_and_group_masking(self) -> None:
        async def fake_probe(url, key, **kw):
            ok = "good" in url
            return {"base": A.normalize_api_base(url), "key": key, "models": ["m"] if ok else [], "model": "m" if ok else None,
                    "ok": ok, "message": "ok" if ok else "bad（HTTP 401）", "elapsed": 0.1 if ok else None}

        text = f"https://good.example.com|sk-{'a' * 32}\nhttps://bad.example.com sk-{'b' * 32}\n多余文字"
        upd, replies, status = self._update(text, chat_type="supergroup", chat_id=-100)
        ctx = NS(user_data={}, args=[], bot=NS(id=42, username="subsbot"))
        B.interaction_data(upd, ctx)["await"] = "api_test"
        with patch.object(B, "probe_api", side_effect=fake_probe):
            await B.handle_text(upd, ctx)
        self.assertIn("✅ 1 组可用，❌ 1 组失败", status.edits[-1][0])
        self.assertIn("无法配对", status.edits[-1][0])
        results = [r for r in replies if "API 地址" in r[0]]
        self.assertEqual(len(results), 2)
        for body, kw in results:
            self.assertNotIn("a" * 32, body)  # 群聊不回显完整 Key
            labels = [b.text for row in kw["reply_markup"].inline_keyboard for b in row]
            self.assertNotIn("📋 复制 Key", labels)

    async def test_ai_mode_format_error(self) -> None:
        upd, replies, _ = self._update("not-an-api")
        ctx = NS(user_data={}, args=[], bot=NS(id=42, username="subsbot"))
        B.interaction_data(upd, ctx)["await"] = "api_test"
        await B.handle_text(upd, ctx)
        self.assertIn("格式错误", replies[-1][0])


if __name__ == "__main__":
    unittest.main()
