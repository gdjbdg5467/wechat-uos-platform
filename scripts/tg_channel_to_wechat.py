#!/usr/bin/env python3
"""
Telegram Channel → WeChat Group forwarder (with media support).

Monitors a Telegram channel (bot must be channel admin) and forwards new
messages (text + images/video/files) to WeChat groups via itchat-uos.

Usage:
    python3 tg_channel_to_wechat.py --config /path/to/config.json
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import logging
import shutil
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STATE_DIR = Path(os.getenv("HERMES_HOME", "/root/.hermes")) / "data" / "tg_fwd"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = STATE_DIR / "media_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tg-fwd")


# ---------------------------------------------------------------------------
# Telegram Bot API helper
# ---------------------------------------------------------------------------
def tg_api_call(token: str, method: str, params: dict = None) -> dict:
    """Call Telegram Bot API and return parsed JSON."""
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"https://api.telegram.org/bot{token}/{method}?{qs}"
    else:
        url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log.error("TG API %s error %d: %s", method, e.code, body[:200])
        return {"ok": False, "description": body}
    except Exception as e:
        log.error("TG API %s exception: %s", method, e)
        return {"ok": False, "description": str(e)}


def download_file(token: str, file_path: str, local_path: Path) -> bool:
    """Download a file from Telegram servers."""
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            local_path.write_bytes(data)
            return True
    except Exception as e:
        log.error("Download failed %s: %s", file_path, e)
        return False


def get_channel_posts(token: str, offset: int = None, limit: int = 10) -> list:
    """Fetch updates with long polling, return only channel_post entries.
    
    timeout=0 means no long polling (for cron mode).
    timeout=30+ means long polling (for daemon mode).
    """
    params = {"timeout": 0, "limit": limit}
    if offset:
        params["offset"] = offset
    result = tg_api_call(token, "getUpdates", params)
    if not result.get("ok"):
        log.warning("getUpdates failed: %s", result.get("description", ""))
        return []

    posts = []
    for update in result.get("result", []):
        uid = update.get("update_id")
        if "channel_post" in update:
            posts.append((uid, update["channel_post"]))
    return posts


# ---------------------------------------------------------------------------
# Media helpers
# ---------------------------------------------------------------------------
def get_biggest_photo(photo_sizes: list) -> dict:
    """Get the largest photo from a photo size array."""
    if not photo_sizes:
        return {}
    return max(photo_sizes, key=lambda x: x.get("file_size", 0) or 0)


def resolve_file_id(msg: dict) -> tuple:
    """Resolve the best file_id and approximate extension from a message.
    Returns (file_id, ext) or (None, None).
    """
    if "photo" in msg:
        best = get_biggest_photo(msg["photo"])
        return best.get("file_id"), "jpg"
    if "video" in msg:
        v = msg["video"]
        return v.get("file_id"), "mp4"
    if "document" in msg:
        d = msg["document"]
        fname = d.get("file_name", "")
        ext = Path(fname).suffix.lstrip(".") or "bin"
        return d.get("file_id"), ext
    if "audio" in msg:
        a = msg["audio"]
        fname = a.get("file_name", a.get("title", "audio"))
        ext = Path(fname).suffix.lstrip(".") or "mp3"
        return a.get("file_id"), ext
    if "animation" in msg:
        a = msg["animation"]
        return a.get("file_id"), "mp4"
    if "voice" in msg:
        v = msg["voice"]
        return v.get("file_id"), "ogg"
    return None, None


def send_tg_message(token: str, chat_id: str, text: str) -> bool:
    """Send a text message to a Telegram chat. Used for testing."""
    result = tg_api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})
    return result.get("ok", False)


# ---------------------------------------------------------------------------
# WeChat (itchat-uos) helper
# ---------------------------------------------------------------------------
def download_media(token: str, msg: dict, msg_id: int) -> Path:
    """Download a media file from Telegram. Returns local path or None."""
    file_id, ext = resolve_file_id(msg)
    if not file_id:
        return None

    # Get file path from Telegram
    result = tg_api_call(token, "getFile", {"file_id": file_id})
    if not result.get("ok"):
        log.warning("getFile failed for %s", file_id)
        return None

    tg_path = result["result"].get("file_path", "")
    if not tg_path:
        log.warning("No file_path for file_id %s", file_id)
        return None

    local_path = CACHE_DIR / f"msg_{msg_id}_{msg_id}.{ext}"
    if local_path.exists():
        return local_path

    if download_file(token, tg_path, local_path):
        return local_path

    # Try alternate approach - construct URL directly
    alt_url = f"https://api.telegram.org/file/bot{token}/{tg_path}"
    try:
        req = urllib.request.Request(alt_url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            local_path.write_bytes(resp.read())
            return local_path
    except Exception as e:
        log.error("Alt download failed: %s", e)

    return None


# ---------------------------------------------------------------------------
# WeChat (itchat-uos) helper
# ---------------------------------------------------------------------------
def _init_itchat():
    """Load itchat session from PKL. Returns itchat module or None."""
    try:
        import itchat
    except ImportError:
        log.error("itchat-uos not installed. pip install itchat-uos")
        return None

    pkl = Path("/root/.hermes/wechat_uos/itchat.pkl")
    if not pkl.exists():
        log.error("WeChat PKL not found at %s", pkl)
        return None

    try:
        itchat.load_login_status(str(pkl))
        itchat.web_init()
        itchat.start_receiving()
        return itchat
    except Exception as e:
        log.error("itchat init failed: %s", e)
        return None


def send_to_wechat_group(group_id: str, text: str) -> bool:
    """Send text to a WeChat group."""
    itchat = _init_itchat()
    if not itchat:
        return False
    try:
        for i in range(0, len(text), 3500):
            chunk = text[i:i + 3500]
            result = itchat.send(chunk, toUserName=group_id)
            if result is False or result is None:
                return False
        return True
    except Exception as e:
        log.error("itchat send text to %s failed: %s", group_id, e)
        return False


def send_media_to_wechat(group_id: str, media_path: Path) -> bool:
    """Send an image/video/file to a WeChat group."""
    itchat = _init_itchat()
    if not itchat:
        return False
    try:
        ext = media_path.suffix.lower()

        # Images: use send_image
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"):
            result = itchat.send_image(str(media_path), toUserName=group_id)
            if result is False or result is None:
                # Fallback: send as file
                result = itchat.send_file(str(media_path), toUserName=group_id)
        # Video: use send_video, fallback to send_file
        elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            result = itchat.send_file(str(media_path), toUserName=group_id)
            if result is False or result is None:
                result = itchat.send_file(str(media_path), toUserName=group_id)
        # Other files
        else:
            result = itchat.send_file(str(media_path), toUserName=group_id)

        if result is False or result is None:
            log.warning("itchat send_media returned %s for %s", result, media_path.name)
            return False
        return True
    except Exception as e:
        log.error("itchat send media to %s failed: %s", group_id, e)
        return False


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
def _state_file(channel: str) -> Path:
    safe = channel.replace("@", "").replace("/", "_").replace(".", "_")
    return STATE_DIR / f"{safe}.json"


def load_state(channel: str) -> dict:
    sf = _state_file(channel)
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    return {"last_msg_id": None, "last_update_id": None}


def save_state(channel: str, state: dict):
    sf = _state_file(channel)
    sf.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def format_message(msg: dict) -> str:
    """Format a Telegram channel post text for WeChat delivery."""
    text = msg.get("text") or msg.get("caption") or ""
    lines = [l.strip() for l in text.split("\n")]
    text = "\n".join(l for l in lines if l)

    # Media type emoji
    prefix = ""
    if "photo" in msg:
        prefix = "📷"
    elif "video" in msg:
        prefix = "🎬"
    elif "document" in msg:
        d = msg["document"]
        fname = d.get("file_name", "")
        prefix = "📄"
        if fname:
            prefix += f" {fname}"
    elif "audio" in msg:
        prefix = "🎵"
    elif "animation" in msg:
        prefix = "🎞️"
    elif "voice" in msg:
        prefix = "🎤"

    # Build output
    out = prefix
    if text:
        out = f"{prefix}\n{text}" if prefix else text

    # Add URLs from entities
    entities = msg.get("entities", [])
    for ent in entities:
        if ent.get("type") == "text_link":
            url = ent.get("url", "")
            if url:
                out += f"\n🔗 {url}"

    if len(out) > 3500:
        out = out[:3497] + "..."

    return out.strip()


# ---------------------------------------------------------------------------
# Per-group toggle state
# ---------------------------------------------------------------------------
GROUP_STATE_FILE = STATE_DIR / "group_state.json"

def is_forwarding_enabled(group_id: str) -> bool:
    """Check if forwarding is enabled for this group (default: enabled)."""
    if GROUP_STATE_FILE.exists():
        try:
            state = json.loads(GROUP_STATE_FILE.read_text())
            return state.get(group_id, {}).get("enabled", True)
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Main forwarding logic
# ---------------------------------------------------------------------------
def forward_channel_posts(token: str, tg_channel: str, wechat_groups: list[str]):
    """Forward new posts from tg_channel to each wechat_group."""
    state = load_state(tg_channel)
    last_msg_id = state.get("last_msg_id")
    last_update_id = state.get("last_update_id")

    posts = get_channel_posts(token, offset=last_update_id)
    if not posts:
        log.debug("No new channel posts for %s", tg_channel)
        return

    posts.sort(key=lambda x: x[0])

    new_forwarded = 0
    for update_id, msg in posts:
        msg_id = msg.get("message_id")

        if last_msg_id is not None and msg_id is not None and msg_id <= last_msg_id:
            continue

        formatted = format_message(msg)
        media_path = download_media(token, msg, msg_id) if any(
            k in msg for k in ("photo", "video", "document", "audio", "animation", "voice")
        ) else None

        log.info("Forwarding msg %s from %s → %d WeChat groups (media: %s)",
                 msg_id, tg_channel, len(wechat_groups),
                 media_path.name if media_path else "none")

        for wx_group in wechat_groups:
            if not is_forwarding_enabled(wx_group):
                log.debug("Forwarding disabled for group %s, skipping", wx_group)
                continue
            # Send text first (with caption/file description)
            if formatted:
                ok_text = send_to_wechat_group(wx_group, formatted)
            else:
                ok_text = True

            # Send media
            ok_media = True
            if media_path:
                ok_media = send_media_to_wechat(wx_group, media_path)

            if ok_text and ok_media:
                new_forwarded += 1
            else:
                log.error("Failed to forward to WeChat group %s", wx_group)

        # Update state
        if msg_id is not None:
            last_msg_id = msg_id
        last_update_id = update_id + 1

    save_state(tg_channel, {
        "last_msg_id": last_msg_id,
        "last_update_id": last_update_id,
    })

    if new_forwarded:
        log.info("Forwarded %d messages from %s", new_forwarded, tg_channel)


def forward_channel_posts_longpoll(token: str, tg_channel: str, wechat_groups: list[str]):
    """Forward posts using long polling (timeout=30) for near-real-time delivery."""
    state = load_state(tg_channel)
    last_update_id = state.get("last_update_id")

    params = {"timeout": 30, "limit": 10, "allowed_updates": json.dumps(["channel_post"])}
    if last_update_id:
        params["offset"] = last_update_id

    result = tg_api_call(token, "getUpdates", params)
    if not result.get("ok"):
        return

    posts = []
    for update in result.get("result", []):
        uid = update.get("update_id")
        if "channel_post" in update:
            posts.append((uid, update["channel_post"]))
        last_update_id = uid + 1 if uid else last_update_id

    if not posts:
        # Update offset even on empty response to ack
        if last_update_id and last_update_id != state.get("last_update_id"):
            save_state(tg_channel, {"last_msg_id": state.get("last_msg_id"),
                                     "last_update_id": last_update_id})
        return

    posts.sort(key=lambda x: x[0])
    last_msg_id = state.get("last_msg_id")

    for update_id, msg in posts:
        msg_id = msg.get("message_id")
        if last_msg_id is not None and msg_id is not None and msg_id <= last_msg_id:
            continue

        formatted = format_message(msg)
        media_path = download_media(token, msg, msg_id) if any(
            k in msg for k in ("photo", "video", "document", "audio", "animation", "voice")
        ) else None

        # Skip only truly empty messages (no text and no media)
        if not formatted and not media_path:
            log.info("[LIVE] Skipping empty msg %s from %s", msg_id, tg_channel)
            continue

        log.info("[LIVE] Forwarding msg %s from %s (media: %s)",
                 msg_id, tg_channel, media_path.name if media_path else "none")

        for wx_group in wechat_groups:
            if formatted:
                send_to_wechat_group(wx_group, formatted)
            if media_path:
                send_media_to_wechat(wx_group, media_path)

        if msg_id is not None:
            last_msg_id = msg_id
        last_update_id = update_id + 1

    save_state(tg_channel, {
        "last_msg_id": last_msg_id,
        "last_update_id": last_update_id,
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Telegram channel → WeChat group forwarder")
    parser.add_argument("--config", help="Path to config JSON file")
    parser.add_argument("--bot-token", help="Telegram bot token")
    parser.add_argument("--channel", help="Telegram channel username (e.g. @channel)")
    parser.add_argument("--wechat-group", help="WeChat group ID (e.g. @@xxx), repeatable", action="append")
    parser.add_argument("--daemon", help="Run in daemon (long-polling) mode", action="store_true")
    parser.add_argument("--test-tg", help="Send a test message to Telegram", metavar="CHAT_ID")
    parser.add_argument("--test-wx", help="Send a test message to WeChat group", metavar="GROUP_ID")
    args = parser.parse_args()

    if args.config:
        with open(args.config) as f:
            cfg = json.load(f)
        bot_token = cfg.get("bot_token", "")
        rules = cfg.get("forward_rules", [])
        if not rules:
            log.error("No forward_rules in config")
            sys.exit(1)
    else:
        bot_token = args.bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            log.error("No bot token. Use --bot-token, --config, or TELEGRAM_BOT_TOKEN env")
            sys.exit(1)
        tg_channel = args.channel or ""
        wx_groups = args.wechat_group or []
        if not tg_channel or not wx_groups:
            log.error("--channel and --wechat-group required (or use --config)")
            sys.exit(1)
        rules = [{"tg_channel": tg_channel, "wechat_groups": wx_groups}]

    if args.test_tg:
        ok = send_tg_message(bot_token, args.test_tg,
                             "🤖 转发机器人测试：消息发送正常")
        print("TG test:", "OK" if ok else "FAILED")
        return

    if args.test_wx:
        ok = send_to_wechat_group(args.test_wx, "🤖 转发机器人测试：itchat 连接正常")
        print("WeChat test:", "OK" if ok else "FAILED")
        return

    if args.daemon:
        log.info("Starting daemon mode (long-polling every 30s)...")
        while True:
            for rule in rules:
                channel = rule.get("tg_channel", "")
                groups = rule.get("wechat_groups", [])
                if channel and groups:
                    forward_channel_posts_longpoll(bot_token, channel, groups)
            time.sleep(1)
        return

    # One-shot mode (cron)
    for rule in rules:
        channel = rule.get("tg_channel", "")
        groups = rule.get("wechat_groups", [])
        if not channel or not groups:
            log.warning("Skipping incomplete rule: %s", rule)
            continue
        log.info("Forwarding %s → %s", channel, ", ".join(groups))
        forward_channel_posts(bot_token, channel, groups)

    log.info("Done")


if __name__ == "__main__":
    main()