# 腾讯云部署说明（每日自动更新网页仪表盘）

目标：云服务器每个交易日自动跑完整流水线，生成网页版仪表盘，手机/电脑随时访问。

## 〇、腾讯云购买配置清单（按控制台字段顺序）

1. **计费模式**：包年包月；时长选 **3 年**（长租折扣更深，页面按实付为准）。
2. **地域/可用区**：就近选（如上海二/三/四区；广州/南京/北京均可，不影响功能）。
3. **机型规格**：**蜂驰型BF1 2核4G**（若 BF1 无货，选标准型 S5 2核4G 亦可）。
4. **镜像**：**Ubuntu Server 22.04 LTS 64位**（别选 CentOS，官方已停止维护）。
5. **系统盘**：高性能云硬盘 **50GB**（数据仅 ~1GB，报告/日志增长很慢，50GB 富余）。
6. **公网带宽**：**按固定带宽 1Mbps**（全量数据走打包上传，上传不计费；每日增量仅
   几 MB；网页流量极小。不够用再升，控制台随时可调）。
7. **公网 IP**：新分配。
8. **安全组**：新建安全组，放行 **22（SSH）、80（HTTP）、443（HTTPS）**；
   22 端口来源建议只填你自己的 IP，80/443 对全网开放。
9. **登录方式**：设置 root 密码（简单省事）或 SSH 密钥。
10. 确认订单支付。购买后记录：**公网 IP、root 密码**。

## 一、服务器初始化

- 腾讯云轻量应用服务器 / CVM，Ubuntu 22.04，**建议 2核4G 起步**（实测本机全量
  行情 707 万行加载 + 全市场扫描进程峰值内存约 3.0GB，2核2G 会内存不足；4G 建议
  再挂 2-4G 交换分区保险）。带宽 1Mbps 也够用——全量数据不需要在服务器下载，
  直接打包上传（见下节）。

```bash
ssh root@<公网IP>
apt update && apt install -y python3-venv python3-pip nginx apache2-utils
fallocate -l 4G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```
- 安装 Python 3.9+ 与 Nginx：

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip nginx apache2-utils
```

## 二、上传代码与依赖

```bash
cd ~
git clone https://github.com/Longtianhong88888/Zhushenglang.git chaoduan   # 或 scp 上传
cd chaoduan
python3 -m venv venv
venv/bin/pip install -r requirements-server.txt
```

## 三、全量数据打包上传（不用在服务器下载）

在本机（有完整行情数据的电脑）执行：

```bash
./deploy/package_data.sh        # 生成 build/server_data.tar.gz（约 200MB，含
                                # 全量行情+名册+交易日历+持仓/观察池，压缩几分钟）
scp build/server_data.tar.gz ubuntu@<服务器IP>:~/chaoduan/
```

服务器上解压（路径解压后正好是项目的 data/ 和 output/state/）：

```bash
cd ~/chaoduan && tar -xzf server_data.tar.gz
```

之后 `mainrise init` / `mainrise update` 只会做增量补齐，不会再下载全量。

## 四、首次跑通

```bash
venv/bin/python -m mainrise.cli init      # 上传数据后只需增量补齐
```

数据源已全部改为腾讯/东财公开接口，**无需任何 API Key**（旧 `.api_key` 文件可保留，不影响运行）。

## 五、手动跑一次完整流水线

```bash
venv/bin/python -m mainrise.cli update
venv/bin/python -m mainrise.cli backtest
venv/bin/python -m mainrise.cli evaluate
venv/bin/python -m mainrise.cli report
venv/bin/python -m mainrise.cli track    # 自动同步 Excel + 网页仪表盘
```

生成网页：`output/web/index.html`（Nginx 托管目录）。

## 六、定时任务

```bash
crontab -e
```

加入（交易日 17:30，收盘后数据发布）：

```
30 17 * * 1-5 /home/ubuntu/chaoduan/deploy/daily_update.sh
```

日志在 `output/logs/daily_YYYY-MM-DD.log`；如需失败告警，可在脚本末尾追加
钉钉/Server酱/企业微信 webhook 通知。

## 七、Nginx 托管网页

```bash
sudo cp deploy/nginx_web.conf /etc/nginx/conf.d/mainrise_web.conf
sudo sed -i 's|/path/to/chaoduan|/home/ubuntu/chaoduan|' /etc/nginx/conf.d/mainrise_web.conf
sudo htpasswd -c /etc/nginx/.mainrise_htpasswd zhushang   # 设置访问口令
sudo nginx -t && sudo systemctl reload nginx
```

浏览器打开 `http://服务器IP/`，输入口令即可查看；手机浏览器"添加到主屏幕"可当 App 用。

## 八、HTTPS（推荐）

腾讯云控制台申请免费 SSL 证书，或安装 certbot：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名
```

## 九、盘中实时盯盘（网页版，无推送）

服务器常开，顺便在盘中盯 持仓+观察池，输出实时网页
`http://服务器IP/live.html`（同一访问口令，页面内每 3 秒轮询更新 + 分时缩略图）：

```bash
# 1. 环境变量文件（已无需 API Key，留空即可）
echo "MAINRISE_API_KEY=" > /home/ubuntu/chaoduan/.monitor_env
chmod 600 /home/ubuntu/chaoduan/.monitor_env

# 2. 先手动跑一轮测试
cd /home/ubuntu/chaoduan
venv/bin/python -m mainrise.cli monitor --once

# 3. 安装为系统服务（开机自启，盘中轮询、盘后休眠）
sudo cp deploy/mainrise-monitor.service /etc/systemd/system/
sudo chown -R ubuntu:ubuntu /home/ubuntu/chaoduan/output   # 网页目录归 ubuntu，服务可写
sudo systemctl daemon-reload
sudo systemctl enable --now mainrise-monitor
sudo systemctl status mainrise-monitor --no-pager
```

提醒规则：持仓跌破止损 -5%（低点触发）、自峰值回落 8%、观察池涨跌幅 ±5%、
现价回踩 MA10/MA20（日线参考值 ±1%）；每只票同类提醒 10 分钟限频。
日志：`journalctl -u mainrise-monitor -f`。

## 常见问题

- 全市场扫描较慢：默认 18 组参数约 3-4 分钟，属正常；不要在生产跑 `--grid`。
- `evaluate` 失败：多为东财接口偶发超时，重跑 `mainrise evaluate` 即可；无需 Key。
- 内存不足：2C4G 建议加 4G 交换分区（`fallocate -l 4G /swapfile` 等）。
- 行情不更新：先 `venv/bin/python -m mainrise.cli update` 看日志；zzshare 免费接口偶发限流，可稍后重试。
