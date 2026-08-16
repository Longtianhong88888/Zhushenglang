#!/bin/bash
# 打包 macOS 独立可执行文件（无需目标机器安装 Python）
# 产物: dist/mainrise  （下载后 chmod +x mainrise 即可运行）
set -e
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m PyInstaller \
  --onefile \
  --name mainrise \
  --clean \
  --add-data "mainrise/resources/dashboard_template.xlsx:mainrise/resources" \
  --collect-all zzshare \
  --hidden-import tqdm \
  mainrise/cli.py
echo "打包完成: dist/mainrise"
echo "验证: ./dist/mainrise home"
