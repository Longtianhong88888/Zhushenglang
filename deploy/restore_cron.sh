#!/usr/bin/env bash
# 一键恢复 mainrise 全部 cron 定时任务（2026-08-16 审计：8/15 重装后任务丢失，
# 14:50 push / 17:30 daily / 17:35 weekly 均未触发，故从 setup_server.sh 提取本段）。
#
# 用法（必须 root，与 setup_server.sh 一致）:
#   sudo bash deploy/restore_cron.sh
set -euo pipefail

APP_DIR="${MAINRISE_APP_DIR:-/home/ubuntu/chaoduan}"
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行（sudo bash $0）" >&2; exit 1; }
[ -d "$APP_DIR" ] || { echo "未找到项目目录: $APP_DIR" >&2; exit 1; }
mkdir -p "$APP_DIR/output/logs"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "恢复 mainrise 定时任务（daily/推送/巡检/周报/夜间研究/点火/月度优化）"
CRON_LINES=(
  "30 17 * * 1-5 $APP_DIR/deploy/daily_update.sh >> $APP_DIR/output/logs/cron.log 2>&1"
  "50 14 * * 1-5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli push >> $APP_DIR/output/logs/cron.log 2>&1"
  "5 9 * * 1-5 $APP_DIR/deploy/health_check.sh >> $APP_DIR/output/logs/cron.log 2>&1"
  "35 17 * * 5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli weekly >> $APP_DIR/output/logs/cron.log 2>&1"
  "30 22 * * 0 $APP_DIR/deploy/night_research.sh >> $APP_DIR/output/logs/cron.log 2>&1"
  "35,45,55 9-11 * * 1-5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli ignite5 >> $APP_DIR/output/logs/cron.log 2>&1"
  "5,15,25,35,45,55 13-14 * * 1-5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli ignite5 >> $APP_DIR/output/logs/cron.log 2>&1"
  "10 18 28-31 * * [ \"\$(date -d tomorrow +%d)\" = \"01\" ] && cd $APP_DIR && ./venv/bin/python -m mainrise.cli m5optimize >> $APP_DIR/output/logs/cron.log 2>&1"
)
( crontab -l 2>/dev/null | grep -vE 'daily_update\.sh|mainrise\.cli push|health_check\.sh|mainrise\.cli weekly|night_research\.sh|mainrise\.cli ignite5|mainrise\.cli m5optimize' \
  ; printf '%s\n' "${CRON_LINES[@]}" ) | crontab -

echo ""
echo "已安装的 root crontab（mainrise 相关）:"
crontab -l | grep -E 'daily_update|mainrise' || echo "  （无 mainrise 任务！）"
echo ""
echo "✅ 完成。周一 09:05 健康巡检 / 14:50 推送 / 17:30 收盘流水线将自动触发。"
