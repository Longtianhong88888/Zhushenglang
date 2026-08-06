#!/bin/bash
# 打包 macOS 图形界面软件（PyQt5，.app，Finder 双击打开，无需 Python）
# 使用 --onedir：避免 onefile 每次启动解压 80MB，启动快（接近秒开）
# 产物: dist/主升浪跟踪.app（整个目录即软件，Finder 中拖拽/双击均正常）
set -e
cd "$(dirname "$0")"

# Python 解释器：可用 PYTHON 环境变量指定（如 pyenv/虚拟环境），默认 python3
PYTHON="${PYTHON:-python3}"

ADD_DATA=()
if [ -z "$SKIP_BUNDLE" ]; then
  echo "构建内置行情数据包（SKIP_BUNDLE=1 可跳过，做轻量版）..."
  "$PYTHON" scripts/build_bundle.py
  ADD_DATA=(--add-data "build/bundled_data.zip:mainrise_data")
else
  echo "轻量版：不内置行情数据（新电脑首次使用需联网初始化）"
fi

"$PYTHON" -m PyInstaller \
  --windowed \
  --onedir \
  --noconfirm \
  --name "主升浪跟踪" \
  --clean \
  --collect-all zzshare \
  --hidden-import tqdm \
  "${ADD_DATA[@]}" \
  mainrise/gui_pyqt.py
echo "打包完成: dist/主升浪跟踪.app"
