"""
WxPowerBot — TG 频道 → 微信群转发
"""
import json
import logging
import time
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode as _urlencode

logger = logging.getLogger("WxPowerBot.tg_fwd")

MAX_MESSAGE_LENGTH = 3500


class TGForwarder:
    """Telegram 频道消息转发到微信群。"""

    def __init__(self, data_dir: Path):
        self.tg_fwd_dir = data_dir / "tg_fwd"
        self.tg_fwd_cache = self.tg_fwd_dir / "media_cache"
        self.tg_fwd_dir.mkdir(parents=True, exist_ok=True)
        self.tg_fwd_cache.mkdir(parents=True, exist_ok=True)

    def _api_call(self, token: str, method: str, params: dict = None, timeout: int = 30) -> dict:
        qs = _urlencode(params) if params else ""
        url = f"https://api.telegram.org/bot{token}/{method}?{qs}" if qs else f"https://api.telegram.org/bot{token}/{method}"
        try:
            import urllib.request
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if "timed out" not in str(e):
                logger.warning("TG API %s: %s", method, e)
            return {"ok": False, "description": str(e)}

    def _biggest_photo(self, sizes: list) -> dict:
        return max(sizes, key=lambda x: x.get("file_size", 0) or 0) if sizes else {}

    def _resolve_file(self, msg: dict):
        if "photo" in msg:
            return self._biggest_photo(msg["photo"]).get("file_id"), "jpg"
        if "video" in msg:
            return msg["video"].get("file_id"), "mp4"
        if "document" in msg:
            d = msg["document"]
            return d.get("file_id"), Path(d.get("file_name", "")).suffix.lstrip(".") or "bin"
        if "audio" in msg:
            a = msg["audio"]
            return a.get("file_id"), Path(a.get("file_name", a.get("title", "audio"))).suffix.lstrip(".") or "mp3"
        if "animation" in msg:
            return msg["animation"].get("file_id"), "mp4"
        if "voice" in msg:
            return msg["voice"].get("file_id"), "ogg"
        return None, None

    def _format(self, msg: dict) -> str:
        text = msg.get("text") or msg.get("caption") or ""
        lines = [l.strip() for l in text.split("\n")]
        text = "\n".join(l for l in lines if l)
        prefix = ""
        if "photo" in msg: prefix = "📷"
        elif "video" in msg: prefix = "🎬"
        elif "document" in msg:
            d = msg["document"]
            prefix = f"📄 {d.get('file_name', '')}" if d.get("file_name") else "📄"
        elif "audio" in msg: prefix = "🎵"
        elif "animation" in msg: prefix = "🎞️"
        elif "voice" in msg: prefix = "🎤"
        out = prefix
        if text:
            out = f"{prefix}\n{text}" if prefix else text
        for ent in msg.get("entities", []):
            if ent.get("type") == "text_link" and ent.get("url"):
                out += f"\n🔗 {ent['url']}"
        return out[:MAX_MESSAGE_LENGTH].strip() if len(out) > MAX_MESSAGE_LENGTH else out.strip()

    def _orig_filename(self, msg: dict, msg_id: int, ext: str) -> str:
        for k in ("document", "video", "audio", "animation"):
            fn = msg.get(k, {}).get("file_name", "")
            if fn:
                return fn
        if "photo" in msg: return f"photo_{msg_id}.{ext}"
        if "voice" in msg: return f"voice_{msg_id}.{ext}"
        return f"file_{msg_id}.{ext}"

    def _download_media(self, token: str, msg: dict, msg_id: int) -> Optional[Path]:
        file_id, ext = self._resolve_file(msg)
        if not file_id:
            return None
        result = self._api_call(token, "getFile", {"file_id": file_id})
        if not result.get("ok"):
            return None
        tg_path = result["result"].get("file_path", "")
        if not tg_path:
            return None
        fname = self._orig_filename(msg, msg_id, ext)
        local_path = self.tg_fwd_cache / fname
        if local_path.exists():
            return local_path
        dl_url = f"https://api.telegram.org/file/bot{token}/{tg_path}"
        try:
            import urllib.request
            req = urllib.request.Request(dl_url)
            with urllib.request.urlopen(req, timeout=60) as resp:
                local_path.write_bytes(resp.read())
            return local_path
        except Exception:
            return None

    def _load_state(self, tg_channel: str) -> dict:
        sf = self.tg_fwd_dir / f"{tg_channel.replace('@', '')}.json"
        if sf.exists():
            try: return json.loads(sf.read_text())
            except Exception: pass
        return {}

    def _save_state(self, tg_channel: str, state: dict) -> None:
        sf = self.tg_fwd_dir / f"{tg_channel.replace('@', '')}.json"
        sf.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def _load_gstate(self) -> dict:
        gs = self.tg_fwd_dir / "group_state.json"
        if gs.exists():
            try: return json.loads(gs.read_text())
            except Exception: pass
        return {}

    def _is_enabled(self, group_id: str) -> bool:
        return self._load_gstate().get(group_id, {}).get("enabled", True)

    def start_poll(self, itchat_instance) -> None:
        """启动 TG 轮询线程。"""
        threading.Thread(
            target=self._poll_loop,
            args=(itchat_instance,),
            name="tg-fwd-poll",
            daemon=True,
        ).start()

    def _poll_loop(self, itchat) -> None:
        last_cleanup = 0.0
        while True:
            try:
                cfg_file = self.tg_fwd_dir / "config.json"
                if not cfg_file.exists():
                    time.sleep(30)
                    continue
                cfg = json.loads(cfg_file.read_text())
                token = cfg.get("bot_token", "")
                if not token:
                    time.sleep(30)
                    continue
                rules = cfg.get("forward_rules", [])
                if not rules:
                    time.sleep(30)
                    continue

                for rule in rules:
                    tg_channel = rule.get("tg_channel", "")
                    wechat_groups = rule.get("wechat_groups", [])
                    if not tg_channel or not wechat_groups:
                        continue
                    state = self._load_state(tg_channel)
                    last_update_id = state.get("last_update_id")
                    params = {"timeout": 30, "limit": 10, "allowed_updates": json.dumps(["channel_post"])}
                    if last_update_id:
                        params["offset"] = last_update_id
                    result = self._api_call(token, "getUpdates", params, timeout=35)
                    if not result.get("ok"):
                        continue
                    updates = result.get("result", [])
                    if not updates:
                        continue
                    for update in updates:
                        update_id = update.get("update_id")
                        msg = update.get("channel_post")
                        if not msg:
                            continue
                        msg_id = msg.get("message_id")
                        if not msg_id:
                            continue
                        if state.get("last_msg_id") is not None and msg_id is not None and msg_id <= state.get("last_msg_id", 0):
                            continue
                        formatted = self._format(msg)
                        has_media = any(k in msg for k in ("photo", "video", "document", "audio", "animation", "voice"))
                        media_path = self._download_media(token, msg, msg_id) if has_media else None
                        for wx_group in wechat_groups:
                            if not self._is_enabled(wx_group):
                                continue
                            if itchat is None:
                                continue
                            if formatted:
                                try: itchat.send(formatted, toUserName=wx_group)
                                except Exception as e: logger.error("send to %s failed: %s", wx_group[:16], e)
                            if media_path:
                                try:
                                    ext = media_path.suffix.lower()
                                    if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"):
                                        ok = itchat.send_image(str(media_path), toUserName=wx_group)
                                        if ok is False or ok is None:
                                            ok = itchat.send_file(str(media_path), toUserName=wx_group)
                                    else:
                                        ok = itchat.send_file(str(media_path), toUserName=wx_group)
                                except Exception as e: logger.error("media send failed: %s", e)
                        if media_path and media_path.exists():
                            try: media_path.unlink()
                            except Exception: pass
                        if msg_id is not None:
                            state["last_msg_id"] = msg_id
                        state["last_update_id"] = update_id + 1
                    self._save_state(tg_channel, state)
                now = time.time()
                if now - last_cleanup > 300:
                    last_cleanup = now
                    cutoff = now - 3600
                    for f in self.tg_fwd_cache.iterdir():
                        if f.is_file() and f.stat().st_mtime < cutoff:
                            try: f.unlink()
                            except Exception: pass
                time.sleep(1)
            except Exception:
                logger.exception("TG forward poll error")
                time.sleep(30)