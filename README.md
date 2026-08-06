# 主升浪信号跟踪模型

抓取 A 股"兆易创新式"趋势股启动信号（主升浪），结合财务质量 + 产业链地位做综合评分，
每日跟踪买点并管理纸面持仓。回测年化 +36.9%（2022-01 ~ 2026-08，全样本 PF 1.32），
是当前项目唯一在跟踪验证的模型。

## 两种使用方式

### 方式一：图形界面软件（推荐）

运行 `./build_gui_macos.sh` 打包，产物为 `dist/主升浪跟踪.app`（onedir 模式，
启动约 0.2 秒）。把这个 .app 整个拷到任何 macOS 电脑，双击即可打开，界面按钮一目了然：

> 界面基于 PyQt5（macOS 系统 Tk 渲染空白，故弃用 Tk 改用 PyQt5）。

| 分组 | 按钮 | 说明 |
| --- | --- | --- |
| 数据 | 初始化全量数据 | 首次下载 2021 起全市场日线（约 650MB，需联网） |
| 数据 | 更新行情 | 增量更新到最新交易日 |
| 模型分析 | 回测 / 财务评估 / 综合评分 | 参数敏感性与评分报告（评估需 API Key） |
| 每日跟踪 | 生成跟踪报告 | 买点提示 / 持仓管理 / 新信号 |
| 实时行情 | 查询行情 | 输入代码看实时涨跌幅（需 API Key） |
| 报告 | 打开报告目录 / 最新报告 | 用系统默认程序打开 |

首次打开请点右上角【更改】选择数据目录（选项目根目录可复用已有数据，或留空用
`~/.mainrise` 后点【初始化全量数据】）。所有操作日志显示在右侧窗口。

**内置行情数据**：软件已封装 2021 起全量日线（压缩后约 246MB，随 .app 分发）。
新电脑首次打开自动解压到数据目录（约 14 秒，无需联网下载 655MB），
之后只需点【更新行情】联网补齐最近几个交易日即可。

Windows 版在 Windows 电脑上运行 `build_gui_windows.bat`，产物
`dist\mainrise_gui\mainrise_gui.exe`（整个 mainrise_gui 目录即软件，压缩后分发）。

### 方式三：GitHub Actions 云端打包（Windows 轻量版）

仓库 `Longtianhong88888/Zhushenglang` 已配置 workflow（`.github/workflows/windows-build.yml`）：

- 推送 `main` 分支或仓库 Actions 页面手动「Run workflow」即触发
- 产物为 **Windows 轻量版**（约 32MB，**不含内置行情数据**），从
  Actions 运行页 → Artifacts → `mainrise-windows` 下载
- 轻量版首次打开：点【初始化全量数据】联网下载行情（约 650MB）后使用，
  之后每天【更新行情】增量补齐
- 如需带内置数据的完整版，在 Windows 机器本地跑 `build_gui_windows.bat`
  （默认包含数据包），或设置 `SKIP_BUNDLE=1` 跳过数据做轻量版

### 方式二：独立命令行（免装 Python）

在 macOS 上执行 `./build_macos.sh`，产物为单文件 `dist/mainrise`（约 63MB，已内置
Python 解释器与全部依赖）。把该文件拷到任何 macOS 电脑：

```bash
chmod +x mainrise
./mainrise init       # 首次自动下载 2021 起全市场日线（约 650MB，视网速 10~40 分钟）
./mainrise track      # 每日收盘后运行，生成报告
```

Windows 版需要在 Windows 电脑上执行 `build_windows.bat`，产物 `dist\mainrise.exe`；
PyInstaller 打包是平台相关的，不能跨系统复用。

### 方式二：pip 安装（需 Python 3.9+）

```bash
pip install .         # 或 pip install -e .（开发模式）
mainrise init
mainrise track
```

## 数据目录

- 数据默认存 `~/.mainrise/`（data/ 行情缓存、reports/ 报告、state/ 观察池与持仓账本）
- 在项目根（含 `pyproject.toml` + `data/zzshare_daily`）运行时自动复用项目内数据，
  报告写入 `output/reports/`
- 可用环境变量 `MAINRISE_HOME` 指定目录；`mainrise home` 显示当前目录

## 命令

```bash
mainrise init         # 首次初始化：交易日历 + 股票名册 + 全量行情（需联网）
mainrise update       # 增量更新行情
mainrise backtest     # 回测（生成 mainrise_trades.csv，供评估用）
mainrise evaluate     # 信号日财务评估（需 MAINRISE_API_KEY）
mainrise report       # 综合评分 + 观察池
mainrise track        # 每日跟踪：买点提示 / 纸面持仓 / 新信号扫描
mainrise snapshot 601899 000975   # 实时行情验证（需 MAINRISE_API_KEY）
mainrise gui                      # 打开图形界面软件
```

同花顺 API key（`evaluate`/`snapshot` 用）通过环境变量 `MAINRISE_API_KEY` 提供，
不写入任何文件。`init`/`update`/`track` 使用免费 zzshare 数据，无需 key。

## 信号规则

- **信号日 T0**：均线多头（MA5>MA10>MA20）+ 收盘创 20 日新高 +（涨幅≥5% 且量比≥1.5）或涨停
- **买点1**：T0 次日确认（收盘>MA5 且 低点≥T0 收盘×0.97）→ 次日开盘买入
- **买点2**：确认后回踩 MA10 缩量企稳（低点≥MA20×0.99）→ 低吸
- **纪律**：止损 -5% 或跌破 MA10；止盈高点回落 8%；5 日时间止损；单票 ≤1/3 仓，最多 3 只并行
- **防追高**：10 个交易日涨幅 ≥80% 的标的自动移出买点提示（`--max-10d-gain` 可调）

## 目录结构

```
src/
mainrise/
  paths.py               数据目录解析（$MAINRISE_HOME / 项目模式 / ~/.mainrise）
  data.py                zzshare 数据层：交易日历/股票名册/日线抓取与解压
  signals.py             信号指标（均线/量比/创新高/状态判定）
  tracker.py             每日跟踪：买点提示/纸面持仓/报告（Markdown+CSV+Excel）
  evaluate.py            信号日财务评估（同花顺 API，无前视）
  report.py              综合评分（40%财务+30%信号+30%产业地位）
  backtest.py            参数敏感性回测
  snapshot.py            实时行情（股票/ETF 自动路由）
  gui_pyqt.py            PyQt5 图形界面
  excel_report.py        Excel 报告生成
  resources/             产业链信息 CSV（行业地位可配置）
scripts/
  backtest_mainrise.py   主升浪信号回测（参数敏感性 + 年化收益）
  （旧命令兼容壳，等价 mainrise 子命令）
tests/                   单元测试（信号引擎/快照路由/持仓平仓逻辑）
docs/                    工作笔记
data/
  zzshare_daily/         全市场日线缓存（按交易日，2021 起）
  stock_list.csv         代码→名称映射
  trade_dates.csv        交易日历
output/
  reports/               综合评估 / 每日跟踪报告
  state/                 观察池 + 纸面持仓账本
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 每日运行

```bash
python3 scripts/track_mainrise.py     # 每日收盘后跑一次（默认最新缓存交易日）
python3 scripts/track_mainrise.py --date 2026-08-05   # 指定日期
python3 scripts/track_mainrise.py --no-scan           # 跳过全市场新信号扫描
```

产出 `output/reports/主升浪跟踪_YYYY-MM-DD.md`：

1. **今日买点提示**：T0/T1/回踩低吸，低分与 10 日涨幅过大自动剔除/标注
2. **持仓管理**：纸面账本自动建仓、更新峰值、触发平仓（止损/止盈/时间止损）
3. **观察池状态**：按综合分排序，含趋势状态与提示
4. **全市场新信号**：当日 T0/T1 候选（量比前 20），待财务评估

纸面持仓账本 `output/state/mainrise_positions.csv` 可手工编辑（改成真实成交价后继续自动管理）。

## 观察池刷新（有新信号或定期）

```bash
python3 scripts/evaluate_signals.py    # 最近40交易日信号标的财务评估
python3 scripts/build_full_report.py   # 综合评分 + 生成 output/state/mainrise_watchlist.csv
```

## 数据源

- **zzshare**（`data/zzshare_daily/`）：全市场日线，含交易所官方涨停价、换手率、ST/停牌标记，2021 起按交易日缓存。
- **同花顺金融数据 API**（fuyao.aicubes.cn，`X-api-key` 鉴权）：
  - 行情快照 `/api/a-share/prices/snapshot`（盘中实时验证，如买点验证）
  - 财务指标（`evaluate_signals.py` 内联调用，按信号日已披露报告期取数）

> 分析结果为研究线索，不构成投资建议；回测未计交易成本/容量，实盘需打 6-7 折验证。
