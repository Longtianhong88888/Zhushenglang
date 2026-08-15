@echo off
REM 打包 Windows 图形界面软件（PyQt5，onedir 启动快，双击打开，无需 Python）
REM 需在 Windows + Python 3.9+ 环境运行
cd /d %~dp0

set "ADD_DATA="
if "%SKIP_BUNDLE%"=="1" goto :ready
if exist "build\bundled_data.zip" (
  echo 复用内置数据包: build\bundled_data.zip
) else (
  echo 构建内置行情数据包（SKIP_BUNDLE=1 可跳过，做轻量版）...
  python scripts\build_bundle.py
)
set "ADD_DATA=--add-data build\bundled_data.zip;mainrise_data"

:ready

REM 仪表盘模板（每日跟踪后同步更新仪表盘需要）
set "ADD_DATA=%ADD_DATA% --add-data mainrise\resources\dashboard_template.xlsx;mainrise\resources"

REM 应用图标与启动画面
set "ADD_DATA=%ADD_DATA% --add-data mainrise\resources\app_icon.png;mainrise\resources"
set "ADD_DATA=%ADD_DATA% --add-data mainrise\resources\splash.png;mainrise\resources"
REM KLineChart 看盘套件（标的详情页 K线内联渲染）
set "ADD_DATA=%ADD_DATA% --add-data mainrise\resources\klinecharts.min.js;mainrise\resources"
REM 行业卡点企业名单（110 家；缺失则 GUI 卡点范围静默缩水到内置兜底 36 只）
set "ADD_DATA=%ADD_DATA% --add-data mainrise\resources\industry_info.csv;mainrise\resources"

python -m PyInstaller ^
  --windowed ^
  --onedir ^
  --noconfirm ^
  --name mainrise_gui ^
  --icon mainrise\resources\app.ico ^
  --clean ^
  --collect-all zzshare ^
  --hidden-import tqdm ^
  %ADD_DATA% ^
  mainrise\gui_pyqt.py
echo 打包完成: dist\mainrise_gui\mainrise_gui.exe（整个 dist\mainrise_gui 目录即软件）
