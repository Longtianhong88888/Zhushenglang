#!/usr/bin/env bash
# 每日自动更新流水线（腾讯云 cron 调用）：
#   更新行情 → 回测 → 财务评估 → 综合评分 → 跟踪报告 → 大牛模型 → 收盘确认推送
#   任一步失败 → 微信告警（企业微信优先，Server酱兜底）
#
# crontab 示例（每个交易日 17:30 执行，节假日由交易日历自动兜底）:
#   30 17 * * 1-5 /path/to/chaoduan/deploy/daily_update.sh >> /path/to/chaoduan/output/logs/cron.log 2>&1
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$APP_DIR/venv"
PY="$VENV/bin/python"
LOG_DIR="$APP_DIR/output/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily_$(date +%Y-%m-%d).log"

# 失败告警：流水线任一步失败（set -e 退出）→ 尾日志推微信
# chown 放这里：无论成败都执行（失败时 output/ 也会残留 root 属主文件，
# 若不清归 ubuntu 则 monitor 次日无法覆写 → 级联故障）
alert_fail() {
  rc=$?
  if [ -d "$APP_DIR/output" ]; then
    chown -R ubuntu:ubuntu "$APP_DIR/output" 2>/dev/null || true
  fi
  if [ $rc -ne 0 ]; then
    tail -25 "$LOG" 2>/dev/null | "$PY" -m mainrise.cli alert \
      "每日流水线失败(exit $rc) $(date '+%Y-%m-%d %H:%M')" \
      "$(tail -25 "$LOG" 2>/dev/null)" >/dev/null 2>&1 || true
  fi
  exit $rc
}
trap alert_fail EXIT

# 数据源已全部改为腾讯/东财公开接口，无需 API Key

# 交易日判断：非交易日（周末/节假日）直接跳过流水线，避免空转与误告警
TODAY="$(date +%Y-%m-%d)"
IS_TD="$LOG_DIR/.is_trade_day"
"$PY" - "$APP_DIR" "$TODAY" "$IS_TD" <<'EOF' >/dev/null 2>&1 || true
import sys
from pathlib import Path
root, today, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
tds = set()
for cand in (root / "data" / "trade_dates.csv",
             Path.home() / ".mainrise" / "data" / "trade_dates.csv"):
    if cand.exists():
        tds = {ln.strip() for ln in cand.read_text(encoding="utf-8").splitlines()
               if len(ln.strip()) == 10}
        break
out.write_text("1" if today in tds else "0")
EOF
if [ "$(cat "$IS_TD" 2>/dev/null || echo 1)" != "1" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') 非交易日（$TODAY），跳过每日流水线"
  exit 0
fi

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 开始 ====="
  cd "$APP_DIR"
  # 节奏控制（2026-08-15）：服务器 2C4G，全市场 bigbull 内存峰值 ~2.6G。
  # 任务串行 + 步间 sleep，让前序任务的内存完全释放再进入下一段，避免
  # 叠加峰值触发 OOM（monitor OOM 教训）。整体约 15-20 分钟，17:30 起
  # 时间充裕，不追求几分钟内完成。
  run() {  # 顺序执行一步 + 步间缓冲
    echo "--- $(date '+%H:%M:%S') $*"
    "$@"
    sleep 6
  }
  run "$PY" -m mainrise.cli update
  # 数据新鲜度校验（2026-08-15 起 16:00 跑）：zzshare 当日日线若尚未发布，
  # 最新文件日期 < 今天 → 告警并每 10 分钟重试（最多 6 次，1 小时窗口），
  # 避免基于昨日数据生成"今日"报告。
  FRESH_WAIT=0
  FRESH_ALERTED=0
  while [ $FRESH_WAIT -lt 6 ]; do
    LATEST_CSV=$(ls -1 "$APP_DIR"/data/zzshare_daily/[0-9]*.csv 2>/dev/null | tail -1)
    LATEST_DATE=$(basename "${LATEST_CSV:-none}" .csv)
    if [ "$LATEST_DATE" = "$(date +%Y%m%d)" ]; then
      echo "--- $(date '+%H:%M:%S') 数据新鲜度 OK（$LATEST_DATE）"
      break
    fi
    echo "--- $(date '+%H:%M:%S') 数据未发布：最新 $LATEST_DATE，等 10 分钟重试（第 $((FRESH_WAIT+1))/6 次）"
    if [ $FRESH_ALERTED -eq 0 ]; then
      "$PY" -m mainrise.cli alert "行情数据未发布" \
        "最新日线 ${LATEST_DATE:-无}，期待 $(date +%Y%m%d)；每10分钟重试" >/dev/null 2>&1 || true
      FRESH_ALERTED=1
    fi
    sleep 600
    FRESH_WAIT=$((FRESH_WAIT + 1))
    "$PY" -m mainrise.cli update
  done
  if [ "$(ls -1 "$APP_DIR"/data/zzshare_daily/[0-9]*.csv 2>/dev/null | tail -1 | xargs basename .csv)" != "$(date +%Y%m%d)" ]; then
    echo "--- $(date '+%H:%M:%S') ⚠ 数据 1 小时内仍未发布，继续（可能数据源异常）"
  fi
  run "$PY" -m mainrise.cli backtest
  run "$PY" -m mainrise.cli evaluate
  run "$PY" -m mainrise.cli report
  run "$PY" -m mainrise.cli track --no-dashboard   # 不自动生成旧 Excel/网页仪表盘
  run "$PY" -m mainrise.cli cycle-state    # 市场周期状态卡（JSON + cycle.html，门户注入）
  run "$PY" -m mainrise.cli industry-trend # 产业景气度卡（单季净利同比+业绩预告，门户注入）
  run "$PY" -m mainrise.cli price-watch    # 供需涨价监控（期货见顶回落→有色标签）
  run "$PY" -m mainrise.cli price-events   # 产业链涨价追踪（存储/覆铜板/MLCC标签）
  run "$PY" -m mainrise.cli m5data         # 5分钟K线归档（腾讯m5→data/m5daily，逐日积累供月度优化）
  # 全市场 bigbull：内存峰值 ~2.6G，前后各加长缓冲（30s）等前序内存释放
  echo "--- $(date '+%H:%M:%S') sleep 30s 等内存释放（bigbull 前置）"
  sleep 30
  run "$PY" -m mainrise.cli bigbull        # 门户（index.html）只由大牛模型生成（含周期/景气度卡）
  sleep 30   # bigbull 后置缓冲：等 RSS 回落再进下一任务
  run "$PY" -m mainrise.cli ignite5 --report  # 当日盘中点火信号汇总报告
  run "$PY" -m mainrise.cli push --close   # 17:30 收盘确认推送（读 bigbull 交割单）
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 完成 ====="
} >> "$LOG" 2>&1
