"""
WxPowerBot — 群聊命令处理器
"""

import json
import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import quote as _urlquote

logger = logging.getLogger("WxPowerBot.handlers")


class CommandHandlers:
    """命令处理器 Mixin，需要绑定到 WxPowerBot 实例使用。"""

    # ── ACL 命令 ──────────────────────────────────────────────────────

    def handle_acl_command(self, text: str, *, sender: str, sender_id: str, chat_id: str, group_name: str) -> bool:
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
                    _add_unique = lambda s, v: (s.append(v) if v and v not in s else None)
                    _add_unique(group.setdefault("admins", []), sender_id)
                    group["updated_at"] = int(time.time())
                    self._save_acl()
                    try:
                        self._refresh_group_members(chat_id, group_name)
                    except Exception:
                        pass
                    self._send_text(chat_id, f"本群已授权成功。\n管理员：{sender}")
                    try:
                        self._auto_add_tg_fwd_group(chat_id, group_name)
                    except Exception:
                        pass
                    return True
                if self._is_group_admin(group, sender, sender_id):
                    if group.get("restored_from_group_id"):
                        return True
                    self._send_text(chat_id, "本群已授权，无需重复授权。")
                    return True
                group["initial_admin_uid"] = sender_id
                _add_unique = lambda s, v: (s.append(v) if v and v not in s else None)
                _add_unique(group.setdefault("admins", []), sender_id)
                group.pop("restored_from_group_id", None)
                group["updated_at"] = int(time.time())
                self._save_acl()
                try:
                    self._refresh_group_members(chat_id, group_name)
                except Exception:
                    pass
                self._send_text(chat_id, f"本群已授权成功。\n管理员：{sender}")
                return True

        if self._is_group_deauthorize_command(text):
            with self._acl_lock:
                group = self._group_acl(chat_id, group_name)
                if not group.get("authorized"):
                    self._send_text(chat_id, "本群尚未授权，无需关闭。")
                    return True
                if not self._is_group_admin(group, sender, sender_id):
                    self._send_text(chat_id, "你没有权限关闭本群的授权。")
                    return True
                group["authorized"] = False
                group["updated_at"] = int(time.time())
                self._save_acl()
            self._send_text(chat_id, "本群已关闭授权，机器人将不再响应群消息。\n如需重新开启，请发送：@机器人 开启授权")
            return True

        # ACL 管理命令
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
            self._send_text(chat_id, "当前群尚未授权。请发送：@机器人 开启授权 或 授权此群聊")
            return True
        if not is_admin:
            self._send_text(chat_id, "你没有权限管理本群机器人。")
            return True
        if cmd in {"/acl", "acl", "权限列表", "名单"}:
            self._send_text(chat_id, self._format_acl(chat_id, group_name))
            return True
        if cmd in {"/refresh", "refresh", "刷新成员", "刷新群成员"}:
            count = self._refresh_group_members(chat_id, group_name)
            self._send_text(chat_id, f"成员列表已刷新。\n当前缓存成员数：{count}")
            return True
        if len(parts) < 2:
            self._send_text(chat_id, "格式：授权/取消授权/设管理员/取消管理员 昵称或UID")
            return True
        target_name = " ".join(parts[1:]).strip()
        target_uid, err = self._find_member_uid(chat_id, group_name, target_name)
        if err:
            self._send_text(chat_id, err)
            return True
        with self._acl_lock:
            group = self._group_acl(chat_id, group_name)
            label = self._member_display(group, target_uid, target_name)
            if cmd in {"/allow", "allow", "授权", "添加权限", "加权限"}:
                _add_unique(group.setdefault("allowed_users", []), target_uid)
                action = "已授权"
            elif cmd in {"/deny", "deny", "取消授权", "移除权限", "删权限"}:
                if target_uid in group.get("admins", []):
                    self._send_text(chat_id, "不能用取消授权命令移除管理员。")
                    return True
                group.setdefault("allowed_users", [])[:] = [u for u in group.get("allowed_users", []) if u != target_uid]
                action = "已取消授权"
            elif cmd in {"/admin", "admin", "设管理员", "添加管理员", "管理员"}:
                _add_unique(group.setdefault("admins", []), target_uid)
                action = "已设为管理员"
            else:
                if target_uid == group.get("owner_uid") or target_uid == group.get("initial_admin_uid"):
                    self._send_text(chat_id, "不能移除群主或初始管理员。")
                    return True
                group.setdefault("admins", [])[:] = [u for u in group.get("admins", []) if u != target_uid]
                action = "已取消管理员"
            group["updated_at"] = int(time.time())
            self._save_acl()
        self._send_text(chat_id, f"{action}：{label}")
        return True

    # ── PanSou 盘搜 ──────────────────────────────────────────────────

    PANSOU_ALLOWED_TYPES = {"quark", "115", "baidu", "uc", "magnet"}
    PANSOU_SOURCE_LABELS = {
        "quark": "夸克网盘", "115": "115网盘", "baidu": "百度网盘",
        "uc": "UC网盘", "magnet": "磁力链接",
    }

    def _is_pansou_command(self, text: str) -> Optional[str]:
        if not text:
            return None
        s = text.strip()
        if s.startswith("搜索"):
            kw = s[2:].strip()
            return kw if kw else None
        if s.startswith("搜 "):
            return s[3:].strip() or None
        if s.startswith("搜"):
            kw = s[2:].strip() if len(s) > 2 else ""
            return kw if kw else None
        return None

    def _pansou_is_enabled(self, group_id: str) -> bool:
        with self._acl_lock:
            return self._group_acl(group_id, "").get("pansou_enabled", True)

    def handle_pansou_search(self, text: str, group_id: str, group_name: str, sender: str = "", sender_id: str = "") -> Optional[bool]:
        keyword = self._is_pansou_command(text)
        if keyword is None:
            return None
        if self._itchat is None:
            return True
        if not self._pansou_is_enabled(group_id):
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            if not group.get("authorized"):
                self._send_text(group_id, "本群尚未开启 Hermes，请先发送：开启授权")
                return True
            if not sender_id and not sender:
                self._send_text(group_id, "无法识别发送者身份。")
                return True
            if not self._is_group_admin(group, sender, sender_id) and sender_id not in group.get("allowed_users", []):
                if self.get("allowed_users", []) and (sender in self.get("allowed_users", []) or sender_id in self.get("allowed_users", [])):
                    pass
                else:
                    self._send_text(group_id, "你没有使用盘搜的权限。请联系管理员为你授权。")
                    return True
        pansou_api = self.get("pansou_api", "")
        if not pansou_api:
            self._send_text(group_id, "盘搜 API 未配置。")
            return True
        try:
            url = f"{pansou_api}?kw={_urlquote(keyword)}&page=1&size=400"
            req = urllib.request.Request(url, headers={"User-Agent": "WxPowerBot/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode("utf-8"), strict=False)
            merged = raw.get("data", {}).get("merged_by_type", {})
            results = []
            for src_type, items in merged.items():
                if str(src_type).lower() not in self.PANSOU_ALLOWED_TYPES:
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
            self._send_pansou_results(keyword, results, group_id)
        except Exception as e:
            logger.exception("PanSou API error")
            self._send_text(group_id, f"[盘搜] API 请求失败: {e}")
        return True

    def _send_pansou_results(self, keyword: str, results: List[Dict[str, Any]], group_id: str) -> None:
        if not results:
            self._send_text(group_id, f"🔍 搜索「{keyword}」没找到匹配的资源")
            return
        grouped = {}
        for r in results:
            grouped.setdefault(r["source"], []).append(r)
        self._send_text(group_id, f"🔍 搜索「{keyword}」共 {len(results)} 条结果")
        for src_type, items in grouped.items():
            label = self.PANSOU_SOURCE_LABELS.get(src_type, src_type)
            lines = [f"━━━ {label} ━━━"]
            for i, item in enumerate(items[:5], 1):
                lines.append(f"{i}. {item['note']}")
                lines.append(f"   📎 {item['url_str']}")
            self._send_text(group_id, "\n".join(lines))

    def handle_pansou_toggle_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        s = (text or "").strip().lower()
        c = s.replace(" ", "")
        if c not in ("开启盘搜", "关闭盘搜"):
            return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._send_text(group_id, "本群尚未开启，请先发送：开启授权"); return True
            if not self._is_group_admin(group, sender, sender_id):
                self._send_text(group_id, "你没有权限管理盘搜。"); return True
            en = c == "开启盘搜"
            group["pansou_enabled"] = en
            group["updated_at"] = int(time.time())
            self._save_acl()
        self._send_text(group_id, f"✅ 盘搜{'已开启' if en else '已关闭'}")
        return True

    # ── TG 转发开关 ──────────────────────────────────────────────────

    def handle_tg_fwd_toggle_command(self, compact: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        if compact not in ("开启转发", "关闭转发", "开启tg转发", "关闭tg转发"):
            return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._send_text(group_id, "本群尚未开启，请先发送：开启授权"); return True
            if not self._is_group_admin(group, sender, sender_id):
                self._send_text(group_id, "你没有权限管理转发。"); return True
        en = compact in ("开启转发", "开启tg转发")
        gs = self.tg_fwd_dir / "group_state.json"
        st = {}
        if gs.exists():
            try: st = json.loads(gs.read_text())
            except Exception: pass
        st[group_id] = {"enabled": en, "updated_at": int(time.time())}
        gs.write_text(json.dumps(st, ensure_ascii=False, indent=2))
        self._send_text(group_id, f"✅ TG 转发{'已开启' if en else '已关闭'}")
        return True

    def auto_add_tg_fwd_group(self, group_id: str, group_name: str) -> None:
        cfg_file = self.tg_fwd_dir / "config.json"
        if not cfg_file.exists():
            return
        try:
            cfg = json.loads(cfg_file.read_text())
        except Exception:
            return
        changed = False
        for rule in cfg.get("forward_rules", []):
            groups = rule.get("wechat_groups", [])
            if group_id not in groups:
                groups.append(group_id)
                changed = True
        if changed:
            cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        gs = self.tg_fwd_dir / "group_state.json"
        st = {}
        if gs.exists():
            try: st = json.loads(gs.read_text())
            except Exception: pass
        entry = st.setdefault(group_id, {})
        if not entry.get("enabled"):
            entry["enabled"] = True
            entry["updated_at"] = int(time.time())
            gs.write_text(json.dumps(st, ensure_ascii=False, indent=2))
        logger.info("TG forward: auto-added %s (%s)", group_name, group_id[:16])

    # ── CFTC 上传 ────────────────────────────────────────────────────

    def handle_cftc_toggle_command(self, compact: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        compact = compact.strip()
        if compact not in {"开启上传", "关闭上传"}:
            return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._send_text(group_id, "当前群尚未授权。请发送：@机器人 开启授权"); return True
            if not self._is_group_admin(group, sender, sender_id):
                self._send_text(group_id, "你没有权限。"); return True
            en = compact == "开启上传"
            group["cftc_enabled"] = en
            group["updated_at"] = int(time.time())
            self._save_acl()
        self._send_text(group_id, f"✅ 图床上传已{'开启' if en else '关闭'}")
        return True

    def handle_cftc_upload_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        s = text.strip().lower().replace(" ", "")
        if s != "上传" and not s.startswith("上传"):
            return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            if not group.get("authorized"):
                self._send_text(group_id, "当前群尚未授权。"); return True
            if not group.get("cftc_enabled", True):
                self._send_text(group_id, "本群图床上传已关闭。"); return True
            if not self._can_use_group(group_id, group_name, sender, sender_id):
                return True
        parts = text.strip().split()
        storage_type = parts[-1].strip().lower() if len(parts) >= 2 and parts[-1].strip().lower() in ("telegram", "r2") else "telegram"
        media = self._cftc_find_latest(group_id)
        if not media:
            self._send_text(group_id, "没有找到可上传的媒体文件。请先发送图片/文件到群聊。"); return True
        fp = media["path"]
        if not fp.exists():
            self._send_text(group_id, "媒体文件已过期，请重新发送。"); return True
        self._send_text(group_id, f"⏫ 正在上传 {media['name']} 到 CFTC...")
        url = self._cftc_upload_file(fp, media["name"], storage_type)
        self._send_text(group_id, f"✅ 上传成功：{url}" if url else "上传失败，请重试。")
        return True

    # ── LSPosed 模块更新 ─────────────────────────────────────────────

    def handle_lsposed_text_command(self, text: str, *, group_id: str, group_name: str, sender: str, sender_id: str) -> bool:
        s = text.strip().lower().replace(" ", "")
        if s not in {"开启更新", "关闭更新", "模块更新状态"}:
            return False
        if self._itchat is None:
            return True
        with self._acl_lock:
            group = self._group_acl(group_id, group_name)
            self._remember_member(group, sender_id, nick=sender, display=sender)
            if not group.get("authorized"):
                self._send_text(group_id, "当前群尚未授权。"); return True
            if not self._is_group_admin(group, sender, sender_id):
                self._send_text(group_id, "你没有权限。"); return True
        if s == "开启更新":
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                group["lsposed_enabled"] = True
                group["updated_at"] = int(time.time())
                self._save_acl()
            self._send_text(group_id, "✅ 模块更新已开启"); return True
        if s == "关闭更新":
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                group["lsposed_enabled"] = False
                group["updated_at"] = int(time.time())
                self._save_acl()
            self._send_text(group_id, "✅ 模块更新已关闭"); return True
        if s == "模块更新状态":
            cfg = self._lsposed_load_config()
            state = self._lsposed_load_state()
            with self._acl_lock:
                group = self._group_acl(group_id, group_name)
                en = "🟢 已开启" if group.get("lsposed_enabled") else "🔴 已关闭"
            self._send_text(group_id, f"模块更新：{en}\n已跟踪：{len(state.get('modules', {}))} 个模块\n自定义仓库：{len(cfg.get('custom_repos', []))} 个")
            return True
        return False

    # ── GID 迁移 ────────────────────────────────────────────────────

    def migrate_external_gid(self, old_gid: str, new_gid: str, group_name: str) -> None:
        try:
            cfg_file = self.tg_fwd_dir / "config.json"
            if cfg_file.exists():
                cfg = json.loads(cfg_file.read_text())
                changed = False
                for rule in cfg.get("forward_rules", []):
                    for i, gid in enumerate(rule.get("wechat_groups", [])):
                        if gid == old_gid:
                            rule["wechat_groups"][i] = new_gid
                            changed = True
                if changed:
                    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

            gs_file = self.tg_fwd_dir / "group_state.json"
            if gs_file.exists():
                gs = json.loads(gs_file.read_text())
                if old_gid in gs:
                    gs[new_gid] = gs.pop(old_gid)
                    gs_file.write_text(json.dumps(gs, ensure_ascii=False, indent=2))

            lsposed_cfg = self.lsposed_dir / "config.json"
            if lsposed_cfg.exists():
                lcfg = json.loads(lsposed_cfg.read_text())
                tg = lcfg.get("target_groups", [])
                changed = False
                for i, g in enumerate(tg):
                    if g == old_gid:
                        tg[i] = new_gid
                        changed = True
                if changed:
                    lcfg["target_groups"] = tg
                    lsposed_cfg.write_text(json.dumps(lcfg, ensure_ascii=False, indent=2))
        except Exception:
            logger.exception("GID migration error")