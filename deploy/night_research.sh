#!/usr/bin/env bash
# 夜间研究任务（周日 22:30 cron 调用）：把重研究全部放到服务器跑，
# 本机不占资源；产物落 output/reports（部分研究同时归档 docs/）。
#
# crontab 示例:
#   30 22 * * 0 /path/to/chaoduan/deploy/night_research.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$APP_DIR/venv/bin/python"
LOG_DIR="$APP_DIR/output/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/night_$(date +%Y-%m-%d).log"

RESEARCH_CMDS="entry-study bigtrend wave bullcnt candidate-bt reversal launch strategy"
# 总超时 45 分钟（8 项研究网络抖动时防止无限阻塞周日 cron）
TOTAL_TIMEOUT=2700

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 夜间研究开始 ====="
  cd "$APP_DIR"
  FAILED=""
  for cmd in $RESEARCH_CMDS; do
    echo "===== $(date '+%H:%M:%S') $cmd ====="
    if ! timeout "$TOTAL_TIMEOUT" "$PY" -m mainrise.cli "$cmd"; then
      echo "[$cmd 失败/超时]"
      FAILED="$FAILED $cmd"
    fi
  done
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 完成，失败:${FAILED:-无} ====="
  # cron 以 root 运行，产物归 ubuntu
  chown -R ubuntu:ubuntu "$APP_DIR/output" "$APP_DIR/docs" 2>/dev/null || true
} >> "$LOG" 2>&1

# 只在有失败时告警（成功不占 Server酱 5 次/天配额；周报/巡检已覆盖正常状态）
if [ -n "${FAILED:-}" ]; then
  "$PY" -m mainrise.cli alert "夜间研究任务有失败 $(date '+%m-%d')" \
    "失败命令:${FAILED}，见日志 $LOG" >/dev/null 2>&1 || true
fi
