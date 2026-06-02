#!/bin/bash
# Glink Daemon 自检脚本 — 用于 cron 定时检查
# 建议 cron: */5 * * * * ~/glink/bin/glink-healthcheck.sh

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$BASE_DIR/.glink-daemon.pid"
LOG="$BASE_DIR/glink-daemon-v0.5.log"
DAEMON="$BASE_DIR/glink-daemon.py"
BOOT_TS="$BASE_DIR/.glink-boot.ts"

# 检查端口 8426
if lsof -ti :8426 > /dev/null 2>&1; then
    # API 端口存活 — daemon 正常运行
    # 更新 pidfile（如果进程重启了但 pidfile 没更新）
    ACTUAL_PID=$(lsof -ti :8426 | head -1)
    if [ -f "$PIDFILE" ] && [ "$(cat "$PIDFILE")" != "$ACTUAL_PID" ]; then
        echo "$ACTUAL_PID" > "$PIDFILE"
        echo "$(date '+%H:%M:%S') pidfile 更新: $ACTUAL_PID" >> "$BASE_DIR/.glink-healthcheck.log"
    fi
    exit 0
fi

# 端口 8426 死了 — 需要重启
echo "$(date '+%H:%M:%S') ⚠ Daemon 不在线，准备重启" >> "$BASE_DIR/.glink-healthcheck.log"

# 如果 pidfile 存在但进程死了，清理
if [ -f "$PIDFILE" ]; then
    rm -f "$PIDFILE"
fi

# 读取上次的项目名（从日志最后出现的项目名）
PROJECT=$(grep -oP '(?<=项目: )\w+' "$LOG" 2>/dev/null | tail -1)
PROJECT="${PROJECT:-testglink}"

# 告警：通知主人
if [ -n "${GLINK_ALERT_WEBHOOK:-}" ]; then
curl -s -m 5 -X POST -H "Content-Type: application/json" \
  -d "$(cat <<EOF
{
  "msg_type": "interactive",
  "card": {
    "header": {
      "title": {"tag": "plain_text", "content": "⚠️ Glink Daemon 已离线，正在自动恢复"},
      "template": "red"
    },
    "elements": [
      {"tag": "markdown", "content": "**项目**: $PROJECT\n**主机**: $(hostname)\n**操作**: 自动重启 daemon"},
      {"tag": "hr"},
      {"tag": "note", "elements": [{"tag": "plain_text", "content": "Glink Healthcheck | $(date '+%Y-%m-%d %H:%M:%S')"}]}
    ]
  }
}
EOF
)" "$GLINK_ALERT_WEBHOOK" >/dev/null 2>&1
fi

# 启动
cd "$BASE_DIR" || exit 1
nohup python3 "$DAEMON" "$PROJECT" >> "$LOG" 2>&1 &
NEW_PID=$!
echo $NEW_PID > "$PIDFILE"
echo "$(date '+%H:%M:%S') ✅ Daemon 已重启 (pid=$NEW_PID，项目=$PROJECT)" >> "$BASE_DIR/.glink-healthcheck.log"
