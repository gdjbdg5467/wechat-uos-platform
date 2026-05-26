"""WeChat UOS personal-account platform adapter for Hermes Agent.

Uses ``itchat-uos`` to log in a real personal WeChat account via QR code and
relay group @mentions (and optionally DMs) into Hermes Gateway.

WARNING: itchat-uos is a reverse-engineered UOS Web WeChat protocol. Use a
secondary WeChat account; Tencent may break or restrict it at any time.
"""

from __future__ import annotations

import asyncio
import html
import json
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
ACL_FILE = STATE_DIR / "acl.json"
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
        self._acl_lock = threading.RLock()
        self._acl: Dict[str, Any] = self._load_acl()

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
                if not self._group_in_allowlist(group_id, group_name):
                    return
                if self._handle_acl_command(text, sender=sender, sender_id=sender_id, chat_id=group_id, group_name=group_name):
                    return
                if not self._can_use_group(group_id, group_name, sender, sender_id):
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

    def _normalize_group_name(self, group_name: str) -> str:
        return html.unescape(str(group_name or "")).strip()

    def _find_restorable_group_acl(self, group_id: str, group_name: str) -> Optional[Dict[str, Any]]:
        """Find an existing authorized ACL record for the same WeChat group name.

        itchat/UOS group ``@@`` identifiers can change across logins.  Without a
        secondary lookup, a gateway restart can make an already-authorized group
        look brand new and force admins to run ``授权此群聊`` again.  Keep the
        current group id as the primary key, but restore from a prior authorized
        record with the same display name when a new id appears.
        """
        normalized = self._normalize_group_name(group_name)
        if not normalized:
            return None
        groups = self._acl.setdefault("groups", {})
        for existing_id, existing in groups.items():
            if existing_id == group_id or not isinstance(existing, dict):
                continue
            if not existing.get("authorized"):
                continue
            if self._normalize_group_name(existing.get("name", "")) == normalized:
                return existing
        return None

    def _load_acl(self) -> Dict[str, Any]:
        try:
            if ACL_FILE.exists():
                data = json.loads(ACL_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("groups", {})
                    return data
        except Exception:
            logger.exception("WeChatUOS ACL: failed to load %s", ACL_FILE)
        return {"groups": {}}

    def _save_acl(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = ACL_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._acl, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(ACL_FILE)
        except Exception:
            logger.exception("WeChatUOS ACL: failed to save %s", ACL_FILE)

    def _group_acl(self, group_id: str, group_name: str = "") -> Dict[str, Any]:
        groups = self._acl.setdefault("groups", {})
        now = int(time.time())
        group = groups.get(group_id)
        if not isinstance(group, dict):
            restored = self._find_restorable_group_acl(group_id, group_name)
            if isinstance(restored, dict):
                restored_from_group_id = next(
                    (existing_id for existing_id, existing in groups.items() if existing is restored),
                    "",
                )
                # Copy the persisted ACL to the current runtime group id.  Keep a
                # new members_cache map so fresh messages can repopulate display
                # names for this login, but preserve the authorization/admin UID
                # lists that are stable enough for itchat to match senders.
                group = {
                    "name": group_name or restored.get("name") or group_id,
                    "authorized": bool(restored.get("authorized")),
                    "owner_uid": restored.get("owner_uid", ""),
                    "initial_admin_uid": restored.get("initial_admin_uid", ""),
                    "admins": list(restored.get("admins", [])),
                    "allowed_users": list(restored.get("allowed_users", [])),
                    "members_cache": {},
                    "created_at": restored.get("created_at", now),
                    "updated_at": now,
                    "restored_from_group_id": restored_from_group_id,
                }
                groups[group_id] = group
                logger.info(
                    "WeChatUOS ACL: restored authorization for group=%s new_id=%s old_id=%s",
                    group.get("name") or group_name,
                    group_id,
                    restored_from_group_id,
                )
        if not isinstance(group, dict):
            group = groups.setdefault(group_id, {
                "name": group_name or group_id,
                "authorized": False,
                "owner_uid": "",
                "initial_admin_uid": "",
                "admins": [],
                "allowed_users": [],
                "members_cache": {},
                "created_at": now,
                "updated_at": now,
            })
        if group_name and group.get("name") != group_name:
            group["name"] = group_name
            group["updated_at"] = now
        group.setdefault("authorized", False)
        group.setdefault("owner_uid", "")
        group.setdefault("initial_admin_uid", "")
        group.setdefault("admins", [])
        group.setdefault("allowed_users", [])
        group.setdefault("members_cache", {})
        group.setdefault("created_at", now)
        group.setdefault("updated_at", now)
        group.setdefault("restored_from_group_id", "")
        if group.get("restored_from_group_id") and group.get("updated_at") == now:
            self._save_acl()
        return group

    def _group_in_allowlist(self, group_id: str, group_name: str) -> bool:
        if self.allowed_groups and group_id not in self.allowed_groups and group_name not in self.allowed_groups:
            logger.debug("WeChatUOS: ignored group %s/%s not in allowlist", group_name, group_id)
            return False
        return True

    def _member_display(self, group: Dict[str, Any], uid: str, fallback: str = "") -> str:
        meta = group.get("members_cache", {}).get(uid)
        if isinstance(meta, dict):
            for key in ("display_name", "remark_name", "nick_name"):
                if meta.get(key):
                    return str(meta[key])
        elif isinstance(meta, str) and meta:
            return meta
        return fallback or uid

    def _remember_member(self, group: Dict[str, Any], uid: str, nick: str = "", display: str = "", remark: str = "") -> None:
        if not uid:
            return
        cache = group.setdefault("members_cache", {})
        old = cache.get(uid) if isinstance(cache.get(uid), dict) else {}
        cache[uid] = {
            "nick_name": html.unescape(str(nick or old.get("nick_name") or "")),
            "display_name": html.unescape(str(display or old.get("display_name") or nick or old.get("display_name") or "")),
            "remark_name": html.unescape(str(remark or old.get("remark_name") or "")),
        }

    def _add_unique(self, seq: List[str], value: str) -> None:
        if value and value not in seq:
            seq.append(value)

    def _refresh_group_members(self, group_id: str, group_name: str = "") -> int:
        """Refresh group member cache. Best-effort: itchat APIs differ by version."""
        if self._itchat is None or not group_id:
            return 0
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            room = None
            try:
                updater = getattr(self._itchat, "update_chatroom", None)
                if callable(updater):
                    try:
                        room = updater(group_id, detailedMember=True)
                    except TypeError:
                        room = updater(group_id)
            except Exception:
                logger.debug("WeChatUOS ACL: update_chatroom failed for %s", group_name or group_id, exc_info=True)
            if not isinstance(room, dict):
                try:
                    room = self._itchat.search_chatrooms(userName=group_id)
                except Exception:
                    room = None
            if not isinstance(room, dict):
                logger.warning("WeChatUOS ACL: failed to refresh members for group=%s", group_name or group_id)
                return 0
            if room.get("NickName"):
                group["name"] = html.unescape(str(room.get("NickName")))
            members = room.get("MemberList") or []
            count = 0
            for member in members:
                if not isinstance(member, dict):
                    continue
                uid = str(member.get("UserName") or "")
                if not uid:
                    continue
                self._remember_member(
                    group,
                    uid,
                    nick=member.get("NickName") or "",
                    display=member.get("DisplayName") or "",
                    remark=member.get("RemarkName") or "",
                )
                count += 1
            if members:
                owner = members[0]
                owner_uid = str(owner.get("UserName") or "")
                if owner_uid:
                    group["owner_uid"] = owner_uid
                    self._add_unique(group.setdefault("admins", []), owner_uid)
                    logger.info("WeChatUOS ACL: owner detected group=%s owner=%s uid=%s", group.get("name") or group_id, self._member_display(group, owner_uid), owner_uid)
            group["updated_at"] = int(time.time())
            self._save_acl()
            return count

    def _find_member_uid(self, group_id: str, group_name: str, target: str):
        target = (target or "").strip()
        if not target:
            return None, "请提供群内昵称。"
        if target.startswith("@"):
            return target, None
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
        # Refresh once before matching, so new members/nicknames are available.
        self._refresh_group_members(group_id, group_name)
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            matches = []
            for uid, meta in group.get("members_cache", {}).items():
                if isinstance(meta, dict):
                    names = {str(meta.get("nick_name") or ""), str(meta.get("display_name") or ""), str(meta.get("remark_name") or "")}
                else:
                    names = {str(meta or "")}
                if target in names:
                    matches.append(uid)
            if not matches:
                # Commands accept either a cached group display name or a raw WeChat UID.
                # Raw UIDs are not always present in MemberList (and may not start with
                # "@" on every protocol/version), so fall back to treating the input as
                # a UID after an exact-name lookup fails.
                return target, None
            if len(matches) > 1:
                names = "、".join(self._member_display(group, uid) for uid in matches[:5])
                return None, f"找到多个叫“{target}”的成员，请使用更完整的群昵称。匹配：{names}"
            return matches[0], None

    def _is_group_authorize_command(self, text: str) -> bool:
        s = (text or "").strip().lower()
        compact = s.replace(" ", "")
        return compact in {"授权此群聊", "授权本群", "启用本群", "开启本群", "authorizegroup", "/authorizegroup"}

    def _can_use_group(self, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                logger.info("WeChatUOS ACL: ignored unauthorized group=%s sender=%s uid=%s", group_name, sender, sender_id)
                self._save_acl()
                return False
            allowed = sender_id in group.get("admins", []) or sender_id in group.get("allowed_users", [])
            # Backward-compatible escape hatch for existing global env allowlist.
            if not allowed and self.allowed_users and (sender in self.allowed_users or sender_id in self.allowed_users):
                allowed = True
            if not allowed:
                logger.info("WeChatUOS ACL: ignored unauthorized sender %s uid=%s in group %s", sender, sender_id, group_name)
            self._save_acl()
            return allowed

    def _group_member_role(self, group_id: str, group_name: str, sender: str, sender_id: str) -> str:
        """Return accepted ACL tier for a group sender.

        ``admin`` keeps full gateway/tool access. ``user`` is restricted by the
        gateway to chat/search/query toolsets and read-only slash commands.
        """
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            if self._is_group_admin(group, sender, sender_id):
                return "admin"
            if sender_id in group.get("allowed_users", []):
                return "user"
            if self.allowed_users and (sender in self.allowed_users or sender_id in self.allowed_users):
                return "user"
        return "user"

    def _is_group_admin(self, group: Dict[str, Any], sender: str, sender_id: str) -> bool:
        # Existing global admin env remains a bootstrap/admin escape hatch.
        if sender in self.admin_users or sender_id in self.admin_users:
            return True
        return sender_id in group.get("admins", [])

    def _format_acl(self, group_id: str, group_name: str) -> str:
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            admins = group.get("admins", [])
            allowed = group.get("allowed_users", [])
            admin_lines = [f"- {self._member_display(group, uid)}" for uid in admins] or ["- 无"]
            allowed_lines = [f"- {self._member_display(group, uid)}" for uid in allowed] or ["- 无"]
            status = "已授权" if group.get("authorized") else "未授权"
            return "\n".join([
                f"当前群：{group.get('name') or group_name or group_id}",
                f"状态：{status}",
                "",
                "管理员：",
                *admin_lines,
                "",
                "已授权用户：",
                *allowed_lines,
            ])

    def _handle_acl_command(self, text: str, *, sender: str, sender_id: str, chat_id: str, group_name: str) -> bool:
        """Handle per-group ACL commands. Returns True if consumed."""
        parts = (text or "").strip().split()
        if not parts:
            return False
        cmd = parts[0].lower()
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(chat_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)

        if self._is_group_authorize_command(text):
            with self._acl_lock:
                group = self._group_acl(chat_id, group_name)
                if not group.get("authorized"):
                    group["authorized"] = True
                    group["initial_admin_uid"] = sender_id
                    self._add_unique(group.setdefault("admins", []), sender_id)
                    group["updated_at"] = int(time.time())
                    self._save_acl()
                    logger.info("WeChatUOS ACL: group initialized group=%s admin=%s uid=%s", group_name, sender, sender_id)
                    try:
                        self._refresh_group_members(chat_id, group_name)
                    except Exception:
                        logger.debug("WeChatUOS ACL: member refresh after authorization failed", exc_info=True)
                    self._itchat.send(f"本群已授权成功。\n管理员：{sender}", toUserName=chat_id)
                    return True
                if self._is_group_admin(group, sender, sender_id):
                    if group.get("restored_from_group_id"):
                        logger.info(
                            "WeChatUOS ACL: remembered group authorization silently accepted group=%s sender=%s uid=%s restored_from=%s",
                            group_name,
                            sender,
                            sender_id,
                            group.get("restored_from_group_id"),
                        )
                        return True
                    self._itchat.send("本群已授权，无需重复授权。", toUserName=chat_id)
                    return True
            logger.info("WeChatUOS ACL: duplicate group authorization ignored group=%s sender=%s uid=%s", group_name, sender, sender_id)
            return True

        aliases = {
            "/allow", "allow", "授权", "添加权限", "加权限",
            "/deny", "deny", "取消授权", "移除权限", "删权限",
            "/admin", "admin", "设管理员", "添加管理员", "管理员",
            "/unadmin", "unadmin", "取消管理员", "移除管理员",
            "/acl", "acl", "权限列表", "名单",
            "/refresh", "refresh", "刷新成员", "刷新群成员",
        }
        if cmd not in aliases:
            chinese_prefixes = [
                "取消管理员", "移除管理员", "取消授权", "移除权限",
                "添加管理员", "设管理员", "添加权限", "加权限", "删权限", "管理员", "授权",
            ]
            for prefix in chinese_prefixes:
                if cmd.startswith(prefix) and cmd != prefix:
                    target = cmd[len(prefix):].strip()
                    if target:
                        cmd = prefix
                        parts = [prefix, target]
                        break
            else:
                return False
        with self._acl_lock:
            group = self._group_acl(chat_id, group_name)
            authorized = bool(group.get("authorized"))
            is_admin = self._is_group_admin(group, sender, sender_id)
        if not authorized:
            self._itchat.send("当前群尚未授权。请发送：@机器人 授权此群聊", toUserName=chat_id)
            return True
        if not is_admin:
            self._itchat.send("你没有权限管理本群机器人。", toUserName=chat_id)
            return True
        if cmd in {"/acl", "acl", "权限列表", "名单"}:
            self._itchat.send(self._format_acl(chat_id, group_name), toUserName=chat_id)
            return True
        if cmd in {"/refresh", "refresh", "刷新成员", "刷新群成员"}:
            count = self._refresh_group_members(chat_id, group_name)
            self._itchat.send(f"成员列表已刷新。\n当前缓存成员数：{count}", toUserName=chat_id)
            return True
        if len(parts) < 2:
            self._itchat.send("格式：授权/取消授权/设管理员/取消管理员 昵称或UID", toUserName=chat_id)
            return True
        target_name = " ".join(parts[1:]).strip()
        target_uid, err = self._find_member_uid(chat_id, group_name, target_name)
        if err:
            self._itchat.send(err, toUserName=chat_id)
            return True
        with self._acl_lock:
            group = self._group_acl(chat_id, group_name)
            label = self._member_display(group, target_uid, target_name)
            if cmd in {"/allow", "allow", "授权", "添加权限", "加权限"}:
                self._add_unique(group.setdefault("allowed_users", []), target_uid)
                action = "已授权"
            elif cmd in {"/deny", "deny", "取消授权", "移除权限", "删权限"}:
                if target_uid in group.get("admins", []):
                    self._itchat.send("不能用取消授权命令移除管理员。", toUserName=chat_id)
                    return True
                group.setdefault("allowed_users", [])[:] = [uid for uid in group.get("allowed_users", []) if uid != target_uid]
                action = "已取消授权"
            elif cmd in {"/admin", "admin", "设管理员", "添加管理员", "管理员"}:
                self._add_unique(group.setdefault("admins", []), target_uid)
                action = "已设为管理员"
            else:
                if target_uid == group.get("owner_uid") or target_uid == group.get("initial_admin_uid"):
                    self._itchat.send("不能移除群主或初始管理员。", toUserName=chat_id)
                    return True
                group.setdefault("admins", [])[:] = [uid for uid in group.get("admins", []) if uid != target_uid]
                action = "已取消管理员"
            group["updated_at"] = int(time.time())
            self._save_acl()
        self._itchat.send(f"{action}：{label}", toUserName=chat_id)
        logger.info("WeChatUOS ACL: %s %s uid=%s by admin %s/%s in group %s", action, label, target_uid, sender, sender_id, group_name)
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
        if chat_type == "group":
            # Group messages only reach this point after the adapter's
            # per-group ACL has accepted them. Mark them as pre-authorized so
            # the gateway's global allowlist does not block the same sender a
            # second time.
            setattr(source, "trusted_by_adapter", True)
            setattr(source, "wechat_uos_acl_role", self._group_member_role(chat_id, chat_name, user_name, user_id))
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

