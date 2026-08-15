#!/usr/bin/env bash
# 服务器一键初始化（以 root 在服务器上运行一次；也可由本机远程执行）。
# 负责：系统依赖 + 4G swap + venv + 依赖安装 + 数据包解压 + 手动流水线
#       + cron 定时 + Nginx 托管 + 网页口令。
#
# 用法:
#   bash deploy/setup_server.sh                 # 全流程（含手动跑一次流水线 ~15-30 分钟）
#   bash deploy/setup_server.sh --skip-pipeline # 只装环境/定时/网页，流水线稍后自己跑
#   MAINRISE_WEB_USER=abc MAINRISE_WEB_PASS=xxx bash deploy/setup_server.sh
set -euo pipefail

# 统一部署路径 /home/ubuntu/chaoduan（与 mainrise-monitor.service / README 一致；
# /root 0700 会致 nginx www-data 403 全站不可访问）
APP_DIR="${MAINRISE_APP_DIR:-/home/ubuntu/chaoduan}"
WEB_USER="${MAINRISE_WEB_USER:-zhushang}"
SKIP_PIPELINE=0
[[ "${1:-}" == "--skip-pipeline" ]] && SKIP_PIPELINE=1

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行（或 sudo bash $0）" >&2; exit 1; }
[ -d "$APP_DIR" ] || { echo "未找到项目目录: $APP_DIR" >&2; exit 1; }

# 1. 系统依赖 + swap
say "安装系统依赖（nginx / python3-venv / apache2-utils）"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-venv python3-pip nginx apache2-utils openssl curl

if ! swapon --show | grep -q '/swapfile'; then
  say "创建 4G 交换分区"
  fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. venv + 依赖
say "创建 venv 并安装依赖（requirements-server.txt）"
cd "$APP_DIR"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements-server.txt

# 3. 数据包解压（若已上传）
if [ -f "$APP_DIR/server_data.tar.gz" ]; then
  say "解压全量数据包 server_data.tar.gz"
  tar -xzf "$APP_DIR/server_data.tar.gz"
fi

# 4. API Key：实时行情/财务评估已改用腾讯+东财公开接口，无需 Key
# （如仍有旧 .api_key 文件可保留，不会影响运行）

# 5. 手动跑一次完整流水线（可选跳过）
if [ "$SKIP_PIPELINE" -eq 0 ]; then
  say "运行完整流水线（update→backtest→evaluate→report→track，约 15-30 分钟）"
  venv/bin/python -m mainrise.cli update || true
  venv/bin/python -m mainrise.cli backtest
  venv/bin/python -m mainrise.cli evaluate || true
  venv/bin/python -m mainrise.cli report
  venv/bin/python -m mainrise.cli track
fi

# 6. cron 每日定时（M11：补齐全部 5 条任务，不再只装 17:30 一条）
say "配置每日定时任务（daily/推送/巡检/周报/夜间研究）"
mkdir -p "$APP_DIR/output/logs"
CRON_LINES=(
  "30 17 * * 1-5 $APP_DIR/deploy/daily_update.sh >> $APP_DIR/output/logs/cron.log 2>&1"
  "50 14 * * 1-5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli push >> $APP_DIR/output/logs/cron.log 2>&1"
  "5 9 * * 1-5 $APP_DIR/deploy/health_check.sh >> $APP_DIR/output/logs/cron.log 2>&1"
  "35 17 * * 5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli weekly >> $APP_DIR/output/logs/cron.log 2>&1"
  "30 22 * * 0 $APP_DIR/deploy/night_research.sh >> $APP_DIR/output/logs/cron.log 2>&1"
  # 盘中 5 分钟点火检测：09:35-14:55 每 10 分钟（cron 分钟列表 35/45/55,05/15/25）
  "35,45,55 9-11 * * 1-5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli ignite5 >> $APP_DIR/output/logs/cron.log 2>&1"
  "5,15,25,35,45,55 13-14 * * 1-5 cd $APP_DIR && ./venv/bin/python -m mainrise.cli ignite5 >> $APP_DIR/output/logs/cron.log 2>&1"
  # 月度买卖点优化：每月最后一天 18:10（用累计 5 分钟归档数据校准点火阈值）
  "10 18 28-31 * * [ \"\$(date -d tomorrow +%d)\" = \"01\" ] && cd $APP_DIR && ./venv/bin/python -m mainrise.cli m5optimize >> $APP_DIR/output/logs/cron.log 2>&1"
)
( crontab -l 2>/dev/null | grep -vE 'daily_update\.sh|mainrise\.cli push|health_check\.sh|mainrise\.cli weekly|night_research\.sh|mainrise\.cli ignite5|mainrise\.cli m5optimize' \
  ; printf '%s\n' "${CRON_LINES[@]}" ) | crontab -

# 7. Nginx 托管网页 + 访问口令
say "配置 Nginx（output/web → http://服务器IP/）"
sed "s|/path/to/chaoduan|$APP_DIR|g" "$APP_DIR/deploy/nginx_web.conf" \
  > /etc/nginx/conf.d/mainrise_web.conf
if [ ! -f /etc/nginx/.mainrise_htpasswd ]; then
  if [ -n "${MAINRISE_WEB_PASS:-}" ]; then
    htpasswd -cb /etc/nginx/.mainrise_htpasswd "$WEB_USER" "$MAINRISE_WEB_PASS"
  else
    WEB_PASS="$(openssl rand -base64 12 | tr -d '/+=')"
    htpasswd -cb /etc/nginx/.mainrise_htpasswd "$WEB_USER" "$WEB_PASS"
    echo ""
    echo "  网页访问口令：$WEB_USER / $WEB_PASS"
    echo "  （保存好；以后可改: htpasswd -b /etc/nginx/.mainrise_htpasswd $WEB_USER 新密码）"
  fi
fi
nginx -t
systemctl enable nginx
systemctl reload nginx

IP="$(curl -s --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')"
say "全部完成！浏览器打开: http://$IP/  （用户 $WEB_USER）"
