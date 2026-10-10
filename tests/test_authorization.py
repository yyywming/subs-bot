"""授权行为：有效权限、撤销跨重启、管理员不可提权/撤销、公开模式显式拒绝。

不启动网络，也不比较回复文案。权限断言走真实的 bot.allowed / cmd_auth。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_tmp = tempfile.mkdtemp(prefix="subs-auth-")
os.environ["BOT_TOKEN"] = "0:TEST"
os.environ["ALLOWED_USER_IDS"] = ""
os.environ.pop("ADMIN_USER_IDS", None)
os.environ["DATA_DIR"] = _tmp
os.environ["DB_PATH"] = str(Path(_tmp) / "auth.db")
sys.path.insert(0, str(_root))

import config  # noqa: E402
import bot  # noqa: E402
from db import Store  # noqa: E402

FAILED: list[str] = []
_TG_MAX = 2**63 - 1


def check(name: str, cond: bool) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILED.append(name)


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Chat:
    def __init__(self, kind: str) -> None:
        self.type = kind
        self.id = -100


class _Msg:
    def __init__(self) -> None:
        self.reply_to_message = None
        self.replies = 0
        self.callback_answers = 0

    async def reply_text(self, _text: str, **_kwargs: object) -> None:
        self.replies += 1


class _Query:
    def __init__(self, msg: _Msg) -> None:
        self.msg = msg

    async def answer(self, _text: str, show_alert: bool = False) -> None:
        self.msg.callback_answers += 1


class _Update:
    def __init__(
        self,
        user_id: int,
        chat_type: str = "private",
        reply_user: int | None = None,
        callback: bool = False,
    ) -> None:
        self.effective_user = _User(user_id)
        self.effective_chat = _Chat(chat_type)
        self.effective_message = _Msg()
        self.callback_query = _Query(self.effective_message) if callback else None
        if reply_user is not None:
            replied = _Msg()
            replied.from_user = _User(reply_user)  # type: ignore[attr-defined]
            self.effective_message.reply_to_message = replied


class _Ctx:
    def __init__(self, args: list[str]) -> None:
        self.args = args


def test_id_and_admin_resolution() -> None:
    print("=== 解析与管理员来源 ===")
    allowed = {1, 2}
    admins = config.resolve_admin_ids(allowed, None)
    admins.add(9)
    check("未设置 ADMIN 时复制白名单且不别名", allowed == {1, 2} and 9 not in allowed)
    check("显式空 ADMIN 不是白名单", config.resolve_admin_ids({1, 2}, "") == set())
    check("显式 ADMIN 可以只是白名单的子集", config.resolve_admin_ids({1, 2}, "1") == {1})
    check("超 int64 的环境 ID 被丢掉", config.resolve_admin_ids({1}, f"1,{_TG_MAX + 1}") == {1})
    check("int64 上界可解析", config.parse_tg_user_id(str(_TG_MAX)) == _TG_MAX)
    check("int64 上界之外不可解析", config.parse_tg_user_id(str(_TG_MAX + 1)) is None)
    check("0、前导 0、符号和空串都不可解析", (
        config.parse_tg_user_id("0") is None
        and config.parse_tg_user_id("01") is None
        and config.parse_tg_user_id("+5") is None
        and config.parse_tg_user_id("") is None
        and config.parse_tg_user_id("-3") is None
    ))
    check("逗号名单只保留合法 ID", config.parse_user_id_set("1, 2, 0, 01, +3, 5") == {1, 2, 5})


async def _reset(allowed: set[int], admins: set[int]) -> None:
    config.ALLOWED_USER_IDS = set(allowed)
    config.ADMIN_USER_IDS = set(admins)
    async with bot.store.connection() as db:
        await db.execute("DELETE FROM auth_overrides")
        await db.commit()


async def test_init_does_not_seed() -> None:
    print("=== 启动不灌环境白名单 ===")
    await bot.store.close()
    bot.store = Store(str(Path(_tmp) / "fresh.db"))
    config.ALLOWED_USER_IDS = {1, 2}
    config.ADMIN_USER_IDS = {1}
    check("没有覆盖项时环境用户仍可用", await bot.allowed(2) is True)
    check("陌生人在白名单模式下不可用", await bot.allowed(3) is False)
    check("建表没有写入任何授权行", await bot.store.list_auth_overrides() == [])
    check("管理员不依赖覆盖项", await bot.store.get_auth_override(1) is None and await bot.allowed(1) is True)


async def test_revoke_survives_reopen() -> None:
    print("=== 撤销立即生效且重启不回灌 ===")
    await _reset({1, 2}, {1})
    await bot.cmd_auth(_Update(1), _Ctx(["add", "3"]))
    await bot.cmd_auth(_Update(1), _Ctx(["remove", "2"]))
    check("环境用户被撤销后立即不可用", await bot.allowed(2) is False)
    check("动态授权立即可用", await bot.allowed(3) is True)
    path = bot.store.path
    await bot.store.close()
    reopened = Store(path)
    bot.store = reopened
    config.ALLOWED_USER_IDS = {1, 2}
    config.ADMIN_USER_IDS = {1}
    check("重新打开后撤销仍在", await bot.allowed(2) is False)
    check("重新打开后动态授权仍在", await bot.allowed(3) is True)
    check("没有把环境 ID 重新写成允许", await reopened.list_auth_overrides() == [(2, False), (3, True)])
    await bot.cmd_auth(_Update(1), _Ctx(["add", "2"]))
    check("再次授权会清掉拒绝", await bot.allowed(2) is True and await bot.store.get_auth_override(2) is True)


async def test_nonadmin_cannot_manage() -> None:
    print("=== 非管理员不能授权或撤销 ===")
    await _reset({1, 2}, {1})
    check("白名单里的非管理员可以使用", await bot.allowed(2) is True)
    check("非管理员不在管理员集合", 2 not in config.ADMIN_USER_IDS)
    before = await bot.store.list_auth_overrides()
    await bot.cmd_auth(_Update(2), _Ctx(["add", "3"]))
    await bot.cmd_auth(_Update(2), _Ctx(["remove", "1"]))
    check("非管理员的命令不写库", await bot.store.list_auth_overrides() == before)
    check("因此陌生人仍然不可用", await bot.allowed(3) is False)
    check("管理员没有被非管理员撤销", await bot.allowed(1) is True)
    await bot.cmd_auth(_Update(1), _Ctx(["add", "4"]))
    check("被授权的人可以使用", await bot.allowed(4) is True)
    check("被授权的人不会变成管理员", 4 not in config.ADMIN_USER_IDS)
    granted = await bot.store.list_auth_overrides()
    await bot.cmd_auth(_Update(4), _Ctx(["add", "5"]))
    await bot.cmd_auth(_Update(4), _Ctx(["remove", "2"]))
    check("动态用户不能再授权或撤销", await bot.store.list_auth_overrides() == granted)
    check("动态用户的目标没有获得权限", await bot.allowed(5) is False)
    check("环境用户没有被动态用户撤销", await bot.allowed(2) is True)


async def test_admin_cannot_be_revoked() -> None:
    print("=== 管理员不能通过 Bot 撤销 ===")
    await _reset({1, 5}, {1, 5})
    await bot.store.set_auth_override(1, False)
    check("已有的拒绝行也不能关掉管理员", await bot.allowed(1) is True)
    await bot.cmd_auth(_Update(1), _Ctx(["remove", "5"]))
    await bot.cmd_auth(_Update(1), _Ctx(["remove", "1"]))
    check("命令不会给管理员写入拒绝", await bot.store.get_auth_override(5) is None)
    check("另一名管理员仍然可用", await bot.allowed(5) is True)
    check("管理员命令没有把目标提成新的动态授权", 5 in config.ADMIN_USER_IDS)


async def test_public_mode_denial() -> None:
    print("=== 公开模式仍遵守显式拒绝 ===")
    await _reset(set(), {1})
    check("空白名单时陌生人可用", await bot.allowed(50) is True)
    await bot.cmd_auth(_Update(1), _Ctx(["remove", "50"]))
    check("公开模式下显式拒绝生效", await bot.allowed(50) is False)
    check("管理员在公开模式下仍可用", await bot.allowed(1) is True)
    rows = await bot.store.list_auth_overrides()
    await bot.cmd_auth(_Update(60), _Ctx(["add", "70"]))
    check("公开模式下的普通用户不能写授权", await bot.store.list_auth_overrides() == rows)
    check("未被拒绝的人仍然可用", await bot.allowed(70) is True and await bot.store.get_auth_override(70) is None)


async def test_no_first_user_admin() -> None:
    print("=== 没有管理员时不能自封 ===")
    await _reset(set(), set())
    check("没有管理员时公开模式仍可用", await bot.allowed(7) is True)
    await bot.cmd_auth(_Update(7), _Ctx(["add", "8"]))
    await bot.cmd_auth(_Update(7), _Ctx(["remove", "9"]))
    check("管理关闭时不写库", await bot.store.list_auth_overrides() == [])
    check("调用者没有变成管理员", config.ADMIN_USER_IDS == set() and 7 not in config.ADMIN_USER_IDS)
    check("目标也没有被拒绝或授权", await bot.allowed(8) is True and await bot.allowed(9) is True)


async def test_command_id_bounds_and_reply() -> None:
    print("=== 命令只接受安全的用户 ID ===")
    await _reset({1}, {1})
    await bot.cmd_auth(_Update(1), _Ctx(["add", str(_TG_MAX)]))
    check("int64 上界可以授权", await bot.allowed(_TG_MAX) is True)
    rows = await bot.store.list_auth_overrides()
    for bad in ("0", "-1", str(_TG_MAX + 1), "01", "+5", "12a", "1 2", "", "1.0"):
        await bot.cmd_auth(_Update(1), _Ctx(["add", bad]))
    await bot.cmd_auth(_Update(1), _Ctx(["add", "6", "7"]))
    check("非法 ID 不产生新的覆盖项", await bot.store.list_auth_overrides() == rows)
    try:
        await bot.store.set_auth_override(_TG_MAX + 1, True)
        rejected = False
    except ValueError:
        rejected = True
    check("越界写入被拒绝且库不变", rejected and await bot.store.list_auth_overrides() == rows)
    await bot.cmd_auth(_Update(1, reply_user=42), _Ctx(["add"]))
    check("回复消息可以授权", await bot.store.get_auth_override(42) is True and await bot.allowed(42) is True)
    await bot.cmd_auth(_Update(1, reply_user=42), _Ctx(["remove"]))
    check("回复消息可以撤销", await bot.allowed(42) is False and await bot.store.get_auth_override(42) is False)


async def test_group_does_not_mutate() -> None:
    print("=== 群里的 /auth 不改名单 ===")
    await _reset({1, 2}, {1})
    before = await bot.store.list_auth_overrides()
    await bot.cmd_auth(_Update(1, chat_type="group"), _Ctx(["add", "9"]))
    await bot.cmd_auth(_Update(1, chat_type="supergroup"), _Ctx(["remove", "2"]))
    check("群和超群都不写覆盖项", await bot.store.list_auth_overrides() == before)
    check("群里的撤销没有生效", await bot.allowed(2) is True)
    check("群里的添加没有生效", await bot.allowed(9) is False)


async def test_deny_quiet_and_callback() -> None:
    print("=== 拒绝：群消息可静默，回调仍然明确 ===")
    await _reset({1}, {1})
    quiet = _Update(99, chat_type="group")
    check("静默拒绝", await bot.deny(quiet, quiet=True) is True)
    check("静默拒绝不发消息", quiet.effective_message.replies == 0)
    spoken = _Update(99, chat_type="group")
    check("命令式拒绝", await bot.deny(spoken) is True and spoken.effective_message.replies == 1)
    allowed_update = _Update(1, chat_type="group")
    check("已授权用户不会被拒绝", await bot.deny(allowed_update, quiet=True) is False)
    check("已授权用户不会收到拒绝消息", allowed_update.effective_message.replies == 0)
    callback = _Update(99, callback=True)
    check("回调拒绝", await bot.deny(callback) is True)
    check("回调走 answer 而不是聊天回复", (
        callback.effective_message.callback_answers == 1
        and callback.effective_message.replies == 0
    ))


async def main() -> None:
    test_id_and_admin_resolution()
    await test_init_does_not_seed()
    await test_revoke_survives_reopen()
    await test_nonadmin_cannot_manage()
    await test_admin_cannot_be_revoked()
    await test_public_mode_denial()
    await test_no_first_user_admin()
    await test_command_id_bounds_and_reply()
    await test_group_does_not_mutate()
    await test_deny_quiet_and_callback()
    await bot.store.close()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    asyncio.run(main())
