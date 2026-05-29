#!/bin/bash
# Start TG→WeChat forwarding daemon (long-polling, real-time)
# Called from cron @reboot or systemd

cd /root/.hermes/hermes-agent || exit 1

PIDFILE=/tmp/tg_fwd_daemon.pid
LOGFILE=/tmp/tg_fwd.log

# Check if already running
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Daemon already running (PID $OLD_PID)"
        exit 0
    fi
fi

nohup ./venv/bin/python /root/.hermes/scripts/tg_channel_to_wechat.py \
    --config /root/.hermes/data/tg_fwd/config.json \
    --daemon >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
echo "Started daemon, PID $(cat $PIDFILE)"
