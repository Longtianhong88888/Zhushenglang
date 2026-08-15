"""综合评分报告（财务 + 信号 + 产业链地位）+ 观察池导出。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from mainrise import paths
from mainrise.signals import load_names


INFO = {
    "603986": {"name": "兆易创新", "track": "存储芯片（NOR Flash/利基DRAM龙头）", "status": "全球NOR Flash前三，MCU+存储双轮", "pos": 95, "risk": "存储价格周期波动；大基金减持"},
    "000657": {"name": "中钨高新", "track": "钨全产业链（硬质合金/PCB微钻/数控刀具）", "status": "硬质合金产能全球第一，PCB微钻龙头", "pos": 95, "risk": "钨价高位回落；矿山注入进度"},
    "601899": {"name": "紫金矿业", "track": "铜金锂资源（超一流矿业集团）", "status": "全球矿业龙头，2025净利517.8亿+61.6%", "pos": 100, "risk": "商品价格波动；海外政治/矿震风险"},
    "001309": {"name": "德明利", "track": "存储模组（SSD/嵌入式/企业级）", "status": "企业级存储领先，AI驱动需求", "pos": 85, "risk": "存储价格波动；估值高（PE 148x）"},
    "003031": {"name": "中瓷电子", "track": "电子陶瓷外壳/光模块封装", "status": "光模块陶瓷外壳全球头部，GaN/SiC布局", "pos": 90, "risk": "光模块需求波动；并购整合"},
    "603061": {"name": "金海通", "track": "半导体测试分选机", "status": "分选机龙头，Q3净利+832%", "pos": 88, "risk": "半导体资本开支波动；竞争加剧"},
    "000975": {"name": "山金国际", "track": "黄金矿产（金矿龙头）", "status": "中国领先黄金生产商，金价上行受益", "pos": 88, "risk": "金价回调；矿山品位下降"},
    "002297": {"name": "博云新材", "track": "航空刹车材料/碳碳复合材料", "status": "C919机轮刹车国内独家，航空新材料", "pos": 85, "risk": "军品定价/交付节奏；业务体量小"},
    "603256": {"name": "宏和科技", "track": "玻纤电子布（覆铜板上游）", "status": "低介电玻纤布卡位AI算力/高速PCB", "pos": 85, "risk": "玻纤价格波动；电子布供需"},
    "002842": {"name": "翔鹭钨业", "track": "钨产业链（钨丝/硬质合金）", "status": "2025扭亏，光伏钨丝产能释放", "pos": 78, "risk": "钨价波动；光伏需求"},
    "002653": {"name": "海思科", "track": "创新药（麻醉/特色专科）", "status": "创新转型，扣非+90%，新药放量", "pos": 82, "risk": "集采降价；研发投入大"},
    "603979": {"name": "金诚信", "track": "矿山服务+自有铜矿", "status": "矿服龙头，铜当量9.14万吨+88%", "pos": 82, "risk": "铜价波动；海外运营"},
    "002192": {"name": "融捷股份", "track": "锂矿采选（锂电上游）", "status": "锂矿+锂盐一体化", "pos": 75, "risk": "锂价低迷；业绩波动大"},
    "301201": {"name": "诚达药业", "track": "医药CDMO/左旋肉碱/CGT", "status": "CDMO+减肥/小核酸布局，2025减亏", "pos": 70, "risk": "2025仍亏损；新业务未盈利"},
    "000989": {"name": "九芝堂", "track": "中药老字号（OTC/处方药）", "status": "百年品牌，2025净利+3%", "pos": 72, "risk": "营收下滑-6%；营销改革"},
    "002338": {"name": "奥普光电", "track": "光电测控/光栅编码器/碳纤维", "status": "禹衡光学编码器国内龙头，国家编码器工程中试基地", "pos": 78, "risk": "传统编码器业务萎缩；半导体新业务未放量"},
    "300666": {"name": "江丰电子", "track": "超高纯溅射靶材/半导体零部件", "status": "靶材出货量全球第一，零部件国产领先", "pos": 95, "risk": "半导体资本开支波动；零部件毛利率承压"},
    "600520": {"name": "文一科技", "track": "半导体封装模具/设备", "status": "国内少有的封装模具上市企业，12寸塑封设备调试中", "pos": 62, "risk": "体量小业绩波动大；新设备未量产"},
    "601869": {"name": "长飞光纤", "track": "光纤预制棒/光纤/光缆", "status": "全球市占第一，AI光缆/空芯光纤受益", "pos": 95, "risk": "光纤价格周期；AI需求兑现节奏"},
    "600183": {"name": "生益科技", "track": "覆铜板（CCL）", "status": "全球第二大刚性覆铜板（市占13.7%），AI服务器受益", "pos": 92, "risk": "原材料涨价；PCB需求波动"},
    "600489": {"name": "中金黄金", "track": "黄金矿产", "status": "中国黄金集团旗下龙头，2025净利预增42-59%", "pos": 90, "risk": "金价回调；内蒙古矿业停产扰动"},
    "600549": {"name": "厦门钨业", "track": "钨全产业链+稀土+锂电材料", "status": "钨全产业链龙头，2025净利+33.6%", "pos": 92, "risk": "钨价波动；稀土/锂电材料承压"},
    "001339": {"name": "智微智能", "track": "边缘AI/ICT基础设施/智算", "status": "边缘AI Box全栈产品，2025前三季净利+59%", "pos": 70, "risk": "智算需求波动；Q3利润承压"},
    "002793": {"name": "罗欣药业", "track": "医药（原料药+制剂+创新药）", "status": "替戈拉生等创新药布局，原料药自产", "pos": 58, "risk": "集采降价；创新药放量不确定"},
}

RESOURCES = Path(__file__).resolve().parent / "resources" / "industry_info.csv"


def load_industry_info() -> dict:
    """从 CSV 读取产业链信息；CSV 缺失/损坏时回退到内置 INFO（向后兼容）。"""
    try:
        df = pd.read_csv(RESOURCES, dtype={"code": str})
        out = {}
        for _, r in df.iterrows():
            out[str(r["code"])] = {
                "name": str(r["name"]),
                "track": str(r["track"]),
                "status": str(r["status"]),
                "pos": float(r["pos"]),
                "risk": str(r["risk"]),
            }
        if out:
            return out
    except Exception:  # noqa: BLE001  文件缺失/损坏时使用内置兜底
        pass
    return INFO


def load_chokepoint_codes() -> set[str]:
    """行业卡点企业名单：industry_info.csv 覆盖的代码集合。
    模型只追踪这些产业链关键环节企业，全市场扫描/回测/研究均以此为范围。"""
    return set(load_industry_info())


def parse_prev_report() -> pd.DataFrame:
    md = (paths.report_dir() / f"信号评估_{datetime.now().strftime('%Y-%m-%d')}.md")
    if not md.exists():
        # 回退到最近一份评估报告
        cands = sorted(paths.report_dir().glob("信号评估_*.md"))
        if not cands:
            raise SystemExit("缺少信号评估报告，请先运行: mainrise evaluate")
        md = cands[-1]
    text = md.read_text(encoding="utf-8")

    def _block(after: str, before: str) -> list[dict]:
        rows = []
        try:
            block = text.split(after)[1].split(before)[0]
        except IndexError:
            return rows
        for l in block.splitlines():
            if l.startswith("| ") and not l.startswith("| ---") and not l.startswith("| 代码"):
                p = [x.strip() for x in l.split("|")]
                try:
                    rows.append({"code": p[1], "signals": int(p[2]),
                                 "fin_score": int(p[9])})
                except (IndexError, ValueError):
                    continue
        return rows

    rows = _block("### 重点线索", "### 观察线索")
    if not rows:
        # 重点线索为空时回退到观察线索，避免空表导致下游崩溃
        rows = _block("### 观察线索", "### 暂不适合")
    return pd.DataFrame(rows)


def run() -> str:
    df = parse_prev_report()
    if df.empty:
        raise SystemExit("评估报告无可用的重点/观察线索，请检查 evaluate 是否成功"
                         "（API Key 是否已配置生效）")
    industry = load_industry_info()
    for code, info in industry.items():
        m = df["code"] == code
        if m.any():
            df.loc[m, "name"] = info["name"]
            df.loc[m, "track"] = info["track"]
            df.loc[m, "status"] = info["status"]
            df.loc[m, "pos"] = info["pos"]
            df.loc[m, "risk"] = info["risk"]
            if code in ("003032", "300497", "002197"):
                df.loc[m, "fin_score"] = df.loc[m, "fin_score"].clip(upper=40)
    # 中文名回退到股票名册，避免观察池出现"待补"
    names = load_names()
    df["name"] = df["name"].fillna(df["code"].map(names)).fillna("待补")
    df["signal_score"] = df["signals"].map({1: 50, 2: 70, 3: 90}).fillna(50)
    df["composite"] = (0.4 * df["fin_score"] + 0.3 * df["signal_score"]
                       + 0.3 * df["pos"]).round(1)
    df = df.sort_values("composite", ascending=False)

    date = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# 主升浪信号标的综合评估（{date}）",
             "> 综合评分 = 40%财务质量 + 30%信号强度 + 30%产业链地位（联网检索2025-2026公开信息）",
             "> 方法：财务指标（信号日已披露报告期）+ 主升浪信号频次 + 行业地位；红队风险为公开信息归纳",
             "> 免责：研究线索，不构成投资建议",
             "",
             "## 一、综合评分排序（全部重点线索）",
             "",
             "| 排名 | 代码 | 名称 | 赛道 | 财务分 | 信号次 | 产业地位分 | **综合分** | 行业地位摘要 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, (_, r) in enumerate(df.iterrows(), 1):
        lines.append(f"| {i} | {r['code']} | {r.get('name','?')} | {r.get('track','待核验')} | "
                     f"{r['fin_score']} | {r['signals']} | {r.get('pos','-')} | **{r['composite']}** | {r.get('status','-')} |")
    lines += ["", "## 二、买点提示（主升浪信号规则，回测年化+36.9%）",
              "",
              "- **第一级 B3 打底仓**：均线粘合≤3% + 放量阳线（量比≥2） + 站上三均线 + 低位 → 明日开盘打底仓（2/3）",
              "- **第二级 二波加仓**：B3 后深回调 2-12% + 均线再次粘合≤2% + 缩量 → 放量再启动 → 明日开盘加仓（1/3）",
              "- **止损**：买入价 -4%，或跌破 MA10（趋势破坏）",
              "- **持仓**：20 日时间止损（让赢家跑）；冲高回落 8% 止盈（条件单）",
              "- **仓位**：单票 ≤1/3，最多 3 只并行；优先综合分 Top10",
              "",
              "## 三、红队风险提示（Top10）",
              "",
              "| 代码 | 名称 | 最大风险 |", "| --- | --- | --- |"]
    for _, r in df.head(10).iterrows():
        lines.append(f"| {r['code']} | {r.get('name','?')} | {r.get('risk','待核验')} |")
    lines += ["", "## 四、防幻觉检查",
              "",
              "| 可能被夸大的结论 | 为什么可能夸大 | 谨慎表达方式 |",
              "| --- | --- | --- |",
              "| 主升浪信号年化+36.9% | 训练期红利+未计交易成本/容量 | 样本外需验证，实盘打6-7折 |",
              "| 综合分Top标的必然上涨 | 评分含主观产业地位判断 | 作为研究线索，买点以信号确认为准 |",
              "| 行业地位信息 | 部分来自研报/媒体（C级证据） | 需公告/年报复核（A级证据） |"]

    paths.ensure_dirs()
    path = paths.report_dir() / f"主升浪信号综合评估_{date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")

    watch = df[["code", "name", "track", "fin_score", "signals", "pos", "composite"]].copy()
    watch["name"] = watch["name"].fillna("待补")
    watch["track"] = watch["track"].fillna("待核验")
    watch["pos"] = watch["pos"].fillna(50.0)
    watch.to_csv(paths.state_dir() / "mainrise_watchlist.csv",
                 index=False, encoding="utf-8-sig")
    print(f"报告: {path}")
    print(f"观察池: {paths.state_dir() / 'mainrise_watchlist.csv'} ({len(watch)} 只)")
    return str(path)


def main() -> None:
    try:
        run()
    except SystemExit as e:
        print(e)
        sys.exit(1)
