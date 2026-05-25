#!/bin/bash
# Glink Daemon 启动脚本 — v0.5
# 自动检测 pidfile，支持 --serve 模式

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$BASE_DIR/.glink-daemon.pid"
LOG="$BASE_DIR/glink-daemon-v0.5.log"
DAEMON="$BASE_DIR/glink-daemon.py"
PROJECT="${1:-testglink}"
MODE="${2:-}"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 检查是否有旧进程
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo -e "${YELLOW}⚠ 已有 Glink daemon 在运行 (pid=$OLD_PID)${NC}"
        echo "   重启: kill $OLD_PID && $0 $PROJECT"
        exit 0
    else
        echo -e "${YELLOW}⚠ 清理过期 pidfile (pid=$OLD_PID)${NC}"
        rm -f "$PIDFILE"
    fi
fi

# 启动
echo -e "${GREEN}🚀 启动 Glink Daemon v0.5${NC}"
echo "   项目: $PROJECT"
echo "   日志: $LOG"
echo "   Daemon: $DAEMON"

cd "$BASE_DIR" || exit 1

if [ "$MODE" = "--serve" ]; then
    echo -e "${GREEN}   模式: serve-only (只启动 API)${NC}"
    nohup python3 "$DAEMON" "$PROJECT" --serve > "$LOG" 2>&1 &
else
    echo -e "${GREEN}   模式: 完整模式 (工作流 + API)${NC}"
    nohup python3 "$DAEMON" "$PROJECT" > "$LOG" 2>&1 &
fi

PID=$!
echo $PID > "$PIDFILE"
echo -e "${GREEN}✅ 已启动 (pid=$PID)${NC}"
echo "   查看日志: tail -f $LOG"
echo "   停止: kill $PID"
echo "   API: http://127.0.0.1:8426/health"
