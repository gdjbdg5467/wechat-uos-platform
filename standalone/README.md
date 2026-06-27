# WxWeChatBot - 独立版微信机器人

基于 itchat-uos 的微信个人号机器人，支持多模块扩展：内容搜索、Telegram 转发、抖音解析、RSS 订阅等。

---

## 简介

WxWeChatBot 是一个运行在 Linux 环境下的微信个人号机器人。通过模拟 UOS 协议登录微信，实现群聊消息自动回复、内容搜索、消息转发等功能。支持 Docker 部署，配置灵活。

### 主要特性

- 基于 itchat-uos 协议登录
- 插件化模块架构
- 支持 Docker 一键部署
- 多管理员权限控制
- 可扩展的转发规则引擎

---

## 快速开始（直接安装）

### 前置要求

- Python 3.9+
- pip

### 安装步骤

1. **克隆仓库**

```bash
git clone https://github.com/your-repo/wx-wechat-bot.git
cd wx-wechat-bot/standalone
```

2. **安装依赖**

```bash
pip install -r requirements.txt
```

3. **创建配置文件**

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml 填写必要的配置项
```

4. **启动机器人**

```bash
python main.py
```

首次启动会生成二维码，使用微信扫描即可登录。

---

## Docker 部署

### 前置要求

- Docker
- Docker Compose（可选）

### 使用 Docker 构建并运行

```bash
cd standalone

# 创建配置文件
cp config.example.yaml config.yaml
# 编辑 config.yaml 填写配置

# 构建并启动
docker build -t wxbot .
docker run -d \
  --name wxbot \
  -v $(pwd)/state:/app/state \
  -v $(pwd)/config.yaml:/app/config.yaml \
  --restart always \
  wxbot
```

### 使用 Docker Compose

```bash
cd standalone
cp config.example.yaml config.yaml
# 编辑 config.yaml 填写配置
docker-compose up -d
```

### 数据持久化

- `./state` 目录：存储登录状态、QR 码、缓存数据
- `./config.yaml`：配置文件（需自行创建）

---

## 配置说明

### 基础配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `data_dir` | 数据存储目录 | `./state` |

### 微信配置 (`wechat`)

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `hotReload` | 是否启用热重载（复用登录状态） | `true` |
| `qr_port` | 二维码 HTTP 服务端口 | `8080` |
| `qr_http` | 是否通过 HTTP 展示二维码 | `true` |

### 管理员配置 (`admin`)

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `super_admin_names` | 超级管理员微信昵称列表 | `["夢魚", "庾梦"]` |

### 盘搜模块 (`pansou`)

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_url` | 盘搜 API 地址 | `http://192.168.10.216:850/api/search` |
| `allowed_types` | 允许搜索的资源类型 | `["video", "image", "article"]` |

### Telegram 转发 (`tg_fwd`)

| 配置项 | 说明 |
|--------|------|
| `bot_token` | Telegram Bot Token |
| `forward_rules` | 转发规则列表 |

### CFTC 模块 (`cftc`)

| 配置项 | 说明 |
|--------|------|
| `api_url` | CFTC API 地址 |
| `credentials.api_key` | API 密钥 |
| `credentials.secret` | API 密钥 |

### LSPosed 模块 (`lsposed`)

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enabled` | 是否启用 | `false` |
| `interval_seconds` | 轮询间隔（秒） | `300` |
| `target_groups` | 监听的目标群聊列表 | `[]` |
| `custom_repos` | 自定义仓库列表 | `[]` |
| `web_sources` | 网页源列表 | `[]` |

### 抖音模块 (`douyin`)

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_base` | 抖音 API 基础地址 | `https://api.douyin.com` |

### WeRSS 模块 (`werss`)

| 配置项 | 说明 |
|--------|------|
| `base_url` | WeRSS 服务地址 |
| `username` | 登录用户名 |
| `password` | 登录密码 |

---

## 功能列表

- [x] 微信 UOS 协议登录（扫码/热重载）
- [x] 群聊消息自动回复
- [x] 盘搜网盘资源搜索
- [x] 抖音视频解析
- [x] Telegram 消息双向转发
- [x] LSPosed 模块更新检测
- [x] RSS 订阅推送
- [x] 多管理员权限控制
- [ ] 插件热加载（开发中）
- [ ] Web 管理面板（计划中）

---

## License

MIT
