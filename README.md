# WeChat UOS Platform for Hermes Agent

Hermes Agent 个人微信平台插件，基于 `itchat-uos` 通过 UOS Web WeChat 协议登录真实微信号，把微信群里的 `@机器人` 文本消息转发给 Hermes Gateway，并把回复发回群聊。

> ⚠️ 注意：`itchat-uos` 属于逆向协议，可能随时被微信限制或失效。建议使用小号，不建议用主力微信号。

## 功能

- 微信扫码登录
- 群聊 @ 机器人触发回复
- 可选私聊回复
- 支持用户/群白名单
- 支持群内管理员命令维护名单
- 自动生成二维码图片和二维码 HTTP 页面

## 文件结构

```text
wechat_uos/
├── __init__.py
├── adapter.py
└── plugin.yaml
```

## 安装

假设 Hermes Agent 安装在：

```bash
/root/.hermes/hermes-agent
```

### 1. 安装依赖

```bash
cd /root/.hermes/hermes-agent
venv/bin/pip install itchat-uos Pillow
```

### 2. 下载插件

```bash
cd /tmp
git clone https://github.com/gdjbdg5467/wechat-uos-platform.git
```

把 `gdjbdg5467` 替换成你的 GitHub 用户名。

### 3. 复制插件到 Hermes

```bash
mkdir -p /root/.hermes/hermes-agent/plugins/platforms/wechat_uos
cp -r /tmp/wechat-uos-platform/wechat_uos/* \
  /root/.hermes/hermes-agent/plugins/platforms/wechat_uos/
```

### 4. 配置环境变量

编辑：

```bash
nano /root/.hermes/.env
```

添加：

```env
WECHAT_UOS_ENABLED=true
WECHAT_UOS_QR_HTTP=true
WECHAT_UOS_QR_PORT=8646
```

可选配置：

```env
# 只允许指定群；为空表示不限制群
WECHAT_UOS_ALLOWED_GROUPS=

# 只允许指定用户；建议填日志里的真实 UserName，例如 @93cd...
WECHAT_UOS_ALLOWED_USERS=

# 管理员名单；可用群内命令授权/取消授权
WECHAT_UOS_ADMIN_USERS=

# 是否响应私聊，默认 false
WECHAT_UOS_RESPOND_TO_DMS=false
```

## 启动/重启 Gateway

```bash
cd /root/.hermes/hermes-agent
venv/bin/python -m hermes_cli.main gateway run --replace
```

如果你已经配置成服务运行，重启服务即可。

## 扫码登录

启动后二维码会保存到：

```text
/root/.hermes/wechat_uos/itchat_qr.png
```

如果开启了 `WECHAT_UOS_QR_HTTP=true`，也可以打开：

```text
http://你的服务器IP:8646/
```

页面会每 3 秒刷新二维码。

公网/局域网访问前，确认防火墙放行端口：

```bash
ufw allow 8646/tcp
```

## 使用方式

在微信群里 @ 登录的微信号并发送文字：

```text
@机器人 你好
```

插件只处理文本消息：图片、语音、表情、文件不会触发。

## 群内管理员命令

在群里 @ 机器人后发送：

```text
授权 昵称或UserName
取消授权 昵称或UserName
设管理员 昵称或UserName
取消管理员 昵称或UserName
权限列表
```

建议最终使用真实 UserName 授权，而不是昵称。因为 Hermes Gateway 授权层按 `user_id` 判断。

查看真实 UserName 的方法：先临时放开或让用户发送测试消息，然后查看日志：

```bash
grep -E 'WeChatUOS: @ from|Unauthorized user' /root/.hermes/logs/gateway.log | tail -50
```

示例：

```text
Unauthorized user: @93cd48b77e61676027744fc999412758 (夢魚) on wechat_uos
```

则配置：

```env
WECHAT_UOS_ALLOWED_USERS=@93cd48b77e61676027744fc999412758
WECHAT_UOS_ADMIN_USERS=@93cd48b77e61676027744fc999412758
```

重启 Gateway 后生效。

## 常见问题

### 1. 扫码后没登录成功

看日志：

```bash
grep -E 'WeChatUOS QR|login successful|logged out|listener crashed' \
  /root/.hermes/logs/gateway.log | tail -80
```

状态含义：

- `status=0`：新二维码
- `status=201`：已扫码，等待手机确认
- `status=200`：确认登录
- `status=408`：等待扫码超时
- `status=400`：二维码失效/登录流程失败

### 2. 群里发了消息没有回复

先确认是否收到了消息：

```bash
grep -E 'WeChatUOS: @ from|Unauthorized user|response ready|Sending response' \
  /root/.hermes/logs/gateway.log | tail -100
```

如果出现：

```text
Unauthorized user: xxx (昵称) on wechat_uos
```

说明被 Hermes Gateway 授权层拦截，把 `xxx` 加到：

```env
WECHAT_UOS_ALLOWED_USERS=xxx
```

然后重启 Gateway。

### 3. 二维码过期

等插件生成新的二维码，或重启 Gateway 重新生成。

### 4. 不支持普通微信群？

这个插件使用 `itchat-uos`，是个人微信 Web/UOS 协议路径，能处理个人微信号所在群的 @ 文本消息。但协议稳定性取决于微信服务端。

## 卸载

```bash
rm -rf /root/.hermes/hermes-agent/plugins/platforms/wechat_uos
```

并从 `/root/.hermes/.env` 删除 `WECHAT_UOS_*` 配置后重启 Gateway。

## License

MIT
