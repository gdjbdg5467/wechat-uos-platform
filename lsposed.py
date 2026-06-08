"""
WxPowerBot — LSPosed 模块更新追踪
"""
import json
import logging
import os
import ssl
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("WxPowerBot.lsposed")


class LSPosedTracker:
    """Xposed 模块更新追踪，轮询 GitHub API 并推送更新到微信群。"""

    def __init__(self, data_dir: Path):
        self.config_file = data_dir / "lsposed" / "config.json"
        self.state_file = data_dir / "lsposed" / "state.json"
        self.modules_cache = data_dir / "lsposed" / "modules.json"
        (data_dir / "lsposed").mkdir(parents=True, exist_ok=True)

    def load_config(self) -> dict:
        default = {
            "enabled": True, "target_groups": [], "interval_seconds": 1800,
            "max_updates_per_tick": 10, "modules_url": "https://modules.lsposed.org/modules.json",
            "custom_repos": [], "github_token": "",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
        }
        try:
            if self.config_file.exists():
                data = json.loads(self.config_file.read_text())
            else:
                data = {}
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception:
            logger.exception("LSPosed config error")
            self.config_file.write_text(json.dumps(default, ensure_ascii=False, indent=2))
            return dict(default)

    def load_state(self) -> dict:
        try:
            if self.state_file.exists():
                return json.loads(self.state_file.read_text())
        except Exception: pass
        return {"modules": {}, "custom_repos": {}, "web_sources": {}}

    def save_state(self, state: dict) -> None:
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def start(self, bot, acl_getter) -> None:
        """启动轮询线程。"""
        threading.Thread(
            target=self._poll_loop,
            args=(bot, acl_getter),
            name="lsposed-poll",
            daemon=True,
        ).start()

    def _poll_loop(self, bot, acl_getter) -> None:
        logger.info("LSPosed: poll loop started")
        first_run = True
        while True:
            try:
                cfg = self.load_config()
                if cfg.get("enabled", False):
                    self._run_once(cfg, bot, acl_getter, first_run)
                first_run = False
            except Exception:
                logger.exception("LSPosed poll error")
            for _ in range(cfg.get("interval_seconds", 1800) // 5):
                time.sleep(5)

    def _run_once(self, cfg: dict, bot, acl_getter, first_run: bool) -> int:
        state = self.load_state()
        updates = []
        max_up = cfg.get("max_updates_per_tick", 10)

        # 模块市场
        modules = self._fetch_modules(cfg)
        if modules:
            for mod in modules:
                if len(updates) >= max_up: break
                mname = mod.get("name", "") or mod.get("moduleName", "")
                if not mname: continue
                ver = mod.get("version", "") or mod.get("versionName", "") or "0"
                old_ver = state.get("modules", {}).get(mname, "")
                if ver and ver != old_ver:
                    updates.append({"type": "module", "name": mname, "version": ver, "old_version": old_ver, "mod": mod})
            state.setdefault("modules", {}).update({u["name"]: u["version"] for u in updates})

        # 自定义仓库 (GitHub Releases)
        for repo in cfg.get("custom_repos", []):
            if len(updates) >= max_up: break
            owner, repo_name, label = repo.get("owner", ""), repo.get("repo", ""), repo.get("name", f"{repo.get('owner','?')}/{repo.get('repo','?')}")
            if not owner or not repo_name: continue
            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{owner}/{repo_name}/releases/latest",
                    headers={"User-Agent": cfg.get("user_agent"), "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    release = json.loads(resp.read())
                tag = release.get("tag_name", "")
                old_tag = state.get("custom_repos", {}).get(f"{owner}/{repo_name}", "")
                if tag and tag != old_tag:
                    asset = (release.get("assets") or [{}])[0]
                    updates.append({
                        "type": "custom", "name": label, "version": tag, "old_version": old_tag,
                        "url": asset.get("browser_download_url", ""),
                        "body": (release.get("body") or "")[:500],
                    })
                state.setdefault("custom_repos", {})[f"{owner}/{repo_name}"] = tag
            except Exception:
                logger.debug("GitHub fetch failed for %s/%s", owner, repo_name)

        self.save_state(state)
        if first_run:
            logger.info("LSPosed: baseline %d modules, %d custom repos", len(state.get("modules", {})), len(state.get("custom_repos", {})))
            return 0
        if not updates:
            return 0

        # 推送更新
        groups = cfg.get("target_groups", [])
        acl_groups = acl_getter()
        for up in updates[:max_up]:
            msg = self._format_update(up)
            sent = False
            for gid in groups:
                if bot is None: break
                gi = acl_groups.get(gid, {})
                if not gi.get("authorized") or not gi.get("lsposed_enabled"):
                    continue
                try:
                    bot.send(msg, toUserName=gid)
                    time.sleep(0.5)
                    sent = True
                except Exception: pass
            if sent:
                logger.info("LSPosed pushed: %s -> %s", up["name"], up["version"])
        return len(updates)

    def _fetch_modules(self, cfg: dict) -> list:
        token = cfg.get("github_token", "")
        org = cfg.get("org", "Xposed-Modules-Repo")
        if token:
            try:
                modules = self._fetch_via_github(org, token, cfg)
                if modules:
                    self.modules_cache.write_text(json.dumps(modules, ensure_ascii=False))
                    return modules
            except Exception as e:
                logger.debug("GitHub API failed: %s", e)
        # Fallback: local cache
        try:
            if self.modules_cache.exists():
                data = json.loads(self.modules_cache.read_text())
                if isinstance(data, list): return data
                if isinstance(data, dict): return data.get("modules", data.get("data", []))
        except Exception: pass
        return []

    def _fetch_via_github(self, org: str, token: str, cfg: dict) -> list:
        results = []
        cursor = None
        has_next = True
        ua = cfg.get("user_agent", "WxPowerBot/1.0")
        ctx = ssl.create_default_context()
        while has_next:
            after = f'"{cursor}"' if cursor else "null"
            query = f"""
            query {{
              organization(login: "{org}") {{
                repositories(first: 100, after: {after}, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
                  pageInfo {{ hasNextPage endCursor }}
                  nodes {{
                    name description homepageUrl url stargazerCount
                    createdAt updatedAt isArchived
                    repositoryTopics(first: 5) {{ nodes {{ topic {{ name }} }} }}
                    latestRelease {{
                      tagName isPrerelease isDraft name description createdAt publishedAt
                      releaseAssets(first: 5) {{ nodes {{ name contentType downloadCount size downloadUrl }} }}
                    }}
                  }}
                }}
              }}
            }}
            """
            req = urllib.request.Request(
                "https://api.github.com/graphql",
                data=json.dumps({"query": query}).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": ua,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                body = json.loads(resp.read())
            if "errors" in body:
                logger.warning("GraphQL errors: %s", body.get("errors"))
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
                entry = {
                    "name": node.get("name", ""),
                    "description": node.get("description", ""),
                    "url": node.get("url", ""),
                    "homepageUrl": node.get("homepageUrl", ""),
                    "stargazerCount": node.get("stargazerCount", 0),
                    "updatedAt": node.get("updatedAt", ""),
                    "createdAt": node.get("createdAt", ""),
                    "latestRelease": lr,
                    "latestReleaseTime": lr.get("publishedAt", lr.get("createdAt", "")),
                }
                topics = node.get("repositoryTopics", {}).get("nodes", [])
                entry["isModule"] = any(t.get("topic", {}).get("name") == "xposed-module" for t in topics)
                results.append(entry)
            logger.info("LSPosed: fetched page (%d total)", len(results))
        return results

    def _format_update(self, up: dict) -> str:
        t = up.get("type", "unknown")
        name = up.get("name", "?")
        ver = up.get("version", "?")
        old_ver = up.get("old_version", "")
        lines = [f"📦 {name}", f"版本：{ver}"]
        if old_ver:
            lines.append(f"旧版本：{old_ver}")
        if t == "custom":
            dl_url = up.get("url", "")
            body = up.get("body", "")
            if dl_url:
                lines.append(f"下载：{dl_url}")
            if body:
                lines.append(f"说明：{body[:200]}")
        return "\n".join(lines)