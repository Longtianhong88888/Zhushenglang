#!/usr/bin/env bash
# 本机执行：把全量行情 + 股票名册 + 交易日历 + 持仓/观察池打成 tar.gz，
# 上传到服务器后解压即用，服务器无需重新下载 650MB 全量数据。
#
# 用法:
#   ./deploy/package_data.sh
#   scp build/server_data.tar.gz ubuntu@<服务器IP>:~/chaoduan/
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$APP_DIR/build/server_data.tar.gz"

if [ ! -d "$APP_DIR/data/zzshare_daily" ]; then
  echo "未找到 data/zzshare_daily，请确认在项目根目录运行" >&2
  exit 1
fi

mkdir -p "$APP_DIR/build"
cd "$APP_DIR"
echo "打包全量行情（约 650MB，压缩需几分钟）..."
tar -czf "$OUT" \
  data/zzshare_daily \
  data/stock_list.csv \
  data/trade_dates.csv \
  output/state

SIZE="$(du -h "$OUT" | cut -f1)"
echo "已打包: $OUT（$SIZE）"
echo ""
echo "下一步上传到服务器并在项目根目录解压:"
echo "  scp $OUT ubuntu@<服务器IP>:~/chaoduan/"
echo "  cd ~/chaoduan && tar -xzf server_data.tar.gz"
echo "之后 mainrise init/update 只会做增量补齐，不再下载全量。"
