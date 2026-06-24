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
import re
import threading
import time
import urllib.request
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, quote as _urlquote, urlencode as _urlencode
from urllib.request import urlopen as _urlopen, Request as _URLRequest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult


def _to_short_url(url: str) -> str:
    """Convert long WeChat article URL to short format (mp_id_idx).
    
    Long: http://mp.weixin.qq.com/s?__biz=...&mid=2247485803&idx=2&sn=...
    Short: https://mp.weixin.qq.com/s/2247485803_2
    """
    if not url or "?__biz=" not in url:
        return url
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        mid = params.get('mid', [''])[0]
        idx = params.get('idx', [''])[0]
        if mid and idx:
            return f"https://mp.weixin.qq.com/s/{mid}_{idx}"
    except Exception:
        pass
    return url


logger = logging.getLogger(__name__)

# ── TG channel_post forwarder registration ──────────────────────────────
# Registered so the gateway pushes channel posts to us instead of us polling
# getUpdates — avoids 409 Conflict from duplicate bot-token connections.
try:
    from gateway.platforms.telegram import register_channel_post_forwarder
    TG_FWD_REGISTRY_AVAILABLE = True
except Exception:
    TG_FWD_REGISTRY_AVAILABLE = False
SUPER_ADMIN_NAMES = {"夢魚", "庾梦"}  # only these nicknames can be admin across all groups
HERMES_HOME = Path(os.getenv("HERMES_HOME") or "/root/.hermes")
STATE_DIR = HERMES_HOME / "wechat_uos"
QR_PNG = STATE_DIR / "itchat_qr.png"
QR_URL_TXT = STATE_DIR / "itchat_qr_url.txt"
PKL_FILE = STATE_DIR / "itchat.pkl"
ACL_FILE = STATE_DIR / "acl.json"
DEFAULT_QR_PORT = 8646
MAX_MESSAGE_LENGTH = 3500

# ── PanSou 盘搜 ────────────────────────────────────────────────────────────
PANSW_API = "http://192.168.10.216:850/api/search"
PANSOU_ALLOWED_TYPES = {"quark", "115", "baidu", "uc", "magnet"}
# ── CFTC 图床上传 ────────────────────────────────────────────────────────────
CFTC_DIR = HERMES_HOME / "data" / "cftc"
CFTC_CONFIG_FILE = CFTC_DIR / "config.json"
CFTC_STATE_FILE = CFTC_DIR / "group_state.json"
CFTC_COOKIE_FILE = CFTC_DIR / "cftc_cookie.json"
CFTC_MEDIA_DIR = CFTC_DIR / "media"
# ── LSPosed 模块更新 ─────────────────────────────────────────────────────────
LSPOSED_DIR = HERMES_HOME / "data" / "lsposed_tracker"
LSPOSED_CONFIG = LSPOSED_DIR / "config.json"
LSPOSED_STATE_FILE = LSPOSED_DIR / "state.json"
# ── 抖音解析 API ─────────────────────────────────────────────────────────
DOUYIN_API_BASE = "http://192.168.10.216:8002"
# ── WeRSS 公众号文章推送 ──
WERSS_BASE = "http://localhost:8001/api/v1/wx"
WERSS_USER = "admin"
WERSS_PASS = "admin123"
WERSS_STATE_FILE = STATE_DIR / "werss_state.json"
WERSS_POLL_INTERVAL = 300  # 5 minutes

PANSOU_SOURCE_LABELS = {
    "quark": "夸克网盘",
    "115": "115网盘",
    "baidu": "百度网盘",
    "uc": "UC网盘",
    "magnet": "磁力链接",
}
PANSOU_MESSAGE_HEADER = "🔍 搜索「{keyword}」"


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
        # ── CFTC upload state ──
        self._cftc_recent_media: Dict[str, list] = {}
        self._cftc_processed_ids: set = set()
        # ── LSPosed tracker state ──
        self._lsposed_stop = threading.Event()
        self._lsposed_thread: Optional[threading.Thread] = None
        # ── WeRSS poller state ──
        self._werss_stop = threading.Event()
        self._werss_thread: Optional[threading.Thread] = None
        self._startup_ts = time.time()
        self._login_ts = 0
        self._acl_lock = threading.RLock()
        self._acl: Dict[str, Any] = self._load_acl()
        self._super_admin_uid: str = self._acl.get("_super_admin_uid", "")

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
        self._werss_stop.set()

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
        self._login_ts = time.time()
        try:
            user = self._itchat.search_friends() if self._itchat else {}
            if not isinstance(user, dict):
                user = {}
            self._login_name = html.unescape(str(user.get("NickName") or ""))
            logger.info("WeChatUOS: login successful as %s at %.0f", self._login_name, self._login_ts)
        except Exception:
            logger.info("WeChatUOS: login successful at %.0f", self._login_ts)

    # ─────────────────────────────────────────────────────────────────────
    #  登录后自维护：自动迁移 home channel GID + 修复 bot token
    # ─────────────────────────────────────────────────────────────────────
    def _post_login_maintenance(self) -> None:
        """After login, auto-fix home channel GID and TG_FWD bot token.

        - WECHAT_UOS_HOME_CHANNEL in config.yaml gets stale when itchat-uos
          reconnects with new group IDs.  Find the current '机器人测试' group
          and update it.
        - The tg_fwd bot token can be corrupted to '***' by tool redaction.
          Restore from a safe backup file on every login.
        """
        try:
            self._migrate_home_channel_gid()
        except Exception:
            logger.exception("WeChatUOS: home channel migration failed")
        try:
            self._fix_tg_fwd_token()
        except Exception:
            logger.exception("WeChatUOS: tg_fwd token fix failed")

    def _migrate_home_channel_gid(self) -> None:
        """Find the current '机器人测试' group GID and update home channel."""
        if not self._itchat:
            return
        try:
            # Get all current chatrooms
            from pathlib import Path
            import pickle
            if not PKL_FILE.exists():
                return
            data = pickle.loads(PKL_FILE.read_bytes())
            rooms = data.get("storage", {}).get("chatroomList", [])
            target_name = None
            new_gid = None
            # Find the first non-empty-name group (usually 机器人测试)
            for room in rooms:
                gid = room.get("UserName", "")
                name = room.get("NickName", "") or room.get("DisplayName", "") or ""
                if name.strip() and gid:
                    target_name = name
                    new_gid = gid
                    break
            if not new_gid:
                logger.warning("WeChatUOS: no chatrooms found for home channel migration")
                return
            old_gid = self.home_channel
            if new_gid == old_gid:
                return  # already up to date
            # Update in-memory
            self.home_channel = new_gid
            logger.info("WeChatUOS: home channel migrated %s (%s) -> %s (%s)",
                        old_gid[:16], target_name, new_gid[:16], target_name)
            # Persist to config.yaml
            self._update_config_key("WECHAT_UOS_HOME_CHANNEL", new_gid)
        except Exception:
            logger.exception("WeChatUOS: home channel GID migration failed")

    def _fix_tg_fwd_token(self) -> None:
        """Restore tg_fwd bot token from safe backup if corrupted."""
        import json
        bup = TG_FWD_TOKEN_BACKUP
        cfg = TG_FWD_CONFIG
        if not cfg.exists():
            return
        raw = cfg.read_bytes()
        # Check if token is corrupted (has literal *** or too short)
        try:
            cfg_data = json.loads(raw)
        except Exception:
            return
        token = cfg_data.get("bot_token", "")
        if not token:
            return
        # Valid Telegram bot token is ~45 chars like 8951409744:ABC...
        if "***" not in token and len(token) > 20:
            # Token looks valid; make sure backup exists
            if not bup.exists() or bup.read_text().strip() != token:
                bup.write_text(token)
                logger.info("WeChatUOS: backed up tg_fwd bot token")
            return
        # Token is corrupted — restore from backup
        if bup.exists():
            good = bup.read_text().strip()
            if good and "***" not in good and len(good) > 20:
                cfg_data["bot_token"] = good
                cfg.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2))
                logger.info("WeChatUOS: restored tg_fwd bot token from backup")
                return
        logger.warning("WeChatUOS: tg_fwd bot token is corrupted and no valid backup found")

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
                # ── 静默启动：过滤登录前重放的旧消息 ──
                if self._login_ts > 0:
                    msg_time = getattr(msg, "createTime", None) or msg.get("CreateTime", 0)
                    if isinstance(msg_time, (int, float)) and msg_time < self._login_ts - 2:
                        return
                raw_text = getattr(msg, "text", None) or msg.get("Text") or msg.get("Content") or ""
                text = _strip_at_prefix(raw_text)
                group_name = self._resolve_group_name(group_id)
                if not self._group_in_allowlist(group_id, group_name):
                    return
                if self._handle_acl_command(text, sender=sender, sender_id=sender_id, chat_id=group_id, group_name=group_name):
                    return
                # ── 帮助 ──
                if self._handle_help_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                # ── PanSou 盘搜 ──
                pansou_result = self._handle_pansou_search(text, group_id, group_name, sender=sender, sender_id=sender_id)
                if pansou_result is not None:
                    return
                # ── PanSou toggle ──
                if self._handle_pansou_toggle_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                # ── TG forward toggle ──
                if self._handle_tg_fwd_toggle_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                # ── 抖音解析 toggle ──
                if self._handle_douyin_toggle_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                # ── 抖音链接解析 ──
                if self._handle_douyin_parse(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                # ── CFTC toggle/upload commands ──
                if self._handle_cftc_toggle_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                if self._handle_cftc_upload_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                # ── LSPosed tracker commands ──
                if self._handle_lsposed_text_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
                    return
                # ── WeRSS toggle commands ──
                if self._handle_werss_text_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id):
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
            # ── 登录后维护：自动修复 home channel GID 和 bot token ──
            self._post_login_maintenance()
            logger.info("WeChatUOS: listening for group @mentions")
            self._start_tg_fwd()
            # ── Start LSPosed module tracker polling ──
            self._start_lsposed_tracker()
            # ── Start WeRSS article poller ──
            self._start_werss_poller()
            # ── Register media handlers for CFTC upload ──
            self._register_cftc_media_handlers()
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

        Scoring: prefer records that have ``allowed_mps`` (user configured
        subscriptions), then favor more recently updated ones.  This avoids
        picking an empty restored record (fresh timestamp, no features) over
        a record that actually has per-group preferences.
        """
        normalized = self._normalize_group_name(group_name)
        if not normalized:
            return None
        groups = self._acl.setdefault("groups", {})
        best: Optional[Dict[str, Any]] = None
        best_score = -1
        best_ts = 0
        for existing_id, existing in groups.items():
            if existing_id == group_id or not isinstance(existing, dict):
                continue
            if not existing.get("authorized"):
                continue
            if self._normalize_group_name(existing.get("name", "")) == normalized:
                ts = existing.get("updated_at", 0)
                has_features = bool(existing.get("allowed_mps"))
                score = (2 if has_features else 0) + (1 if ts >= best_ts else 0)
                if score > best_score or (score == best_score and ts >= best_ts):
                    best = existing
                    best_score = score
                    best_ts = ts
        return best

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
                # Also carry over all feature-specific settings (werss, douyin, etc.)
                # so GID migration doesn't lose per-group preferences.
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
                # Carry over feature-specific settings from the old record
                for _key in ("allowed_mps", "allowed_mps_since", "werss_enabled", "douyin_enabled",
                             "pansou_enabled", "cftc_enabled", "tg_fwd_enabled",
                             "tg_forward_enabled", "allow_reply", "auto_reply"):
                    _val = restored.get(_key)
                    if _val is not None:
                        group[_key] = _val
                groups[group_id] = group
                logger.info(
                    "WeChatUOS ACL: restored authorization for group=%s new_id=%s old_id=%s",
                    group.get("name") or group_name,
                    group_id,
                    restored_from_group_id,
                )
                # Also migrate GID in tg_fwd config and CFTC group state
                self._migrate_external_gid(restored_from_group_id, group_id, group.get("name") or group_name)
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
        group.setdefault("lsposed_enabled", False)
        group.setdefault("werss_enabled", True)
        if group.get("restored_from_group_id") and group.get("updated_at") == now:
            self._save_acl()
        return group

    @staticmethod
    def _update_config_key(key: str, value: str) -> None:
        """Update a top-level key in ~/.hermes/config.yaml and persist."""
        import re
        cfg_path = HERMES_HOME / "config.yaml"
        if not cfg_path.exists():
            return
        raw = cfg_path.read_text()
        # Match existing key:value line (case-insensitive)
        pattern = re.compile(r"^(?P<indent>\s*)" + re.escape(key) + r"\s*:\s*['\"]?(.*?)['\"]?\s*$", re.MULTILINE)
        new_line = f"{key}: '{value}'"
        if pattern.search(raw):
            raw = pattern.sub(new_line, raw)
        else:
            raw = raw.rstrip() + "\n" + new_line + "\n"
        cfg_path.write_text(raw)
        logger.info("WeChatUOS: persisted %s = %s...", key, value[:16])

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

    def _is_super_admin(self, sender: str, sender_id: str) -> bool:
        """Check if sender is 夢魚/庾梦 — the only super admin across all groups."""
        # Known UID match (cached after first identify)
        if self._super_admin_uid and sender_id == self._super_admin_uid:
            return True
        # Name-based match for first-time identification
        if sender in SUPER_ADMIN_NAMES:
            if sender_id and sender_id != self._super_admin_uid:
                self._super_admin_uid = sender_id
                self._acl["_super_admin_uid"] = sender_id
                self._save_acl()
                logger.info("WeChatUOS ACL: super admin UID cached %s (%s)", sender_id[:16], sender)
            return True
        return False

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
                    # Only record owner uid for reference; do NOT auto-add as admin.
                    if self._is_super_admin(self._member_display(group, owner_uid), owner_uid):
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
        if compact in {"授权此群聊", "授权本群", "启用本群", "开启本群", "开启授权", "authorizegroup", "/authorizegroup"}:
            return True
        # Also check if command appears as second word (bot name prefix stripped imperfectly)
        parts = s.split()
        if len(parts) >= 2 and parts[1] in {"授权此群聊", "授权本群", "启用本群", "开启本群", "开启授权"}:
            return True
        return False

    def _is_group_deauthorize_command(self, text: str) -> bool:
        s = (text or "").strip().lower()
        compact = s.replace(" ", "")
        if compact in {"关闭授权", "关闭本群", "禁用本群", "deauthorizegroup", "/deauthorizegroup"}:
            return True
        # Also check if command appears as second word (bot name prefix stripped imperfectly)
        parts = s.split()
        if len(parts) >= 2 and parts[1] in {"关闭授权", "关闭本群", "禁用本群"}:
            return True
        return False

    def _can_use_group(self, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                logger.info("WeChatUOS ACL: ignored unauthorized group=%s sender=%s uid=%s", group_name, sender, sender_id)
                self._save_acl()
                return False
            # Super admin (夢魚) and group-level admins can use the bot.
            allowed = self._is_group_admin(group, sender, sender_id)
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
        # Super admin (夢魚/庾梦) is admin everywhere.
        # Group-level admins are explicitly authorized by 夢魚 via "授权昵称".
        if self._is_super_admin(sender, sender_id):
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
                    # Only super admin (夢魚/庾梦) can authorize a new group
                    if not self._is_super_admin(sender, sender_id):
                        self._itchat.send("你没有权限开启本群授权，请联系管理员。", toUserName=chat_id)
                        return True
                    group["authorized"] = True
                    group["initial_admin_uid"] = sender_id
                    group["updated_at"] = int(time.time())
                    # Auto-enable LSPosed push if this group is in lsposed_tracker target_groups
                    try:
                        _lsp_cfg_path = HERMES_HOME / "data" / "lsposed_tracker" / "config.json"
                        if _lsp_cfg_path.exists():
                            _lsp_cfg = json.loads(_lsp_cfg_path.read_text())
                            _lsp_targets = _lsp_cfg.get("target_groups", []) or []
                            if chat_id in _lsp_targets and not group.get("lsposed_enabled"):
                                group["lsposed_enabled"] = True
                                logger.info("WeChatUOS ACL: auto-enabled LSPosed push for %s (in target_groups)", group_name)
                    except Exception:
                        logger.debug("WeChatUOS ACL: auto-enable lsposed check failed", exc_info=True)
                    self._save_acl()
                    logger.info("WeChatUOS ACL: group authorized group=%s super_admin=%s uid=%s", group_name, sender, sender_id)
                    try:
                        self._refresh_group_members(chat_id, group_name)
                    except Exception:
                        logger.debug("WeChatUOS ACL: member refresh after authorization failed", exc_info=True)
                    if group.get("lsposed_enabled"):
                        self._itchat.send("本群已授权成功。\n模块更新推送已自动开启（白名单群）。", toUserName=chat_id)
                    else:
                        self._itchat.send("本群已授权成功。", toUserName=chat_id)
                    # 自动加入 TG 转发列表
                    try:
                        self._auto_add_tg_fwd_group(chat_id, group_name)
                    except Exception:
                        logger.debug("WeChatUOS ACL: auto TG fwd add after auth failed", exc_info=True)
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
                # Non-super-admin sending auth in an authorized group → denied
                self._itchat.send("你没有权限管理本群授权。", toUserName=chat_id)
                return True
            logger.info("WeChatUOS ACL: duplicate group authorization ignored group=%s sender=%s uid=%s", group_name, sender, sender_id)
            return True

        if self._is_group_deauthorize_command(text):
            with self._acl_lock:
                group = self._group_acl(chat_id, group_name)
                if not group.get("authorized"):
                    self._itchat.send("本群尚未授权，无需关闭。", toUserName=chat_id)
                    return True
                if not self._is_group_admin(group, sender, sender_id):
                    self._itchat.send("你没有权限关闭本群的授权。", toUserName=chat_id)
                    return True
                group["authorized"] = False
                group["updated_at"] = int(time.time())
                self._save_acl()
            logger.info("WeChatUOS ACL: group deauthorized group=%s admin=%s uid=%s", group_name, sender, sender_id)
            self._itchat.send("本群已关闭授权，机器人将不再响应群消息。\n如需重新开启，请发送：@机器人 开启授权", toUserName=chat_id)
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
            # Try to match cmd against known prefixes
            matched = False
            for prefix in chinese_prefixes:
                if cmd.startswith(prefix) and cmd != prefix:
                    target = cmd[len(prefix):].strip()
                    if target:
                        cmd = prefix
                        parts = [prefix, target]
                        matched = True
                    break
            if not matched:
                # cmd didn't match any prefix; check if the next word is the command
                if len(parts) >= 2:
                    candidate = parts[1].lower()
                    if candidate in aliases:
                        cmd = candidate
                        parts = [cmd] + parts[2:]
                    else:
                        for prefix in chinese_prefixes:
                            if candidate.startswith(prefix) and candidate != prefix:
                                target = candidate[len(prefix):].strip()
                                if target:
                                    cmd = prefix
                                    parts = [prefix, target]
                                matched = True
                                break
                        if not matched:
                            return False
                else:
                    return False
        with self._acl_lock:
            group = self._group_acl(chat_id, group_name)
            authorized = bool(group.get("authorized"))
            is_admin = self._is_group_admin(group, sender, sender_id)
        if not authorized:
            self._itchat.send("当前群尚未授权。请发送：@机器人 开启授权 或 授权此群聊", toUserName=chat_id)
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
                # Only super admin (夢魚) can add/remove group-level admins
                if not self._is_super_admin(sender, sender_id):
                    self._itchat.send("你没有权限设置管理员。请联系超级管理员。", toUserName=chat_id)
                    return True
                self._add_unique(group.setdefault("admins", []), target_uid)
                action = "已设为管理员"
            else:
                if not self._is_super_admin(sender, sender_id):
                    self._itchat.send("你没有权限取消管理员。请联系超级管理员。", toUserName=chat_id)
                    return True
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

    # ── PanSou 盘搜 ────────────────────────────────────────────────────────

    @staticmethod
    def _is_pansou_command(text: str) -> Optional[str]:
        """Check if text starts with 搜索/搜. Returns keyword or None."""
        if not text:
            return None
        s = text.strip()
        # Helper: extract keyword after a command word (搜索 or 搜)
        def _kw_after(s: str, cmd_len: int) -> str:
            rest = s[cmd_len:].strip()
            return rest if rest else ""
        # First word check (normal case)
        if s.startswith("搜索"):
            return _kw_after(s, 2)
        if s.startswith("搜 ") or s.startswith("搜"):
            return _kw_after(s, 2)
        # Second word fallback (bot name prefix stripped imperfectly)
        parts = s.split()
        if len(parts) >= 2:
            # If second word is exactly a command, keyword is the rest
            if parts[1] == "搜索" or parts[1] == "搜":
                return " ".join(parts[2:]).strip()
            # If second word starts with command, treat as combined (e.g., 搜索关键词)
            if parts[1].startswith("搜索") or parts[1].startswith("搜"):
                kw = parts[1][2:].strip()
                return kw if kw else None
        return None

    def _pansou_is_enabled(self, group_id: str) -> bool:
        with self._acl_lock:
            return self._group_acl(group_id, "").get("pansou_enabled", True)

    def _handle_pansou_search(self, text: str, group_id: str, group_name: str, sender: str = "", sender_id: str = "") -> Optional[bool]:
        """Handle 搜索/搜 keyword. Returns True if consumed, None if not a search command."""
        keyword = self._is_pansou_command(text)
        if keyword is None:
            return None
        if self._itchat is None:
            return True
        # Check if pansou is enabled for this group
        if not self._pansou_is_enabled(group_id):
            return True
        # Check sender permission: admin or allowed_users can search
        # Always enforce — do NOT skip when sender_id/sender are empty.
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            if not group.get("authorized"):
                self._itchat.send("本群尚未开启 Hermes，请先发送：开启授权", toUserName=group_id)
                return True
            if not sender_id and not sender:
                self._itchat.send("无法识别发送者身份，盘搜搜索已拒绝。", toUserName=group_id)
                return True
            if not self._is_group_admin(group, sender, sender_id) and sender_id not in group.get("allowed_users", []):
                self._itchat.send("你没有使用盘搜的权限。请联系管理员为你授权。", toUserName=group_id)
                return True
        logger.info("WeChatUOS PanSou: searching '%s' in %s", keyword, group_name)
        try:
            results = self._search_pansou(keyword)
        except Exception as e:
            logger.exception("WeChatUOS PanSou: API error for '%s'", keyword)
            self._itchat.send(f"[盘搜] API 请求失败: {e}", toUserName=group_id)
            return True
        self._send_pansou_results(keyword, results, group_id)
        return True

    def _search_pansou(self, keyword: str) -> List[Dict[str, Any]]:
        """Call PanSou API, return results grouped by source type."""
        url = f"{PANSW_API}?kw={_urlquote(keyword)}&page=1&size=400"
        req = _URLRequest(url, headers={"User-Agent": "Hermes-WeChatUOS/1.0"})
        with _urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"), strict=False)
        merged = raw.get("data", {}).get("merged_by_type", {})
        results: List[Dict[str, Any]] = []
        for src_type, items in merged.items():
            if str(src_type).lower() not in PANSOU_ALLOWED_TYPES:
                continue
            for item in items[:5]:
                url_str = item.get("url", "")
                pw = item.get("password", "")
                if pw:
                    url_str = f"{url_str} 密码:{pw}"
                results.append({
                    "source": str(src_type).lower(),
                    "note": item.get("note", ""),
                    "url_str": url_str,
                    "datetime": item.get("datetime", ""),
                })
        results.sort(key=lambda x: x.get("datetime", ""), reverse=True)
        return results

    def _send_pansou_results(self, keyword: str, results: List[Dict[str, Any]], group_id: str) -> None:
        """Send search results to group, grouped by pan type, each as a separate message."""
        if not results:
            self._itchat.send(f"🔍 搜索「{keyword}」没找到匹配的资源", toUserName=group_id)
            return

        # Group by source type
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in results:
            grouped.setdefault(r["source"], []).append(r)

        # Send header message first
        self._itchat.send(f"🔍 搜索「{keyword}」共 {len(results)} 条结果", toUserName=group_id)

        # Send one message per pan type
        for src_type, items in grouped.items():
            label = PANSOU_SOURCE_LABELS.get(src_type, src_type)
            lines = [f"━━━ {label} ━━━"]
            for i, item in enumerate(items[:5], 1):
                lines.append(f"{i}. {item['note']}")
                lines.append(f"   📎 {item['url_str']}")
            self._itchat.send("\n".join(lines), toUserName=group_id)

    def _handle_pansou_toggle_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        """Handle 开启盘搜 / 关闭盘搜 commands. Returns True if consumed."""
        s = (text or "").strip().lower()
        # Exact match after removing all spaces
        compact = s.replace(" ", "")
        if compact in ("开启盘搜", "关闭盘搜"):
            cmd = compact
        else:
            # Check if command is second word (bot name prefix stripped imperfectly)
            parts = s.split()
            if len(parts) >= 2 and parts[1] in ("开启盘搜", "关闭盘搜"):
                cmd = parts[1]
            else:
                return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._itchat.send("本群尚未开启 Hermes，请先发送：开启授权", toUserName=group_id)
                return True
            if not self._is_group_admin(group, sender, sender_id):
                self._itchat.send("你没有权限管理盘搜。", toUserName=group_id)
                return True
            is_enable = cmd == "开启盘搜"
            group["pansou_enabled"] = is_enable
            group["updated_at"] = int(time.time())
            self._save_acl()
        status = "已开启" if is_enable else "已关闭"
        self._itchat.send(f"✅ 盘搜{status}，{'管理员和已授权成员可使用「搜索 xxx」' if is_enable else ''}", toUserName=group_id)
        logger.info("WeChatUOS PanSou: %s for %s by %s/%s", "enabled" if is_enable else "disabled", group_name, sender, sender_id)
        return True

    def _handle_tg_fwd_toggle_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        """Handle 开启转发 / 关闭转发 commands. Returns True if consumed."""
        s = (text or "").strip().lower()
        # Exact match after removing all spaces
        compact = s.replace(" ", "")
        if compact in ("开启转发", "关闭转发", "开启tg转发", "关闭tg转发"):
            cmd = compact
        else:
            # Check if command is second word (bot name prefix stripped imperfectly)
            parts = s.split()
            if len(parts) >= 2 and parts[1] in ("开启转发", "关闭转发", "开启tg转发", "关闭tg转发"):
                cmd = parts[1]
            else:
                return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._itchat.send("本群尚未开启 Hermes，请先发送：开启授权", toUserName=group_id)
                return True
            if not self._is_group_admin(group, sender, sender_id):
                self._itchat.send("你没有权限管理 TG 转发。", toUserName=group_id)
                return True
        is_enable = cmd in ("开启转发", "开启tg转发")
        # Also update config.json forward_rules — add group when enabling, remove when disabling
        try:
            if TG_FWD_CONFIG.exists():
                cfg = json.loads(TG_FWD_CONFIG.read_text())
                changed = False
                for rule in cfg.get("forward_rules", []):
                    wx_groups = rule.get("wechat_groups", [])
                    if is_enable:
                        if group_id not in wx_groups:
                            wx_groups.append(group_id)
                            changed = True
                    else:
                        if group_id in wx_groups:
                            wx_groups.remove(group_id)
                            changed = True
                if changed:
                    TG_FWD_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        except Exception:
            logger.debug("WeChatUOS TG_fwd: config update failed", exc_info=True)
        gs_path = TG_FWD_DIR / "group_state.json"
        gstate = {}
        if gs_path.exists():
            try:
                gstate = json.loads(gs_path.read_text())
            except Exception:
                pass
        gstate[group_id] = {"enabled": is_enable, "updated_at": int(time.time())}
        gs_path.write_text(json.dumps(gstate, ensure_ascii=False, indent=2))
        status = "已开启" if is_enable else "已关闭"
        self._itchat.send(f"✅ TG 转发{status}", toUserName=group_id)
        logger.info("WeChatUOS TG_fwd: %s for %s by %s/%s", status, group_name, sender, sender_id)
        return True

    def _auto_add_tg_fwd_group(self, group_id: str, group_name: str) -> None:
        """Auto-add a group to TG forward rules and enable forwarding."""
        import json
        if not TG_FWD_CONFIG.exists():
            return
        cfg = json.loads(TG_FWD_CONFIG.read_text())
        changed = False
        for rule in cfg.get("forward_rules", []):
            groups = rule.get("wechat_groups", [])
            if group_id not in groups:
                groups.append(group_id)
                changed = True
        if changed:
            TG_FWD_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        # Also set enabled=True in group_state
        gs_path = TG_FWD_DIR / "group_state.json"
        gstate = {}
        if gs_path.exists():
            try:
                gstate = json.loads(gs_path.read_text())
            except Exception:
                pass
        entry = gstate.setdefault(group_id, {})
        if not entry.get("enabled"):
            entry["enabled"] = True
            entry["updated_at"] = int(time.time())
            gs_path.write_text(json.dumps(gstate, ensure_ascii=False, indent=2))
        logger.info("WeChatUOS TG_fwd: auto-added group %s (%s) to forward rules", group_name, group_id[:16])

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


    # ── TG Channel → WeChat forwarder (uses adapter's itchat instance) ──

    # ═══════════════════════════════════════════════════════════════════════
    #  CFTC Upload Methods
    # ═══════════════════════════════════════════════════════════════════════

    def _register_cftc_media_handlers(self) -> None:
        """Register itchat handlers for media messages (PICTURE/VIDEO/ATTACHMENT)."""
        if self._itchat is None:
            return
        import itchat
        from itchat.content import PICTURE, VIDEO, ATTACHMENT

        @itchat.msg_register(PICTURE, isGroupChat=True)
        def group_picture_handler(msg):
            self._cftc_cache_media_msg(msg)

        @itchat.msg_register(VIDEO, isGroupChat=True)
        def group_video_handler(msg):
            self._cftc_cache_media_msg(msg)

        @itchat.msg_register(ATTACHMENT, isGroupChat=True)
        def group_attachment_handler(msg):
            self._cftc_cache_media_msg(msg)

        logger.info("WeChatUOS CFTC: media handlers registered")

    def _cftc_cache_media_msg(self, msg) -> None:
        """Cache a media message for potential upload."""
        group_id = getattr(msg, "fromUserName", None) or msg.get("FromUserName", "")
        if not group_id:
            return
        file_name = getattr(msg, "fileName", None) or msg.get("FileName", "unknown")
        new_msg_id = getattr(msg, "newMsgId", None) or msg.get("NewMsgId", 0)
        msg_id = getattr(msg, "msgId", None) or msg.get("MsgId", 0)
        now = time.time()
        entry = {
            "path": CFTC_MEDIA_DIR / f"cftc_{new_msg_id}_{file_name}",
            "name": file_name,
            "msg_id": str(msg_id),
            "new_msg_id": str(new_msg_id),
            "size": getattr(msg, "fileSize", None) or msg.get("FileSize", 0),
            "time": now,
        }
        CFTC_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if hasattr(msg, "download"): msg.download(str(entry["path"]))
            else: msg["Text"](str(entry["path"]))
        except Exception as e:
            logger.warning("CFTC: media download failed: %s", e)
            return
        cache = self._cftc_recent_media.setdefault(group_id, [])
        cache.append(entry)
        if len(cache) > 10:
            oldest = cache.pop(0)
            self._cftc_remove_file(oldest["path"])
        self._cftc_processed_ids.add(str(new_msg_id))
        logger.info("CFTC: cached media %s for group %s", file_name, group_id)

    def _cftc_remove_file(self, path) -> None:
        try:
            if path and hasattr(path, "exists") and path.exists():
                path.unlink()
        except Exception: pass

    def _cftc_clean_expired(self, group_id: str) -> None:
        cache = self._cftc_recent_media.get(group_id, [])
        now = time.time()
        fresh = [e for e in cache if now - e["time"] < 600]
        for e in cache:
            if now - e["time"] >= 600: self._cftc_remove_file(e["path"])
        self._cftc_recent_media[group_id] = fresh

    def _cftc_find_latest(self, group_id: str) -> dict:
        self._cftc_clean_expired(group_id)
        cache = self._cftc_recent_media.get(group_id, [])
        return cache[-1] if cache else {}

    def _cftc_upload_file(self, file_path, file_name, storage_type="telegram") -> str:
        """Upload a file to CFTC server. Returns URL or empty string.

        Uses the CFTC v2 API: POST /login → POST /upload (multipart FormData).
        """
        try:
            import json
            import os
            import subprocess
            import tempfile

            cfg_path = CFTC_CONFIG_FILE
            if not cfg_path.exists():
                return ""
            cfg = json.loads(cfg_path.read_text())
            cftc_url = cfg.get("cftc_url", "https://cftc.lliic.com")
            username = cfg.get("cftc_username", "")
            password = cfg.get("cftc_password", "")
            if not username or not password:
                return ""

            cookie_file = CFTC_COOKIE_FILE if CFTC_COOKIE_FILE.exists() else None
            jar = tempfile.NamedTemporaryFile(prefix="cftc_jar_", suffix=".txt", delete=False)
            jar_path = jar.name
            jar.close()

            if cookie_file:
                with open(cookie_file) as f:
                    saved = json.load(f)
                old_cookie = saved.get("cookie", "")
                # Write Netscape cookie format for curl
                # Parse "auth_token=xxx; Path=/; HttpOnly; Secure; ..."
                if old_cookie and "auth_token" in old_cookie:
                    token_part = old_cookie.split(";")[0].strip()
                    with open(jar_path, "w") as jf:
                        jf.write("# Netscape HTTP Cookie File\n")
                        jf.write(f"cftc.lliic.com\tFALSE\t/\tTRUE\t0\t{token_part}\n")

            # Login if needed (try upload first, login on auth failure)
            # Use curl -F for clean multipart upload
            result = subprocess.run(
                ["curl", "-s", "-b", jar_path, "-c", jar_path,
                 "-X", "POST", f"{cftc_url}/login",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps({"username": username, "password": password})],
                capture_output=True, text=True, timeout=15,
            )

            # Save cookie for next time
            saved_cookie = ""
            if os.path.exists(jar_path):
                with open(jar_path) as jf:
                    saved_cookie = jf.read()
                CFTC_COOKIE_FILE.write_text(json.dumps({"cookie": saved_cookie}))

            # Upload via curl -F
            upload_result = subprocess.run(
                ["curl", "-s", "-b", jar_path, "-c", jar_path,
                 "-X", "POST", f"{cftc_url}/upload",
                 "-F", f"file=@{file_path};filename={file_name}",
                 "-F", "category=",
                 "-F", f"storage_type={storage_type}"],
                capture_output=True, text=True, timeout=120,
            )
            os.unlink(jar_path)

            resp = json.loads(upload_result.stdout)
            if resp.get("status") == 1:
                return resp.get("url", "")
            logger.warning("CFTC: upload failed: %s", resp.get("msg", "unknown"))
            return ""
        except Exception:
            logger.exception("CFTC: upload error")
            return ""

    # ── 抖音解析 ────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_douyin_url(text: str) -> Optional[str]:
        """Extract a Douyin/TikTok share URL from text, or return None."""
        if not text:
            return None
        patterns = [
            r'https?://v\.douyin\.com/\S+',
            r'https?://www\.douyin\.com/video/\d+',
            r'https?://www\.iesdouyin\.com/share/video/\d+',
            r'https?://v\.tiktok\.com/\S+',
            r'https?://www\.tiktok\.com/@[^/]+/video/\d+',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(0).rstrip('/')
        return None

    def _douyin_is_enabled(self, group_id: str) -> bool:
        """Check if Douyin parsing is enabled for this group."""
        with self._acl_lock:
            return self._group_acl(group_id, "").get("douyin_enabled", True)

    def _handle_douyin_toggle_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        """Handle 开启抖音解析 / 关闭抖音解析 commands. Returns True if consumed."""
        s = (text or "").strip().lower()
        compact = s.replace(" ", "")
        if compact in ("开启抖音解析", "关闭抖音解析"):
            cmd = compact
        else:
            parts = s.split()
            if len(parts) >= 2 and parts[1] in ("开启抖音解析", "关闭抖音解析"):
                cmd = parts[1]
            else:
                return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            enable = "开启" in cmd
            group["douyin_enabled"] = enable
            self._save_acl()
        status = "✅ 已开启" if enable else "❌ 已关闭"
        self._itchat.send(f"{status}抖音链接自动解析", toUserName=group_id)
        logger.info("WeChatUOS: douyin parse %s for %s", "enabled" if enable else "disabled", group_name)
        return True

    def _handle_douyin_parse(self, text: str, *, group_id: str, group_name: str, sender: str = "", sender_id: str = "") -> bool:
        """Parse a Douyin/TikTok video or image post. Returns True if consumed."""
        url = self._extract_douyin_url(text)
        if url is None:
            return False
        if not self._douyin_is_enabled(group_id):
            return False
        if self._itchat is None:
            return True
        logger.info("WeChatUOS Douyin: parsing %s in %s", url, group_name)
        try:
            api_url = f"{DOUYIN_API_BASE}/api/hybrid/video_data?url={_urlquote(url, safe='')}"
            req = _URLRequest(api_url, headers={"User-Agent": "Hermes-WeChatUOS/1.0"})
            with _urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"), strict=False)
        except Exception as e:
            logger.exception("WeChatUOS Douyin: API error for %s", url)
            self._itchat.send(f"❌ 抖音解析失败: {e}", toUserName=group_id)
            return True
        code = body.get("api_status_code") or body.get("code")
        if code != 200:
            msg = body.get("detail", {}).get("message", "unknown error") if isinstance(body.get("detail"), dict) else body.get("message", str(body))
            self._itchat.send(f"❌ 抖音解析失败: {msg}", toUserName=group_id)
            return True
        data = body.get("api_body", {}).get("data", body.get("data", {}))
        title = data.get("desc", data.get("title", "无标题"))
        author_info = data.get("author", {})
        author = author_info.get("nickname", "") if isinstance(author_info, dict) else str(author_info)

        # ── Detect image post (aweme_type 2 or 68, has "images" field) ──
        images = data.get("images")
        is_image_post = isinstance(images, list) and len(images) > 0

        if is_image_post:
            # ── Handle image post ──
            self._itchat.send(f"🖼️ {title}（共{len(images)}张）", toUserName=group_id)
            for idx, img_obj in enumerate(images):
                if isinstance(img_obj, dict):
                    img_url = img_obj.get("url_list", [None])[0]
                else:
                    img_url = None
                if not img_url:
                    continue
                try:
                    self._itchat.send(f"⏳ 下载第{idx+1}张图片…", toUserName=group_id)
                    img_req = _URLRequest(img_url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://www.douyin.com/"
                    })
                    with _urlopen(img_req, timeout=60) as img_resp:
                        img_bytes = img_resp.read()
                    tmp_img = f"/tmp/douyin_img_{int(time.time())}_{idx}.jpg"
                    with open(tmp_img, "wb") as f:
                        f.write(img_bytes)
                    # UOS mode: send_file works better than send_image for images
                    r = self._itchat.send_file(tmp_img, toUserName=group_id)
                    import os as _os
                    _os.unlink(tmp_img)
                    if not r:
                        err_msg = r.get("BaseResponse", {}).get("ErrMsg", "未知错误")
                        logger.warning("WeChatUOS Douyin: send_file failed for image #%d: %s", idx, err_msg)
                        self._itchat.send(f"❌ 第{idx+1}张图片发送失败", toUserName=group_id)
                except Exception as e:
                    logger.exception("WeChatUOS Douyin: image download error #%d for %s", idx, url)
                    self._itchat.send(f"❌ 第{idx+1}张图片下载失败: {e}", toUserName=group_id)
            return True

        # ── Handle video post (existing logic) ──
        # Try multiple paths to get video URL
        video_url = data.get("video_url", "")
        if not video_url:
            play_list = data.get("video_data", [])
            if play_list:
                video_url = play_list[0].get("play_addr", "")
        if not video_url:
            video_url = data.get("nwm_video_url", "")
        if not video_url:
            video_obj = data.get("video", {})
            if isinstance(video_obj, dict):
                play_addr = video_obj.get("play_addr", {})
                if isinstance(play_addr, dict):
                    url_list = play_addr.get("url_list", [])
                    if url_list:
                        video_url = url_list[0]
        if not video_url:
            video_obj = data.get("video", {})
            if isinstance(video_obj, dict):
                download_addr = video_obj.get("download_addr", {})
                if isinstance(download_addr, dict):
                    url_list = download_addr.get("url_list", [])
                    if url_list:
                        video_url = url_list[0]
        if not video_url:
            self._itchat.send(f"❌ 解析成功但未找到视频链接\n标题: {title}", toUserName=group_id)
            return True
        try:
            logger.info("WeChatUOS Douyin: downloading %s", title[:30])
            self._itchat.send(f"⏳ 下载中… {title[:30]}", toUserName=group_id)

            # Try H.265 1440p first (higher quality / smaller size)
            video_obj = data.get("video", {})
            h265 = video_obj.get("play_addr_265", {})
            h265_url = h265.get("url_list", [None])[0] if isinstance(h265.get("url_list"), list) else None

            if h265_url and h265.get("height", 0) >= 720:
                dl_target = h265_url
                is_hevc = True
                logger.info("WeChatUOS Douyin: using H.265 %dp source", h265.get("height", 0))
            else:
                dl_target = f"{DOUYIN_API_BASE}/api/download?url={_urlquote(url, safe='')}"
                is_hevc = False

            dl_req = _URLRequest(dl_target, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/"
            } if is_hevc else {"User-Agent": "Hermes-WeChatUOS/1.0"})
            with _urlopen(dl_req, timeout=120) as dl_resp:
                data_bytes = dl_resp.read()

            tmp_raw = f"/tmp/douyin_raw_{int(time.time())}.mp4"
            tmp_out = f"/tmp/douyin_out_{int(time.time())}.mp4"
            with open(tmp_raw, "wb") as f:
                f.write(data_bytes)

            import subprocess as _sp
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", tmp_raw,
                "-c:v", "libx264", "-preset", "fast",
                "-crf", "18", "-maxrate", "10M", "-bufsize", "20M",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                tmp_out
            ]
            _sp.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)
            import os as _os
            _os.unlink(tmp_raw)

            self._itchat.send_video(tmp_out, toUserName=group_id)
            _os.unlink(tmp_out)
            logger.info("WeChatUOS Douyin: sent video '%s' (%d KB)", title[:20], len(data_bytes) // 1024)
        except Exception as e:
            logger.exception("WeChatUOS Douyin: download/send error for %s", url)
            lines = [f"🎬 {title}"]
            if author:
                lines.append(f"👤 {author}")
            lines.append(f"🔗 {video_url}")
            lines.append("⚠️ 视频直链可能被防刷，点开后复制链接到浏览器打开")
            self._itchat.send("\n".join(lines), toUserName=group_id)
        return True

    # ── 帮助 ────────────────────────────────────────────────────────────────

    def _handle_help_command(self, text: str, *, group_id: str, group_name: str, sender: str = "", sender_id: str = "") -> bool:
        """Handle 帮助 command. Shows feature list. Returns True if consumed."""
        s = (text or "").strip().lower().replace(" ", "")
        if s not in {"帮助", "help", "功能", "命令"}:
            parts = s.split()
            if len(parts) >= 2 and parts[1] in {"帮助", "help", "功能", "命令"}:
                pass
            else:
                return False
        if self._itchat is None:
            return True
        msg = (
            "🤖 发哥机器人使用帮助\n\n"
            "📋 基本命令（@机器人 + 命令）\n\n"
            "🔐 授权管理（管理员）\n"
            "  开启授权 / 关闭授权\n"
            "  授权 昵称 / 取消授权 昵称\n"
            "  设管理员 昵称 / 取消管理员 昵称\n"
            "  权限列表 / 名单\n\n"
            "🔍 盘搜\n"
            "  搜索 <关键词>\n"
            "  开启盘搜 / 关闭盘搜\n\n"
            "🎵 抖音解析\n"
            "  直接发送抖音/TikTok链接自动解析\n"
            "  开启抖音解析 / 关闭抖音解析\n\n"
            "📰 公众号推送\n"
            "  订阅 公众号名 / 取消订阅 公众号名\n"
            "  订阅列表 / 查看订阅\n"
            "  开启推文 / 关闭推文\n\n"
            "📤 TG转发\n"
            "  开启转发 / 关闭转发\n\n"
            "📎 图床上传\n"
            "  开启上传 / 关闭上传\n"
            "  上传\n\n"
            "🔄 模块更新\n"
            "  开启更新 / 关闭更新\n\n"
            "💬 直接发消息与 AI 对话"
        )
        self._itchat.send(msg, toUserName=group_id)
        return True

    def _handle_cftc_toggle_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        """Handle 开启上传 / 关闭上传 commands. Returns True if consumed."""
        s = (text or "").strip().lower()
        # Exact match after removing all spaces
        compact = s.replace(" ", "")
        if compact in {"开启上传", "关闭上传"}:
            cmd = compact
        else:
            # Check if command is second word (bot name prefix stripped imperfectly)
            parts = s.split()
            if len(parts) >= 2 and parts[1] in {"开启上传", "关闭上传"}:
                cmd = parts[1]
            else:
                return False
        if self._itchat is None: return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._itchat.send("当前群尚未授权。请发送：@机器人 开启授权 或 授权此群聊", toUserName=group_id); return True
            if not self._is_group_admin(group, sender, sender_id):
                self._itchat.send("你没有权限管理本群机器人。", toUserName=group_id); return True
            enabled = cmd == "开启上传"
            group["cftc_enabled"] = enabled; group["updated_at"] = int(time.time()); self._save_acl()
        self._itchat.send(f"✅ 图床上传已{'开启' if enabled else '关闭'}", toUserName=group_id)
        logger.info("CFTC: %s for group %s by %s/%s", "enabled" if enabled else "disabled", group_name, sender, sender_id)
        return True

    def _handle_cftc_upload_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        s = text.strip().lower().replace(" ", "")
        # Check exact match or second word (bot name prefix stripped imperfectly)
        if s == "上传" or s.startswith("上传"):
            cmd_word = "上传"
        else:
            parts = text.strip().lower().split()
            if len(parts) >= 2 and parts[1] == "上传":
                cmd_word = "上传"
            else:
                return False
        if self._itchat is None: return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            if not group.get("authorized"):
                self._itchat.send("当前群尚未授权。请发送：@机器人 开启授权 或 授权此群聊", toUserName=group_id); return True
            if not group.get("cftc_enabled", True):
                self._itchat.send("本群图床上传已关闭。", toUserName=group_id); return True
            if not self._can_use_group(group_id, group_name, sender, sender_id): return True
        parts = text.strip().split()
        storage_type = parts[-1].strip().lower() if len(parts) >= 2 and parts[-1].strip().lower() in ("telegram", "r2") else "telegram"
        media = self._cftc_find_latest(group_id)
        if not media:
            self._itchat.send("没有找到可上传的媒体文件。请先发送图片/文件到群聊。", toUserName=group_id); return True
        fp = media["path"]
        if not fp.exists():
            self._itchat.send("媒体文件已过期，请重新发送。", toUserName=group_id); return True
        self._itchat.send(f"⏫ 正在上传 {media['name']} 到 CFTC...", toUserName=group_id)
        url = self._cftc_upload_file(fp, media["name"], storage_type)
        self._itchat.send(f"✅ 上传成功：{url}" if url else "上传失败，请重试。", toUserName=group_id)
        return True
        return False

    # ═══════════════════════════════════════════════════════════════════════
    #  LSPosed Module Tracker Methods
    # ═══════════════════════════════════════════════════════════════════════

    def _start_lsposed_tracker(self) -> None:
        if self._lsposed_thread and self._lsposed_thread.is_alive(): return
        self._lsposed_stop.clear()
        self._lsposed_thread = threading.Thread(target=self._lsposed_poll_loop, name="wechat-uos-lsposed", daemon=True)
        self._lsposed_thread.start()
        logger.info("WeChatUOS LSPosed: tracker started")

    def _lsposed_poll_loop(self) -> None:
        logger.info("WeChatUOS LSPosed: poll loop started")
        first_run = True
        while not self._lsposed_stop.is_set():
            cfg = None
            try:
                cfg = self._lsposed_load_config()
                if cfg.get("enabled", False): self._lsposed_run_once(cfg, first_run)
                first_run = False
            except Exception: logger.exception("WeChatUOS LSPosed: poll error")
            interval = (cfg or {}).get("interval_seconds", 1800)
            for _ in range(interval // 5):
                if self._lsposed_stop.is_set(): return
                time.sleep(5)

    def _lsposed_load_config(self) -> dict:
        default = {"enabled": True, "target_groups": [], "interval_seconds": 1800, "max_updates_per_tick": 10,
            "modules_url": "https://modules.lsposed.org/modules.json", "custom_repos": [], "web_sources": [],
            "pkl_path": str(PKL_FILE), "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"}
        try:
            if LSPOSED_CONFIG.exists(): data = json.loads(LSPOSED_CONFIG.read_text())
            else: data = {}
            for k, v in default.items(): data.setdefault(k, v)
            return data
        except Exception: logger.exception("LSPosed: config load error")
        LSPOSED_DIR.mkdir(parents=True, exist_ok=True)
        LSPOSED_CONFIG.write_text(json.dumps(default, ensure_ascii=False, indent=2))
        return dict(default)

    def _lsposed_save_state(self, state: dict) -> None:
        LSPOSED_DIR.mkdir(parents=True, exist_ok=True); LSPOSED_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def _lsposed_load_state(self) -> dict:
        try:
            if LSPOSED_STATE_FILE.exists(): return json.loads(LSPOSED_STATE_FILE.read_text())
        except Exception: pass
        return {"modules": {}, "custom_repos": {}, "web_sources": {}}

    def _lsposed_run_once(self, cfg: dict, first_run: bool) -> int:
        state = self._lsposed_load_state(); updates = []; max_up = cfg.get("max_updates_per_tick", 10)
        modules = self._lsposed_fetch_modules(cfg)
        if modules:
            for mod in modules:
                if len(updates) >= max_up: break
                mname = mod.get("name", "") or mod.get("moduleName", "")
                if not mname: continue
                # Extract version from latestRelease.tagName (GitHub GraphQL structure)
                lr = mod.get("latestRelease") or {}
                ver = lr.get("tagName", "") or mod.get("version", "") or mod.get("versionName", "") or ""
                desc = mod.get("description", "")
                old_ver = state.get("modules", {}).get(mname, "")
                if ver and ver != old_ver: updates.append({"type": "module", "name": mname, "description": desc, "version": ver, "old_version": old_ver, "mod": mod})
            state.setdefault("modules", {}).update({u["name"]: u["version"] for u in updates})
        import urllib.request
        for repo in cfg.get("custom_repos", []):
            if len(updates) >= max_up: break
            owner, repo_name, label = repo.get("owner", ""), repo.get("repo", ""), repo.get("name", f"{repo.get('owner','?')}/{repo.get('repo','?')}")
            if not owner or not repo_name: continue
            try:
                req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo_name}/releases/latest", headers={"User-Agent": cfg.get("user_agent"), "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=15) as resp: release = json.loads(resp.read())
                tag = release.get("tag_name", "")
                old_tag = state.get("custom_repos", {}).get(f"{owner}/{repo_name}", "")
                if tag and tag != old_tag:
                    asset = (release.get("assets") or [{}])[0]
                    updates.append({"type": "custom", "name": label, "version": tag, "old_version": old_tag, "url": asset.get("browser_download_url", ""), "body": (release.get("body") or "")[:500]})
                state.setdefault("custom_repos", {})[f"{owner}/{repo_name}"] = tag
            except Exception: logger.debug("LSPosed: GitHub fetch failed for %s/%s", owner, repo_name)
        state["last_polled_at"] = int(time.time())
        self._lsposed_save_state(state)
        if first_run:
            logger.info("LSPosed: baseline %d modules, %d custom repos", len(state.get("modules", {})), len(state.get("custom_repos", {})))
            return 0
        if not updates: return 0
        groups = cfg.get("target_groups", [])
        # Load ACL once for permission checks
        with self._acl_lock:
            acl_groups = json.loads(ACL_FILE.read_text()).get("groups", {}) if ACL_FILE.exists() else {}
        for up in updates[:max_up]:
            msg = self._lsposed_format_update(up)
            sent = False
            for gid in groups:
                if not self._itchat:
                    break
                group_info = acl_groups.get(gid, {})
                if not group_info.get("authorized") or not group_info.get("lsposed_enabled"):
                    continue
                try:
                    self._itchat.send(msg, toUserName=gid)
                    time.sleep(0.5)
                    sent = True
                except Exception:
                    logger.warning("LSPosed: send to %s failed", gid)
            if sent:
                logger.info("LSPosed: pushed update for %s -> %s", up["name"], up["version"])
        return len(updates)

    def _lsposed_fetch_modules(self, cfg: dict) -> list:
        """Fetch Xposed modules via GitHub GraphQL API. Falls back to local cache."""
        token = cfg.get("github_token", "")
        org = cfg.get("org", "Xposed-Modules-Repo")
        if token:
            try:
                modules = self._lsposed_fetch_via_github(org, token, cfg)
                if modules:
                    # Cache to local file
                    output = cfg.get("output_file", str(LSPOSED_DIR / "modules.json"))
                    LSPOSED_DIR.mkdir(parents=True, exist_ok=True)
                    with open(output, "w") as f: json.dump(modules, f, ensure_ascii=False)
                    logger.info("LSPosed: cached %d modules from GitHub", len(modules))
                    return modules
            except Exception as e:
                logger.debug("LSPosed: GitHub API failed: %s", e)
        # Fallback: local cache
        cache_path = cfg.get("output_file", str(LSPOSED_DIR / "modules.json"))
        try:
            if os.path.exists(cache_path):
                with open(cache_path) as f: data = json.load(f)
                if isinstance(data, list): return data
                if isinstance(data, dict): return data.get("modules", data.get("data", []))
        except Exception: logger.debug("LSPosed: cache fallback failed for %s", cache_path)
        return []

    def _lsposed_fetch_via_github(self, org: str, token: str, cfg: dict) -> list:
        """Fetch all repos (modules) from a GitHub org via GraphQL API, paginated."""
        import ssl
        results = []
        cursor = None
        has_next = True
        ua = cfg.get("user_agent", "Hermes/1.0")
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": ua,
            "Content-Type": "application/json",
        }
        # Explicitly create an unverified SSL context so we don't fail on edge cases
        ctx = ssl.create_default_context()

        while has_next:
            after = f'"{cursor}"' if cursor else "null"
            query = f"""
            query {{
              organization(login: "{org}") {{
                repositories(first: 100, after: {after}, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
                  pageInfo {{ hasNextPage endCursor }}
                  nodes {{
                    name
                    description
                    homepageUrl
                    url
                    stargazerCount
                    createdAt
                    updatedAt
                    isArchived
                    repositoryTopics(first: 5) {{ nodes {{ topic {{ name }} }} }}
                    latestRelease {{
                      tagName
                      isPrerelease
                      isDraft
                      name
                      description
                      createdAt
                      publishedAt
                      releaseAssets(first: 5) {{
                        nodes {{ name contentType downloadCount size downloadUrl }}
                      }}
                    }}
                    latestBetaRelease: latestRelease {{
                      tagName
                    }}
                    latestSnapshotRelease: latestRelease {{
                      tagName
                    }}
                  }}
                }}
              }}
            }}
            """
            req = urllib.request.Request(
                "https://api.github.com/graphql",
                data=json.dumps({"query": query}).encode(),
                headers=headers,
            )
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                body = json.loads(resp.read())
            if "errors" in body:
                logger.warning("LSPosed GraphQL errors: %s", body["errors"])
                break
            org_data = body.get("data", {}).get("organization", {})
            repos = org_data.get("repositories", {})
            page_info = repos.get("pageInfo", {})
            has_next = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")
            for node in repos.get("nodes", []):
                if node.get("isArchived") or node.get("name", "").startswith("."):
                    continue
                lr = node.pop("latestRelease", None) or {}
                # Normalize to match expected format
                entry = {
                    "name": node.get("name", ""),
                    "description": node.get("description", ""),
                    "url": node.get("url", ""),
                    "homepageUrl": node.get("homepageUrl", ""),
                    "stargazerCount": node.get("stargazerCount", 0),
                    "updatedAt": node.get("updatedAt", ""),
                    "createdAt": node.get("createdAt", ""),
                    "latestRelease": lr,
                    "latestBetaRelease": None,
                    "latestSnapshotRelease": None,
                    "latestReleaseTime": lr.get("publishedAt", lr.get("createdAt", "")),
                    "latestBetaReleaseTime": "1970-01-01T00:00:00Z",
                    "latestSnapshotReleaseTime": "1970-01-01T00:00:00Z",
                }
                # Check topics for isModule flag
                topics = node.get("repositoryTopics", {}).get("nodes", [])
                entry["isModule"] = any(t.get("topic", {}).get("name") == "xposed-module" for t in topics)
                results.append(entry)
            logger.info("LSPosed: fetched page (%d repos so far, hasNext=%s)", len(results), has_next)
        return results

    def _lsposed_format_update(self, up: dict) -> str:
        t = up.get("type", "unknown")
        name = up.get("name", "?")
        desc = up.get("description", "")
        ver = up.get("version", "?")
        old_ver = up.get("old_version", "")
        # 第一行：名称 + 描述（如描述存在）
        if desc:
            line1 = f"📦 {name} - {desc}"
        else:
            line1 = f"📦 {name}"
        lines = [line1, f"版本：{ver}"]
        if old_ver:
            lines.append(f"旧版本：{old_ver}")
        # 下载链接：module 和 custom 类型都支持
        dl_url = ""
        if t == "custom":
            dl_url = up.get("url", "")
        elif t == "module":
            mod = up.get("mod", {})
            lr = mod.get("latestRelease") or {}
            assets = lr.get("releaseAssets", {}).get("nodes", [])
            if assets:
                dl_url = assets[0].get("downloadUrl", "")
        if dl_url:
            lines.append(f"链接：{dl_url}")
        if t == "custom":
            body = up.get("body", "")
            if body:
                lines.append(f"说明：{body[:200]}")
        lines.append("")
        return "\n".join(lines)

    def _handle_lsposed_text_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        s = text.strip().lower().replace(" ", "")
        if s in {"开启更新", "关闭更新", "模块更新状态"}:
            cmd = s
        else:
            parts = text.strip().lower().split()
            if len(parts) >= 2 and parts[1] in {"开启更新", "关闭更新", "模块更新状态"}:
                cmd = parts[1]
            else:
                return False
        if self._itchat is None: return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"): self._itchat.send("当前群尚未授权。", toUserName=group_id); return True
            if not self._is_group_admin(group, sender, sender_id): self._itchat.send("你没有权限。", toUserName=group_id); return True
        if cmd == "开启更新":
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                group["lsposed_enabled"] = True
                group["updated_at"] = int(time.time())
                self._save_acl()
            self._itchat.send("✅ 模块更新已开启", toUserName=group_id)
            logger.info("LSPosed: enabled for %s by %s/%s", group_name, sender, sender_id)
            return True
        if cmd == "关闭更新":
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                group["lsposed_enabled"] = False
                group["updated_at"] = int(time.time())
                self._save_acl()
            self._itchat.send("✅ 模块更新已关闭", toUserName=group_id)
            logger.info("LSPosed: disabled for %s by %s/%s", group_name, sender, sender_id)
            return True
        if cmd == "模块更新状态":
            cfg = self._lsposed_load_config()
            state = self._lsposed_load_state()
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                enabled = "🟢 已开启" if group.get("lsposed_enabled") else "🔴 已关闭"
            self._itchat.send(f"模块更新：{enabled}\\n已跟踪：{len(state.get('modules',{}))} 个模块\\n自定义仓库：{len(cfg.get('custom_repos',[]))} 个", toUserName=group_id)
            return True
        return False

    def _handle_werss_text_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        raw = text.strip()
        lowered = raw.lower()
        s = lowered.replace(" ", "")

        # ── Whitelist commands: 订阅 / 取消订阅 / 订阅列表 ─────────────────
        # All are space-tolerant and work with or without bot-name prefix.
        # Admin-only (same gate as 开启推文/关闭推文).
        if s in {"订阅列表", "查看订阅"}:
            cmd = "订阅列表"
        elif s.startswith("订阅") and len(s) > 2:
            # "订阅 机器之心" or "订阅机器之心" or "订阅 机器之心,量子位"
            arg = raw[raw.find("订") + 1:].lstrip(" ,，").strip()
            cmd = ("订阅", arg)
        elif s.startswith("取消订阅") and len(s) > 4:
            arg = raw[raw.find("消") + 4:].lstrip(" ,，").strip()
            cmd = ("取消订阅", arg)
        elif s in {"开启推文", "关闭推文"}:
            cmd = s
        else:
            parts = lowered.split()
            # Bot-name prefix: "dream 订阅列表" → parts[1]="订阅列表"
            if len(parts) >= 2:
                part1 = parts[1]
                if part1 in {"订阅列表", "查看订阅"}:
                    cmd = "订阅列表"
                elif part1 == "订阅":
                    # "dream 订阅 机器之心" → arg from parts[2:]
                    arg = raw.split(maxsplit=2)[2] if len(parts) >= 3 else ""
                    cmd = ("订阅", arg.strip())
                elif part1 == "取消订阅":
                    arg = raw.split(maxsplit=2)[2] if len(parts) >= 3 else ""
                    cmd = ("取消订阅", arg.strip())
                elif part1 in {"开启推文", "关闭推文"}:
                    cmd = part1
                else:
                    return False
            else:
                return False

        if self._itchat is None: return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._itchat.send("当前群尚未授权。", toUserName=group_id); return True
            if not self._is_group_admin(group, sender, sender_id):
                self._itchat.send("你没有权限。", toUserName=group_id); return True

        # ── Whitelist command dispatch ─────────────────────────────────
        if cmd == "订阅列表":
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                allowed = list(group.get("allowed_mps") or [])
            if not allowed:
                self._itchat.send(
                    "📚 本群未设置白名单\n（当前订阅所有公众号推文）",
                    toUserName=group_id,
                )
            else:
                lines = ["📚 本群订阅的公众号："]
                for name in allowed:
                    lines.append(f"• {name}")
                lines.append("\n发送「取消订阅 公众号名」可移除")
                self._itchat.send("\n".join(lines), toUserName=group_id)
            return True

        if isinstance(cmd, tuple) and cmd[0] == "订阅":
            mp_arg = cmd[1]
            if not mp_arg:
                self._itchat.send(
                    "用法：订阅 公众号名\n例如：订阅 机器之心\n（多个用「,」或「，」分隔）",
                    toUserName=group_id,
                )
                return True
            new_names = [n.strip() for n in mp_arg.replace("，", ",").split(",") if n.strip()]
            if not new_names:
                self._itchat.send("公众号名为空。", toUserName=group_id); return True
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                allowed = list(group.get("allowed_mps") or [])
                since = dict(group.get("allowed_mps_since") or {})
                now = int(time.time())
                added = [n for n in new_names if n not in allowed]
                for n in added:
                    since[n] = now
                allowed.extend(added)
                group["allowed_mps"] = allowed
                group["allowed_mps_since"] = since
                group["updated_at"] = now
                self._save_acl()
            if added:
                self._itchat.send(
                    f"✅ 已订阅：{', '.join(added)}\n发送「订阅列表」查看",
                    toUserName=group_id,
                )
            else:
                self._itchat.send("这些公众号已在订阅列表中。", toUserName=group_id)
            logger.info("WeRSS: subscribed %s for %s by %s", added, group_name, sender)
            return True

        if isinstance(cmd, tuple) and cmd[0] == "取消订阅":
            mp_arg = cmd[1]
            if not mp_arg:
                self._itchat.send(
                    "用法：取消订阅 公众号名\n例如：取消订阅 机器之心",
                    toUserName=group_id,
                )
                return True
            targets = [n.strip() for n in mp_arg.replace("，", ",").split(",") if n.strip()]
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                allowed = list(group.get("allowed_mps") or [])
                if not allowed:
                    self._itchat.send(
                        "本群未设置白名单（订阅所有公众号），无需取消。",
                        toUserName=group_id,
                    )
                    return True
                removed = [n for n in targets if n in allowed]
                group["allowed_mps"] = [n for n in allowed if n not in targets]
                # If whitelist becomes empty, drop the key entirely so the group
                # falls back to the "subscribe all" default.
                if not group["allowed_mps"]:
                    group.pop("allowed_mps", None)
                group["updated_at"] = int(time.time())
                self._save_acl()
            if removed:
                self._itchat.send(f"✅ 已取消订阅：{', '.join(removed)}", toUserName=group_id)
            else:
                self._itchat.send("订阅列表中没有这些公众号。", toUserName=group_id)
            logger.info("WeRSS: unsubscribed %s for %s by %s", removed, group_name, sender)
            return True

        # ── Existing on/off commands ──────────────────────────────────
        if cmd == "开启推文":
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                group["werss_enabled"] = True
                group["updated_at"] = int(time.time())
                self._save_acl()
            self._itchat.send("✅ 公众号推文推送已开启", toUserName=group_id)
            logger.info("WeRSS: enabled for %s by %s/%s", group_name, sender, sender_id)
            return True
        if cmd == "关闭推文":
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                group["werss_enabled"] = False
                group["updated_at"] = int(time.time())
                self._save_acl()
            self._itchat.send("✅ 公众号推文推送已关闭", toUserName=group_id)
            logger.info("WeRSS: disabled for %s by %s/%s", group_name, sender, sender_id)
            return True
        return False

    def _start_werss_poller(self) -> None:
        if self._werss_thread and self._werss_thread.is_alive(): return
        self._werss_stop.clear()
        self._werss_thread = threading.Thread(target=self._werss_poll_loop, name="wechat-uos-werss", daemon=True)
        self._werss_thread.start()
        logger.info("WeChatUOS WeRSS: poller started")

    def _werss_login(self) -> Optional[str]:
        try:
            data = _urlopen(
                _URLRequest(
                    f"{WERSS_BASE}/auth/login",
                    data=_urlencode({"username": WERSS_USER, "password": WERSS_PASS}).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ),
                timeout=10,
            ).read()
            resp = json.loads(data)
            return resp.get("data", {}).get("access_token")
        except Exception:
            logger.exception("WeChatUOS WeRSS: login failed")
            return None

    def _werss_fetch_articles(self, token: str) -> List[Dict[str, Any]]:
        try:
            # Paginate to fetch all articles (WeRSS API max limit=100)
            all_articles: List[Dict[str, Any]] = []
            offset = 0
            limit = 100
            while True:
                data = _urlopen(
                    _URLRequest(
                        f"{WERSS_BASE}/articles?offset={offset}&limit={limit}",
                        headers={"Authorization": f"Bearer {token}"},
                    ),
                    timeout=15,
                ).read()
                resp = json.loads(data)
                page = resp.get("data", {}).get("list", [])
                all_articles.extend(page)
                # Stop if we got fewer than 'limit' articles (last page)
                if len(page) < limit:
                    break
                offset += limit
                # Safety cap: stop after 5 pages (500 articles)
                if offset >= 500:
                    logger.warning("WeChatUOS WeRSS: fetched 500+ articles, stopping pagination")
                    break
            return all_articles
        except Exception:
            logger.exception("WeChatUOS WeRSS: fetch articles failed")
            return []

    def _werss_poll_loop(self) -> None:
        logger.info("WeChatUOS WeRSS: poll loop started")
        WERSS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        last_creates: set = set()
        if WERSS_STATE_FILE.exists():
            try:
                st = json.loads(WERSS_STATE_FILE.read_text())
                last_creates = set(st.get("seen_ids", []))
            except Exception:
                pass
        logger.info("WeChatUOS WeRSS: seen_ids count=%d", len(last_creates))
        while not self._werss_stop.is_set():
            try:
                logger.info("WeChatUOS WeRSS: poll iteration starting")
                token = self._werss_login()
                logger.info("WeChatUOS WeRSS: login result=%s", "OK" if token else "NONE")
                if not token:
                    logger.info("WeChatUOS WeRSS: login failed, sleeping 60s")
                    time.sleep(60)
                    continue
                articles = self._werss_fetch_articles(token)
                # Fix articles with status=1000 in WeRSS DB: set them to 1 (published)
                # so Hermes API can fetch them and push to groups
                self._werss_fix_deleted_status()
                new_articles = [a for a in articles if a.get("id") not in last_creates]
                if new_articles:
                    fresh_ids = set()
                    # 集客之家: batch newest 3
                    jk_articles = [a for a in new_articles if a.get("mp_name", "") == "集客之家"]
                    other_articles = [a for a in new_articles if a.get("mp_name", "") != "集客之家"]
                    # Other accounts: only push the newest article per account per poll
                    from collections import defaultdict
                    by_mp: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                    for a in other_articles:
                        by_mp[a.get("mp_name", "未知公众号")].append(a)
                    for mp_name, mp_arts in by_mp.items():
                        mp_arts.sort(key=lambda a: a.get("publish_time", 0) or a.get("id", ""), reverse=True)
                        a = mp_arts[0]
                        fresh_ids.add(a.get("id", ""))
                        self._werss_push_article(a)
                    # 集客之家: batch newest 3
                    if jk_articles:
                        jk_articles.sort(key=lambda a: a.get("publish_time", 0) or a.get("id", ""), reverse=True)
                        batch = jk_articles[:3]
                        for a in batch:
                            fresh_ids.add(a.get("id", ""))
                        self._werss_push_article_batch(batch)
                    # Mark ALL fetched articles as seen to avoid re-push
                    for a in new_articles:
                        fresh_ids.add(a.get("id", ""))
                    last_creates |= fresh_ids
                    # Prune to last 200 IDs
                    if len(last_creates) > 200:
                        last_creates = set(sorted(last_creates, reverse=True)[:200])
                    try:
                        WERSS_STATE_FILE.write_text(json.dumps({
                            "seen_ids": sorted(last_creates),
                            "updated_at": int(time.time()),
                        }, ensure_ascii=False))
                    except Exception:
                        pass
            except Exception:
                logger.exception("WeChatUOS WeRSS: poll loop error")
            self._werss_stop.wait(WERSS_POLL_INTERVAL)
        logger.info("WeChatUOS WeRSS: poll loop stopped")

    def _werss_fix_deleted_status(self) -> None:
        """Fix WeRSS articles with status=1000 (deleted) → 1 (published) so API returns them."""
        try:
            import sqlite3
            db_path = Path("/vol1/1000/docker/werss/data/we_mp_rss.db")
            if not db_path.exists():
                return
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("UPDATE articles SET status=1 WHERE status=1000")
            updated = cur.rowcount
            conn.commit()
            conn.close()
            if updated:
                logger.info("WeChatUOS WeRSS: fixed %d deleted articles (status 1000→1)", updated)
        except Exception:
            logger.debug("WeChatUOS WeRSS: fix deleted status skipped", exc_info=True)

    def _werss_get_enabled_groups_for_mp(self, mp_name: str, publish_time: int = 0) -> List[str]:
        """Return group IDs that are authorized + werss_enabled + allowed to receive ``mp_name``.

        Per-group ``allowed_mps`` whitelist (in ACL group record):
        - missing or empty list  → group accepts ALL accounts (default, backward-compatible)
        - non-empty list         → group accepts only listed accounts

        ``publish_time``: article publish timestamp. When the group has an
        ``allowed_mps_since`` entry for this account, skip articles published
        before the subscription was made (avoids pushing historical articles).
        """
        enabled: List[str] = []
        with self._acl_lock:
            for gid, g in list(self._acl.get("groups", {}).items()):
                if not (g.get("authorized") and g.get("werss_enabled", True)):
                    continue
                allowed = g.get("allowed_mps") or []
                if allowed and mp_name not in allowed:
                    continue
                # Skip historical articles published before subscription timestamp
                if allowed and publish_time:
                    since = g.get("allowed_mps_since") or {}
                    mp_since = since.get(mp_name, 0)
                    if mp_since and publish_time < mp_since:
                        continue
                enabled.append(gid)
        return enabled

    def _werss_push_article(self, article: Dict[str, Any]) -> None:
        try:
            art_id = article.get("id", "")
            mp_name = article.get("mp_name", "未知公众号")
            title = article.get("title", "无标题")
            url = _to_short_url(article.get("url", ""))
            desc = article.get("description", "")
            pub_time = article.get("publish_time", 0) or article.get("id", 0)
            # Format message
            msg = f"📰 {mp_name}\n{title}"
            if desc:
                msg += f"\n{desc}"
            if url:
                msg += f"\n{url}"
            # Get enabled groups (filtered by allowed_mps + subscription timestamp)
            groups = self._werss_get_enabled_groups_for_mp(mp_name, publish_time=pub_time)
            for gid in groups:
                try:
                    if self._itchat:
                        self._itchat.send(msg, toUserName=gid)
                        logger.info("WeChatUOS WeRSS: pushed %s -> %s to %s", mp_name, title[:50], gid[:16])
                        time.sleep(0.5)  # rate limit
                except Exception:
                    logger.exception("WeChatUOS WeRSS: push to %s failed", gid[:16])
        except Exception:
            logger.exception("WeChatUOS WeRSS: push article failed")

    def _werss_push_article_batch(self, articles: List[Dict[str, Any]]) -> None:
        """Push multiple articles from the same account as a single merged message."""
        if not articles:
            return
        try:
            mp_name = articles[0].get("mp_name", "未知公众号")
            # Use the earliest publish time among batch articles to filter
            earliest_pub = min(
                (a.get("publish_time", 0) or a.get("id", 0) for a in articles),
                default=0
            )
            lines = [f"📰 {mp_name}"]
            for i, art in enumerate(articles, 1):
                title = art.get("title", "无标题")
                url = _to_short_url(art.get("url", ""))
                desc = art.get("description", "")
                lines.append("")
                lines.append(f"─── {i} ───")
                lines.append(title)
                if desc:
                    lines.append(desc)
                if url:
                    lines.append(url)
            msg = "\n".join(lines)
            # Get enabled groups (filtered by allowed_mps + subscription timestamp)
            groups = self._werss_get_enabled_groups_for_mp(mp_name, publish_time=earliest_pub)
            for gid in groups:
                try:
                    if self._itchat:
                        self._itchat.send(msg, toUserName=gid)
                        logger.info("WeChatUOS WeRSS: batch pushed %s (%d articles) to %s", mp_name, len(articles), gid[:16])
                        time.sleep(0.5)
                except Exception:
                    logger.exception("WeChatUOS WeRSS: batch push to %s failed", gid[:16])
        except Exception:
            logger.exception("WeChatUOS WeRSS: batch push failed")

    def _migrate_external_gid(self, old_gid: str, new_gid: str, group_name: str) -> None:
        """Migrate GID in TG forward config and CFTC group state after ACL restoration."""
        try:
            # 1. Migrate TG forward config.json
            if TG_FWD_CONFIG.exists():
                cfg = json.loads(TG_FWD_CONFIG.read_text())
                changed = False
                for rule in cfg.get("forward_rules", []):
                    groups = rule.get("wechat_groups", [])
                    for i, gid in enumerate(groups):
                        if gid == old_gid:
                            groups[i] = new_gid
                            changed = True
                if changed:
                    TG_FWD_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
                    logger.info("WeChatUOS GID migration: updated tg_fwd config %s: %s -> %s", group_name, old_gid[:16], new_gid[:16])

            # 2. Migrate CFTC group state
            cftc_state_file = CFTC_DIR / "group_state.json"
            if cftc_state_file.exists():
                cftc_state = json.loads(cftc_state_file.read_text())
                if old_gid in cftc_state:
                    cftc_state[new_gid] = cftc_state.pop(old_gid)
                    cftc_state_file.write_text(json.dumps(cftc_state, ensure_ascii=False, indent=2))
                    logger.info("WeChatUOS GID migration: updated CFTC state %s: %s -> %s", group_name, old_gid[:16], new_gid[:16])

            # 3. Migrate TG forward group_state.json
            tg_state_file = TG_FWD_DIR / "group_state.json"
            if tg_state_file.exists():
                tg_state = json.loads(tg_state_file.read_text())
                if old_gid in tg_state:
                    tg_state[new_gid] = tg_state.pop(old_gid)
                    tg_state_file.write_text(json.dumps(tg_state, ensure_ascii=False, indent=2))
                    logger.info("WeChatUOS GID migration: updated TG fwd group_state %s: %s -> %s", group_name, old_gid[:16], new_gid[:16])

            # 4. Migrate LSPosed target_groups + sync with ACL
            lsposed_cfg = HERMES_HOME / "data" / "lsposed_tracker" / "config.json"
            if lsposed_cfg.exists():
                lcfg = json.loads(lsposed_cfg.read_text())
                tg = lcfg.get("target_groups", [])
                changed = False
                for i, g in enumerate(tg):
                    if g == old_gid:
                        tg[i] = new_gid
                        changed = True
                # Sync: ensure all authorized + lsposed_enabled groups are in target_groups
                try:
                    acl_groups = json.loads(ACL_FILE.read_text()).get("groups", {}) if ACL_FILE.exists() else {}
                    for agid, ag in acl_groups.items():
                        if ag.get("authorized") and ag.get("lsposed_enabled") and agid not in tg:
                            tg.append(agid)
                            changed = True
                except Exception:
                    logger.debug("WeChatUOS GID migration: LSPosed ACL sync skipped")
                if changed:
                    lcfg["target_groups"] = tg
                    lsposed_cfg.write_text(json.dumps(lcfg, ensure_ascii=False, indent=2))
                    logger.info("WeChatUOS GID migration: updated LSPosed config %s: %s -> %s (synced %d groups)", group_name, old_gid[:16], new_gid[:16], len(tg))

            # 5. Migrate Pansou group_state
            pansou_state = HERMES_HOME / "data" / "pansou" / "group_state.json"
            if pansou_state.exists():
                ps = json.loads(pansou_state.read_text())
                if old_gid in ps:
                    ps[new_gid] = ps.pop(old_gid)
                    pansou_state.write_text(json.dumps(ps, ensure_ascii=False, indent=2))
                    logger.info("WeChatUOS GID migration: updated Pansou state %s: %s -> %s", group_name, old_gid[:16], new_gid[:16])

            # 6. Migrate GID name map
            gid_map_file = STATE_DIR / "gid_name_map.json"
            if gid_map_file.exists():
                gm = json.loads(gid_map_file.read_text())
                changed = False
                if gm.get("by_name", {}).get(group_name) == old_gid:
                    gm["by_name"][group_name] = new_gid
                    changed = True
                if old_gid in gm.get("by_old_gid", {}):
                    gm["by_old_gid"][new_gid] = gm["by_old_gid"].pop(old_gid)
                    changed = True
                if changed:
                    gm["updated_at"] = 9999999999
                    gid_map_file.write_text(json.dumps(gm, ensure_ascii=False, indent=2))
                    logger.info("WeChatUOS GID migration: updated gid_name_map %s: %s -> %s", group_name, old_gid[:16], new_gid[:16])

        except Exception:
            logger.exception("WeChatUOS GID migration: error updating external configs for %s", old_gid)




    def _start_tg_fwd(self) -> None:
        """Register TG channel_post forwarder callback. No-op if config not found or registry unavailable."""
        if not TG_FWD_REGISTRY_AVAILABLE:
            logger.warning("WeChatUOS TG_fwd: telegram channel_post registry unavailable, skipping")
            return
        if not TG_FWD_CONFIG.exists():
            logger.info("WeChatUOS TG_fwd: no config at %s, skipping", TG_FWD_CONFIG)
            return
        TG_FWD_CACHE.mkdir(parents=True, exist_ok=True)
        try:
            register_channel_post_forwarder("wechat_uos", self._tg_fwd_callback)
            logger.info("WeChatUOS TG_fwd: registered channel_post forwarder (no more polling)")
        except Exception:
            logger.exception("WeChatUOS TG_fwd: failed to register forwarder")

    def _tg_fwd_callback(self, message) -> None:
        """Callback invoked by gateway on each channel_post (sync, runs in gateway event loop).

        Receives a PTB Message object (not a raw dict). Converts to dict for
        compatibility with existing media-download / formatting helpers.
        """
        try:
            # Hot-reload config each invocation
            if not TG_FWD_CONFIG.exists():
                return
            cfg = json.loads(TG_FWD_CONFIG.read_text())
            rules = cfg.get("forward_rules", [])
            if not rules:
                return

            # Identify the source channel from the PTB Message
            chat = message.chat
            if not chat:
                return
            channel_username = (chat.username or "").strip()
            channel_id = str(chat.id)
            channel_key = f"@{channel_username}" if channel_username else channel_id

            # Find matching rules
            wechat_groups: list[str] = []
            for rule in rules:
                tg_channel = rule.get("tg_channel", "").strip()
                if not tg_channel:
                    continue
                norm_rule = tg_channel.lstrip("@").lower()
                norm_channel = channel_username.lower()
                if norm_rule and norm_channel and norm_rule == norm_channel:
                    wechat_groups.extend(rule.get("wechat_groups", []))
                elif tg_channel == channel_id:
                    wechat_groups.extend(rule.get("wechat_groups", []))

            if not wechat_groups:
                return

            msg_id = message.message_id
            if not msg_id:
                return

            # Deduplicate by last_msg_id state
            state = _tg_fwd_load_state(channel_key)
            if state.get("last_msg_id") is not None and msg_id <= state.get("last_msg_id", 0):
                return

            # Convert PTB Message to dict for helper compatibility
            msg_dict = message.to_dict()

            # Format text
            formatted = _tg_format(msg_dict)

            # Download media if present
            token = cfg.get("bot_token", "")
            media_path = None
            if token and any(
                k in msg_dict for k in ("photo", "video", "document", "audio", "animation", "voice")
            ):
                media_path = _tg_download_media(token, msg_dict, msg_id, TG_FWD_CACHE)

            logger.info(
                "WeChatUOS TG_fwd: forwarding msg %s from %s (media: %s)",
                msg_id, channel_key, media_path.name if media_path else "none",
            )

            # Send to each configured WeChat group
            itchat = self._itchat
            if itchat is None:
                return

            for wx_group in wechat_groups:
                if not _tg_fwd_is_enabled(wx_group):
                    logger.debug("WeChatUOS TG_fwd: forwarding disabled for %s", wx_group)
                    continue
                # ── Send text ──
                if formatted:
                    try:
                        itchat.send(formatted, toUserName=wx_group)
                    except Exception as e:
                        logger.error("WeChatUOS TG_fwd: text send to %s failed: %s", wx_group, e)
                # ── Send media ──
                if media_path:
                    try:
                        ext = media_path.suffix.lower()
                        if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"):
                            ok = itchat.send_image(str(media_path), toUserName=wx_group)
                            if ok is False or ok is None:
                                ok = itchat.send_file(str(media_path), toUserName=wx_group)
                        else:
                            ok = itchat.send_file(str(media_path), toUserName=wx_group)
                        if ok is False or ok is None:
                            logger.warning(
                                "WeChatUOS TG_fwd: media failed for %s -> %s",
                                media_path.name, wx_group,
                            )
                    except Exception as e:
                        logger.error(
                            "WeChatUOS TG_fwd: media send to %s failed: %s", wx_group, e,
                        )

            # Cleanup media file after forwarding
            if media_path and media_path.exists():
                try:
                    media_path.unlink()
                except Exception:
                    pass

            # Update state
            state["last_msg_id"] = msg_id
            _tg_fwd_save_state(channel_key, state)

            # Periodic media cache cleanup (every ~100 calls)
            try:
                cutoff = time.time() - 3600
                for f in TG_FWD_CACHE.iterdir():
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
            except Exception:
                pass

        except Exception:
            logger.exception("WeChatUOS TG_fwd: callback error")


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


# ── TG Channel → WeChat forwarder (runs inside adapter, shares itchat) ──

TG_FWD_DIR = HERMES_HOME / "data" / "tg_fwd"
TG_FWD_CONFIG = TG_FWD_DIR / "config.json"
TG_FWD_CACHE = TG_FWD_DIR / "media_cache"
TG_FWD_TOKEN_BACKUP = HERMES_HOME / ".tg_fwd_token"  # safe backup, not exposed to tools
MAX_RSS_BYTES = 3 * 1024 * 1024 * 1024  # 3 GB self-preservation


def _tg_api_call(token: str, method: str, params: dict = None, timeout: int = 30) -> dict:
    """Call Telegram Bot API with retry. Returns parsed JSON."""
    qs = _urlencode(params) if params else ""
    url = f"https://api.telegram.org/bot{token}/{method}?{qs}" if qs else f"https://api.telegram.org/bot{token}/{method}"
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            req = _URLRequest(url)
            with _urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            msg = str(e)
            is_tls = "TLS" in msg or "SSL" in msg or "EOF" in msg or "Connection aborted" in msg or "Remote end closed" in msg
            if is_tls:
                logger.debug("TG_fwd API %s TLS error (attempt %d/%d): %s", method, attempt, max_attempts, msg)
            elif "timed out" not in msg:
                logger.warning("TG_fwd API %s exception (attempt %d/%d): %s", method, attempt, max_attempts, msg)
            if attempt < max_attempts:
                time.sleep(2 * attempt)
            else:
                return {"ok": False, "description": msg}
    return {"ok": False, "description": "max attempts reached"}


def _tg_biggest_photo(sizes: list) -> dict:
    return max(sizes, key=lambda x: x.get("file_size", 0) or 0) if sizes else {}


def _tg_resolve_file(msg: dict) -> tuple:
    """Return (file_id, ext) or (None, None)."""
    if "photo" in msg:
        p = _tg_biggest_photo(msg["photo"])
        return p.get("file_id"), "jpg"
    if "video" in msg:
        return msg["video"].get("file_id"), "mp4"
    if "document" in msg:
        d = msg["document"]
        fn = d.get("file_name", "")
        return d.get("file_id"), Path(fn).suffix.lstrip(".") or "bin"
    if "audio" in msg:
        a = msg["audio"]
        fn = a.get("file_name", a.get("title", "audio"))
        return a.get("file_id"), Path(fn).suffix.lstrip(".") or "mp3"
    if "animation" in msg:
        return msg["animation"].get("file_id"), "mp4"
    if "voice" in msg:
        return msg["voice"].get("file_id"), "ogg"
    return None, None


def _tg_format(msg: dict) -> str:
    """Format a TG channel post text for WeChat delivery."""
    text = msg.get("text") or msg.get("caption") or ""
    lines = [l.strip() for l in text.split("\n")]
    text = "\n".join(l for l in lines if l)
    prefix = ""
    if "photo" in msg:
        prefix = "📷"
    elif "video" in msg:
        prefix = "🎬"
    elif "document" in msg:
        d = msg["document"]
        prefix = f"📄 {d.get('file_name', '')}" if d.get("file_name") else "📄"
    elif "audio" in msg:
        prefix = "🎵"
    elif "animation" in msg:
        prefix = "🎞️"
    elif "voice" in msg:
        prefix = "🎤"
    out = prefix
    if text:
        out = f"{prefix}\n{text}" if prefix else text
    for ent in msg.get("entities", []):
        if ent.get("type") == "text_link" and ent.get("url"):
            out += f"\n🔗 {ent['url']}"
    return out[:MAX_MESSAGE_LENGTH].strip() if len(out) > MAX_MESSAGE_LENGTH else out.strip()


def _tg_orig_filename(msg: dict, msg_id: int, ext: str) -> str:
    """Try to recover the original filename from a TG message."""
    if "document" in msg:
        fn = msg["document"].get("file_name", "")
        if fn:
            return fn
    if "video" in msg:
        fn = msg["video"].get("file_name", "")
        if fn:
            return fn
    if "audio" in msg:
        fn = msg["audio"].get("file_name", "")
        if fn:
            return fn
    if "animation" in msg:
        fn = msg["animation"].get("file_name", "")
        if fn:
            return fn
    if "photo" in msg:
        return f"photo_{msg_id}.{ext}"
    if "voice" in msg:
        return f"voice_{msg_id}.{ext}"
    return f"file_{msg_id}.{ext}"


def _tg_download_media(token: str, msg: dict, msg_id: int, cache_dir: Path) -> Optional[Path]:
    """Download a media file from Telegram. Returns local path or None."""
    file_id, ext = _tg_resolve_file(msg)
    if not file_id:
        return None
    result = _tg_api_call(token, "getFile", {"file_id": file_id})
    if not result.get("ok"):
        return None
    tg_path = result["result"].get("file_path", "")
    if not tg_path:
        return None
    fname = _tg_orig_filename(msg, msg_id, ext)
    local_path = cache_dir / fname
    if local_path.exists():
        return local_path
    # Download
    dl_url = f"https://api.telegram.org/file/bot{token}/{tg_path}"
    for attempt in range(1, 4):
        try:
            req = _URLRequest(dl_url)
            with _urlopen(req, timeout=60) as resp:
                local_path.write_bytes(resp.read())
            return local_path
        except Exception as e:
            msg = str(e)
            is_tls = "TLS" in msg or "SSL" in msg or "EOF" in msg or "Connection aborted" in msg or "Remote end closed" in msg
            if is_tls:
                logger.debug("TG_fwd media download TLS error (attempt %d/3): %s", attempt, msg)
            else:
                logger.warning("TG_fwd media download error (attempt %d/3): %s", attempt, msg)
            if attempt < 3:
                time.sleep(2 * attempt)
            else:
                return None
    return None


def _tg_fwd_load_state(tg_channel: str) -> dict:
    sf = TG_FWD_DIR / f"{tg_channel.replace('@', '')}.json"
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    return {}


def _tg_fwd_save_state(tg_channel: str, state: dict) -> None:
    sf = TG_FWD_DIR / f"{tg_channel.replace('@', '')}.json"
    sf.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _tg_fwd_load_gstate() -> dict:
    gs = TG_FWD_DIR / "group_state.json"
    if gs.exists():
        try:
            return json.loads(gs.read_text())
        except Exception:
            pass
    return {}


def _tg_fwd_is_enabled(group_id: str) -> bool:
    return _tg_fwd_load_gstate().get(group_id, {}).get("enabled", True)

