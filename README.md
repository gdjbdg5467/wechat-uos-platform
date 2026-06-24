# Hermes Agent — WeChat UOS Adapter

> Hermes Agent 插件：微信（itchat-uos 协议）多功能机器人，集成群 ACL 授权、盘搜、抖音解析、公众号推送、TG 转发、图床上传、LSPosed 模块更新追踪。

## 功能概览

| 功能 | 说明 |
|------|------|
| **微信登录** | itchat-uos 逆向协议，QR 码扫码，支持热重载（pkl） |
| **群 ACL 授权** | 三级权限：全局管理员 → 群管理员 → 授权用户，GID 自动迁移 |
| **盘搜** | 夸克 / 115 / 百度 / UC / 磁力 资源搜索 |
| **抖音解析** | 自动识别并解析抖音/TikTok 视频和图片帖 |
| **公众号订阅** | 基于 WeRSS 的公众号文章推送，支持白名单 + 订阅时间过滤 |
| **TG 转发** | Telegram 频道消息自动转发到微信群（channel_post 回调，无轮询） |
| **CFTC 图床上传** | 图片/文件上传到 CFTC 图床 |
| **模块更新追踪** | LSPosed Xposed 模块 GitHub Release 监控与推送 |
| **群聊接入 Hermes AI** | 微信群 @机器人 直接对话 |

## 架构

```
hermes-agent/plugins/platforms/wechat_uos/
├── adapter.py          # 主适配器 (WeChatUOSAdapter)
├── __init__.py         # 插件入口
├── plugin.yaml         # 插件元数据
├── README.md           # 本文件
└── config.example.yaml # 配置示例
```

依赖外部服务：
- **WeRSS** (`http://localhost:8001`) — 公众号文章抓取
- **盘搜 API** (`http://localhost:850`) — 网盘资源搜索
- **抖音解析 API** (`http://192.168.10.216:8002`) — 抖音链接解析
- **Telegram Bot API** — TG 频道转发（需 bot token）

## 配置

编辑 Hermes Agent 的 `config.yaml`，在 `plugins` 下配置：

```yaml
plugins:
  wechat_uos:
    enabled: true
    admin_users: "昵称1,昵称2"          # 全局管理员（群昵称）
    pansou_api: "http://192.168.1.100:850/api/search"
    qr_http: true
    qr_port: 8646
```

TG 转发配置（`~/.hermes/data/tg_fwd/config.json`）：

```json
{
  "bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
  "forward_rules": [
    {
      "tg_channel": "@channel_name",
      "wechat_groups": ["@@wechat_group_id"]
    }
  ]
}
```

LSPosed 配置（`~/.hermes/data/lsposed_tracker/config.json`）：

```json
{
  "enabled": true,
  "target_groups": ["@@group_id"],
  "github_token": "ghp_xxx",
  "interval_seconds": 1800,
  "custom_repos": [
    {"owner": "mytv-android", "repo": "myDV", "name": "myTV/myDV"}
  ]
}
```

## 群聊命令

在微信群中 `@机器人` + 命令：

### 授权管理

| 命令 | 说明 | 权限 |
|------|------|------|
| `开启授权` / `授权此群聊` | 首次发送者自动成为管理员 | 任意成员 |
| `关闭授权` | 关闭本群授权 | 管理员 |
| `授权 昵称` | 授权成员使用功能 | 管理员 |
| `取消授权 昵称` | 取消成员权限（不包含管理员） | 管理员 |
| `设管理员 昵称` | 设置群管理员 | 管理员 |
| `取消管理员 昵称` | 移除管理员 | 管理员 |
| `权限列表` / `名单` / `acl` | 查看本群 ACL | 管理员 |
| `刷新成员` / `刷新群成员` | 刷新群成员列表 | 管理员 |

### 盘搜

| 命令 | 说明 |
|------|------|
| `搜索 <关键词>` | 搜索夸克/115/百度/UC 网盘资源 |
| `开启盘搜` / `关闭盘搜` | 盘搜开关（管理员） |

### 抖音解析

| 命令 | 说明 |
|------|------|
| `开启抖音解析` / `关闭抖音解析` | 自动解析开关（管理员） |
| *(直接发抖音链接)* | 自动解析并发送视频/图片 |

支持链接格式：`v.douyin.com`、`www.douyin.com/video/`、`www.iesdouyin.com/share/video/`、TikTok 链接。

### 公众号订阅（WeRSS）

| 命令 | 说明 |
|------|------|
| `订阅 公众号名` | 添加白名单，支持用「,」分隔多个 |
| `取消订阅 公众号名` | 移除白名单 |
| `订阅列表` / `查看订阅` | 查看本群已订阅的公众号 |
| `开启推文` / `关闭推文` | 公众号推送开关（管理员） |

### TG 转发

| 命令 | 说明 |
|------|------|
| `开启转发` / `关闭转发` | TG 频道→群转发开关（管理员） |

### CFTC 图床

| 命令 | 说明 |
|------|------|
| `开启上传` / `关闭上传` | 图床上传开关（管理员） |
| `上传` | 上传最新收到的媒体文件到图床 |

### LSPosed 模块更新

| 命令 | 说明 |
|------|------|
| `开启更新` / `关闭更新` | 模块更新推送开关（管理员） |

### 其他

- `@机器人 <任意消息>` — 直接与 Hermes AI 对话
- `帮助` — 显示帮助菜单

## 数据文件

```
~/.hermes/wechat_uos/
├── acl.json              # 群授权数据（含 allowed_mps）
├── itchat.pkl            # itchat 热重载状态
└── itchat_qr.png         # 登录 QR 码

~/.hermes/data/
├── cftc/                 # CFTC 图床缓存
├── tg_fwd/
│   ├── config.json       # TG 转发配置
│   └── state_*.json      # 各频道已转发消息 ID
└── lsposed_tracker/
    ├── config.json       # LSPosed 配置
    └── seen.json         # 已推送的 release IDs
```

## 注意事项

- itchat-uos 是逆向协议，建议使用备用微信号
- 群 ID (`@@...`) 随重连可能变化，系统自动迁移并保留所有功能配置
- Telegram Bot token 不能分享，TG 转发使用 gateway 的 channel_post 回调（无需单独轮询）
- 微信公众号推送依赖 WeRSS 服务（Docker），需在 WeRSS 单独扫码登录 mp.weixin.qq.com
