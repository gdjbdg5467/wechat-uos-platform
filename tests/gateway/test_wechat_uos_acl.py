from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.config import PlatformConfig, Platform
from gateway.session import SessionSource
from plugins.platforms.wechat_uos.adapter import WeChatUOSAdapter


class FakeItChat:
    def __init__(self):
        self.sent = []
        self.room = {
            "NickName": "测试群",
            "MemberList": [
                {"UserName": "@owner", "NickName": "群主"},
                {"UserName": "@alice", "NickName": "Alice", "DisplayName": "爱丽丝"},
                {"UserName": "@bob", "NickName": "Bob"},
            ],
        }

    def send(self, text, toUserName=None):
        self.sent.append((toUserName, text))

    def update_chatroom(self, group_id, detailedMember=True):
        return self.room

    def search_chatrooms(self, userName=None):
        return self.room


def make_adapter(monkeypatch, tmp_path):
    monkeypatch.setattr("plugins.platforms.wechat_uos.adapter.STATE_DIR", tmp_path)
    monkeypatch.setattr("plugins.platforms.wechat_uos.adapter.ACL_FILE", tmp_path / "acl.json")
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = WeChatUOSAdapter(cfg)
    adapter._itchat = FakeItChat()
    return adapter


def test_authorize_group_consumes_command_and_sets_initial_admin(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)

    consumed = adapter._handle_acl_command(
        "授权此群聊",
        sender="张三",
        sender_id="@zhangsan",
        chat_id="@@group",
        group_name="测试群",
    )

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert group["authorized"] is True
    assert group["initial_admin_uid"] == "@zhangsan"
    assert "@zhangsan" in group["admins"]
    assert adapter._itchat.sent[-1] == ("@@group", "本群已授权成功。\n管理员：张三\nUID：@zhangsan")


def test_allowed_member_can_use_group_after_admin_grants_permission(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权 爱丽丝", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert "@alice" in group["allowed_users"]
    assert adapter._can_use_group("@@group", "测试群", "爱丽丝", "@alice") is True


def test_non_admin_cannot_manage_acl(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权 Bob", sender="路人", sender_id="@passerby", chat_id="@@group", group_name="测试群")

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert "@bob" not in group["allowed_users"]
    assert adapter._itchat.sent[-1] == ("@@group", "你没有权限管理本群机器人。")


def test_find_member_uid_accepts_raw_wechat_uid_without_at_prefix(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权 alice_uid", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    assert consumed is True
    assert "alice_uid" in adapter._acl["groups"]["@@group"]["allowed_users"]


def test_chinese_acl_command_accepts_target_without_space(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权爱丽丝", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    assert consumed is True
    assert "@alice" in adapter._acl["groups"]["@@group"]["allowed_users"]


def test_group_event_is_trusted_after_adapter_acl_accepts_sender(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._loop = asyncio.new_event_loop()
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")
    adapter._handle_acl_command("授权 爱丽丝", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    captured = []
    monkeypatch.setattr(adapter, "handle_message", lambda event: captured.append(event))
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: None)

    adapter._submit_event(
        text="你好",
        chat_id="@@group",
        chat_name="测试群",
        chat_type="group",
        user_id="@alice",
        user_name="爱丽丝",
        raw={},
    )

    adapter._loop.close()
    assert captured
    assert getattr(captured[0].source, "trusted_by_adapter", False) is True


def test_gateway_authorizes_adapter_trusted_source_even_with_global_allowlist(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "someone_else")

    source = SessionSource(
        platform=Platform.WEIXIN,
        chat_id="@@group",
        chat_type="group",
        user_id="@alice",
        user_name="爱丽丝",
    )
    setattr(source, "trusted_by_adapter", True)

    assert runner._is_user_authorized(source) is True
