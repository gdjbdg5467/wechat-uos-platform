# WeChat Services — Hermes Agent 插件集

微信生态功能插件合集，作为 [Hermes Agent](https://hermes-agent.nousresearch.com) 的插件运行。

## 安装

```bash
cd ~/.hermes/hermes-agent/plugins/platforms/
git clone https://github.com/gdjbdg5467/wechat-services.git wechat_uos
```

然后在 `~/.hermes/config.yaml` 中添加：

```yaml
plugins:
  - platform: wechat_uos
    enabled: true
env:
  WECHAT_UOS_ENABLED: true
```

## WeChat UOS Platform

基于 itchat-uos（逆向 UOS 微信协议）的个人微信网关适配器。支持 QR 扫码登录、热重载、群 @ 消息处理。

插件路径：`plugins/platforms/wechat_uos/`

### 功能模块

#### 微信登录
QR 码扫码登录（支持 HTTP 页面展示二维码），热重载（自动重启无需重新扫码），可配置静默启动防重播。

#### 群授权系统
三级权限体系：全局管理员 → 群管理员 → 授权用户。群首次 @ 机器人并发送"开启授权"即完成授权，GID 在 itchat 重连后自动迁移，无需重新配置。

#### 盘搜
夸克/115/百度/UC网盘资源搜索。群内发送 `@机器人 搜索 <关键词>` 即可。管理员可单独开关此功能。

#### TG 转发
Telegram 频道消息自动转发到指定微信群。支持文字、图片、视频、文件。管理员可单独开关。自动添加新授权群到转发配置。

#### CFTC 上传
群内发送图片/文件后发送 `@机器人 上传` 自动上传到 CFTC 图床。管理员可单独开关。

#### LSPosed 模块更新追踪
监控 Xposed 模块市场及自定义 GitHub 仓库，有新版本时自动推送到微信群。管理员可单独开关。

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `WECHAT_UOS_ENABLED` | 启用插件 | `false` |
| `WECHAT_UOS_ALLOWED_GROUPS` | 允许的群组 ID/名称，逗号分隔 | 空（所有群） |
| `WECHAT_UOS_ALLOWED_USERS` | 允许的用户昵称，逗号分隔 | 空（所有用户） |
| `WECHAT_UOS_RESPOND_TO_DMS` | 是否响应私聊 | `false` |
| `WECHAT_UOS_QR_HTTP` | 是否启动 QR HTTP 服务 | `true` |
| `WECHAT_UOS_QR_PORT` | QR HTTP 端口 | `8646` |
| `WECHAT_UOS_HOME_CHANNEL` | 默认通知群组 | 空 |

### 群聊命令

发送 `@机器人` + 命令：

#### 授权管理
- `开启授权` — 授权本群，首次发送者自动成为管理员
- `关闭授权` — 关闭本群授权（仅管理员）
- `授权 昵称` — 授权成员使用盘搜（仅管理员）
- `取消授权 昵称` — 取消成员权限（仅管理员）
- `权限列表` — 查看本群权限
- `刷新成员` — 刷新群成员缓存

#### 盘搜
- `搜索 <关键词>` — 搜索网盘资源
- `开启盘搜` / `关闭盘搜` — 盘搜开关（仅管理员）

#### TG 转发
- `开启转发` / `关闭转发` — TG 转发开关（仅管理员）

#### CFTC 上传
- `上传` — 上传最新媒体到图床
- `开启上传` / `关闭上传` — 图床上传开关（仅管理员）

#### LSPosed 模块更新
- `开启更新` / `关闭更新` — 模块更新推送开关（仅管理员）
- `模块更新状态` — 查看当前跟踪状态

#### 其他
- `帮助` — 显示帮助菜单

### 数据文件

保存在 `~/.hermes/data/wechat_uos/` 目录下：

| 文件 | 说明 |
|------|------|
| `acl.json` | 群授权记录（含成员缓存） |
| `itchat.pkl` | 微信热登录状态 |
| `itchat_qr.png` | 最新登录二维码 |
| `tg_fwd/config.json` | TG 转发配置 |
| `tg_fwd/group_state.json` | 各群转发开关状态 |
| `lsposed/config.json` | LSPosed 模块追踪配置 |
| `lsposed/state.json` | 模块版本状态记录 |
| `cftc_media/` | CFTC 上传缓存 |

请定期备份 `acl.json` 和 `itchat.pkl`。