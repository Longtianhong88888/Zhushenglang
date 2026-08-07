# 项目记忆（自动加载）

## 项目定位

A 股主升浪信号跟踪模型，已封装为软件：

- `mainrise/` Python 包：信号扫描 / 财务评估 / 综合评分 / 每日跟踪 / 实时行情 / Excel 报告
- `mainrise/gui_pyqt.py`：PyQt5 图形界面（**不要用 Tk**，macOS 渲染空白，已废弃）
- `dist/主升浪跟踪.app`：macOS 打包产物（onedir，含内置行情数据）
- GitHub `Longtianhong88888/Zhushenglang`：代码仓库 + Actions 自动打包 Windows 轻量版

## 核心模型规则

- **信号日 T0**：均线多头（MA5>MA10>MA20）+ 创20日新高 +（涨幅≥5% 且量比≥1.5）或涨停
- **买点1**：T0 次日确认（收>MA5 且 低点≥T0收盘×0.97）→ 次日开盘买入
- **买点2**：回踩 MA10 缩量企稳（低点≥MA20×0.99）→ 低吸
- **止损**：买入价 -5%（盘中低点触发）
- **止盈**：**盘中最高点到收盘价回落 8%**（用收盘价判断，不是盘中低点——曾误用低点导致误平仓）
- **时间止损**：持仓 5 个交易日
- **防追高**：10 个交易日涨幅 ≥80% 不进买点提示
- 仓位：单票 ≤1/3，最多 3 只并行，优先综合分 Top10

## 数据与路径

- 数据目录：`$MAINRISE_HOME` 优先；项目根运行用项目内 `data/` + `output/`；独立模式 `~/.mainrise/`
- 本机软链：`~/.mainrise` → `/Users/user/Desktop/chaoduan`（数据复用，不占额外空间）
- 行情：zzshare 免费全市场日线（`data/zzshare_daily/`，2021 起）；同花顺 API 用于财务评估/实时行情（`MAINRISE_API_KEY`）
- 实时行情路由：51/56/58 开头沪市 ETF、15 开头深市基金 → `/api/fund/market/snapshot`；股票 → `/api/a-share/prices/snapshot`
- API Key 保存在 `~/.mainrise/settings.json`（权限 600），GUI 输入一次后自动记忆

## 常用命令

```bash
python3 -m mainrise.cli init       # 首次：交易日历+股票名册+行情（有内置包则只增量）
python3 -m mainrise.cli update     # 增量更新行情
python3 -m mainrise.cli backtest   # 回测生成信号明细（--grid 精细扫描约200组/30-40分钟）
python3 -m mainrise.cli evaluate   # 财务评估（需 MAINRISE_API_KEY）
python3 -m mainrise.cli report     # 综合评分 + 观察池
python3 -m mainrise.cli track      # 每日跟踪报告（--no-scan 跳过全市场扫描）
python3 -m unittest discover -s tests   # 19 项单元测试
```

**完整流程顺序**（新信号入池必须按序）：更新行情 → 回测 → 财务评估 → 综合评分 → 跟踪报告。
新信号在信号日次日确认后才进信号明细，当天不会入池（设计如此）。

## 关键机制

- 观察池 `output/state/mainrise_watchlist.csv`：由综合评分生成（36 只）；观察池非空时，全市场新信号只列在报告第四节，不占用买点提示区；观察池为空时新信号才兜底进入买点区
- 纸面持仓 `output/state/mainrise_positions.csv`：自动建仓/平仓，可手工编辑（改真实成交价后继续自动管理）；日期列已统一 object 类型
- 报告输出：Markdown + CSV + Excel（6 个 sheet，涨红跌绿），Excel 含持仓盈亏
- 产业信息在 `mainrise/resources/industry_info.csv`（新增标的信息改 CSV，不用改代码）
- 打包：`build_gui_macos.sh`（内置数据，SKIP_BUNDLE=1 轻量版）、`build_gui_windows.bat`；`PYTHON` 环境变量指定解释器

## 已知决策与坑

- macOS 系统 Tk 渲染空白 → 全部用 PyQt5
- GitHub Actions 打包 Windows：依赖用 `pip install -r requirements.txt pyinstaller`（**必须含 PyQt5**，曾漏装导致 exe 缺界面库）；.bat 用 cmd shell 执行
- PyInstaller 单实例锁 60 秒过期（异常退出不阻塞下次启动）
- 全市场扫描 ~4700 只/组参数，回测默认 18 组约 3-4 分钟
- 免责：所有输出仅用于研究，不构成投资建议（UI 底部已标注）

## 已安装 Skills（本机）

- `officecli` + `officecli-xlsx` + `officecli-data-dashboard`（OfficeCLI v1.0.143，`~/.local/bin/officecli`）：Excel 仪表盘/文档生成
- `hithink-finance`：同花顺金融数据（REST/MCP/CLI，统一 API Key）

## 会话记忆（2026-08-07）

本轮完成：12 条代码改进（清理 src/config/Tk 重复代码、API 单例线程安全、产业信息 CSV 化、回测 --grid、19 项单元测试、打包脚本 PYTHON 支持）；API Key 记忆保存；持仓账本 FutureWarning 修复；平仓规则改为收盘回落 8%；GitHub 推送与 .app 重新打包；安装 officecli skill 链。
