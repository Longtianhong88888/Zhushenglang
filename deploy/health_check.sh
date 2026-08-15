#!/usr/bin/env bash
# 每日健康巡检（交易日 09:05 cron 调用，节假日本身会静默）：
#   monitor 服务 / 数据新鲜度（昨日流水线是否产出）/ 磁盘 / 内存
#   任一异常 → 微信告警（企业微信优先，Server酱兜底）
#
# crontab 示例:
#   5 9 * * 1-5 /path/to/chaoduan/deploy/health_check.sh >> /path/to/chaoduan/output/logs/cron.log 2>&1
set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$APP_DIR/venv/bin/python"
STATE="$APP_DIR/output/state/bigbull_cands.json"
LOG_DIR="$APP_DIR/output/logs"
mkdir -p "$LOG_DIR"

WARN=()

# 1) monitor 服务
if ! systemctl is-active --quiet mainrise-monitor; then
  WARN+=("❌ monitor 盯盘服务未运行")
fi

# 2) 数据新鲜度：今天若是交易日，昨日收盘流水线应产出 昨天 的数据
if [ -f "$STATE" ]; then
  UPD="$("$PY" -c "import json;print(json.load(open('$STATE'))['updated'])" \
    2>/dev/null || echo bad-file)"
  FRESH="$("$PY" - "$APP_DIR" "$UPD" <<'EOF'
import sys
from datetime import date
from pathlib import Path
root = Path(sys.argv[1])
upd = sys.argv[2]
tds = set()
for cand in (root / "data" / "trade_dates.csv",
             Path.home() / ".mainrise" / "data" / "trade_dates.csv"):
    if cand.exists():
        tds = {ln.strip() for ln in cand.read_text(encoding="utf-8").splitlines()
               if len(ln.strip()) == 10}
        break
today = date.today().isoformat()
if today in tds:                       # 今天是交易日 → 预期数据到上一交易日
    # 取今天之前最近的交易日（跳过周末/节假日；周一预期上周五）
    prev = [d for d in sorted(tds) if d < today]
    if not prev:
        print("skip")                  # 数据不足无法判断
    else:
        exp = prev[-1]
        print("stale" if upd < exp else "ok")
else:
    print("skip")                      # 非交易日不查
EOF
)"
  case "$FRESH" in
    stale) WARN+=("❌ 行情数据停留在 ${UPD}（昨日 17:30 流水线可能失败，见 output/logs/daily_*.log）") ;;
    bad-file) WARN+=("❌ bigbull_cands.json 无法解析") ;;
  esac
else
  WARN+=("❌ 缺少 $STATE（大牛模型从未跑过？）")
fi

# 3) 磁盘
USED="$(df / | awk 'NR==2{gsub("%","",$5); print $5}')"
if [ "${USED:-0}" -ge 85 ]; then
  WARN+=("❌ 磁盘使用 ${USED}%（≥85%）")
fi

# 4) 内存
AVAIL="$(free -m | awk '/Mem:/{print $7}')"
if [ "${AVAIL:-0}" -lt 300 ]; then
  WARN+=("❌ 可用内存仅 ${AVAIL}MB（<300MB）")
fi

if [ "${#WARN[@]}" -gt 0 ]; then
  MSG="$(printf '%s\n' "${WARN[@]}")"
  "$PY" -m mainrise.cli alert "健康巡检告警 $(date '+%Y-%m-%d %H:%M')" "$MSG" \
    >/dev/null 2>&1 || true
  echo "健康巡检异常:"; printf '%s\n' "${WARN[@]}"
else
  echo "$(date '+%Y-%m-%d %H:%M') 健康巡检正常（服务/数据/磁盘/内存）"
fi
