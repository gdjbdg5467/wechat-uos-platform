"""WeChat UOS personal-account platform adapter for Hermes Agent.

Uses ``itchat-uos`` to log in a real personal WeChat account via QR code and
relay group @mentions (and optionally DMs) into Hermes Gateway.

WARNING: itchat-uos is a reverse-engineered UOS Web WeChat protocol. Use a
secondary WeChat account; Tencent may break or restrict it at any time.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.getenv("HERMES_HOME") or "/root/.hermes")
STATE_DIR = HERMES_HOME / "wechat_uos"
QR_PNG = STATE_DIR / "itchat_qr.png"
QR_URL_TXT = STATE_DIR / "itchat_qr_url.txt"
PKL_FILE = STATE_DIR / "itchat.pkl"
DEFAULT_QR_PORT = 8646
MAX_MESSAGE_LENGTH = 3500


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _strip_at_prefix(text: str) -> str:
    """Remove common WeChat @ prefix noise while preserving the user's prompt."""
    if not text:
        return ""
    # WeChat group @ text often starts with "@Nick\u2005" or "@Nick "
    s = text.replace("\u2005", " ").strip()
    if s.startswith("@"):
        parts = s.split(maxsplit=1)
        if len(parts) == 2:
            return parts[1].strip()
    return s


class _QRHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # silence access logs
        logger.debug("WeChatUOS QR HTTP: " + format, *args)

    def do_GET(self):  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                f"""<!doctype html><html><head><meta charset='utf-8'>
<meta http-equiv='refresh' content='3'>
<title>WeChat UOS Login</title></head>
<body style='background:#111;color:#ddd;font-family:sans-serif;text-align:center;padding-top:40px'>
<h2>微信扫码登录 Hermes</h2>
<p>页面每 3 秒刷新，始终显示最新二维码。扫码后请在手机上点确认登录。</p>
<img src='/itchat_qr.png?t={int(time.time())}' style='max-width:420px;background:white;padding:12px;border-radius:12px'>
</body></html>""".encode("utf-8")
            )
            return
        if parsed.path == "/itchat_qr.png" and QR_PNG.exists():
            data = QR_PNG.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()


class WeChatUOSAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig, **kwargs: Any):
        super().__init__(config=config, platform=Platform("wechat_uos"))
        extra = getattr(config, "extra", {}) or {}
        STATE_DIR.mkdir(parents=True, exist_ok=True)

        self.allowed_groups = set(_csv(os.getenv("WECHAT_UOS_ALLOWED_GROUPS") or extra.get("allowed_groups")))
        self.allowed_users = set(_csv(os.getenv("WECHAT_UOS_ALLOWED_USERS") or extra.get("allowed_users")))
        self.admin_users = set(_csv(os.getenv("WECHAT_UOS_ADMIN_USERS") or extra.get("admin_users")))
        self.respond_to_dms = _truthy(os.getenv("WECHAT_UOS_RESPOND_TO_DMS"), bool(extra.get("respond_to_dms", False)))
        self.qr_http = _truthy(os.getenv("WECHAT_UOS_QR_HTTP"), bool(extra.get("qr_http", True)))
        self.qr_port = int(os.getenv("WECHAT_UOS_QR_PORT") or extra.get("qr_port", DEFAULT_QR_PORT))
        self.home_channel = os.getenv("WECHAT_UOS_HOME_CHANNEL") or extra.get("home_channel", "")

        self._itchat = None
        self._thread: Optional[threading.Thread] = None
        self._qr_server: Optional[ThreadingHTTPServer] = None
        self._qr_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._login_name = ""
        self._chat_names: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return "WeChat UOS"

    async def connect(self) -> bool:
        try:
            import itchat  # noqa: F401
            from itchat.content import TEXT  # noqa: F401
        except ImportError:
            logger.error("WeChatUOS: itchat-uos not installed. Run: %s", install_hint())
            return False

        self._loop = asyncio.get_running_loop()
        if self.qr_http:
            self._start_qr_server()

        self._thread = threading.Thread(target=self._run_itchat, name="wechat-uos-itchat", daemon=True)
        self._thread.start()
        self._mark_connected()
        logger.info("WeChatUOS: starting; QR file=%s HTTP=%s", QR_PNG, f"http://0.0.0.0:{self.qr_port}" if self.qr_http else "off")
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        try:
            if self._itchat is not None:
                self._itchat.logout()
        except Exception:
            pass
        if self._qr_server:
            try:
                self._qr_server.shutdown()
            except Exception:
                pass
            self._qr_server = None

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if self._itchat is None:
            return SendResult(success=False, error="itchat not connected", retryable=True)
        text = (content or "").strip()
        if not text:
            return SendResult(success=True)
        chunks = self.truncate_message(text, MAX_MESSAGE_LENGTH)
        try:
            for chunk in chunks:
                self._itchat.send(chunk, toUserName=chat_id)
                await asyncio.sleep(0.25)
            return SendResult(success=True, message_id=str(int(time.time() * 1000)))
        except Exception as e:
            logger.exception("WeChatUOS: send failed to %s", chat_id)
            return SendResult(success=False, error=str(e), retryable=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": self._chat_names.get(chat_id, chat_id), "type": "group" if str(chat_id).startswith("@@") else "dm"}

    def _start_qr_server(self) -> None:
        if self._qr_server:
            return
        try:
            self._qr_server = ThreadingHTTPServer(("0.0.0.0", self.qr_port), _QRHandler)
            self._qr_thread = threading.Thread(target=self._qr_server.serve_forever, name="wechat-uos-qr", daemon=True)
            self._qr_thread.start()
            logger.info("WeChatUOS: QR server listening on http://0.0.0.0:%s", self.qr_port)
        except OSError as e:
            logger.warning("WeChatUOS: QR HTTP server failed on port %s: %s", self.qr_port, e)

    def _qr_callback(self, uuid: str, status: str, qrcode: bytes = b"", **kwargs: Any) -> None:
        qrcode_data = qrcode or kwargs.get("qrcode_data") or b""
        logger.info("WeChatUOS QR: uuid=%s status=%s bytes=%s", uuid, status, len(qrcode_data) if qrcode_data else 0)
        if qrcode_data:
            QR_PNG.write_bytes(qrcode_data)
        if uuid:
            QR_URL_TXT.write_text(f"https://login.weixin.qq.com/l/{str(uuid).strip()}\n", encoding="utf-8")

    def _login_callback(self) -> None:
        try:
            user = self._itchat.search_friends() if self._itchat else {}
            if not isinstance(user, dict):
                user = {}
            self._login_name = html.unescape(str(user.get("NickName") or ""))
            logger.info("WeChatUOS: login successful as %s (%s)", self._login_name, user.get("UserName"))
        except Exception:
            logger.info("WeChatUOS: login successful")

    def _run_itchat(self) -> None:
        try:
            import itchat
            from itchat.content import TEXT
            self._itchat = itchat

            @itchat.msg_register(TEXT, isGroupChat=True)
            def group_text_handler(msg):
                if not getattr(msg, "isAt", False):
                    return
                group_id = getattr(msg, "fromUserName", None) or msg.get("FromUserName")
                sender_id = getattr(msg, "actualUserName", None) or msg.get("ActualUserName") or ""
                sender = getattr(msg, "actualNickName", None) or msg.get("ActualNickName") or sender_id
                raw_text = getattr(msg, "text", None) or msg.get("Text") or msg.get("Content") or ""
                text = _strip_at_prefix(raw_text)
                group_name = self._resolve_group_name(group_id)
                if not self._allowed(group_id, group_name, sender, sender_id):
                    return
                admin_handled = self._handle_admin_command(text, sender=sender, sender_id=sender_id, chat_id=group_id)
                if admin_handled:
                    return
                logger.info("WeChatUOS: @ from %s in %s: %s", sender, group_name, text)
                self._submit_event(text=text, chat_id=group_id, chat_name=group_name, chat_type="group", user_id=sender_id, user_name=sender, raw=msg)

            @itchat.msg_register(TEXT, isGroupChat=False)
            def private_text_handler(msg):
                if not self.respond_to_dms:
                    return
                user_id = getattr(msg, "fromUserName", None) or msg.get("FromUserName")
                sender = msg.get("User", {}).get("NickName") if isinstance(msg.get("User"), dict) else ""
                sender = sender or user_id
                text = getattr(msg, "text", None) or msg.get("Text") or msg.get("Content") or ""
                if self.allowed_users and user_id not in self.allowed_users and sender not in self.allowed_users:
                    return
                self._submit_event(text=text, chat_id=user_id, chat_name=sender, chat_type="dm", user_id=user_id, user_name=sender, raw=msg)

            for p in (QR_PNG, QR_URL_TXT):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

            itchat.auto_login(
                hotReload=True,
                statusStorageDir=str(PKL_FILE),
                qrCallback=self._qr_callback,
                loginCallback=self._login_callback,
                exitCallback=lambda: logger.warning("WeChatUOS: logged out/disconnected"),
            )
            logger.info("WeChatUOS: listening for group @mentions")
            itchat.run(blockThread=True)
        except Exception:
            logger.exception("WeChatUOS: listener crashed")
            self._set_fatal_error("listener_crashed", "itchat-uos listener crashed", retryable=True)
            self._mark_disconnected()

    def _resolve_group_name(self, group_id: str) -> str:
        if not group_id:
            return ""
        if group_id in self._chat_names:
            return self._chat_names[group_id]
        try:
            room = self._itchat.search_chatrooms(userName=group_id) if self._itchat else None
            if not isinstance(room, dict):
                room = {}
            name = html.unescape(str(room.get("NickName") or group_id))
        except Exception:
            name = group_id
        self._chat_names[group_id] = name
        return name

    def _allowed(self, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        if self.allowed_groups and group_id not in self.allowed_groups and group_name not in self.allowed_groups:
            logger.debug("WeChatUOS: ignored group %s/%s not in allowlist", group_name, group_id)
            return False
        if self.allowed_users and sender not in self.allowed_users and sender_id not in self.allowed_users:
            logger.debug("WeChatUOS: ignored sender %s/%s not in allowlist", sender, sender_id)
            return False
        return True

    def _is_admin(self, sender: str, sender_id: str) -> bool:
        # If no explicit admin list is set, any allowed user may manage the allowlist.
        if not self.admin_users:
            return True
        return sender in self.admin_users or sender_id in self.admin_users

    def _persist_user_lists(self) -> None:
        """Persist current allow/admin lists to ~/.hermes/.env for restart survival."""
        env_path = HERMES_HOME / ".env"
        text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = text.splitlines()
        updates = {
            "WECHAT_UOS_ALLOWED_USERS": ",".join(sorted(self.allowed_users)),
            "WECHAT_UOS_ADMIN_USERS": ",".join(sorted(self.admin_users)),
        }
        for key, value in updates.items():
            found = False
            for i, line in enumerate(lines):
                if line.startswith(key + "="):
                    lines[i] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _handle_admin_command(self, text: str, *, sender: str, sender_id: str, chat_id: str) -> bool:
        """Handle in-group permission commands. Returns True if consumed."""
        parts = (text or "").strip().split()
        if not parts:
            return False
        cmd = parts[0].lower()
        aliases = {
            "/allow", "allow", "授权", "添加权限", "加权限",
            "/deny", "deny", "取消授权", "移除权限", "删权限",
            "/admin", "admin", "设管理员", "添加管理员", "管理员",
            "/unadmin", "unadmin", "取消管理员", "移除管理员",
            "/acl", "acl", "权限列表", "名单",
        }
        if cmd not in aliases:
            return False
        if self._itchat is None:
            return True
        if not self._is_admin(sender, sender_id):
            try:
                self._itchat.send("你没有权限管理名单。", toUserName=chat_id)
            except Exception:
                pass
            return True
        if cmd in {"/acl", "acl", "权限列表", "名单"}:
            allowed = ", ".join(sorted(self.allowed_users)) or "未限制/空"
            admins = ", ".join(sorted(self.admin_users)) or "未单独设置（允许名单内用户可管理）"
            self._itchat.send(f"当前可使用名单：{allowed}\n管理员名单：{admins}", toUserName=chat_id)
            return True
        if len(parts) < 2:
            self._itchat.send("格式：授权/取消授权/设管理员/取消管理员 昵称或UserName", toUserName=chat_id)
            return True
        target = " ".join(parts[1:]).strip()
        if not target:
            return True
        if cmd in {"/allow", "allow", "授权", "添加权限", "加权限"}:
            self.allowed_users.add(target)
            action = "已授权"
        elif cmd in {"/deny", "deny", "取消授权", "移除权限", "删权限"}:
            self.allowed_users.discard(target)
            self.admin_users.discard(target)
            action = "已取消授权"
        elif cmd in {"/admin", "admin", "设管理员", "添加管理员", "管理员"}:
            self.allowed_users.add(target)
            self.admin_users.add(target)
            action = "已设为管理员"
        else:
            self.admin_users.discard(target)
            action = "已取消管理员"
        self._persist_user_lists()
        self._itchat.send(f"{action}：{target}", toUserName=chat_id)
        logger.info("WeChatUOS ACL: %s %s by %s/%s", action, target, sender, sender_id)
        return True

    def _submit_event(self, *, text: str, chat_id: str, chat_name: str, chat_type: str, user_id: str, user_name: str, raw: Any) -> None:
        if not self._loop:
            return
        raw_get = raw.get if hasattr(raw, "get") else (lambda _key, _default=None: _default)
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=raw,
            message_id=str(getattr(raw, "msgId", "") or raw_get("MsgId") or raw_get("NewMsgId") or int(time.time() * 1000)),
            timestamp=datetime.now(),
        )
        asyncio.run_coroutine_threadsafe(self.handle_message(event), self._loop)


def check_requirements() -> bool:
    try:
        import itchat  # noqa: F401
        return True
    except Exception:
        return False


def validate_config(config: Any) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return _truthy(os.getenv("WECHAT_UOS_ENABLED"), bool(extra.get("enabled", False)))


def _env_enablement() -> Optional[dict]:
    if not _truthy(os.getenv("WECHAT_UOS_ENABLED")):
        return None
    seed: Dict[str, Any] = {"enabled": True}
    for env, key in [
        ("WECHAT_UOS_ALLOWED_GROUPS", "allowed_groups"),
        ("WECHAT_UOS_ALLOWED_USERS", "allowed_users"),
        ("WECHAT_UOS_ADMIN_USERS", "admin_users"),
        ("WECHAT_UOS_RESPOND_TO_DMS", "respond_to_dms"),
        ("WECHAT_UOS_QR_HTTP", "qr_http"),
        ("WECHAT_UOS_QR_PORT", "qr_port"),
    ]:
        val = os.getenv(env)
        if val:
            seed[key] = _csv(val) if key in {"allowed_groups", "allowed_users", "admin_users"} else val
    home = os.getenv("WECHAT_UOS_HOME_CHANNEL")
    if home:
        seed["home_channel"] = {"chat_id": home, "name": os.getenv("WECHAT_UOS_HOME_CHANNEL_NAME") or home}
    return seed


def is_connected(config: Any = None) -> bool:
    return PKL_FILE.exists()


def install_hint() -> str:
    return "/root/.hermes/hermes-agent/venv/bin/pip install itchat-uos Pillow"


def register(ctx) -> None:
    ctx.register_platform(
        name="wechat_uos",
        label="WeChat UOS",
        adapter_factory=lambda cfg: WeChatUOSAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["WECHAT_UOS_ENABLED"],
        install_hint=install_hint(),
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="WECHAT_UOS_HOME_CHANNEL",
        allowed_users_env="WECHAT_UOS_ALLOWED_USERS",
        allow_all_env="WECHAT_UOS_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "你正在通过真实个人微信号的 itchat-uos 通道聊天。"
            "群聊里通常只有用户 @ 你时才会触发。回复要简洁，避免刷屏；"
            "普通微信不渲染完整 Markdown，优先用纯文本。"
        ),
    )
