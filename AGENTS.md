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
python3 -m mainrise.cli dashboard  # 以 QuantDark 模板更新 KPI 仪表盘（track 后自动执行）
python3 -m unittest discover -s tests   # 21 项单元测试
```

**完整流程顺序**（新信号入池必须按序）：更新行情 → 回测 → 财务评估 → 综合评分 → 跟踪报告。
新信号在信号日次日确认后才进信号明细，当天不会入池（设计如此）。

## 关键机制

- 观察池 `output/state/mainrise_watchlist.csv`：由综合评分生成（36 只）；观察池非空时，全市场新信号只列在报告第四节，不占用买点提示区；观察池为空时新信号才兜底进入买点区
- 纸面持仓 `output/state/mainrise_positions.csv`：自动建仓/平仓，可手工编辑（改真实成交价后继续自动管理）；日期列已统一 object 类型
- 报告输出：Markdown + CSV + Excel（6 个 sheet，涨红跌绿），Excel 含持仓盈亏
- **KPI 仪表盘**：`output/reports/主升浪跟踪仪表盘.xlsx` 为最终输出，由
  `mainrise/dashboard.py` 自动更新——模板 `output/reports/主升浪跟踪仪表盘_QuantDark.xlsx`
  （打包软件用内置 `mainrise/resources/dashboard_template.xlsx`），读取全部
  `主升浪跟踪_*.csv` 与持仓 CSV 重建 Data/Watch/Summary/Dashboard 与图表引用；
  `mainrise track` 完成后自动同步，失败不阻塞报告；也可单独 `mainrise dashboard`
  （GUI 有“更新仪表盘”按钮）
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

## Excel 仪表盘设计规范（Quant Dark Theme）

输出文件：`output/reports/主升浪跟踪仪表盘_QuantDark.xlsx`（4 sheets：Dashboard / Summary / Watch / Data）

### 配色

| 层级 | 色值 | 用途 |
|------|------|------|
| `#0D1117` | 根背景 | 全工作表底色、空白区域 |
| `#161B22` | 表面 | 数据行交替色、KPI 卡片 |
| `#0F1826` | 标题栏 | 表头行 |
| `#39D2C0` | 量化青 | 页面标题、分区标题 |
| `#58A6FF` | 量化蓝 | KPI 数值、表头底线 |
| `#3FB950` | 绿 | 上涨/买入信号 |
| `#F85149` | 红 | 下跌/卖出信号、折线图 |
| `#D29922` | 琥珀 | 买点提示>0 高亮、多头持有 |
| `#BC8CFF` | 紫 | 柱状图辅助色 |
| `#E6EDF3` | 主文字 | 数据行 |
| `#8B949E` | 次级文字 | 标签、列标题 |
| `#6E7681` | 脚注 | 免责声明 |

### 字体

**Consolas** 等宽字体，全表统一。层级：KPI 数值 28pt bold 纯白 `#FFFFFF`，标题 13pt bold 量化青，表头 10pt bold 次级色，数据 11pt 主文字色，脚注 9pt 脚注色。

### KPI 卡片

- 6 个 KPI 位于 Dashboard Row 2（标签）+ Row 3（数值），Column **B 起始**（A 列留白做左侧负空间）
- 标签与数值共享 `#161B22` 卡片底色，仅 hair 边框 + 底部 medium 语义强调色
- 6 个强调色：蓝(计数)、青(池)、琥珀(买点)、蓝(综合分)、绿(涨跌幅)、绿(上涨家数)

### 表格

- **无竖线** + hair 横分隔线 + 交替行色（`#0D1117`/`#161B22`）
- 表头仅底部 thin 强调线（`#58A6FF`），无其他边框
- 分区标题左对齐，底部 thin 分隔线

### 图表

- 背景 `#0D1117`（非透明），网格线 `#21262D` 极淡，坐标轴 `#30363D`
- 标题 `#E6EDF3`，标签/图例 `#8B949E`
- **折线图用红色 `#FF0000`**（国内红涨惯例，用户偏好），线宽 2.25pt
- 柱状图用量化青 `#39D2C0` + 量化紫 `#BC8CFF`
- 图表 XML 修改方法：保存为 .xlsx（ZIP）后直接替换 `xl/charts/chartN.xml`

### Summary 状态分布

透视表格式：日期为列头（`=$A$2`/`=$A$3`/`=$A$4`），状态为行头（空头/破位/多头持有/回踩低吸/多头回踩/T1确认买点/T0新信号），COUNTIFS 公式填充。宽表格式比纵向列表信息密度更高。

### 已知待改进

- Data 和 Watch 的条件格式存在新旧两套规则叠加（原始 `...G1000` + 新增 `...G2000`），需去重
- Summary 部分脚注/辅助文字颜色尚未统一到暗色主题色板（`#1F4E79`/`#666666`/`#A6A6A6` 残留）
- Watch 涨跌幅数值的小数位格式（`0.00`）未全覆盖

## 会话记忆（2026-08-07）

本轮完成：12 条代码改进（清理 src/config/Tk 重复代码、API 单例线程安全、产业信息 CSV 化、回测 --grid、19 项单元测试、打包脚本 PYTHON 支持）；API Key 记忆保存；持仓账本 FutureWarning 修复；平仓规则改为收盘回落 8%；GitHub 推送与 .app 重新打包；安装 officecli skill 链；**Excel 仪表盘量化暗色主题全量重设计**（4 sheets 配色/字体/卡片/表格/图表暗化，含图表 XML ZIP 级修改和用户手动精调）。

本轮新增：**仪表盘自动更新功能**（`mainrise/dashboard.py`，模板为
`主升浪跟踪仪表盘_QuantDark.xlsx`/内置 `mainrise/resources/dashboard_template.xlsx`，
输出 `output/reports/主升浪跟踪仪表盘.xlsx`）：`mainrise track` 完成后自动同步
（`--no-dashboard` 可跳过，失败不阻塞），独立命令 `mainrise dashboard`，GUI 报告组
新增“更新仪表盘”按钮；更新器重建 Data/Watch/Summary/Dashboard 并 ZIP 级替换图表
XML（只改引用、保留暗色样式），Summary 各区块随天数自动下移；4 个构建脚本均已
打包模板资源；新增 tests/test_dashboard.py 2 项测试（合计 21 项）。

本轮收尾：观察池数量与模板行数改为动态推导（不再写死 36/192，新增
`test_dynamic_watchlist_size`，合计 22 项测试）；`sys._MEIPASS` 兜底模板解析；
macOS `dist/主升浪跟踪.app` 已重新打包（含内置行情 259MB + 仪表盘模板，
`Contents/Frameworks/mainrise → Resources/mainrise` 符号链接可达）。Windows exe
需在 GitHub Actions / Windows 上跑 `build_gui_windows.bat` 或推送触发。
