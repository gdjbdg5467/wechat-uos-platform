#!/bin/bash
# ====================================================================
# WeChat UOS Platform — 一键安装/更新脚本
# 包含: Hermes 插件 + TG→微信转发 + 群聊盘搜
# ====================================================================
set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/hermes-agent}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DST="$HERMES_HOME/plugins/platforms/wechat_uos"
LOG_FILE="/tmp/wechat_uos_install.log"

echo "╔══════════════════════════════════════════════╗"
echo "║   WeChat UOS Platform 一键安装/更新          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Hermes Home: $HERMES_HOME"
echo ""

# ------------------------------------------------------------------
# 1. 安装 Python 依赖
# ------------------------------------------------------------------
echo ">>> [1/5] 安装 Python 依赖..."

if [ -f "$HERMES_HOME/venv/bin/pip" ]; then
    PIP="$HERMES_HOME/venv/bin/pip"
elif [ -f "$HERMES_HOME/.venv/bin/pip" ]; then
    PIP="$HERMES_HOME/.venv/bin/pip"
else
    PIP="pip3"
fi

$PIP install itchat-uos Pillow requests 2>&1 | tail -3
echo "  ✅ 依赖安装完成"

# ------------------------------------------------------------------
# 2. 安装 Hermes UOS 插件
# ------------------------------------------------------------------
echo ">>> [2/5] 安装 WeChat UOS 插件..."

if [ ! -d "$HERMES_HOME/plugins/platforms" ]; then
    mkdir -p "$HERMES_HOME/plugins/platforms"
fi

# 如果已有则备份
if [ -f "$PLUGIN_DST/adapter.py" ]; then
    cp "$PLUGIN_DST/adapter.py" "$PLUGIN_DST/adapter.py.bak.$(date +%s)"
    echo "  → 已有 adapter.py 已备份"
fi

mkdir -p "$PLUGIN_DST"
cp "$SCRIPT_DIR/wechat_uos/"* "$PLUGIN_DST/"
echo "  ✅ UOS 插件安装完成"

# ------------------------------------------------------------------
# 3. 安装脚本到 ~/.hermes/scripts/
# ------------------------------------------------------------------
echo ">>> [3/5] 安装辅助脚本..."

SCRIPTS_DST="$HOME/.hermes/scripts"
mkdir -p "$SCRIPTS_DST"

# TG 转发
cp "$SCRIPT_DIR/scripts/tg_channel_to_wechat.py" "$SCRIPTS_DST/"
cp "$SCRIPT_DIR/scripts/tg_fwd_daemon.sh" "$SCRIPTS_DST/"
chmod +x "$SCRIPTS_DST/tg_fwd_daemon.sh"

# 群聊盘搜
cp "$SCRIPT_DIR/scripts/pansou_bot.py" "$SCRIPTS_DST/"
chmod +x "$SCRIPTS_DST/pansou_bot.py"

# 配置模板
cp "$SCRIPT_DIR/scripts/config.example.json" "$SCRIPTS_DST/tg_fwd_config.example.json"

echo "  ✅ 脚本安装完成"

# ------------------------------------------------------------------
# 4. 配置环境变量（如果不存在）
# ------------------------------------------------------------------
echo ">>> [4/5] 检查环境变量配置..."

ENV_FILE="$HOME/.hermes/.env"
if [ ! -f "$ENV_FILE" ]; then
    touch "$ENV_FILE"
fi

add_env_if_missing() {
    local key="$1"
    local val="$2"
    if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        echo "${key}=${val}" >> "$ENV_FILE"
        echo "  → 添加 $key=$val"
    fi
}

add_env_if_missing "WECHAT_UOS_ENABLED" "true"
add_env_if_missing "WECHAT_UOS_QR_HTTP" "true"
add_env_if_missing "WECHAT_UOS_QR_PORT" "8646"

echo "  ✅ 环境变量配置完成"

# ------------------------------------------------------------------
# 5. 输出后续步骤
# ------------------------------------------------------------------
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   安装完成！后续操作                         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "▶ 启动/重启 Gateway 使插件生效："
echo "   cd $HERMES_HOME"
echo "   venv/bin/python -m hermes_cli.main gateway run --replace"
echo ""
echo "▶ 扫码登录："
echo "   访问 http://你的IP:8646/ 扫描二维码"
echo ""
echo "▶ 群聊盘搜："
echo "   微信群 @机器人 搜索 xxx"
echo ""
echo "▶ TG 频道转发（可选）："
echo "   1. 创建 config:  cp $SCRIPTS_DST/tg_fwd_config.example.json"
echo "                     $SCRIPTS_DST/tg_fwd_config.json"
echo "   2. 编辑 config，填入 bot token 和转发规则"
echo "   3. 启动: nohup python3 $SCRIPTS_DST/tg_channel_to_wechat.py"
echo "           --config $SCRIPTS_DST/tg_fwd_config.json"
echo "           --daemon > /tmp/tg_fwd.log 2>&1 &"
echo ""