@echo off
REM 打包 Windows 独立可执行文件（需在 Windows + Python 3.9+ 环境运行）
REM 产物: dist\mainrise.exe
cd /d %~dp0
python -m PyInstaller ^
  --onefile ^
  --name mainrise ^
  --clean ^
  --collect-all zzshare ^
  --hidden-import tqdm ^
  mainrise\cli.py
echo 打包完成: dist\mainrise.exe
