"""
WxPowerBot — 独立 WeChat UOS 多功能机器人主程序
===============================================
合并 itchat-uos + 群授权/盘搜/TG转发/CFTC上传/LSPosed模块更新
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from handlers import CommandHandlers
from tg_forward import TGForwarder
from cftc import CFTCUploader
from lsposed import LSPosedTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("WxPowerBot")


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None: return default
    if isinstance(value, bool): return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _csv(value: Any) -> List[str]:
    if not value: return []
    if isinstance(value, (list, tuple, set)): return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _strip_at_prefix(text: str) -> str:
    if not text: return ""
    s = text.replace("\u2005", " ").strip()
    if s.startswith("@"):
        parts = s.split(maxsplit=1)
        if len(parts) == 2: return parts[1].strip()
    return s


def _add_unique(seq: List[str], value: str) -> None:
    if value and value not in seq: seq.append(value)


class _QRHandler(SimpleHTTPRequestHandler):
    QR_PNG: Optional[Path] = None
    def log_message(self, fmt, *args):
        logger.debug("QR HTTP: " + fmt, *args)
    def do_GET(self):
        qp = self.QR_PNG
        if self.path == "/" and qp and qp.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                f"""<!doctype html><html><head><meta charset='utf-8'>
<meta http-equiv='refresh' content='3'>
<title>WxPowerBot Login</title></head>
<body style='background:#111;color:#ddd;font-family:sans-serif;text-align:center;padding-top:40px'>
<h2>微信扫码登录 WxPowerBot</h2>
<p>页面每 3 秒刷新。扫码后请在手机上点确认。</p>
<img src='/itchat_qr.png?t={int(time.time())}' style='max-width:420px;background:white;padding:12px;border-radius:12px'>
</body></html>""".encode()
            )
            return
        if self.path == "/itchat_qr.png" and qp and qp.exists():
            data = qp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404); self.end_headers()


class WxPowerBot:
    """独立版 WeChat UOS 多功能机器人。"""

    def __init__(self, data_dir: str = "/data"):
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "state"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # 配置文件
        self.config_file = self.state_dir / "config.json"
        self.config: Dict[str, Any] = {}
        self._load_config()

        # 路径
        self.acl_file = self.state_dir / "acl.json"
        self.qr_png = self.state_dir / "itchat_qr.png"
        self.qr_url_txt = self.state_dir / "itchat_qr_url.txt"
        self.pkl_file = self.state_dir / "itchat.pkl"
        self.pansou_state_file = self.state_dir / "pansou_state.json"
        (self.data_dir / "cftc_media").mkdir(exist_ok=True)
        (self.data_dir / "tg_fwd").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "lsposed").mkdir(parents=True, exist_ok=True)

        # itchat
        self._itchat = None
        self._thread: Optional[threading.Thread] = None
        self._qr_server: Optional[ThreadingHTTPServer] = None
        self._qr_thread: Optional[threading.Thread] = None
        self._login_name = ""
        self._login_ts = 0
        self._chat_names: Dict[str, str] = {}
        self._startup_ts = time.time()

        # ACL
        self._acl_lock = threading.RLock()
        self._acl: Dict[str, Any] = self._load_acl()
        self._admin_users = _csv(self.get("admin_users", ""))
        self._allowed_users = _csv(self.get("allowed_users", ""))
        self._allowed_groups = _csv(self.get("allowed_groups", ""))
        self._respond_to_dms = bool(self.get("respond_to_dms", False))
        self._qr_http = bool(self.get("qr_http", True))
        self._qr_port = int(self.get("qr_port", 8646))

        # 子模块
        self.tg_forwarder = TGForwarder(self.data_dir)
        self.cftc = CFTCUploader(self.data_dir, self.state_dir)
        self.lsposed = LSPosedTracker(self.data_dir)

    # ── 配置 ──────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            if self.config_file.exists():
                self.config = json.loads(self.config_file.read_text())
        except Exception:
            self.config = {}

    def get(self, key: str, default: Any = None) -> Any:
        v = os.getenv(f"WXPOWERBOT_{key.upper()}")
        if v is not None: return v
        return self.config.get(key, default)

    def reload_config(self) -> None:
        self._load_config()
        self._admin_users = _csv(self.get("admin_users", ""))
        self._allowed_users = _csv(self.get("allowed_users", ""))
        self._allowed_groups = _csv(self.get("allowed_groups", ""))

    # ── ACL ──────────────────────────────────────────────────────────

    def _load_acl(self) -> Dict[str, Any]:
        try:
            if self.acl_file.exists():
                data = json.loads(self.acl_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("groups", {})
                    return data
        except Exception:
            logger.exception("ACL load failed")
        return {"groups": {}}

    def _save_acl(self) -> None:
        try:
            tmp = self.acl_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._acl, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.acl_file)
        except Exception:
            logger.exception("ACL save failed")

    def _normalize_group_name(self, name: str) -> str:
        return html.unescape(str(name or "")).strip()

    def _find_restorable_group_acl(self, group_id: str, group_name: str) -> Optional[Dict[str, Any]]:
        normalized = self._normalize_group_name(group_name)
        if not normalized: return None
        for eid, ex in self._acl.get("groups", {}).items():
            if eid == group_id or not isinstance(ex, dict) or not ex.get("authorized"):
                continue
            if self._normalize_group_name(ex.get("name", "")) == normalized:
                return ex
        return None

    def _group_acl(self, group_id: str, group_name: str = "") -> Dict[str, Any]:
        groups = self._acl.setdefault("groups", {})
        now = int(time.time())
        g = groups.get(group_id)
        if not isinstance(g, dict):
            restored = self._find_restorable_group_acl(group_id, group_name)
            if isinstance(restored, dict):
                old_id = next((eid for eid, ex in groups.items() if ex is restored), "")
                g = {
                    "name": group_name or restored.get("name") or group_id,
                    "authorized": bool(restored.get("authorized")),
                    "owner_uid": restored.get("owner_uid", ""),
                    "initial_admin_uid": restored.get("initial_admin_uid", ""),
                    "admins": list(restored.get("admins", [])),
                    "allowed_users": list(restored.get("allowed_users", [])),
                    "members_cache": {}, "created_at": restored.get("created_at", now),
                    "updated_at": now, "restored_from_group_id": old_id,
                }
                groups[group_id] = g
                logger.info("ACL restored: %s new=%s old=%s", g.get("name"), group_id[:16], old_id[:16])
                self._migrate_external_gid(old_id, group_id, g.get("name") or group_name)
        if not isinstance(g, dict):
            g = groups.setdefault(group_id, {
                "name": group_name or group_id, "authorized": False, "owner_uid": "",
                "initial_admin_uid": "", "admins": [], "allowed_users": [],
                "members_cache": {}, "created_at": now, "updated_at": now,
            })
        if group_name and g.get("name") != group_name:
            g["name"] = group_name; g["updated_at"] = now
        for k in ("authorized", "owner_uid", "initial_admin_uid", "admins",
                  "allowed_users", "members_cache", "created_at", "updated_at",
                  "restored_from_group_id", "lsposed_enabled", "cftc_enabled",
                  "pansou_enabled"):
            if k not in g:
                g[k] = [] if k in ("admins", "allowed_users") else {} if k == "members_cache" else False if k in ("authorized", "lsposed_enabled", "cftc_enabled", "pansou_enabled") else "" if k in ("owner_uid", "initial_admin_uid", "restored_from_group_id") else now
        if g.get("restored_from_group_id") and g.get("updated_at") == now:
            self._save_acl()
        return g

    def _refresh_group_members(self, group_id: str, group_name: str = "") -> int:
        if self._itchat is None or not group_id: return 0
        with self._acl_lock:
            g = self._group_acl(group_id, group_name)
            room = None
            try:
                updater = getattr(self._itchat, "update_chatroom", None)
                if callable(updater):
                    try: room = updater(group_id, detailedMember=True)
                    except TypeError: room = updater(group_id)
            except Exception: pass
            if not isinstance(room, dict):
                try: room = self._itchat.search_chatrooms(userName=group_id)
                except Exception: room = None
            if not isinstance(room, dict): return 0
            if room.get("NickName"): g["name"] = html.unescape(str(room.get("NickName")))
            for m in room.get("MemberList") or []:
                if not isinstance(m, dict): continue
                uid = str(m.get("UserName") or "")
                if not uid: continue
                cache = g.setdefault("members_cache", {})
                old = cache.get(uid) if isinstance(cache.get(uid), dict) else {}
                cache[uid] = {
                    "nick_name": html.unescape(str(m.get("NickName") or old.get("nick_name") or "")),
                    "display_name": html.unescape(str(m.get("DisplayName") or old.get("display_name") or m.get("NickName") or old.get("display_name") or "")),
                    "remark_name": html.unescape(str(m.get("RemarkName") or old.get("remark_name") or "")),
                }
            if room.get("MemberList"):
                owner = room["MemberList"][0]
                ouid = str(owner.get("UserName") or "")
                if ouid: g["owner_uid"] = ouid; _add_unique(g.setdefault("admins", []), ouid)
            g["updated_at"] = int(time.time()); self._save_acl()
            return len(room.get("MemberList") or [])

    def _find_member_uid(self, group_id: str, group_name: str, target: str):
        target = (target or "").strip()
        if not target: return None, "请提供群内昵称。"
        if target.startswith("@"): return target, None
        self._refresh_group_members(group_id, group_name)
        with self._acl_lock:
            g = self._group_acl(group_id, group_name)
            matches = []
            for uid, meta in g.get("members_cache", {}).items():
                if isinstance(meta, dict):
                    names = {str(meta.get("nick_name") or ""), str(meta.get("display_name") or ""), str(meta.get("remark_name") or "")}
                else: names = {str(meta or "")}
                if target in names: matches.append(uid)
            if not matches: return target, None
            if len(matches) > 1:
                return None, f"找到多个叫“{target}”的成员，请使用更完整的群昵称。匹配：{'、'.join(self._member_display(g, uid) for uid in matches[:5])}"
            return matches[0], None

    def _member_display(self, g: Dict[str, Any], uid: str, fallback: str = "") -> str:
        meta = g.get("members_cache", {}).get(uid)
        if isinstance(meta, dict):
            for k in ("display_name", "remark_name", "nick_name"):
                if meta.get(k): return str(meta[k])
        elif isinstance(meta, str) and meta: return meta
        return fallback or uid

    def _remember_member(self, g: Dict[str, Any], uid: str, nick: str = "", display: str = "", remark: str = "") -> None:
        if not uid: return
        cache = g.setdefault("members_cache", {})
        old = cache.get(uid) if isinstance(cache.get(uid), dict) else {}
        cache[uid] = {"nick_name": html.unescape(str(nick or old.get("nick_name") or "")),
                      "display_name": html.unescape(str(display or old.get("display_name") or nick or old.get("display_name") or "")),
                      "remark_name": html.unescape(str(remark or old.get("remark_name") or ""))}

    def _is_group_admin(self, g: Dict[str, Any], sender: str, sender_id: str) -> bool:
        if sender in self._admin_users or sender_id in self._admin_users: return True
        return sender_id in g.get("admins", [])

    def _can_use_group(self, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        with self._acl_lock:
            g = self._group_acl(group_id, group_name)
            self._remember_member(g, sender_id, nick=sender, display=sender)
            if not g.get("authorized"): self._save_acl(); return False
            allowed = sender_id in g.get("admins", []) or sender_id in g.get("allowed_users", [])
            if not allowed: allowed = self._admin_users and (sender in self._admin_users or sender_id in self._admin_users)
            self._save_acl(); return allowed

    def _format_acl(self, group_id: str, group_name: str) -> str:
        with self._acl_lock:
            g = self._group_acl(group_id, group_name)
            al = [f"- {self._member_display(g, uid)}" for uid in g.get("admins", [])] or ["- 无"]
            al2 = [f"- {self._member_display(g, uid)}" for uid in g.get("allowed_users", [])] or ["- 无"]
            s = "已授权" if g.get("authorized") else "未授权"
            return "\n".join([f"当前群：{g.get('name') or group_name or group_id}", f"状态：{s}", "", "管理员：", *al, "", "已授权用户：", *al2])

    def _is_group_authorize_command(self, text: str) -> bool:
        c = (text or "").strip().lower().replace(" ", "")
        return c in {"授权此群聊", "授权本群", "启用本群", "开启本群", "开启授权", "authorizegroup", "/authorizegroup"}

    def _is_group_deauthorize_command(self, text: str) -> bool:
        c = (text or "").strip().lower().replace(" ", "")
        return c in {"关闭授权", "关闭本群", "禁用本群", "deauthorizegroup", "/deauthorizegroup"}

    # ── 发送 ──────────────────────────────────────────────────────────

    def _send_text(self, chat_id: str, text: str) -> None:
        if self._itchat is None: return
        try: self._itchat.send(str(text), toUserName=chat_id)
        except Exception: logger.exception("send to %s failed", chat_id[:16])

    # ── 命令处理 ──────────────────────────────────────────────────────

    _cmd_handlers = CommandHandlers()

    def _handle_acl_command(self, *a, **kw): return self._cmd_handlers.handle_acl_command(self, *a, **kw)
    def _is_pansou_command(self, *a, **kw): return self._cmd_handlers._is_pansou_command(*a, **kw)
    def _pansou_is_enabled(self, *a, **kw): return self._cmd_handlers._pansou_is_enabled(self, *a, **kw)
    def _handle_pansou_search(self, *a, **kw): return self._cmd_handlers.handle_pansou_search(self, *a, **kw)
    def _handle_pansou_toggle_command(self, *a, **kw): return self._cmd_handlers.handle_pansou_toggle_command(self, *a, **kw)
    def _handle_tg_fwd_toggle_command(self, *a, **kw): return self._cmd_handlers.handle_tg_fwd_toggle_command(self, *a, **kw)
    def _auto_add_tg_fwd_group(self, *a, **kw): return self._cmd_handlers.auto_add_tg_fwd_group(self, *a, **kw)
    def _handle_cftc_toggle_command(self, *a, **kw): return self._cmd_handlers.handle_cftc_toggle_command(self, *a, **kw)
    def _handle_cftc_upload_command(self, *a, **kw): return self._cmd_handlers.handle_cftc_upload_command(self, *a, **kw)
    def _handle_lsposed_text_command(self, *a, **kw): return self._cmd_handlers.handle_lsposed_text_command(self, *a, **kw)
    def _migrate_external_gid(self, *a, **kw): return self._cmd_handlers.migrate_external_gid(self, *a, **kw)

    # ── 消息帮助（未被命令处理的消息） ──────────────────────────────

    def _send_help(self, chat_id: str) -> None:
        self._send_text(chat_id, """🤖 WxPowerBot 已就绪

📋 可用命令：
━━━ 授权 ━━━
开启授权 / 关闭授权
授权 昵称 / 取消授权 昵称
权限列表 / 刷新成员

━━━ 功能开关（仅管理员）━━━
开启盘搜 / 关闭盘搜
开启转发 / 关闭转发
开启上传 / 关闭上传
开启更新 / 关闭更新

━━━ 使用 ━━━
搜索 <关键词> — 盘搜资源
上传 — 上传最新图片/文件到图床""")

    # ── itchat 启动 ──────────────────────────────────────────────────

    def _qr_callback(self, uuid: str, status: str, qrcode: bytes = b"", **kwargs: Any) -> None:
        data = qrcode or kwargs.get("qrcode_data") or b""
        logger.info("QR: uuid=%s status=%s", uuid, status)
        if data: self.qr_png.write_bytes(data)
        if uuid: self.qr_url_txt.write_text(f"https://login.weixin.qq.com/l/{str(uuid).strip()}\n")

    def _login_callback(self) -> None:
        self._login_ts = time.time()
        try:
            user = self._itchat.search_friends() if self._itchat else {}
            if not isinstance(user, dict): user = {}
            self._login_name = html.unescape(str(user.get("NickName") or ""))
            logger.info("登录成功：%s", self._login_name)
        except Exception: logger.info("登录成功")

    def _start_qr_server(self) -> None:
        if not self._qr_http or self._qr_server: return
        try:
            _QRHandler.QR_PNG = self.qr_png
            self._qr_server = ThreadingHTTPServer(("0.0.0.0", self._qr_port), _QRHandler)
            self._qr_thread = threading.Thread(target=self._qr_server.serve_forever, name="qr-http", daemon=True)
            self._qr_thread.start()
            logger.info("QR 服务: http://0.0.0.0:%s", self._qr_port)
        except OSError as e: logger.warning("QR 服务启动失败: %s", e)

    def _run_itchat(self) -> None:
        try:
            import itchat
            from itchat.content import TEXT
            self._itchat = itchat

            @itchat.msg_register(TEXT, isGroupChat=True)
            def group_text_handler(msg):
                if not getattr(msg, "isAt", False): return
                group_id = getattr(msg, "fromUserName", None) or msg.get("FromUserName")
                sender_id = getattr(msg, "actualUserName", None) or msg.get("ActualUserName") or ""
                sender = getattr(msg, "actualNickName", None) or msg.get("ActualNickName") or sender_id
                if self._login_ts > 0:
                    mt = getattr(msg, "createTime", None) or msg.get("CreateTime", 0)
                    if isinstance(mt, (int, float)) and mt < self._login_ts - 2: return
                raw_text = getattr(msg, "text", None) or msg.get("Text") or msg.get("Content") or ""
                text = _strip_at_prefix(raw_text)
                group_name = self._resolve_group_name(group_id)
                if self._allowed_groups and group_id not in self._allowed_groups and group_name not in self._allowed_groups:
                    return

                # 命令链
                if self._handle_acl_command(text, sender=sender, sender_id=sender_id, chat_id=group_id, group_name=group_name): return
                if self._handle_pansou_search(text, group_id, group_name, sender=sender, sender_id=sender_id) is not None: return
                if self._handle_pansou_toggle_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id): return
                tc = text.strip().lower().replace(" ", "")
                if self._handle_tg_fwd_toggle_command(tc, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id): return
                cc = text.strip().lower().replace(" ", "")
                if self._handle_cftc_toggle_command(cc, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id): return
                if self._handle_cftc_upload_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id): return
                if self._handle_lsposed_text_command(text, group_id=group_id, group_name=group_name, sender=sender, sender_id=sender_id): return
                if not self._can_use_group(group_id, group_name, sender, sender_id): return
                # 未识别命令 → 帮助
                if text.strip().lower().replace(" ", "") in ("帮助", "help", "菜单", "功能"):
                    self._send_help(group_id)
                else:
                    self._send_text(group_id, f"你好 @{sender}！发送 @机器人 帮助 查看可用命令。")

            @itchat.msg_register(TEXT, isGroupChat=False)
            def private_text_handler(msg):
                if not self._respond_to_dms: return
                user_id = getattr(msg, "fromUserName", None) or msg.get("FromUserName")
                sender = msg.get("User", {}).get("NickName") if isinstance(msg.get("User"), dict) else ""
                sender = sender or user_id
                if self._admin_users and user_id not in self._admin_users and sender not in self._admin_users: return
                text = getattr(msg, "text", None) or msg.get("Text") or msg.get("Content") or ""
                self._send_text(user_id, f"收到私信：{text[:200]}\n请在群中 @ 我使用。")

            for p in (self.qr_png, self.qr_url_txt):
                try: p.unlink()
                except FileNotFoundError: pass

            itchat.auto_login(hotReload=True, statusStorageDir=str(self.pkl_file),
                              qrCallback=self._qr_callback, loginCallback=self._login_callback,
                              exitCallback=lambda: logger.warning("已退出登录"))
            logger.info("消息监听已启动")
            self.tg_forwarder.start_poll(self._itchat)
            self.lsposed.start(self._itchat, lambda: self._acl.get("groups", {}))
            self.cftc.register_handlers(self)
            itchat.run(blockThread=True)
        except Exception:
            logger.exception("itchat 监听器崩溃")

    def _resolve_group_name(self, group_id: str) -> str:
        if group_id in self._chat_names: return self._chat_names[group_id]
        try:
            room = self._itchat.search_chatrooms(userName=group_id) if self._itchat else None
            if not isinstance(room, dict): room = {}
            name = html.unescape(str(room.get("NickName") or group_id))
        except Exception: name = group_id
        self._chat_names[group_id] = name
        return name

    # ── CFTC 快捷方法 ────────────────────────────────────────────────

    def _cftc_find_latest(self, group_id: str) -> dict:
        return self.cftc.find_latest(group_id)

    def _cftc_upload_file(self, fp, name, storage="telegram"):
        return self.cftc.upload_file(fp, name, {
            "url": self.get("cftc_url", "https://cftc.lliic.com"),
            "username": self.get("cftc_username", ""),
            "password": self.get("cftc_password", ""),
        }, storage)

    # ── 启动/停止 ────────────────────────────────────────────────────

    def start(self) -> None:
        """启动机器人。"""
        logger.info("=" * 40)
        logger.info("WxPowerBot 启动中...")
        logger.info("数据目录: %s", self.data_dir)
        logger.info("=" * 40)
        self._start_qr_server()
        self._thread = threading.Thread(target=self._run_itchat, name="wxpowerbot", daemon=True)
        self._thread.start()
        logger.info("WxPowerBot 已启动")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        logger.info("WxPowerBot 停止中...")
        try:
            if self._itchat is not None: self._itchat.logout()
        except Exception: pass
        if self._qr_server:
            try: self._qr_server.shutdown()
            except Exception: pass
            self._qr_server = None
        logger.info("WxPowerBot 已停止")