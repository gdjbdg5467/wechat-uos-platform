# WxPowerBot — 独立 WeChat UOS 多功能机器人

合并 itchat-uos + 群授权/盘搜/TG转发/CFTC上传/LSPosed模块更新，一键 Docker 部署。

## 功能

- **微信登录** — itchat-uos 协议，QR 码扫码登录，支持热重载
- **群授权系统** — 三级权限：全局管理员 → 群管理员 → 授权用户
- **盘搜** — 搜索夸克/115/百度/UC 网盘资源
- **TG 转发** — Telegram 频道消息自动转发到微信群
- **CFTC 上传** — 图片/文件上传到 CFTC 图床
- **模块更新追踪** — LSPosed Xposed 模块更新监控推送
- **GID 自动迁移** — 处理 itchat 重连后群 ID 变化

## 快速开始

### 方式一：Docker（推荐）

```bash
# 克隆仓库
git clone https://github.com/gdjbdg5467/wechat-uos-platform.git
cd wechat-uos-platform

# 创建数据目录
mkdir -p data/state data/tg_fwd data/lsposed

# 编辑配置
vim data/state/config.json   # 见下方配置说明

# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

### 方式二：直接运行

```bash
pip install -r requirements.txt
mkdir -p data/state
# 编辑 data/state/config.json
python3 main.py
```

### 扫码登录

启动后访问 `http://<IP>:8646/` 查看二维码，用微信扫码登录。

## 配置

### 基础配置（data/state/config.json）

```json
{
  "admin_users": "昵称1,昵称2",
  "pansou_api": "http://192.168.1.100:850/api/search",
  "qr_http": true,
  "qr_port": 8646
}
```

也可通过环境变量 `WXPOWERBOT_<KEY>` 配置。

### TG 转发配置（data/tg_fwd/config.json）

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

### LSPosed 配置（data/lsposed/config.json）

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

在微信群中发送 `@机器人` + 命令：

| 命令 | 说明 |
|------|------|
| `开启授权` | 授权本群（首次发送者自动成为管理员） |
| `关闭授权` | 关闭本群授权（管理员） |
| `授权 昵称` | 授权成员使用盘搜（管理员） |
| `取消授权 昵称` | 取消成员权限（管理员） |
| `权限列表` | 查看本群权限 |
| `搜索 xxx` | 搜索网盘资源 |
| `开启盘搜`/`关闭盘搜` | 盘搜开关（管理员） |
| `开启转发`/`关闭转发` | TG 转发开关（管理员） |
| `开启上传`/`关闭上传` | 图床上传开关（管理员） |
| `开启更新`/`关闭更新` | 模块更新推送开关（管理员） |
| `上传` | 上传最新媒体文件到图床 |
| `帮助` | 显示帮助菜单 |

## 文件结构

```
wxpowerbot/
├── bot.py          # 主机器人类
├── handlers.py     # 命令处理器
├── tg_forward.py   # TG 频道转发
├── cftc.py         # CFTC 上传
├── lsposed.py      # LSPosed 模块追踪
├── main.py         # 入口
├── config.yaml     # 配置模板
├── Dockerfile      # Docker 构建
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 注意事项

- itchat-uos 是逆向协议，建议使用备用微信号
- 群 ID (`@@...`) 可能随重连变化，系统会自动迁移
- 数据保存在 `data/` 目录，请定期备份
