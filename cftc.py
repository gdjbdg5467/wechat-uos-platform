"""
WxPowerBot — CFTC 图床上传
"""
import json
import logging
import os
import subprocess
import tempfile
import time
import threading
from pathlib import Path

logger = logging.getLogger("WxPowerBot.cftc")


class CFTCUploader:
    """CFTC 图床/文件上传。"""

    def __init__(self, data_dir: Path, state_dir: Path):
        self.media_dir = data_dir / "cftc_media"
        self.cookie_file = state_dir / "cftc_cookie.json"
        self.state_file = state_dir / "cftc_state.json"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._recent_media = {}

    def register_handlers(self, bot) -> None:
        """注册 itchat 媒体消息处理器。"""
        import itchat
        from itchat.content import PICTURE, VIDEO, ATTACHMENT
        bot_ref = bot

        @itchat.msg_register(PICTURE, isGroupChat=True)
        def pic_handler(msg):
            self._cache_media(msg, bot_ref)

        @itchat.msg_register(VIDEO, isGroupChat=True)
        def video_handler(msg):
            self._cache_media(msg, bot_ref)

        @itchat.msg_register(ATTACHMENT, isGroupChat=True)
        def attach_handler(msg):
            self._cache_media(msg, bot_ref)

        logger.info("CFTC: media handlers registered")

    def _cache_media(self, msg, bot) -> None:
        group_id = getattr(msg, "fromUserName", None) or msg.get("FromUserName", "")
        if not group_id:
            return
        file_name = getattr(msg, "fileName", None) or msg.get("FileName", "unknown")
        new_msg_id = getattr(msg, "newMsgId", None) or msg.get("NewMsgId", 0)
        now = time.time()
        entry = {
            "path": self.media_dir / f"cftc_{new_msg_id}_{file_name}",
            "name": file_name,
            "time": now,
        }
        try:
            if hasattr(msg, "download"):
                msg.download(str(entry["path"]))
            else:
                msg["Text"](str(entry["path"]))
        except Exception as e:
            logger.warning("CFTC: download failed: %s", e)
            return
        cache = self._recent_media.setdefault(group_id, [])
        cache.append(entry)
        if len(cache) > 10:
            oldest = cache.pop(0)
            self._remove_file(oldest["path"])
        logger.info("CFTC: cached %s for %s", file_name, group_id[:16])

    def _remove_file(self, path) -> None:
        try:
            if path and hasattr(path, "exists") and path.exists():
                path.unlink()
        except Exception: pass

    def _clean_expired(self, group_id: str) -> None:
        cache = self._recent_media.get(group_id, [])
        now = time.time()
        fresh = [e for e in cache if now - e["time"] < 600]
        for e in cache:
            if now - e["time"] >= 600:
                self._remove_file(e["path"])
        self._recent_media[group_id] = fresh

    def find_latest(self, group_id: str) -> dict:
        self._clean_expired(group_id)
        cache = self._recent_media.get(group_id, [])
        return cache[-1] if cache else {}

    def upload_file(self, file_path, file_name, cftc_config: dict, storage_type="telegram") -> str:
        try:
            cftc_url = cftc_config.get("url", "https://cftc.lliic.com")
            username = cftc_config.get("username", "")
            password = cftc_config.get("password", "")
            if not username or not password:
                return ""

            jar = tempfile.NamedTemporaryFile(prefix="cftc_jar_", suffix=".txt", delete=False)
            jar_path = jar.name
            jar.close()

            # Login
            subprocess.run(
                ["curl", "-s", "-b", jar_path, "-c", jar_path,
                 "-X", "POST", f"{cftc_url}/login",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps({"username": username, "password": password})],
                capture_output=True, text=True, timeout=15,
            )

            # Save cookie
            if os.path.exists(jar_path):
                self.cookie_file.write_text(json.dumps({"raw_jar": open(jar_path).read()}))

            # Upload
            upload_result = subprocess.run(
                ["curl", "-s", "-b", jar_path, "-c", jar_path,
                 "-X", "POST", f"{cftc_url}/upload",
                 "-F", f"file=@{file_path};filename={file_name}",
                 "-F", "category=",
                 "-F", f"storage_type={storage_type}"],
                capture_output=True, text=True, timeout=120,
            )
            try: os.unlink(jar_path)
            except Exception: pass

            resp = json.loads(upload_result.stdout)
            if resp.get("status") == 1:
                return resp.get("url", "")
            logger.warning("CFTC: upload failed: %s", resp.get("msg", "unknown"))
            return ""
        except Exception:
            logger.exception("CFTC: upload error")
            return ""