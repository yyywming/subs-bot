"""群共享订阅：归属、隔离、回调修改、跨会话状态、批量投递。不访问网络。"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["BOT_TOKEN"] = "0:TEST"
os.environ["ALLOWED_USER_IDS"] = ""
os.environ["ADMIN_USER_IDS"] = ""
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = os.path.join(_tmp, "group-scope.db")
os.environ["BOT_USERNAME"] = "subsbot"
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8787"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as B  # noqa: E402

FAILED: list[str] = []
GROUP = -100200300
OTHER = -100200301
SUPER = -100200302
ACTOR = 11
MEMBER = 22
PRIVATE_CHAT = 11
MISMATCH_USER = 33
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


def chat(cid: int, typ: str) -> Chat:
    return Chat(cid, typ)


async def names_of(owner: int) -> list[str]:
    return [str(row["name"]) for row in await B.store.list_subs_meta(owner)]


async def main() -> None:
    await B.store.init()
    bot = Bot()
    user_a = User(ACTOR, "甲")
    user_b = User(MEMBER, "乙")
    data_a: dict = {}
    data_b: dict = {}
    ctx_a = Ctx(data_a, bot=bot)
    ctx_b = Ctx(data_b, bot=bot)
    group = chat(GROUP, "group")
    other = chat(OTHER, "group")
    super_chat = chat(SUPER, "supergroup")
    private = chat(PRIVATE_CHAT, "private")
    original_refresh = B.refresh_sub

    async def offline_refresh(user_id, sub, rename=False, meta_only=False):
        row = dict(sub)
        row["last_error"] = None
        row["node_count"] = row.get("node_count") or 1
        return row

    print("=== 1. 归属函数 ===")
    check("私聊归属用户，即使 chat id 不同", B.scope_owner(Update(user_a, chat(MISMATCH_CHAT, "private"))) == ACTOR)
    check("群归属负 chat id", B.scope_owner(Update(user_a, group)) == GROUP)
    check("超群归属自己的 chat id", B.scope_owner(Update(user_b, super_chat)) == SUPER)
    bucket_g = B.interaction_data(Update(user_a, group), ctx_a)
    bucket_g["await"] = "sub_rename"
    bucket_p = B.interaction_data(Update(user_a, private), ctx_a)
    check("同一用户的私聊等待不看见群等待", bucket_p.get("await") is None)
    check("群等待仍在", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "sub_rename")
    check("sort_mode 不进会话桶", "sort_mode" not in bucket_g)
    bucket_g.clear()

    print("=== 2. 甲添加，乙看见，其它会话看不见 ===")
    B.refresh_sub = offline_refresh
    add_group = Update(user_a, group, "GroupSharedSub|https://example.invalid/group-shared")
    await B.handle_text(add_group, ctx_a)
    add_private = Update(user_a, private, "PrivateOnlySub|https://example.invalid/private-only")
    await B.handle_text(add_private, ctx_a)
    add_other = Update(user_a, other, "OtherGroupSub|https://example.invalid/other-group")
    await B.handle_text(add_other, ctx_a)
    add_super = Update(user_b, super_chat, "SuperSharedSub|https://example.invalid/super")
    await B.handle_text(add_super, ctx_b)
    B.refresh_sub = original_refresh

    check("群订阅写在负 chat id 下", "GroupSharedSub" in await names_of(GROUP))
    check("私聊订阅仍在正用户 id 下", "PrivateOnlySub" in await names_of(ACTOR))
    check("群里没有私聊订阅", "PrivateOnlySub" not in await names_of(GROUP))
    check("私聊没有群订阅", "GroupSharedSub" not in await names_of(ACTOR))

    list_b = Update(user_b, group)
    ctx_b.args = []
    await B.cmd_list(list_b, ctx_b)
    listed = last_reply(list_b)
    listed_buttons = buttons(listed.markup) if listed else []
    check("乙的列表含群订阅", any("GroupSharedSub" in item for item in listed_buttons), str(listed_buttons))
    check("乙的列表不含甲的私聊订阅", all("PrivateOnlySub" not in item for item in listed_buttons))

    list_private = Update(user_a, private)
    await B.cmd_list(list_private, ctx_a)
    private_buttons = buttons(last_reply(list_private).markup)
    check("私聊列表含原订阅", any("PrivateOnlySub" in item for item in private_buttons))
    check("私聊列表不含群订阅", all("GroupSharedSub" not in item for item in private_buttons))

    list_other = Update(user_a, other)
    await B.cmd_list(list_other, ctx_a)
    other_text = last_reply(list_other).text
    other_buttons = buttons(last_reply(list_other).markup)
    check("其它群看不见这个群的订阅", "GroupSharedSub" not in other_text and all("GroupSharedSub" not in item for item in other_buttons))
    check("其它群看得见自己的订阅", any("OtherGroupSub" in item for item in other_buttons))

    print("=== 3. 搜索 / 编号 / 临期按会话隔离 ===")
    ctx_a.args = ["GroupShared"]
    search_group = Update(user_b, group)
    await B.cmd_search(search_group, Ctx(data_b, args=["GroupShared"], bot=bot))
    search_private = Update(user_a, private)
    await B.cmd_search(search_private, Ctx(data_a, args=["GroupShared"], bot=bot))
    check("乙能搜到群订阅", "GroupSharedSub" in last_reply(search_group).text)
    check("私聊搜不到群订阅", "没有匹配订阅" in last_reply(search_private).text)

    renumber_group = Update(user_b, group)
    await B.cmd_renumber(renumber_group, ctx_b)
    renumber_private = Update(user_a, private)
    await B.cmd_renumber(renumber_private, ctx_a)
    check("群重置编号只数群订阅", "当前 1 条" in last_reply(renumber_group).text, last_reply(renumber_group).text)
    check("私聊重置编号只数私聊订阅", "当前 1 条" in last_reply(renumber_private).text, last_reply(renumber_private).text)

    group_rows = await B.store.list_subs_meta(GROUP)
    shared_for_expire = next(row for row in group_rows if row["name"] == "GroupSharedSub")
    await B.store.update_sub(GROUP, int(shared_for_expire["id"]), expire_at=int(time.time()) + 3 * 86400)
    expire_group = Update(user_b, group)
    await B.cmd_expire(expire_group, ctx_b)
    expire_private = Update(user_a, private)
    await B.cmd_expire(expire_private, ctx_a)
    check("乙看见群临期", "共1个" in last_reply(expire_group).text, last_reply(expire_group).text)
    check("私聊临期不含群订阅", "为空" in last_reply(expire_private).text, last_reply(expire_private).text)

    print("=== 4. 删除匹配不波及私聊 ===")
    B.refresh_sub = offline_refresh
    await B.handle_text(Update(user_a, group, "DupToken|https://example.invalid/dup-group"), ctx_a)
    await B.handle_text(Update(user_a, private, "DupToken|https://example.invalid/dup-private"), ctx_a)
    B.refresh_sub = original_refresh
    await B.cmd_delete_match(Update(user_b, group), Ctx(data_b, args=["DupToken"], bot=bot))
    check("乙删掉的是群里的同名订阅", "DupToken" not in await names_of(GROUP))
    check("私聊同名订阅还在", "DupToken" in await names_of(ACTOR))
    check("群共享订阅还在", "GroupSharedSub" in await names_of(GROUP))

    print("=== 5. 乙的回调能改群订阅，回收站也归群 ===")
    group_rows = await B.store.list_subs_meta(GROUP)
    shared = next(row for row in group_rows if row["name"] == "GroupSharedSub")
    shared_id = int(shared["id"])
    await B.store.update_sub(
        GROUP,
        shared_id,
        nodes_json='[{"name":"kept-node","type":"ss","share":"ss://kept"}]',
    )
    opened = Update(user_b, group, data=f"sub:open:{shared_id}:0")
    await B.on_callback(opened, ctx_b)
    check("乙能打开群订阅详情", "GroupSharedSub" in opened.effective_message.text, opened.effective_message.text[:80])

    deleted = Update(user_b, group, data=f"sub:del:{shared_id}:0")
    await B.on_callback(deleted, ctx_b)
    check("乙的删除回调写的是群", "GroupSharedSub" not in await names_of(GROUP))
    check("删除回调不碰私聊", "PrivateOnlySub" in await names_of(ACTOR))
    check("删除后的列表是群列表", "订阅列表为空" in deleted.effective_message.text or "已删除" in deleted.effective_message.text)

    recycle_private = Update(user_a, private)
    await B.cmd_recycle(recycle_private, ctx_a)
    recycle_group = Update(user_b, group)
    await B.cmd_recycle(recycle_group, ctx_b)
    check("私聊回收站没有群订阅", "GroupSharedSub" not in last_reply(recycle_private).text)
    check("群回收站有刚删的订阅", "GroupSharedSub" in last_reply(recycle_group).text)
    deleted_rows = await B.store.list_deleted(GROUP, days=30)
    shared_deleted = next(row for row in deleted_rows if row["name"] == "GroupSharedSub")
    restore_id = int(shared_deleted["id"])
    restored = Update(user_b, group, data=f"recycle:restore:{restore_id}")
    await B.on_callback(restored, ctx_b)
    check("乙能从群回收站恢复", "GroupSharedSub" in await names_of(GROUP), restored.effective_message.text)
    check("恢复后私聊仍没有该订阅", "GroupSharedSub" not in await names_of(ACTOR))

    print("=== 6. 临时节点 / 路径 / 文件导入 / 短链聚合 ===")
    await B.handle_text(Update(user_a, group, "vmess://group-temp#GroupTemp"), ctx_a)
    await B.handle_text(Update(user_a, private, "vmess://private-temp#PrivateTemp"), ctx_a)
    temp_group = Update(user_b, group)
    await B.cmd_temp(temp_group, ctx_b)
    temp_private = Update(user_a, private)
    await B.cmd_temp(temp_private, ctx_a)
    temp_other = Update(user_a, other)
    await B.cmd_temp(temp_other, ctx_a)
    check("乙看见群临时节点", "GroupTemp" in last_reply(temp_group).text)
    check("私聊只看见自己的临时节点", "PrivateTemp" in last_reply(temp_private).text and "GroupTemp" not in last_reply(temp_private).text)
    check("其它群没有这份临时节点", "临时列表为空" in last_reply(temp_other).text)

    await B.cmd_temp(Update(user_a, private), Ctx(data_a, args=["clear"], bot=bot))
    await B.cmd_temp(temp_group, ctx_b)
    check("私聊清空不影响群临时节点", "GroupTemp" in last_reply(temp_group).text)

    await B.cmd_path(Update(user_a, group), Ctx(data_a, args=["groupkey", "群备注"], bot=bot))
    await B.cmd_path(Update(user_a, private), Ctx(data_a, args=["privkey", "私备注"], bot=bot))
    path_group = Update(user_b, group)
    await B.cmd_path(path_group, ctx_b)
    path_private = Update(user_a, private)
    await B.cmd_path(path_private, Ctx(data_a, bot=bot))
    check("乙看见群路径", "groupkey" in last_reply(path_group).text and "privkey" not in last_reply(path_group).text)
    check("私聊路径不含群规则", "privkey" in last_reply(path_private).text and "groupkey" not in last_reply(path_private).text)
    maps = await B.store.list_path_maps(GROUP)
    path_id = int(maps[0]["id"])
    await B.on_callback(Update(user_b, group, data=f"path:delete:{path_id}"), ctx_b)
    check("乙能删群路径", not await B.store.list_path_maps(GROUP))
    check("私聊路径还在", any(row["node_name"] == "privkey" for row in await B.store.list_path_maps(ACTOR)))
    await B.cmd_path(Update(user_b, group), Ctx(data_b, args=["group-node", "群备注"], bot=bot))

    class Doc:
        file_name = "nodes.txt"
        file_size = 32
        file_id = "file-1"

    bot.file_payload = b"vmess://from-file#FileTemp\n"
    doc_update = Update(user_b, group)
    doc_update.effective_message.document = Doc()
    await B.handle_document(doc_update, ctx_b)
    status = last_reply(doc_update)
    check("文件导入回执在群里", status is not None and "临时节点" in (status.text or ""), status.text if status else "")
    check("文件临时节点归群", any(row["name"] == "FileTemp" for row in await B.store.list_temp(GROUP)))
    check("文件临时节点不进导入者私聊", all(row["name"] != "FileTemp" for row in await B.store.list_temp(ACTOR)))

    short_update = Update(user_b, group, "短链 https://example.invalid/target-b")
    await B.handle_text(short_update, ctx_b)
    short_text = last_reply(short_update).text
    short_code = re.search(r"/s/([^<]+)", short_text)
    check("群里生成了短链", short_code is not None, short_text)
    if short_code:
        row = await B.store.get_short(short_code.group(1))
        check("普通短链归属群", row is not None and int(row["user_id"]) == GROUP, str(row))

    imported = await B.store.add_imported_sub(
        GROUP,
        "ImportedGroup",
        [{"name": "group-node-keyword", "type": "ss", "share": "ss://group-node"}],
    )
    await B.store.add_imported_sub(
        ACTOR,
        "ImportedPrivate",
        [{"name": "private-node-keyword", "type": "ss", "share": "ss://private-node"}],
    )
    B.refresh_sub = offline_refresh
    export_update = Update(user_b, group)
    await B.cmd_export(export_update, ctx_b)
    export_text = last_reply(export_update).text
    export_code = re.search(r"短链码: <code>([^<]+)</code>", export_text)
    check("群导出生成聚合短链", export_code is not None, export_text)
    if export_code:
        agg = await B.store.get_short(export_code.group(1))
        check("聚合短链归属群", agg is not None and int(agg["user_id"]) == GROUP and agg["target_url"] == "aggregate://all")
        if agg is not None:
            nodes = await B.mapped_nodes_for_user(int(agg["user_id"]))
            node_names = [str(node.get("name") or "") for node in nodes]
            check("聚合含群节点和路径备注", any("群备注" in name for name in node_names), str(node_names))
            check("聚合含群临时节点", any(name == "GroupTemp" for name in node_names), str(node_names))
            check("聚合不含私聊节点", all("private-node" not in name and name != "PrivateTemp" for name in node_names))
    search_export = Update(user_b, group)
    await B.cmd_search_export(search_export, Ctx(data_b, args=["ImportedGroup"], bot=bot))
    search_export_text = last_reply(search_export).text
    search_code = re.search(r"/agg/([^/]+)/clash", search_export_text)
    check("搜索导出链接指向聚合", search_code is not None, search_export_text)
    if search_code:
        row = await B.store.get_short(search_code.group(1))
        check("搜索聚合归属群", row is not None and int(row["user_id"]) == GROUP and str(row["target_url"]).startswith("aggregate://ids/"))
        if row is not None and "ImportedGroup" in await names_of(GROUP):
            ids = [int(imported["id"])]
            nodes = await B.mapped_nodes_for_user(int(row["user_id"]), ids)
            check("按 id 聚合只展开群订阅", any("group-node-keyword" in str(node.get("name")) for node in nodes) and all("private-node" not in str(node.get("name")) for node in nodes))
    B.refresh_sub = original_refresh

    print("=== 7. 等待状态不能被其它成员或其它会话消费 ===")
    group_rows = await B.store.list_subs_meta(GROUP)
    shared = next(row for row in group_rows if row["name"] == "GroupSharedSub")
    shared_id = int(shared["id"])
    await B.on_callback(Update(user_a, group, data=f"sub:rename:{shared_id}:0"), ctx_a)
    check("重命名等待记在甲的这个群", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "sub_rename")

    hijack_private = Update(user_a, private, "HijackName")
    await B.handle_text(hijack_private, ctx_a)
    hijack_member = Update(user_b, group, "HijackName")
    await B.handle_text(hijack_member, ctx_b)
    hijack_other = Update(user_a, other, "HijackName")
    await B.handle_text(hijack_other, ctx_a)
    check("私聊文本没有改群名", (await names_of(GROUP)).count("GroupSharedSub") == 1)
    check("乙的群消息没有改群名", "HijackName" not in await names_of(GROUP))
    check("甲在其它群的文本没有改群名", "HijackName" not in await names_of(OTHER))
    check("等待仍只留在原群", B.interaction_data(Update(user_a, group), ctx_a).get("rename_sub_id") == shared_id)

    menu_private = Update(user_a, private, "📦 订阅列表")
    await B.handle_text(menu_private, ctx_a)
    check("私聊菜单不清掉群里的等待", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "sub_rename")
    renamed = Update(user_a, group, "RealGroupName")
    await B.handle_text(renamed, ctx_a)
    check("甲在原群发来的新名称生效", "RealGroupName" in await names_of(GROUP), str(await names_of(GROUP)))
    check("重命名不写进私聊", "RealGroupName" not in await names_of(ACTOR))
    check("原群等待已消费", B.interaction_data(Update(user_a, group), ctx_a).get("await") is None)

    await B.on_callback(Update(user_a, group, data="path:add"), ctx_a)
    await B.handle_text(Update(user_a, private, "crosskey 不该写入"), ctx_a)
    await B.handle_text(Update(user_b, group, "crosskey 不该写入"), ctx_b)
    check("其它会话没有写入群路径", all(row["node_name"] != "crosskey" for row in await B.store.list_path_maps(GROUP)))
    await B.handle_text(Update(user_a, group, "crosskey 群路径"), ctx_a)
    check("原群的路径等待可以写入", any(row["node_name"] == "crosskey" for row in await B.store.list_path_maps(GROUP)))
    check("这条路径不进私聊", all(row["node_name"] != "crosskey" for row in await B.store.list_path_maps(ACTOR)))

    await B.cmd_api_test(Update(user_a, group), ctx_a)
    await B.handle_text(Update(user_a, private, "not-an-api"), ctx_a)
    check("私聊没有吃掉群里的 API 等待", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "api_test")
    api_group = Update(user_a, group, "not-an-api")
    await B.handle_text(api_group, ctx_a)
    check("群里的 API 等待只在本群消费", "格式错误" in last_reply(api_group).text)
    check("API 等待消费后清除", B.interaction_data(Update(user_a, group), ctx_a).get("await") is None)

    await B.cmd_inline_sort(Update(user_a, group), Ctx(data_a, args=[], bot=bot))
    check("排序等待在群桶里", B.interaction_data(Update(user_a, group), ctx_a).get("await") == "inline_sort")
    check("进入等待时不改全局排序", data_a.get("sort_mode") is None)
    await B.handle_text(Update(user_a, private, "名称"), ctx_a)
    check("其它会话不能提交排序", data_a.get("sort_mode") is None)
    sort_group = Update(user_a, group, "名称")
    await B.handle_text(sort_group, ctx_a)
    check("本群提交写入全局 sort_mode", data_a.get("sort_mode") == "名称", str(data_a.get("sort_mode")))
    check("sort_mode 不写进会话桶", B.interaction_data(Update(user_a, group), ctx_a).get("sort_mode") is None)

    print("=== 8. 内联查询仍是个人订阅 ===")
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
    check("内联结果来自个人订阅", any("SortMeAlpha" in title for title in titles) and any("SortMeBravo" in title for title in titles), str(titles))
    check("内联结果不含群订阅", all("RealGroupName" not in title and "GroupShared" not in title for title in titles))
    alpha_at = next(i for i, title in enumerate(titles) if "SortMeAlpha" in title)
    bravo_at = next(i for i, title in enumerate(titles) if "SortMeBravo" in title)
    check("内联排序看全局 sort_mode 而不是会话桶", alpha_at < bravo_at, str(titles))

    group_iq = InlineQuery(user_a, "RealGroupName")
    group_inline = Update(user_a, None)
    group_inline.inline_query = group_iq
    await B.on_inline_query(group_inline, ctx_a)
    group_titles = [item.title for item in (group_iq.results or [])]
    check("用群订阅名做内联也查不到群数据", group_titles == [], str(group_titles))

    print("=== 9. 批量更新文件发到当前会话，归属可以和投递不同 ===")
    refreshed: list[int] = []

    async def record_refresh(user_id, sub, rename=False, meta_only=False):
        refreshed.append(int(user_id))
        row = dict(sub)
        row["last_error"] = None
        row["node_count"] = 1
        return row

    B.refresh_sub = record_refresh
    bot.sent.clear()
    batch = Update(user_b, group, data="upd:run")
    await B.run_update_all(batch, ctx_b)
    doc_chats = [item[1] for item in bot.sent if item[0] == "document"]
    msg_chats = [item[1] for item in bot.sent if item[0] == "message"]
    bodies = "\n".join(item[3] for item in bot.sent if item[0] == "document")
    check("群批量文件发给群", doc_chats == [GROUP, GROUP, GROUP], str(doc_chats))
    check("群批量结果消息发给群", msg_chats and all(cid == GROUP for cid in msg_chats), str(msg_chats))
    check("批量刷新的是群订阅", refreshed and set(refreshed) == {GROUP}, str(refreshed))
    check("结果文件含群订阅", "RealGroupName" in bodies, bodies[:180])
    check("结果文件不含私聊订阅", "PrivateOnlySub" not in bodies and "SortMeAlpha" not in bodies)

    bot.sent.clear()
    refreshed.clear()
    empty = Update(user_a, chat(-100200399, "group"), data="upd:run")
    await B.run_update_all(empty, ctx_a)
    check("空群提示发给该群而不是操作者私聊", bot.sent == [("message", -100200399, "暂无订阅可更新。")], str(bot.sent))
    check("空群不会去刷操作者的私聊订阅", refreshed == [])

    await B.store.add_sub(MISMATCH_USER, "DeliverMe", "https://example.invalid/deliver")
    bot.sent.clear()
    refreshed.clear()
    mismatch = Update(User(MISMATCH_USER, "丙"), chat(MISMATCH_CHAT, "private"), data="upd:run")
    await B.run_update_all(mismatch, Ctx(bot=bot))
    mismatch_chats = [item[1] for item in bot.sent]
    mismatch_body = "\n".join(item[3] for item in bot.sent if item[0] == "document")
    check("私聊归属仍是用户 id", refreshed == [MISMATCH_USER], str(refreshed))
    check("投递用 chat id，可以和归属不同", mismatch_chats and all(cid == MISMATCH_CHAT for cid in mismatch_chats), str(mismatch_chats))
    check("投递内容是这个用户的订阅", "DeliverMe" in mismatch_body, mismatch_body[:180])
    B.refresh_sub = original_refresh

    print("=== 10. 私密分享领取仍记操作者并私聊送达 ===")
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
    check("start 领取回执在当前消息里", "secret-start-claim" in last_reply(start).text)
    async with B.store.connection() as db:
        cur = await db.execute("SELECT user_id FROM share_claims WHERE share_code=?", (start_code,))
        start_row = await cur.fetchone()
    check("start 领取也不记成群 id", start_row is not None and int(start_row["user_id"]) == MEMBER)

    await B.store.close()
    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
