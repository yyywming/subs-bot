"""个人订阅跨会话互通：同一用户在群和私聊共用数据，其他成员看不到也改不了。

不访问网络。负 chat id 上的旧群行只能留给以后有意恢复，个人操作不能暴露或删掉它们。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "0:TEST"
os.environ["ALLOWED_USER_IDS"] = "11,22,33,44"
os.environ["ADMIN_USER_IDS"] = ""
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = os.path.join(_tmp, "personal-scope.db")
os.environ["BOT_USERNAME"] = "subsbot"
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8787"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as B  # noqa: E402

FAILED: list[str] = []
GROUP = -100200300
OTHER = -100200301
SUPER = -100200302
EMPTY_CHAT = -100200399
ACTOR = 11
MEMBER = 22
MISMATCH_USER = 33
EMPTY_USER = 44
ACTOR_PRIVATE = 710011
MEMBER_PRIVATE = 710022
MISMATCH_CHAT = 500


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' | ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


class User:
    def __init__(self, uid: int, first_name: str = "U") -> None:
        self.id = uid
        self.first_name = first_name


class Chat:
    def __init__(self, cid: int, typ: str) -> None:
        self.id = cid
        self.type = typ


class Message:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[Message] = []
        self.edits: list[str] = []
        self.markup = None
        self.reply_to_message = None
        self.document = None

    async def reply_text(self, text: str, **kw):
        msg = Message(text)
        msg.markup = kw.get("reply_markup")
        self.replies.append(msg)
        return msg

    async def reply_html(self, text: str, **kw):
        return await self.reply_text(text, **kw)

    async def reply_document(self, document=None, **kw):
        msg = Message(kw.get("caption") or "")
        msg.document = document
        self.replies.append(msg)
        return msg

    async def edit_text(self, text: str, **kw):
        self.text = text
        self.edits.append(text)
        self.markup = kw.get("reply_markup")


class Query:
    def __init__(self, data: str, message: Message) -> None:
        self.data = data
        self.message = message
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, **kw):
        self.answers.append(text)

    async def edit_message_text(self, text: str, **kw):
        await self.message.edit_text(text, **kw)


class Bot:
    def __init__(self) -> None:
        self.id = 4242
        self.username = "subsbot"
        self.sent: list[tuple] = []
        self.file_payload = b""

    async def send_message(self, chat_id, text="", **kw):
        self.sent.append(("message", chat_id, text))

    async def send_document(self, chat_id, document, **kw):
        raw = getattr(document, "input_file_content", b"") or b""
        body = bytes(raw).decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else ""
        name = getattr(document, "filename", "")
        self.sent.append(("document", chat_id, name, body))

    async def get_file(self, file_id):
        payload = self.file_payload

        class _File:
            async def download_as_bytearray(self):
                return bytearray(payload)

        return _File()


class Update:
    def __init__(self, user: User, chat: Chat | None, text: str = "", data: str | None = None) -> None:
        self.effective_user = user
        self.effective_chat = chat
        self.effective_message = Message(text)
        self.callback_query = None
        self.inline_query = None
        if data is not None:
            self.callback_query = Query(data, self.effective_message)


class Ctx:
    def __init__(self, user_data: dict | None = None, args: list[str] | None = None, bot: Bot | None = None) -> None:
        self.user_data = {} if user_data is None else user_data
        self.args = list(args or [])
        self.bot = bot or Bot()


def buttons(markup) -> list[str]:
    if markup is None:
        return []
    return [btn.text for row in markup.inline_keyboard for btn in row]


def last_reply(update: Update) -> Message | None:
    replies = update.effective_message.replies
    return replies[-1] if replies else None


def reply_text(update: Update) -> str:
    reply = last_reply(update)
    return "" if reply is None else (reply.text or "")


def reply_labels(update: Update) -> list[str]:
    reply = last_reply(update)
    if reply is None:
        return []
    return buttons(reply.markup)


def chat(cid: int, typ: str) -> Chat:
    return Chat(cid, typ)


def blob(*parts: str) -> str:
    return "\n".join(parts)


def has_all(text: str, names: set[str] | list[str]) -> bool:
    return all(name in text for name in names)


def has_none(text: str, names: set[str] | list[str]) -> bool:
    return all(name not in text for name in names)


async def names_of(owner: int) -> list[str]:
    return [str(row["name"]) for row in await B.store.list_subs_meta(owner)]


async def legacy_fingerprint() -> list[tuple]:
    spec = (
        ("subscriptions", "id, user_id, name, url"),
        ("deleted_subs", "id, user_id, name, url"),
        ("temp_nodes", "id, user_id, name, url"),
        ("path_maps", "id, user_id, node_name, remark"),
        ("short_links", "code, user_id, target_url"),
    )
    found: list[tuple] = []
    async with B.store.connection() as db:
        for table, cols in spec:
            cur = await db.execute(
                f"SELECT {cols} FROM {table} WHERE user_id=? ORDER BY 1", (GROUP,)
            )
            found.append((table, tuple(tuple(row) for row in await cur.fetchall())))
    return found


async def offline_refresh(user_id, sub, rename=False, meta_only=False):
    row = dict(sub)
    row["last_error"] = None
    row["node_count"] = row.get("node_count") or 0
    return row


async def main() -> None:
    await B.store.init()
    bot = Bot()
    user_a = User(ACTOR, "甲")
    user_b = User(MEMBER, "乙")
    user_c = User(MISMATCH_USER, "丙")
    user_empty = User(EMPTY_USER, "丁")
    data_a: dict = {}
    data_b: dict = {}
    ctx_a = Ctx(data_a, bot=bot)
    ctx_b = Ctx(data_b, bot=bot)
    group = chat(GROUP, "group")
    other = chat(OTHER, "group")
    super_chat = chat(SUPER, "supergroup")
    private = chat(ACTOR_PRIVATE, "private")
    member_private = chat(MEMBER_PRIVATE, "private")
    original_refresh = B.refresh_sub
    B.refresh_sub = offline_refresh

    async def run_list(user: User, room: Chat, data: dict) -> str:
        update = Update(user, room)
        await B.cmd_list(update, Ctx(data, bot=bot))
        return blob(reply_text(update), "\n".join(reply_labels(update)))

    print("=== 1. 归属函数与会话状态 ===")
    check("私聊归用户，即使 chat id 不同", B.scope_owner(Update(user_a, chat(MISMATCH_CHAT, "private"))) == ACTOR)
    check("群也归操作者，不归负 chat id", B.scope_owner(Update(user_a, group)) == ACTOR)
    check("超群归操作者自己", B.scope_owner(Update(user_b, super_chat)) == MEMBER)
    check("同一个群里两个人归属不同", B.scope_owner(Update(user_b, group)) == MEMBER)
    bucket_g = B.interaction_data(Update(user_a, group), ctx_a)
    bucket_g["await"] = "sub_rename"
    bucket_p = B.interaction_data(Update(user_a, private), ctx_a)
    check("同一用户的私聊等待不看见群等待", bucket_p.get("await") is None)
    check("群等待仍在", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "sub_rename")
    check("乙的用户数据看不到甲的群等待", B.interaction_data(Update(user_b, group), ctx_b).get("await") is None)
    check("sort_mode 不进会话桶", "sort_mode" not in bucket_g)
    bucket_g.clear()

    print("=== 2. 旧群行留着，正数行仍然可见 ===")
    await B.store.add_sub(GROUP, "LegacyGroupSub", "https://example.invalid/legacy-group")
    legacy_dead = await B.store.add_sub(GROUP, "LegacyDeleted", "https://example.invalid/legacy-deleted")
    await B.store.delete_sub(GROUP, int(legacy_dead["id"]))
    await B.store.add_imported_sub(
        GROUP, "LegacyImported", [{"name": "legacy-node", "type": "ss", "share": "ss://legacy"}]
    )
    await B.store.add_temp(GROUP, "vmess://legacy#LegacyTemp", "LegacyTemp")
    await B.store.upsert_path_map(GROUP, "legacy-key", "旧群备注")
    await B.store.create_short(GROUP, "https://example.invalid/legacy-short")
    await B.store.add_sub(ACTOR, "AlreadyMine", "https://example.invalid/already")
    await B.store.add_sub(MEMBER, "MemberPrivate", "https://example.invalid/member-private")
    legacy_before = await legacy_fingerprint()
    check("旧群订阅还在负 id 下", "LegacyGroupSub" in await names_of(GROUP) and "LegacyImported" in await names_of(GROUP))
    check("旧群删除记录还在", any(row["name"] == "LegacyDeleted" for row in await B.store.list_deleted(GROUP)))

    print("=== 3. 群里添加的订阅，私聊和其它群都能看到 ===")
    await B.handle_text(Update(user_a, group, "GroupAdded|https://example.invalid/group-added"), ctx_a)
    await B.handle_text(Update(user_a, private, "PrivateAdded|https://example.invalid/private-added"), ctx_a)
    await B.handle_text(Update(user_a, other, "OtherAdded|https://example.invalid/other-added"), ctx_a)
    await B.handle_text(Update(user_b, group, "MemberAdded|https://example.invalid/member-added"), ctx_b)
    actor_names = {"AlreadyMine", "GroupAdded", "PrivateAdded", "OtherAdded"}
    member_names = {"MemberPrivate", "MemberAdded"}
    hidden_from_actor = {"LegacyGroupSub", "LegacyImported", "MemberPrivate", "MemberAdded"}
    for room in (private, group, other, super_chat):
        shown = await run_list(user_a, room, data_a)
        check(f"甲在 {room.id} 看到自己全部订阅", has_all(shown, actor_names), shown[:180])
        check(f"甲在 {room.id} 看不到他人和旧群", has_none(shown, hidden_from_actor), shown[:180])
    for room in (member_private, group, other, super_chat):
        shown = await run_list(user_b, room, data_b)
        check(f"乙在 {room.id} 只看到自己的", has_all(shown, member_names) and has_none(shown, actor_names | {"LegacyGroupSub", "LegacyImported"}), shown[:180])
    check("新订阅没有写进负 chat id", has_none("\n".join(await names_of(GROUP)), actor_names | member_names))
    check("私聊 chat id 不是另一份库", await names_of(ACTOR_PRIVATE) == [] and await names_of(MEMBER_PRIVATE) == [])

    print("=== 4. 搜索、编号、临期跨会话聚合同一个人 ===")
    search_private = Update(user_a, private)
    await B.cmd_search(search_private, Ctx(data_a, args=["GroupAdded"], bot=bot))
    search_other = Update(user_a, other)
    await B.cmd_search(search_other, Ctx(data_a, args=["GroupAdded"], bot=bot))
    search_member = Update(user_b, group)
    await B.cmd_search(search_member, Ctx(data_b, args=["GroupAdded"], bot=bot))
    search_legacy = Update(user_a, group)
    await B.cmd_search(search_legacy, Ctx(data_a, args=["LegacyGroupSub"], bot=bot))
    check("私聊能搜到群里添的订阅", "GroupAdded" in reply_text(search_private))
    check("另一个群也能搜到", "GroupAdded" in reply_text(search_other))
    check("乙搜不到甲的订阅", "没有匹配订阅" in reply_text(search_member))
    check("搜索不暴露旧群订阅", "没有匹配订阅" in reply_text(search_legacy))

    renumber_group = Update(user_a, group)
    await B.cmd_renumber(renumber_group, ctx_a)
    renumber_private = Update(user_a, private)
    await B.cmd_renumber(renumber_private, ctx_a)
    renumber_member = Update(user_b, member_private)
    await B.cmd_renumber(renumber_member, ctx_b)
    check("群里重置编号数的是甲的全部订阅", "当前 4 条" in reply_text(renumber_group), reply_text(renumber_group))
    check("私聊重置编号与群里一致", "当前 4 条" in reply_text(renumber_private), reply_text(renumber_private))
    check("乙的编号只数自己的", "当前 2 条" in reply_text(renumber_member), reply_text(renumber_member))

    actor_rows = await B.store.list_subs_meta(ACTOR)
    shared = next(row for row in actor_rows if row["name"] == "GroupAdded")
    await B.store.update_sub(ACTOR, int(shared["id"]), expire_at=int(time.time()) + 3 * 86400)
    expire_private = Update(user_a, private)
    await B.cmd_expire(expire_private, ctx_a)
    expire_other = Update(user_a, other)
    await B.cmd_expire(expire_other, ctx_a)
    expire_member = Update(user_b, group)
    await B.cmd_expire(expire_member, ctx_b)
    check("私聊能看到群里那条临期", "共1个" in reply_text(expire_private) and "GroupAdded" in "\n".join(reply_labels(expire_private)))
    check("另一个群看到同一条临期", "共1个" in reply_text(expire_other))
    check("乙看不到甲的临期", "为空" in reply_text(expire_member) and "GroupAdded" not in reply_text(expire_member))

    print("=== 5. 删除匹配只动自己的行 ===")
    await B.handle_text(Update(user_a, group, "DupName|https://example.invalid/dup-actor"), ctx_a)
    await B.handle_text(Update(user_b, private, "DupName|https://example.invalid/dup-member"), ctx_b)
    await B.cmd_delete_match(Update(user_b, group), Ctx(data_b, args=["DupName"], bot=bot))
    check("乙删不掉甲的同名订阅", "DupName" in await names_of(ACTOR))
    check("乙删掉了自己的同名订阅", "DupName" not in await names_of(MEMBER))
    await B.cmd_delete_match(Update(user_a, private), Ctx(data_a, args=["LegacyGroupSub"], bot=bot))
    await B.cmd_delete_match(Update(user_a, other), Ctx(data_a, args=["DupName"], bot=bot))
    check("按旧群名字删除不命中旧行", "LegacyGroupSub" in await names_of(GROUP))
    check("甲在另一个群删掉的是自己的同名订阅", "DupName" not in await names_of(ACTOR))
    check("删除没有波及乙其余订阅", "MemberPrivate" in await names_of(MEMBER) and "MemberAdded" in await names_of(MEMBER))

    print("=== 6. 私聊按钮能改群里添的同一条，乙伪造回调不行 ===")
    actor_rows = await B.store.list_subs_meta(ACTOR)
    shared = next(row for row in actor_rows if row["name"] == "GroupAdded")
    shared_id = int(shared["id"])
    opened = Update(user_a, private, data=f"sub:open:{shared_id}:0")
    await B.on_callback(opened, ctx_a)
    check("甲在私聊打开群里添的订阅", "GroupAdded" in opened.effective_message.text, opened.effective_message.text[:80])

    forged_open = Update(user_b, group, data=f"sub:open:{shared_id}:0")
    await B.on_callback(forged_open, ctx_b)
    check("乙打不开甲的订阅", forged_open.effective_message.text == "订阅不存在", forged_open.effective_message.text)

    refreshed: list[int] = []

    async def recording_refresh(user_id, sub, rename=False, meta_only=False):
        refreshed.append(int(user_id))
        return await offline_refresh(user_id, sub, rename=rename, meta_only=meta_only)

    B.refresh_sub = recording_refresh
    forged_refresh = Update(user_b, private, data=f"sub:refresh:{shared_id}:0")
    await B.on_callback(forged_refresh, Ctx(data_b, bot=bot))
    check("伪造刷新不读甲的订阅", forged_refresh.effective_message.text == "订阅不存在" and refreshed == [])
    B.refresh_sub = offline_refresh

    forged_rename = Update(user_b, group, data=f"sub:rename:{shared_id}:0")
    await B.on_callback(forged_rename, ctx_b)
    check("伪造重命名不进入等待", forged_rename.effective_message.text == "订阅不存在")
    check("乙没有因下重命名状态", B.interaction_data(Update(user_b, group), ctx_b).get("await") is None)

    forged_short = Update(user_b, other, data=f"sub:short:{shared_id}")
    await B.on_callback(forged_short, ctx_b)
    check("伪造短链读不到甲的订阅", "订阅不存在" in reply_text(forged_short), reply_text(forged_short))

    deleted = Update(user_b, group, data=f"sub:del:{shared_id}:0")
    await B.on_callback(deleted, ctx_b)
    check("乙的删除回调失败", "删除失败" in deleted.effective_message.text, deleted.effective_message.text[:80])
    check("甲的订阅还在", "GroupAdded" in await names_of(ACTOR))
    check("伪造删除没动旧群行", "LegacyGroupSub" in await names_of(GROUP))

    removed = Update(user_a, private, data=f"sub:del:{shared_id}:0")
    await B.on_callback(removed, ctx_a)
    check("甲在私聊删掉群里添的那条", "已删除" in removed.effective_message.text and "GroupAdded" not in await names_of(ACTOR))
    gone = await run_list(user_a, group, data_a)
    check("群列表也不再显示这条", "GroupAdded" not in gone and "AlreadyMine" in gone)

    recycle_member = Update(user_b, group)
    await B.cmd_recycle(recycle_member, ctx_b)
    recycle_private = Update(user_a, private)
    await B.cmd_recycle(recycle_private, ctx_a)
    recycle_other = Update(user_a, other)
    await B.cmd_recycle(recycle_other, ctx_a)
    check("乙的回收站没有甲刚删的订阅", "GroupAdded" not in reply_text(recycle_member) and "LegacyDeleted" not in reply_text(recycle_member))
    check("甲的私聊回收站有这条", "GroupAdded" in reply_text(recycle_private) and "LegacyDeleted" not in reply_text(recycle_private))
    check("另一个群的回收站也是同一份", "GroupAdded" in reply_text(recycle_other))
    deleted_rows = await B.store.list_deleted(ACTOR, days=30)
    restore_id = int(next(row for row in deleted_rows if row["name"] == "GroupAdded")["id"])
    forged_restore = Update(user_b, group, data=f"recycle:restore:{restore_id}")
    await B.on_callback(forged_restore, ctx_b)
    check("乙不能恢复甲的回收站", "恢复失败" in forged_restore.effective_message.text)
    check("伪造恢复后记录还在甲这里", "GroupAdded" in [row["name"] for row in await B.store.list_deleted(ACTOR)])
    restored = Update(user_a, super_chat, data=f"recycle:restore:{restore_id}")
    await B.on_callback(restored, ctx_a)
    check("甲在超群里恢复后私聊也能看到", "已恢复" in restored.effective_message.text and "GroupAdded" in await names_of(ACTOR))
    check("恢复没有写成旧群行", "GroupAdded" not in await names_of(GROUP))

    print("=== 7. 临时节点、路径、短链、导出都聚合同一用户 ===")
    await B.store.add_imported_sub(
        ACTOR, "ImportedPersonal", [{"name": "personal-node", "type": "ss", "share": "ss://personal"}]
    )
    await B.store.add_imported_sub(
        MEMBER, "ImportedMember", [{"name": "member-node", "type": "ss", "share": "ss://member"}]
    )
    await B.handle_text(Update(user_a, group, "vmess://actor-temp#ActorTemp"), ctx_a)
    await B.handle_text(Update(user_b, group, "vmess://member-temp#MemberTemp"), ctx_b)
    temp_private = Update(user_a, private)
    await B.cmd_temp(temp_private, ctx_a)
    temp_super = Update(user_a, super_chat)
    await B.cmd_temp(temp_super, ctx_a)
    temp_member = Update(user_b, member_private)
    await B.cmd_temp(temp_member, ctx_b)
    check("私聊能看到群里加入的临时节点", "ActorTemp" in reply_text(temp_private) and has_none(reply_text(temp_private), ["MemberTemp", "LegacyTemp"]))
    check("超群看到同一份临时节点", "ActorTemp" in reply_text(temp_super) and "MemberTemp" not in reply_text(temp_super))
    check("乙只看到自己的临时节点", "MemberTemp" in reply_text(temp_member) and has_none(reply_text(temp_member), ["ActorTemp", "LegacyTemp"]))

    await B.cmd_path(Update(user_a, group), Ctx(data_a, args=["personal-node", "个人备注"], bot=bot))
    await B.cmd_path(Update(user_b, group), Ctx(data_b, args=["member-node", "成员备注"], bot=bot))
    path_private = Update(user_a, private)
    await B.cmd_path(path_private, Ctx(data_a, bot=bot))
    path_member = Update(user_b, other)
    await B.cmd_path(path_member, Ctx(data_b, bot=bot))
    check("私聊能看到群里添的路径", "personal-node" in reply_text(path_private) and has_none(reply_text(path_private), ["member-node", "legacy-key"]))
    check("乙看不到甲的路径", "member-node" in reply_text(path_member) and "personal-node" not in reply_text(path_member))
    actor_path_id = int(next(row["id"] for row in await B.store.list_path_maps(ACTOR) if row["node_name"] == "personal-node"))
    forged_path = Update(user_b, group, data=f"path:delete:{actor_path_id}")
    await B.on_callback(forged_path, ctx_b)
    check("乙删不掉甲的路径", "不存在" in forged_path.effective_message.text)
    check("甲的路径还在", any(row["node_name"] == "personal-node" for row in await B.store.list_path_maps(ACTOR)))
    check("旧群路径还在", any(row["node_name"] == "legacy-key" for row in await B.store.list_path_maps(GROUP)))

    short_update = Update(user_a, group, "短链 https://example.invalid/actor-target")
    await B.handle_text(short_update, ctx_a)
    short_text = reply_text(short_update)
    short_code = re.search(r"/s/([^<]+)", short_text)
    check("群里生成了短链", short_code is not None, short_text)
    if short_code:
        row = await B.store.get_short(short_code.group(1))
        check("普通短链归用户而不是群", row is not None and int(row["user_id"]) == ACTOR, str(row))
    member_short = Update(user_b, private, "短链 https://example.invalid/member-target")
    await B.handle_text(member_short, ctx_b)
    member_text = reply_text(member_short)
    member_code = re.search(r"/s/([^<]+)", member_text)
    check("乙在私聊生成了短链", member_code is not None, member_text)
    if member_code:
        row = await B.store.get_short(member_code.group(1))
        check("乙的短链归乙", row is not None and int(row["user_id"]) == MEMBER, str(row))

    export_update = Update(user_a, super_chat)
    await B.cmd_export(export_update, ctx_a)
    export_text = reply_text(export_update)
    export_code = re.search(r"短链码: <code>([^<]+)</code>", export_text)
    check("超群导出生成聚合短链", export_code is not None, export_text)
    if export_code:
        agg = await B.store.get_short(export_code.group(1))
        check("聚合短链归甲", agg is not None and int(agg["user_id"]) == ACTOR and agg["target_url"] == "aggregate://all")
        if agg is not None:
            nodes = await B.mapped_nodes_for_user(int(agg["user_id"]))
            node_names = [str(node.get("name") or "") for node in nodes]
            joined = "\n".join(node_names)
            check("聚合含个人节点和路径备注", "personal-node" in joined and "个人备注" in joined, joined)
            check("聚合含各会话加入的临时节点", "ActorTemp" in joined, joined)
            check("聚合不含乙和旧群", has_none(joined, ["member-node", "legacy-node", "MemberTemp", "LegacyTemp", "成员备注", "旧群备注"]))

    search_export = Update(user_a, private)
    await B.cmd_search_export(search_export, Ctx(data_a, args=["ImportedPersonal"], bot=bot))
    search_text = reply_text(search_export)
    search_code = re.search(r"/agg/([^/]+)/clash", search_text)
    check("私聊搜索导出能命中个人库", search_code is not None, search_text)
    member_export = Update(user_b, group)
    await B.cmd_search_export(member_export, Ctx(data_b, args=["ImportedPersonal"], bot=bot))
    check("乙搜索导出看不到甲的订阅", "没有匹配订阅" in reply_text(member_export))
    if search_code:
        row = await B.store.get_short(search_code.group(1))
        check(
            "搜索聚合归甲且只指向自己的 id",
            row is not None and int(row["user_id"]) == ACTOR and str(row["target_url"]).startswith("aggregate://ids/"),
            str(row),
        )
        if row is not None and str(row["target_url"]).startswith("aggregate://ids/"):
            payload = str(row["target_url"]).split("aggregate://ids/", 1)[1]
            ids = [int(part) for part in payload.split(",") if part]
            nodes = await B.mapped_nodes_for_user(int(row["user_id"]), ids)
            joined = "\n".join(str(node.get("name") or "") for node in nodes)
            check("按 id 聚合只展开甲的订阅", "personal-node" in joined and has_none(joined, ["member-node", "legacy-node"]))

    await B.cmd_temp(Update(user_a, other), Ctx(data_a, args=["clear"], bot=bot))
    await B.cmd_temp(Update(user_b, group), ctx_b)
    check("甲在别的群清空临时节点后自己没了", await B.store.list_temp(ACTOR) == [])
    check("清空不影响乙", any(row["name"] == "MemberTemp" for row in await B.store.list_temp(MEMBER)))
    check("清空不删旧群临时节点", any(row["name"] == "LegacyTemp" for row in await B.store.list_temp(GROUP)))
    await B.cmd_path(Update(user_a, private), Ctx(data_a, args=["clear"], bot=bot))
    check("私聊清空路径只清甲的", await B.store.list_path_maps(ACTOR) == [])
    check("乙的路径还在", any(row["node_name"] == "member-node" for row in await B.store.list_path_maps(MEMBER)))
    check("旧群路径没被清空", any(row["node_name"] == "legacy-key" for row in await B.store.list_path_maps(GROUP)))

    print("=== 8. 重命名和其它等待只在原会话消费 ===")
    actor_rows = await B.store.list_subs_meta(ACTOR)
    shared = next(row for row in actor_rows if row["name"] == "GroupAdded")
    shared_id = int(shared["id"])
    await B.on_callback(Update(user_a, group, data=f"sub:rename:{shared_id}:0"), ctx_a)
    check("重命名等待记在甲的这个群", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "sub_rename")
    await B.handle_text(Update(user_a, private, "HijackName"), ctx_a)
    await B.handle_text(Update(user_b, group, "HijackName"), ctx_b)
    await B.handle_text(Update(user_a, other, "HijackName"), ctx_a)
    check("其它会话没有改名", "GroupAdded" in await names_of(ACTOR) and "HijackName" not in await names_of(ACTOR))
    check("等待仍只留在原群", B.interaction_data(Update(user_a, group), ctx_a).get("rename_sub_id") == shared_id)
    await B.handle_text(Update(user_a, private, "📦 订阅列表"), ctx_a)
    check("私聊菜单不清掉群里的等待", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "sub_rename")
    await B.handle_text(Update(user_a, group, "RealPersonalName"), ctx_a)
    check("原群发来的新名称写入个人订阅", "RealPersonalName" in await names_of(ACTOR))
    check("私聊列表看到的是同一条", "RealPersonalName" in await run_list(user_a, private, data_a))
    check("新名称没有变成群行", "RealPersonalName" not in await names_of(GROUP))
    check("原群等待已消费", B.interaction_data(Update(user_a, group), ctx_a).get("await") is None)

    await B.on_callback(Update(user_a, group, data="path:add"), ctx_a)
    await B.handle_text(Update(user_a, private, "crosskey 不该写入"), ctx_a)
    await B.handle_text(Update(user_b, group, "crosskey 不该写入"), ctx_b)
    await B.handle_text(Update(user_a, other, "crosskey 不该写入"), ctx_a)
    check("其它会话没有写入路径", all(row["node_name"] != "crosskey" for row in await B.store.list_path_maps(ACTOR)))
    check("群等待还在", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "path_add")
    await B.handle_text(Update(user_a, group, "crosskey 群路径"), ctx_a)
    check("原群的路径等待写入个人路径", any(row["node_name"] == "crosskey" for row in await B.store.list_path_maps(ACTOR)))
    path_seen = Update(user_a, private)
    await B.cmd_path(path_seen, Ctx(data_a, bot=bot))
    check("私聊能看到刚在群里写入的路径", "crosskey" in reply_text(path_seen))
    check("这条路径不属于群 id", all(row["node_name"] != "crosskey" for row in await B.store.list_path_maps(GROUP)))

    await B.cmd_api_test(Update(user_a, group), ctx_a)
    await B.handle_text(Update(user_a, private, "not-an-api"), ctx_a)
    check("私聊没有吃掉群里的 API 等待", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "api_test")
    api_group = Update(user_a, group, "not-an-api")
    await B.handle_text(api_group, ctx_a)
    check("群里的 API 等待只在本群消费", "格式错误" in reply_text(api_group))
    check("API 等待消费后清除", B.interaction_data(Update(user_a, group), ctx_a).get("await") is None)

    await B.cmd_inline_sort(Update(user_a, group), Ctx(data_a, bot=bot))
    check("排序等待在群桶里", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "inline_sort")
    await B.handle_text(Update(user_a, private, "名称"), ctx_a)
    check("其它会话不能提交排序", data_a.get("sort_mode") is None)
    sort_group = Update(user_a, group, "名称")
    await B.handle_text(sort_group, ctx_a)
    check("本群提交写入全局 sort_mode", data_a.get("sort_mode") == "名称", str(data_a.get("sort_mode")))
    check("sort_mode 不写进会话桶", B.interaction_data(Update(user_a, group), ctx_a).get("sort_mode") is None)

    print("=== 9. 内联查询仍用发起人，能看到他在群里添的个人订阅 ===")
    await B.store.add_sub(ACTOR, "SortMeAlpha", "https://example.invalid/sort-alpha")
    alpha = next(row for row in await B.store.list_subs_meta(ACTOR) if row["name"] == "SortMeAlpha")
    await B.store.update_sub(ACTOR, int(alpha["id"]), traffic_used=0, traffic_total=1)
    await B.store.add_sub(ACTOR, "SortMeBravo", "https://example.invalid/sort-bravo")
    bravo = next(row for row in await B.store.list_subs_meta(ACTOR) if row["name"] == "SortMeBravo")
    await B.store.update_sub(ACTOR, int(bravo["id"]), traffic_used=0, traffic_total=100)
    data_a.setdefault("interaction", {}).setdefault(GROUP, {})["sort_mode"] = "流量"
    data_a["sort_mode"] = "名称"

    class InlineQuery:
        def __init__(self, user: User, query: str) -> None:
            self.from_user = user
            self.query = query
            self.results = None

        async def answer(self, results, **kw):
            self.results = results

    iq = InlineQuery(user_a, "SortMe")
    inline_update = Update(user_a, None)
    inline_update.inline_query = iq
    await B.on_inline_query(inline_update, ctx_a)
    titles = [item.title for item in (iq.results or [])]
    check("内联结果来自发起人", any("SortMeAlpha" in title for title in titles) and any("SortMeBravo" in title for title in titles), str(titles))
    alpha_at = next(i for i, title in enumerate(titles) if "SortMeAlpha" in title)
    bravo_at = next(i for i, title in enumerate(titles) if "SortMeBravo" in title)
    check("内联排序看全局 sort_mode 而不是会话桶", alpha_at < bravo_at, str(titles))

    group_iq = InlineQuery(user_a, "RealPersonalName")
    group_inline = Update(user_a, None)
    group_inline.inline_query = group_iq
    await B.on_inline_query(group_inline, ctx_a)
    group_titles = [item.title for item in (group_iq.results or [])]
    check("群里改名后的个人订阅能被内联查到", any("RealPersonalName" in title for title in group_titles), str(group_titles))

    legacy_iq = InlineQuery(user_a, "LegacyGroupSub")
    legacy_inline = Update(user_a, None)
    legacy_inline.inline_query = legacy_iq
    await B.on_inline_query(legacy_inline, ctx_a)
    check("内联查不到旧群订阅", [item.title for item in (legacy_iq.results or [])] == [])

    member_iq = InlineQuery(user_a, "MemberPrivate")
    member_inline = Update(user_a, None)
    member_inline.inline_query = member_iq
    await B.on_inline_query(member_inline, ctx_a)
    check("内联查不到乙的私聊订阅", [item.title for item in (member_iq.results or [])] == [])

    print("=== 10. 批量更新仍发到当前会话，刷的是个人订阅 ===")
    batch_refreshed: list[int] = []

    async def record_refresh(user_id, sub, rename=False, meta_only=False):
        batch_refreshed.append(int(user_id))
        row = dict(sub)
        row["last_error"] = None
        row["node_count"] = 1
        return row

    B.refresh_sub = record_refresh
    bot.sent.clear()
    batch = Update(user_a, group, data="upd:run")
    await B.run_update_all(batch, ctx_a)
    doc_chats = [item[1] for item in bot.sent if item[0] == "document"]
    msg_chats = [item[1] for item in bot.sent if item[0] == "message"]
    bodies = "\n".join(item[3] for item in bot.sent if item[0] == "document")
    check("群批量文件发给群", doc_chats == [GROUP, GROUP, GROUP], str(doc_chats))
    check("群批量结果消息发给群", msg_chats and all(cid == GROUP for cid in msg_chats), str(msg_chats))
    check("批量刷新的是甲的全部订阅", batch_refreshed and set(batch_refreshed) == {ACTOR}, str(batch_refreshed))
    check("结果文件含个人订阅", "RealPersonalName" in bodies and "AlreadyMine" in bodies, bodies[:220])
    check("结果文件不含乙和旧群", has_none(bodies, ["MemberPrivate", "LegacyGroupSub", "LegacyImported"]))

    bot.sent.clear()
    batch_refreshed.clear()
    empty = Update(user_empty, chat(EMPTY_CHAT, "group"), data="upd:run")
    await B.run_update_all(empty, Ctx(bot=bot))
    check("没有订阅的人在群里得到的提示发给该群", bot.sent == [("message", EMPTY_CHAT, "暂无订阅可更新。")], str(bot.sent))
    check("空结果不会去刷别人的订阅", batch_refreshed == [])

    await B.store.add_sub(MISMATCH_USER, "DeliverMe", "https://example.invalid/deliver")
    bot.sent.clear()
    batch_refreshed.clear()
    mismatch = Update(user_c, chat(MISMATCH_CHAT, "private"), data="upd:run")
    await B.run_update_all(mismatch, Ctx(bot=bot))
    mismatch_chats = [item[1] for item in bot.sent]
    mismatch_body = "\n".join(item[3] for item in bot.sent if item[0] == "document")
    check("私聊归属仍是用户 id", batch_refreshed == [MISMATCH_USER], str(batch_refreshed))
    check("投递用 chat id，可以和归属不同", mismatch_chats and all(cid == MISMATCH_CHAT for cid in mismatch_chats), str(mismatch_chats))
    check("投递内容是这个用户的订阅", "DeliverMe" in mismatch_body, mismatch_body[:180])
    B.refresh_sub = original_refresh

    print("=== 11. 私密分享领取仍记操作者并私聊送达 ===")
    code = await B.store.create_share(ACTOR, "甲", "secret-group-claim", max_views=2)
    claim = Update(user_b, group, data=f"share:c:{code}")
    bot.sent.clear()
    await B.on_callback(claim, ctx_b)
    dm = [item for item in bot.sent if item[0] == "message"]
    check("领取内容私聊给领取人", any(item[1] == MEMBER and "secret-group-claim" in item[2] for item in dm), str(dm))
    check("领取内容不发到群 id", all(item[1] != GROUP for item in dm))
    async with B.store.connection() as db:
        cur = await db.execute("SELECT user_id FROM share_claims WHERE share_code=?", (code,))
        claim_row = await cur.fetchone()
    check("领取记录是用户 id", claim_row is not None and int(claim_row["user_id"]) == MEMBER)

    start_code = await B.store.create_share(ACTOR, "甲", "secret-start-claim")
    start = Update(user_b, group)
    await B.cmd_start(start, Ctx(data_b, args=[f"claim_{start_code}"], bot=bot))
    check("start 领取回执在当前消息里", "secret-start-claim" in reply_text(start))
    async with B.store.connection() as db:
        cur = await db.execute("SELECT user_id FROM share_claims WHERE share_code=?", (start_code,))
        start_row = await cur.fetchone()
    check("start 领取也不记成群 id", start_row is not None and int(start_row["user_id"]) == MEMBER)

    print("=== 12. 群里上传的节点文件成为个人订阅 ===")
    vless = "vless://11111111-1111-1111-1111-111111111111@example.invalid:443#\u4e2d\u6587\u8282\u70b9\n"
    bot.file_payload = vless.encode()

    class Doc:
        file_name = "group-nodes.txt"
        file_size = len(bot.file_payload)
        file_id = "file-nodes"

    doc_update = Update(user_a, group)
    doc_update.effective_message.document = Doc()
    await B.handle_document(doc_update, ctx_a)
    status = last_reply(doc_update)
    status_text = "" if status is None else (status.text or "")
    check("文件导入回执是订阅而不是临时节点", "group-nodes" in status_text and "临时节点" not in status_text, status_text)
    imported_rows = [row for row in await B.store.list_subs(ACTOR) if row["name"] == "group-nodes"]
    check("文件订阅归上传者，名字是文件名", len(imported_rows) == 1 and str(imported_rows[0]["url"]).startswith("uploaded://"))
    if imported_rows:
        node_names = [str(node.get("name") or "") for node in B.nodes_of(imported_rows[0])]
        check("文件里的 VLESS 节点进了这条订阅", node_names == ["中文节点"], str(node_names))
    check("文件内容没有写进临时节点", await B.store.list_temp(ACTOR) == [] and await B.store.list_temp(GROUP) != [])
    check("文件订阅不归群", "group-nodes" not in await names_of(GROUP))
    check("私聊列表看得到群里上传的文件", "group-nodes" in await run_list(user_a, private, data_a))
    check("乙从群里看不到这份文件订阅", "group-nodes" not in await run_list(user_b, group, data_b))

    print("=== 13. 个人操作结束后旧群行原样还在 ===")
    legacy_after = await legacy_fingerprint()
    check("负 id 的订阅、回收站、临时节点、路径、短链都没被改动", legacy_after == legacy_before)
    check("原有正数行仍然可见", "AlreadyMine" in await run_list(user_a, group, data_a))

    await B.store.close()
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
