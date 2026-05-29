# WeChat UOS Platform for Hermes Agent

一站式个人微信解决方案。含 Hermes 插件 + 辅助工具，基于 `itchat-uos` 协议。

> ⚠️ `itchat-uos` 属于逆向协议，可能随时被微信限制或失效。建议使用小号，不建议用主力微信号。

---

## 📦 包含内容

| 组件 | 说明 |
|------|------|
| `wechat_uos/` | Hermes Agent 微信平台插件 — 群聊 @ 机器人聊天 |
| `scripts/tg_channel_to_wechat.py` | **TG 频道转发** — 监测 Telegram 频道，自动转发到微信群 |
| `scripts/pansou_bot.py` | **群聊盘搜** — 微信群 @机器人 搜索 xxx，自动返回网盘资源链接 |
| `install.sh` | 一键安装/更新脚本 |

---

## 🚀 快速安装

```bash
# 1. 下载
cd /tmp
git clone https://github.com/gdjbdg5467/wechat-uos-platform.git
cd wechat-uos-platform

# 2. 一键安装（Hermes 插件 + 所有脚本）
bash install.sh

# 3. 启动 Gateway
cd /root/.hermes/hermes-agent
venv/bin/python -m hermes_cli.main gateway run --replace
```

安装脚本会：
1. 安装 itchat-uos + requests 等依赖
2. 复制 UOS 插件到 Hermes plugins 目录
3. 复制 TG 转发 + 盘搜脚本到 `~/.hermes/scripts/`
4. 自动添加环境变量配置

---

## 🔌 WeChat UOS 插件

Hermes Agent 个人微信平台插件，基于 `itchat-uos` 通过 UOS Web WeChat 协议登录真实微信号，把微信群里的 `@机器人` 文本消息转发给 Hermes Gateway，并把回复发回群聊。

### 手动安装

```bash
# 如果不用 install.sh，可以手动操作
mkdir -p /root/.hermes/hermes-agent/plugins/platforms/wechat_uos
cp -r wechat_uos/* /root/.hermes/hermes-agent/plugins/platforms/wechat_uos/

# 安装依赖
pip install itchat-uos Pillow
```

### 环境变量

编辑 `/root/.hermes/.env`：

```env
WECHAT_UOS_ENABLED=true
WECHAT_UOS_QR_HTTP=true
WECHAT_UOS_QR_PORT=8646
```

可选：

```env
# 只允许指定群；为空表示不限制群
WECHAT_UOS_ALLOWED_GROUPS=
# 只允许指定用户
WECHAT_UOS_ALLOWED_USERS=
# 管理员名单
WECHAT_UOS_ADMIN_USERS=
# 是否响应私聊，默认 false
WECHAT_UOS_RESPOND_TO_DMS=false
```

### 扫码登录

启动后二维码保存到：
- `/root/.hermes/wechat_uos/itchat_qr.png`
- 或打开 `http://你的服务器IP:8646/`（每 3 秒刷新）

### 使用方式

在微信群 @ 登录的微信号：`@机器人 你好`

### 群内管理员命令

```
@机器人 授权 昵称或UserName
@机器人 取消授权 昵称或UserName
@机器人 设管理员 昵称或UserName
@机器人 取消管理员 昵称或UserName
@机器人 权限列表
```

---

## 📡 TG 频道 → 微信群 转发

监测 Telegram 频道最新消息，自动推送到指定微信群。支持文字 + 图片 + 视频 + 文件。

**独立 daemon，不走 Hermes，0 token 消耗。**

### 配置

```bash
cp ~/.hermes/scripts/tg_fwd_config.example.json ~/.hermes/data/tg_fwd/config.json
```

编辑 `config.json`：

```json
{
  "bot_token": "YOUR_BOT_TOKEN",
  "forward_rules": [
    {
      "tg_channel": "@your_channel",
      "wechat_groups": ["@@your_wechat_group_id"]
    }
  ]
}
```

### 获取 bot token

1. 打开 Telegram，找 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 创建新 bot
3. 把 bot 添加为频道管理员（发消息权限即可）

### 启动

```bash
cd /root/.hermes/hermes-agent
nohup ./venv/bin/python ~/.hermes/scripts/tg_channel_to_wechat.py \
    --config ~/.hermes/data/tg_fwd/config.json \
    --daemon > /tmp/tg_fwd.log 2>&1 &
```

### 开机自启

```bash
crontab -e
# 添加：
@reboot /root/.hermes/scripts/tg_fwd_daemon.sh
```

---

## 🔍 群聊盘搜（PanSou）

微信群聊中 `@机器人 搜索 xxx` → 自动调 PanSou API → 返回网盘资源链接。

**也独立于 Hermes，0 token 消耗。**

### 前置条件

需要本地运行 PanSou 服务（Docker 部署）：

```bash
docker run -d --name pansou -p 850:850 pansou/pansou-api
```

API 默认地址：`http://192.168.10.216:850/api/search`

### 启动

```bash
cd /root/.hermes/hermes-agent
nohup ./venv/bin/python ~/.hermes/scripts/pansou_bot.py \
    --daemon > /tmp/pansou_bot.log 2>&1 &
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PANSW_API` | `http://192.168.10.216:850/api/search` | PanSou API 地址 |
| `ITCHAT_PKL` | `~/.hermes/wechat_uos/itchat.pkl` | itchat 登录缓存路径 |
| `PANSW_PAGE_SIZE` | `300` | 每次获取的数据量 |

### 使用

微信群中发送（不需要 @）：

```
搜索 凡人修仙传
```

或 @bot：

```
@机器人 搜索 完美世界
```

### 终端测试

```bash
python3 ~/.hermes/scripts/pansou_bot.py --search "凡人修仙传"
```

---

## 📁 文件结构

```
wechat-uos-platform/
├── install.sh                          # 一键安装脚本
├── README.md
├── LICENSE
├── pyproject.toml
├── wechat_uos/                         # Hermes 插件
│   ├── __init__.py
│   ├── adapter.py
│   └── plugin.yaml
└── scripts/                            # 独立工具
    ├── tg_channel_to_wechat.py         # TG→微信转发 daemon
    ├── tg_fwd_daemon.sh                # TG 转发开机自启脚本
    ├── config.example.json             # TG 转发配置模板
    └── pansou_bot.py                   # 群聊盘搜 daemon
```

---

## 🔄 更新

```bash
cd /tmp/wechat-uos-platform
git pull
bash install.sh    # 自动覆盖更新
# 重新启动相应服务
```

---

## ❌ 卸载

```bash
# 移除 Hermes 插件
rm -rf /root/.hermes/hermes-agent/plugins/platforms/wechat_uos

# 从 .env 删除 WECHAT_UOS_* 配置

# 停止后台 daemon
pkill -f "tg_channel_to_wechat.*daemon"
pkill -f "pansou_bot.*daemon"
```

---

## License

MIT
