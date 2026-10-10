"""Behavioral regression tests for group text recognition; no Telegram/network calls."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "0:TEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as B
from db import Store


class Message:
    def __init__(self, text: str, reply_to_message=None):
        self.text = text
        self.reply_to_message = reply_to_message
        self.replies: list[str] = []
        self.markups = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def reply_html(self, text: str, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class GroupTextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_store = B.store
        self.old_allowed = B.allowed
        B.store = Store(os.path.join(self.tmp.name, "test.db"))

        async def allowed(uid):
            return uid == 1

        B.allowed = allowed
        self.context = SimpleNamespace(
            user_data={}, args=[], bot=SimpleNamespace(id=999, username="smoke_bot")
        )

    async def asyncTearDown(self):
        await B.store.close()
        B.store = self.old_store
        B.allowed = self.old_allowed
        self.tmp.cleanup()

    def update(self, text, uid=1, chat_id=-100123, reply=None):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=uid),
            effective_chat=SimpleNamespace(
                id=chat_id, type="supergroup" if chat_id < 0 else "private"
            ),
            effective_message=Message(text, reply), callback_query=None,
        )

    async def test_ordinary_group_messages_are_silent_for_every_member(self):
        for uid in (1, 2):
            for text in ("今天吃什么", "今天的订阅列表很长", "123", "x=1", "看这个 https://example.invalid/news"):
                with self.subTest(uid=uid, text=text):
                    update = self.update(text, uid)
                    await B.handle_text(update, self.context)
                    self.assertEqual(update.effective_message.replies, [])
        self.assertEqual(await B.store.list_path_maps(-100123), [])
        self.assertEqual(await B.store.list_subs_meta(-100123), [])

    async def test_exact_menu_still_displays_group_list(self):
        await B.store.add_imported_sub(-100123, "shared-sub", [])
        await B.store.add_imported_sub(1, "private-sub", [])
        update = self.update("订阅列表")
        await B.handle_text(update, self.context)
        labels = [button.text for row in update.effective_message.markups[0].inline_keyboard for button in row]
        self.assertTrue(any("shared-sub" in label for label in labels))
        self.assertFalse(any("private-sub" in label for label in labels))

    async def test_deliberate_node_import_is_shared(self):
        update = self.update("vless://id@example.invalid:443#group-node")
        await B.handle_text(update, self.context)
        rows = await B.store.list_temp(-100123)
        self.assertEqual([(r["name"], r["url"]) for r in rows], [
            ("group-node", "vless://id@example.invalid:443#group-node")
        ])
        self.assertEqual(await B.store.list_temp(1), [])

    async def test_mention_and_bot_reply_allow_number_selection(self):
        await B.store.add_imported_sub(-100123, "shared-detail", [{"name": "n", "type": "vless"}])
        await B.store.add_imported_sub(1, "private-detail", [{"name": "p", "type": "vless"}])
        for text, reply in (
            ("@smoke_bot 1", None),
            ("1", SimpleNamespace(from_user=SimpleNamespace(id=999))),
        ):
            update = self.update(text, reply=reply)
            await B.handle_text(update, self.context)
            detail = "\n".join(update.effective_message.replies)
            self.assertIn("shared-detail", detail)
            self.assertNotIn("private-detail", detail)

    async def test_replying_to_another_bot_does_not_trigger_response(self):
        update = self.update("无关消息", reply=SimpleNamespace(from_user=SimpleNamespace(id=998)))
        await B.handle_text(update, self.context)
        self.assertEqual(update.effective_message.replies, [])


    async def test_unauthorized_explicit_import_cannot_mutate_group(self):
        update = self.update("@smoke_bot vless://id@example.invalid:443#denied", uid=2)
        await B.handle_text(update, self.context)
        self.assertEqual(await B.store.list_temp(-100123), [])
        self.assertEqual(await B.store.list_temp(2), [])

    async def test_senderless_channel_post_cannot_import_or_reply(self):
        update = self.update("vless://id@example.invalid:443#channel")
        update.effective_chat.type = "channel"
        update.effective_user = None
        await B.handle_text(update, self.context)
        self.assertEqual(update.effective_message.replies, [])
        self.assertEqual(await B.store.list_temp(-100123), [])


if __name__ == "__main__":
    unittest.main()
