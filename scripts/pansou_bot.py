#!/usr/bin/env python3
"""
群聊盘搜 — 微信群聊中 @机器人 搜索 xxx → 自动调 PanSou API 返回资源链接
完全独立于 Hermes，0 token 消耗

依赖: pip install itchat-uos requests Pillow
用法:
  python3 pansou_bot.py --daemon                  # 常驻模式
  python3 pansou_bot.py --search "凡人修仙传"     # 终端搜索测试
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
PANSW_API = os.getenv("PANSW_API", "http://192.168.10.216:850/api/search")
ITCHAT_PKL = os.getenv("ITCHAT_PKL", str(Path.home() / ".hermes" / "wechat_uos" / "itchat.pkl"))
QR_PATH = os.getenv("QR_PATH", str(Path.home() / ".hermes" / "wechat_uos" / "itchat_qr.png"))
LOG_FILE = os.getenv("PANSW_LOG", "/tmp/pansou_bot.log")
SEARCH_SIZE = int(os.getenv("PANSW_PAGE_SIZE", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("pansou")

# ---------------------------------------------------------------------------
# PanSou API
# ---------------------------------------------------------------------------
CACHE: dict | None = None
CACHE_TIME = 0
CACHE_TTL = 60  # seconds


def fetch_all_data() -> dict:
    """Fetch full PanSou dataset (keyword param is ignored server-side)."""
    global CACHE, CACHE_TIME
    now = time.time()
    if CACHE and (now - CACHE_TIME) < CACHE_TTL:
        return CACHE

    try:
        url = f"{PANSW_API}?keyword=all&page=1&size={SEARCH_SIZE}"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = json.loads(r.text, strict=False)
        CACHE = data
        CACHE_TIME = now
        return data
    except Exception as e:
        log.error("PanSou API error: %s", e)
        return {}


def search_pansou(keyword: str, max_results: int = 15) -> list[dict]:
    """Search pan resources, return [{source, note, url, password, datetime}, ...]."""
    data = fetch_all_data()
    if not data:
        return []

    merged = data.get("data", {}).get("merged_by_type", {})
    kw_lower = keyword.lower()
    results = []

    for src_type, items in merged.items():
        for item in items:
            note = item.get("note", "")
            if kw_lower in note.lower():
                results.append({
                    "source": src_type,
                    "note": note,
                    "url": item.get("url", ""),
                    "password": item.get("password", ""),
                    "datetime": item.get("datetime", ""),
                })

    results.sort(key=lambda x: x.get("datetime", ""), reverse=True)
    return results[:max_results]


def format_search_results(keyword: str, results: list[dict]) -> str:
    """Format results for WeChat group message."""
    if not results:
        return f"🔍 「{keyword}」没找到匹配的资源"

    lines = [f"🔍 「{keyword}」搜索结果：", "━" * 22]
    for i, r in enumerate(results, 1):
        note = r["note"]
        url = r["url"]
        pwd = f" 密码:{r['password']}" if r.get("password") else ""
        lines.append(f"{i}. [{r['source']}] {note}")
        lines.append(f"   📎 {url}{pwd}")
        lines.append("━" * 22)

    total = len(results)
    # WeChat message limit ~4000 chars, truncate if needed
    body = "\n".join(lines)
    if len(body) > 3800:
        # Keep first 10 results only
        lines = [f"🔍 「{keyword}」搜索结果（前10条）：", "━" * 22]
        for i, r in enumerate(results[:10], 1):
            note = r["note"]
            url = r["url"]
            pwd = f" 密码:{r['password']}" if r.get("password") else ""
            lines.append(f"{i}. [{r['source']}] {note}")
            lines.append(f"   📎 {url}{pwd}")
            lines.append("━" * 22)
        lines.append(f"... 还有 {total - 10} 条结果")
        body = "\n".join(lines)

    return body


# ---------------------------------------------------------------------------
# WeChat UOS (itchat-uos) — message handling
# ---------------------------------------------------------------------------
def init_itchat() -> Optional[object]:
    """Initialize itchat-uos, try hot reload first."""
    try:
        import itchat
    except ImportError:
        log.error("itchat-uos not installed. Run: pip install itchat-uos")
        return None

    pkl_path = Path(ITCHAT_PKL)
    if pkl_path.exists():
        log.info("Hot loading itchat from %s", ITCHAT_PKL)
        itchat.auto_login(hotReload=True, statusStorageDir=str(pkl_path), enableCmdQR=False)
    else:
        log.info("No cached login, generating QR at %s", QR_PATH)
        itchat.auto_login(
            hotReload=True,
            statusStorageDir=str(pkl_path),
            enableCmdQR=False,
            picDir=QR_PATH,
        )

    log.info("WeChat login successful")
    return itchat


def run_daemon():
    """Main daemon loop."""
    itchat_mod = init_itchat()
    if itchat_mod is None:
        sys.exit(1)

    import itchat  # noqa: F811 — itchat_mod is the module
    import itchat.content as ic

    @itchat.msg_register(ic.TEXT, isGroupChat=True)
    def group_text_handler(msg):
        """Handle @bot 搜索 xxx in group chats."""
        text = msg.get("Text", "").strip()
        chatroom = msg.get("User", {})
        group_id = chatroom.get("UserName", "")
        group_name = chatroom.get("NickName", "未知群")
        actual_username = msg.get("ActualUserName", "")
        actual_nickname = msg.get("ActualNickName", "")

        # Check for "搜索" command
        # Format: "搜索 xxx" or "@bot 搜索 xxx"
        search_match = re.search(r"搜索\s*(.+)", text)
        if not search_match:
            return

        keyword = search_match.group(1).strip()
        if not keyword:
            return

        log.info("Search request from %s in %s: %s", actual_nickname, group_name, keyword)

        try:
            results = search_pansou(keyword)
            reply = format_search_results(keyword, results)
            itchat.send(reply, toUserName=group_id)
            log.info("Sent %d results for '%s' to %s", len(results), keyword, group_name)
        except Exception as e:
            log.error("Search error: %s", e)
            try:
                itchat.send(f"⚠️ 搜索出错：{e}", toUserName=group_id)
            except Exception:
                pass

    log.info("PanSou bot daemon started. Listening for '搜索' commands...")
    itchat.run()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="群聊盘搜 — WeChat PanSou search bot")
    parser.add_argument("--daemon", action="store_true", help="Run as WeChat bot daemon")
    parser.add_argument("--search", type=str, help="Test search from terminal")
    parser.add_argument("--keyword", type=str, help="Alias for --search")
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
        return

    keyword = args.search or args.keyword
    if keyword:
        log.info("Terminal search: %s", keyword)
        results = search_pansou(keyword)
        output = format_search_results(keyword, results)
        print(output)
        return

    parser.print_help()


if __name__ == "__main__":
    main()