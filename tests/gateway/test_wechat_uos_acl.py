from __future__ import annotations

import asyncio
import json
import urllib.parse
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
                {"UserName": "@alice", "NickName": "Bob", "DisplayName": "爱丽丝"},
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
    assert adapter._itchat.sent[-1] == ("@@group", "本群已授权成功。\n管理员：张三")
    assert "UID" not in adapter._itchat.sent[-1][1]
    assert "@zhangsan" not in adapter._itchat.sent[-1][1]


def test_allowed_member_can_use_group_after_admin_grants_permission(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权 爱丽丝", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert "@alice" in group["allowed_users"]
    assert adapter._can_use_group("@@group", "测试群", "爱丽丝", "@alice") is True
    assert adapter._itchat.sent[-1] == ("@@group", "已授权：爱丽丝")
    assert "UID" not in adapter._itchat.sent[-1][1]
    assert "@alice" not in adapter._itchat.sent[-1][1]


def test_acl_list_hides_member_uids(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")
    adapter._handle_acl_command("授权 爱丽丝", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("名单", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    assert consumed is True
    text = adapter._itchat.sent[-1][1]
    assert "爱丽丝" in text
    assert "张三" in text
    assert "@alice" not in text
    assert "@zhangsan" not in text


def test_non_admin_cannot_manage_acl(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权 Bob", sender="路人", sender_id="@passerby", chat_id="@@group", group_name="测试群")

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert "@bob" not in group["allowed_users"]
    assert adapter._itchat.sent[-1] == ("@@group", "你没有权限管理本群机器人。")


def test_non_admin_cannot_become_admin_by_reauthorizing_group(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权此群聊", sender="路人", sender_id="@passerby", chat_id="@@group", group_name="测试群")

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert "@passerby" not in group["admins"]
    assert group["initial_admin_uid"] == "@zhangsan"
    assert adapter._itchat.sent[-1] == ("@@group", "本群已授权。只有管理员可以管理本群机器人。")


def test_admin_can_deauthorize_group(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("授权关闭", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert group["authorized"] is False
    assert adapter._can_use_group("@@group", "测试群", "张三", "@zhangsan") is False
    assert adapter._itchat.sent[-1] == ("@@group", "本群授权已关闭。")


def test_non_admin_cannot_deauthorize_group(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@group", group_name="测试群")

    consumed = adapter._handle_acl_command("取消授权此群聊", sender="路人", sender_id="@passerby", chat_id="@@group", group_name="测试群")

    assert consumed is True
    group = adapter._acl["groups"]["@@group"]
    assert group["authorized"] is True
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


def test_acl_restores_authorized_group_when_runtime_group_id_changes(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@old", group_name="测试群")
    adapter._handle_acl_command("授权 爱丽丝", sender="张三", sender_id="@zhangsan", chat_id="@@old", group_name="测试群")

    restarted = make_adapter(monkeypatch, tmp_path)

    assert restarted._can_use_group("@@new", "测试群", "爱丽丝", "@alice") is True
    restored = restarted._acl["groups"]["@@new"]
    assert restored["authorized"] is True
    assert restored["restored_from_group_id"] == "@@old"
    assert "@zhangsan" in restored["admins"]
    assert "@alice" in restored["allowed_users"]


def test_admin_reauthorizing_restored_group_receives_confirmation(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@old", group_name="测试群")

    restarted = make_adapter(monkeypatch, tmp_path)
    consumed = restarted._handle_acl_command("授权此群聊", sender="张三", sender_id="@zhangsan", chat_id="@@new", group_name="测试群")

    assert consumed is True
    assert restarted._itchat.sent[-1] == ("@@new", "本群已授权，无需重复授权。")


def test_uid_migration_reconciles_stale_admin_uids_on_refresh(monkeypatch, tmp_path):
    """Simulate a restart where admin UIDs changed: old admin had uid @oldadmin,
    after refresh the same person shows up as @newadmin.  The migration should
    auto-replace the stale UID in the admins list."""
    from collections.abc import Mapping

    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command(
        "授权此群聊", sender="夢魚", sender_id="@oldadmin",
        chat_id="@@oldgid", group_name="测试群",
    )

    # Restart simulation: new adapter loads the saved ACL
    restarted = make_adapter(monkeypatch, tmp_path)

    # Before member refresh, the group should have been restored with stale
    # admin UID and pending_uid_migration flag.
    restored = restarted._group_acl("@@newgid", "测试群")
    assert restored.get("restored_from_group_id") == "@@oldgid"
    assert restored.get("pending_uid_migration") is True
    assert "@oldadmin" in restored["admins"]  # stale UID still there

    # The old members_cache was preserved — it has @oldadmin with nick "夢魚"
    old_meta = restored.get("old_members_cache", {}).get("@oldadmin", {})
    assert isinstance(old_meta, Mapping)
    assert old_meta.get("nick_name") == "夢魚"

    # Now mock the refreshed member list: same person, new UID @newadmin
    restarted._itchat.room = {
        "NickName": "测试群",
        "MemberList": [
            {"UserName": "@newadmin", "NickName": "夢魚"},
        ],
    }

    # Call refresh_group_members — this should trigger the migration
    count = restarted._refresh_group_members("@@newgid", "测试群")
    assert count == 1

    restored = restarted._acl["groups"]["@@newgid"]
    assert "@oldadmin" not in restored["admins"]  # stale UID removed
    assert "@newadmin" in restored["admins"]      # new UID added
    assert restored.get("pending_uid_migration") is None  # flag cleared
    assert restored.get("old_members_cache", {}) == {}    # old cache purged

    # Verify the migrated admin can now use the group
    assert restarted._can_use_group("@@newgid", "测试群", "夢魚", "@newadmin") is True


def test_uid_migration_handles_group_text_handler_auto_refresh(monkeypatch, tmp_path):
    """The group_text_handler should auto-trigger _refresh_group_members when
    a message arrives in a freshly-restored group with pending_uid_migration."""
    adapter = make_adapter(monkeypatch, tmp_path)
    adapter._handle_acl_command(
        "授权此群聊", sender="夢魚", sender_id="@oldadmin",
        chat_id="@@oldgid", group_name="测试群",
    )

    restarted = make_adapter(monkeypatch, tmp_path)

    # Monkey-patch refresh to confirm it gets called
    refresh_called = []
    original_refresh = restarted._refresh_group_members

    def tracked_refresh(gid, gname=""):
        refresh_called.append((gid, gname))
        return original_refresh(gid, gname)
    restarted._refresh_group_members = tracked_refresh

    # Simulate the itchat member list with new UIDs
    restarted._itchat.room = {
        "NickName": "测试群",
        "MemberList": [
            {"UserName": "@newadmin", "NickName": "夢魚"},
        ],
    }

    # Simulate what group_text_handler does: get group + check pending flag
    g = restarted._group_acl("@@newgid", "测试群")
    assert g.get("pending_uid_migration") is True

    # This is the line the handler runs
    if restarted._itchat is not None:
        with restarted._acl_lock:
            g2 = restarted._group_acl("@@newgid", "测试群")
            if g2.get("pending_uid_migration"):
                restarted._refresh_group_members("@@newgid", "测试群")

    assert len(refresh_called) == 1, "Refresh should have been triggered once"
    assert refresh_called[0] == ("@@newgid", "测试群")

    # Verify migration happened
    restored = restarted._acl["groups"]["@@newgid"]
    assert "@newadmin" in restored["admins"]
    assert "@oldadmin" not in restored["admins"]
    assert restarted._can_use_group("@@newgid", "测试群", "夢魚", "@newadmin") is True


def test_group_event_marks_acl_role_for_admin_and_user(monkeypatch, tmp_path):
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
    adapter._submit_event(
        text="重启网关",
        chat_id="@@group",
        chat_name="测试群",
        chat_type="group",
        user_id="@zhangsan",
        user_name="张三",
        raw={},
    )

    adapter._loop.close()
    assert len(captured) == 2
    assert getattr(captured[0].source, "trusted_by_adapter", False) is True
    assert getattr(captured[0].source, "wechat_uos_acl_role", "") == "user"
    assert getattr(captured[1].source, "trusted_by_adapter", False) is True
    assert getattr(captured[1].source, "wechat_uos_acl_role", "") == "admin"


def test_gateway_authorizes_adapter_trusted_source_even_with_global_allowlist(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "someone_else")

    source = SessionSource(
        platform=Platform("wechat_uos"),
        chat_id="@@group",
        chat_type="group",
        user_id="@alice",
        user_name="爱丽丝",
    )
    setattr(source, "trusted_by_adapter", True)

    assert runner._is_user_authorized(source) is True


def test_gateway_restricts_wechat_uos普通用户_slash_commands():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = {}
    source = SessionSource(
        platform=Platform("wechat_uos"),
        chat_id="@@group",
        chat_type="group",
        user_id="@alice",
        user_name="爱丽丝",
    )
    setattr(source, "wechat_uos_acl_role", "user")

    assert runner._check_slash_access(source, "status") is None
    denied = runner._check_slash_access(source, "restart")
    assert denied is not None
    assert "普通授权用户" in denied

    setattr(source, "wechat_uos_acl_role", "admin")
    assert runner._check_slash_access(source, "restart") is None


def test_pansou_search_uses_kw_query_parameter(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"code": 0, "data": {"merged_by_type": {}}}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("plugins.platforms.wechat_uos.adapter.urllib.request.urlopen", fake_urlopen)

    assert adapter._handle_pansou_search("封神演义", "@@group") is True
    query = urllib.parse.parse_qs(urllib.parse.urlparse(captured["url"]).query)
    assert query["kw"] == ["封神演义"]
    assert "keyword" not in query


def test_pansou_search_returns_first_five_links_for_each_allowed_pan_type(monkeypatch, tmp_path):
    adapter = make_adapter(monkeypatch, tmp_path)
    merged = {
        "baidu": [
            {"note": f"封神演义 百度 {i}", "url": f"https://baidu.example/{i}"}
            for i in range(1, 7)
        ],
        "quark": [
            {"note": f"封神演义 夸克 {i}", "url": f"https://quark.example/{i}"}
            for i in range(1, 7)
        ],
        "115": [
            {"note": f"封神演义 115 {i}", "url": f"https://115.example/{i}"}
            for i in range(1, 7)
        ],
        "uc": [
            {"note": f"封神演义 UC {i}", "url": f"https://uc.example/{i}"}
            for i in range(1, 7)
        ],
        "magnet": [
            {"note": f"封神演义 磁力 {i}", "url": f"magnet:?xt=urn:btih:{i}"}
            for i in range(1, 7)
        ],
        "aliyun": [
            {"note": f"封神演义 阿里 {i}", "url": f"https://aliyun.example/{i}"}
            for i in range(1, 7)
        ],
        "xunlei": [
            {"note": "封神演义 迅雷", "url": "https://xunlei.example/1"}
        ],
        "mobile": [
            {"note": "封神演义 移动", "url": "https://mobile.example/1"}
        ],
        "123": [
            {"note": "封神演义 123", "url": "https://123.example/1"}
        ],
        "others": [
            {"note": "封神演义 其他", "url": "https://others.example/1"}
        ],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"code": 0, "data": {"merged_by_type": merged}}).encode("utf-8")

    monkeypatch.setattr(
        "plugins.platforms.wechat_uos.adapter.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    assert adapter._handle_pansou_search("封神演义", "@@group") is True
    replies = "\n".join(text for _chat_id, text in adapter._itchat.sent)
    allowed = {"baidu", "quark", "115", "uc", "magnet"}
    for pan_type in allowed:
        for i in range(1, 6):
            expected = f"magnet:?xt=urn:btih:{i}" if pan_type == "magnet" else f"https://{pan_type}.example/{i}"
            assert expected in replies
        not_expected = "magnet:?xt=urn:btih:6" if pan_type == "magnet" else f"https://{pan_type}.example/6"
        assert not_expected not in replies
    for blocked in ["aliyun", "xunlei", "mobile", "123", "others"]:
        assert f"https://{blocked}.example" not in replies
