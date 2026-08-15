"""下午回测复盘：成功率与全市场成交量（成交额）的关系。

数据源：
- 交易明细 output/reports/mainrise_trades.csv（当日跑出的最优参数组）
- 全市场日线 data/zzshare_daily/*.csv（sum(amount)=两市成交额，sum(volume)=两市成交量）

收益口径与 backtest.py 完全一致（v3 纪律）：
- 买点：buy_date 开盘买入（成本 COST=0.002）
- 止损：收盘 ≤ 买入价×0.96（-4%，2026-08-13 起与主模型一致）
- 止盈：收盘价自峰值回落 8%（(close-peak)/peak ≤ -0.08）
- 时间止损：持仓 5 个交易日
- 胜率 = rets>0 占比；PF = 盈利和 / 亏损和绝对值

量能水位：信号日全市场成交额（成交量）/ 其 20 日均值（剔除新股扩容趋势）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.backtest import COST, END, START
from mainrise.report import load_chokepoint_codes


def load_market_totals() -> pd.DataFrame:
    """逐日全市场成交额(亿元)/成交量(亿股)/涨停家数(全市场)。"""
    rows = []
    for p in sorted(paths.zzshare_dir().glob("[0-9]*.csv")):
        try:
            df = pd.read_csv(p, usecols=["date", "amount", "volume",
                                         "close", "limit_price"])
        except Exception:  # noqa: BLE001
            continue
        g = df.groupby("date", sort=False)
        rows.append(pd.DataFrame({
            "date": list(g.groups),
            "mkt_amount": (g["amount"].sum() / 1e8).values,   # 亿元
            "mkt_vol": (g["volume"].sum() / 1e8).values,      # 亿股
            "mkt_zt": (df["close"] >= df["limit_price"] - 1e-6
                       ).groupby(df["date"]).sum().values,
        }))
    out = pd.concat(rows, ignore_index=True).sort_values("date").reset_index(drop=True)
    out["amount_ma20"] = out["mkt_amount"].rolling(20, min_periods=10).mean()
    out["vol_ma20"] = out["mkt_vol"].rolling(20, min_periods=10).mean()
    out["amount_wl"] = out["mkt_amount"] / out["amount_ma20"]   # 成交额水位
    out["vol_wl"] = out["mkt_vol"] / out["vol_ma20"]            # 成交量水位
    return out


def load_chokepoint_panels() -> dict[str, pd.DataFrame]:
    codes = set(load_chokepoint_codes())
    parts = []
    for p in sorted(paths.zzshare_dir().glob("[0-9]*.csv")):
        try:
            df = pd.read_csv(p, dtype={"code": str},
                             usecols=["date", "code", "open", "high",
                                      "low", "close"])
        except Exception:  # noqa: BLE001
            continue
        parts.append(df[df["code"].isin(codes)])
    allp = pd.concat(parts, ignore_index=True).sort_values(["code", "date"])
    return {c: g.reset_index(drop=True) for c, g in allp.groupby("code")}


def v3_return(panel: pd.DataFrame, buy_i: int) -> float | None:
    """与 backtest.py 完全一致的 v3 纪律收益（含成本）。"""
    if buy_i >= len(panel):
        return None
    buy = panel.iloc[buy_i]["open"]
    if buy <= 0:
        return None
    peak = panel.iloc[buy_i]["high"]
    for j in range(buy_i + 1, min(buy_i + 7, len(panel))):
        rr = panel.iloc[j]
        peak = max(peak, rr["high"])
        if rr["close"] <= buy * 0.96:
            return (buy * 0.96 - buy) / buy - COST
        if (rr["close"] - peak) / peak <= -0.08:
            return (rr["close"] - buy) / buy - COST
        if j - buy_i >= 5:
            return (rr["close"] - buy) / buy - COST
    return (panel.iloc[-1]["close"] - buy) / buy - COST


def stats(s: pd.Series) -> dict:
    pos = s[s > 0].sum()
    neg = abs(s[s < 0].sum())
    return {"n": len(s), "win": (s > 0).mean(), "mean": s.mean(),
            "pf": pos / neg if neg else 99.0}


def fmt(st: dict) -> str:
    return (f"n={st['n']:5d} 胜率{st['win']:.1%} "
            f"均收{st['mean']:+.2%} PF={st['pf']:.2f}")


def main() -> None:
    mkt = load_market_totals()
    panels = load_chokepoint_panels()
    trades = pd.read_csv(paths.report_dir() / "mainrise_trades.csv",
                         dtype={"code": str})
    trades = trades[(trades["S_date"] >= START) & (trades["S_date"] <= (END or "9999"))]

    rets = []
    for _, r in trades.iterrows():
        rr = v3_return(panels[r["code"]], int(r["buy_i"]))
        rets.append(rr)
    trades = trades.assign(ret=rets).dropna(subset=["ret"])

    trades = trades.merge(
        mkt[["date", "mkt_amount", "mkt_vol", "mkt_zt", "amount_wl", "vol_wl"]],
        left_on="S_date", right_on="date", how="left").drop(columns=["date"])
    trades = trades.merge(
        mkt[["date", "amount_wl", "vol_wl"]],
        left_on="buy_date", right_on="date", how="left",
        suffixes=("", "_buy")).drop(columns=["date"])
    trades["win"] = (trades["ret"] > 0).astype(int)
    trades["year"] = trades["S_date"].str[:4]

    print(f"总样本（卡点名单）: {fmt(stats(trades['ret']))}")
    print(f"信号日范围: {trades['S_date'].min()} ~ {trades['S_date'].max()}")
    print(f"全市场成交额: {trades['mkt_amount'].describe()[['mean','min','max']].to_dict()}")
    print()

    # 1) 按信号日成交额水位五分位
    trades["q_amount"] = pd.qcut(trades["amount_wl"], 5, labels=False,
                                 duplicates="drop")
    print("=== 按信号日全市场成交额水位（amount/MA20）五分位 ===")
    for q in sorted(trades["q_amount"].dropna().unique()):
        sub = trades[trades["q_amount"] == q]
        rng = f"[{sub['amount_wl'].min():.2f}, {sub['amount_wl'].max():.2f}]"
        print(f"Q{q + 1} 水位{rng}: {fmt(stats(sub['ret']))}")

    # 2) 地量 / 放量 极值对照
    print("\n=== 地量 vs 放量 对照 ===")
    for name, mask in [
            ("地量(<0.90)", trades["amount_wl"] < 0.90),
            ("常态(0.90~1.10)", (trades["amount_wl"] >= 0.90)
             & (trades["amount_wl"] <= 1.10)),
            ("温和放量(1.10~1.30)", (trades["amount_wl"] > 1.10)
             & (trades["amount_wl"] <= 1.30)),
            ("显著放量(>1.30)", trades["amount_wl"] > 1.30)]:
        sub = trades[mask]
        if len(sub):
            print(f"{name}: {fmt(stats(sub['ret']))}")

    # 2b) 精细切分（找出倒U拐点）
    print("\n=== 成交额水位 精细切分 ===")
    bins = [(0, 0.90), (0.90, 1.00), (1.00, 1.10), (1.10, 1.20),
            (1.20, 1.30), (1.30, 1.50), (1.50, 9.99)]
    for lo, hi in bins:
        sub = trades[(trades["amount_wl"] >= lo) & (trades["amount_wl"] < hi)]
        if len(sub):
            print(f"[{lo:.2f},{hi:.2f}): {fmt(stats(sub['ret']))}")

    # 2c) 极端放量是否集中在某一年？
    print("\n=== 极端放量(>1.30) 按年份分布与胜率 ===")
    trades["hot"] = (trades["amount_wl"] > 1.30).astype(int)
    for y, g in trades.groupby("year"):
        h = g[g["hot"] == 1]
        c = g[g["hot"] == 0]
        hs = stats(h["ret"]) if len(h) else {"n": 0, "win": float("nan")}
        cs = stats(c["ret"])
        print(f"{y}: 信号{len(h):5d}个(占比{len(h)/len(g):.0%}) "
              f"放量日胜率{hs['win'] if len(h) else float('nan'):.1%} vs "
              f"非放量胜率{cs['win']:.1%}")

    # 2d) 买入日成交额水位（与信号日对比）
    trades["q_amount_buy"] = pd.qcut(trades["amount_wl_buy"], 5,
                                     labels=False, duplicates="drop")
    print("\n=== 按买入日成交额水位五分位 ===")
    for q in sorted(trades["q_amount_buy"].dropna().unique()):
        sub = trades[trades["q_amount_buy"] == q]
        rng = f"[{sub['amount_wl_buy'].min():.2f}, {sub['amount_wl_buy'].max():.2f}]"
        print(f"Q{q + 1} 水位{rng}: {fmt(stats(sub['ret']))}")

    # 2e) 过滤效果量化（可落地规则）
    print("\n=== 过滤效果量化 ===")
    for name, mask in [
            ("全部", trades["amount_wl"] == trades["amount_wl"]),
            ("剔除天量(>=1.50)", trades["amount_wl"] < 1.50),
            ("保留0.90~1.30", (trades["amount_wl"] >= 0.90)
             & (trades["amount_wl"] <= 1.30)),
            ("仅温和放量1.10~1.30", (trades["amount_wl"] > 1.10)
             & (trades["amount_wl"] <= 1.30))]:
        sub = trades[mask]
        if len(sub):
            print(f"{name}: {fmt(stats(sub['ret']))}")

    # 3) 成交量（亿股）水位五分位
    trades["q_vol"] = pd.qcut(trades["vol_wl"], 5, labels=False,
                              duplicates="drop")
    print("\n=== 按信号日全市场成交量（亿股）水位五分位 ===")
    for q in sorted(trades["q_vol"].dropna().unique()):
        sub = trades[trades["q_vol"] == q]
        rng = f"[{sub['vol_wl'].min():.2f}, {sub['vol_wl'].max():.2f}]"
        print(f"Q{q + 1} 水位{rng}: {fmt(stats(sub['ret']))}")

    # 4) 相关系数（量能水位 / 涨停家数 vs 收益 / 胜负）
    print("\n=== 相关系数（Pearson / Spearman） ===")
    for col, label in [("amount_wl", "成交额水位"),
                       ("vol_wl", "成交量水位"),
                       ("mkt_zt", "全市场涨停家数")]:
        p = trades[[col, "ret"]].dropna().corr(method="pearson").iloc[0, 1]
        s = trades[[col, "ret"]].dropna().corr(method="spearman").iloc[0, 1]
        pw = trades[[col, "win"]].dropna().corr(method="pearson").iloc[0, 1]
        print(f"{label}: 与收益 P={p:+.3f} S={s:+.3f} | 与胜负 P={pw:+.3f}")

    # 5) 年度对照（量能水位均值 vs 胜率）
    print("\n=== 年度：n/胜率/均收/成交额水位均值 ===")
    for y, g in trades.groupby("year"):
        st = stats(g["ret"])
        print(f"{y}: {fmt(st)} 水位均值{g['amount_wl'].mean():.2f}")

    # 6) 输出 markdown 报告
    lines = [
        "# 回测成功率与全市场成交量关系（2026-08-13 下午回测复盘）",
        "",
        f"> 样本：{len(trades)} 笔（行业卡点名单 {len(panels)} 家），"
        f"信号日 {trades['S_date'].min()} ~ {trades['S_date'].max()}，"
        f"v3 纪律（开盘买入/止损-4%/峰值回落8%/5日时间止损，成本0.2%）。",
        "",
        f"**总口径：** {fmt(stats(trades['ret']))}",
        "",
        "## 1. 按信号日全市场成交额水位（当日成交额 / 20日均值）",
        "",
        "| 分组 | 成交额水位 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for q in sorted(trades["q_amount"].dropna().unique()):
        sub = trades[trades["q_amount"] == q]
        st = stats(sub["ret"])
        lines.append(
            f"| Q{q + 1} | {sub['amount_wl'].min():.2f}~{sub['amount_wl'].max():.2f} "
            f"| {st['n']} | {st['win']:.1%} | {st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 2. 地量 vs 放量对照",
        "",
        "| 分组 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, mask in [
            ("地量(<0.90)", trades["amount_wl"] < 0.90),
            ("常态(0.90~1.10)", (trades["amount_wl"] >= 0.90)
             & (trades["amount_wl"] <= 1.10)),
            ("温和放量(1.10~1.30)", (trades["amount_wl"] > 1.10)
             & (trades["amount_wl"] <= 1.30)),
            ("显著放量(>1.30)", trades["amount_wl"] > 1.30)]:
        sub = trades[mask]
        if len(sub):
            st = stats(sub["ret"])
            lines.append(f"| {name} | {st['n']} | {st['win']:.1%} "
                         f"| {st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 2b. 成交额水位精细切分（找倒U拐点）",
        "",
        "| 成交额水位 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- |",
    ]
    for lo, hi in bins:
        sub = trades[(trades["amount_wl"] >= lo) & (trades["amount_wl"] < hi)]
        if len(sub):
            st = stats(sub["ret"])
            lines.append(f"| [{lo:.2f},{hi:.2f}) | {st['n']} | "
                         f"{st['win']:.1%} | {st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 2c. 极端放量(>1.30) 按年份：胜率对比",
        "",
        "| 年份 | 放量信号数 | 占比 | 放量日胜率 | 非放量胜率 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for y, g in trades.groupby("year"):
        h = g[g["hot"] == 1]
        c = g[g["hot"] == 0]
        hs = stats(h["ret"]) if len(h) else {"win": float("nan")}
        cs = stats(c["ret"])
        lines.append(f"| {y} | {len(h)} | {len(h)/len(g):.0%} | "
                     f"{hs['win']:.1%} | {cs['win']:.1%} |")
    lines += [
        "",
        "## 2e. 过滤效果量化（可落地规则）",
        "",
        "| 规则 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, mask in [
            ("全部", trades["amount_wl"] == trades["amount_wl"]),
            ("剔除天量(>=1.50)", trades["amount_wl"] < 1.50),
            ("保留0.90~1.30", (trades["amount_wl"] >= 0.90)
             & (trades["amount_wl"] <= 1.30)),
            ("仅温和放量1.10~1.30", (trades["amount_wl"] > 1.10)
             & (trades["amount_wl"] <= 1.30))]:
        sub = trades[mask]
        if len(sub):
            st = stats(sub["ret"])
            lines.append(f"| {name} | {st['n']} | {st['win']:.1%} | "
                         f"{st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 3. 相关系数",
        "",
        "| 因子 | 与收益 Pearson | 与收益 Spearman | 与胜负 Pearson |",
        "| --- | --- | --- | --- |",
    ]
    for col, label in [("amount_wl", "成交额水位"), ("vol_wl", "成交量水位"),
                       ("mkt_zt", "全市场涨停家数")]:
        p = trades[[col, "ret"]].dropna().corr(method="pearson").iloc[0, 1]
        s = trades[[col, "ret"]].dropna().corr(method="spearman").iloc[0, 1]
        pw = trades[[col, "win"]].dropna().corr(method="pearson").iloc[0, 1]
        lines.append(f"| {label} | {p:+.3f} | {s:+.3f} | {pw:+.3f} |")
    lines += ["", "## 4. 年度对照", "",
              "| 年份 | n | 胜率 | 均收 | 成交额水位均值 |",
              "| --- | --- | --- | --- | --- |"]
    for y, g in trades.groupby("year"):
        st = stats(g["ret"])
        lines.append(f"| {y} | {st['n']} | {st['win']:.1%} | {st['mean']:+.2%} "
                     f"| {g['amount_wl'].mean():.2f} |")
    lines += [
        "",
        "## 结论",
        "",
        "- **关系不是线性，是倒 U 型**：信号日全市场成交额温和放量（水位 1.10~1.30，"
        "即当日成交额是 20 日均值的 1.1~1.3 倍）时成功率最高（约 50%，PF≈2.2；"
        "其中 1.20~1.30 达 57.5%、PF 2.67）；"
        "地量（<0.90）与常态（0.90~1.10）差别不大（44~45%，PF 1.5~2.0）；"
        "**极端天量（≥1.50）是断崖：胜率 17.2%、均收 -1.75%、PF 0.57**；"
        "1.30~1.50 尚可（50%）。",
        "- 成交量（亿股）水位与成交额同向：Q5 极端量能 30.6% 胜率、均收 -0.48%、PF 0.84。",
        "- 线性相关系数为负（成交额水位与胜负 -0.17），但主要来自 Q5 极端尾部拖累；"
        "中段 1.15~1.30 反而最好，故不能用『量越大越好』的线性逻辑。",
        "- 全市场涨停家数同样是倒 U/尾部负相关（-0.17），与量能水位高度同步，"
        "两者本质是同一个『市场情绪温度』的不同测度。",
        "- **年份交叉（重要，规则要谨慎）**：天量断崖几乎全是 2024 年贡献——"
        "水位 ≥1.50 的 93 个信号里 88 个在 2024（当年放量日胜率 14.8% vs 非放量 42.5%，"
        "即 2024 年 9-10 月天量行情中的追高信号）；2022/2023 无 ≥1.50 信号，"
        "2025 仅 1 个，2026 仅 4 个（胜率 50%）。2026 年 >1.30 的放量日反而 62.5% 胜率，"
        "（强趋势中的放量=增量资金入场），说明硬性『放量就砍』会在强年份误杀。",
        "- 市场宽度（站上 MA20 个股占比≥50%）无法区分：2024 放量日宽度也 ≥50%"
        "（表面强势的诱多）但胜率仅 17%；宽度不是解药，关键还是量能位置 + 年份环境。",
        "- **可落地规则（建议）**：① 信号日成交额水位 ≥1.50 直接剔除（每年极少，"
        "2024 验证为灾难，过滤后样本 821 笔、胜率 47.1%、PF 2.01）；"
        "② 温和放量 1.10~1.30 加分（57.5%）；③ 若市场处于持续强趋势（如 2026），"
        "放量日不另做惩罚，以规则 ① 为主。后续可在 backtest.py 加"
        "『成交额水位 ≥1.50 跳过』过滤参数做全样本验证（本次仅复盘，未改模型）。",
        "",
        "- 补充说明：以上为 2026-08-13 下午回测的当前最优参数组样本，"
        "换参数组后样本与结论可能变化；仅供研究，不构成投资建议。",
        "",
    ]
    out = (Path(__file__).resolve().parent.parent / "docs"
           / "回测与全市场成交量.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入: {out}")


def main_strategy() -> None:
    """启动加仓模型（r7mA/r9A）：成功率与信号日全市场成交额水位的关系。

    收益直接用 strategy 输出的 ret（完整模型：底仓+加仓，-7%止损/回落10%
    止盈/10日时间止损/退潮保护），不做重算。
    """
    mkt = load_market_totals()
    p = paths.report_dir() / "启动加仓模型_交易明细_2026-08-13.csv"
    trades = pd.read_csv(p, dtype={"code": str}).rename(
        columns={"signal_date": "S_date"})
    trades = trades.merge(
        mkt[["date", "mkt_amount", "mkt_vol", "mkt_zt", "amount_wl", "vol_wl"]],
        left_on="S_date", right_on="date", how="left").drop(columns=["date"])
    trades["win"] = (trades["ret"] > 0).astype(int)
    trades["year"] = trades["S_date"].str[:4]
    trades["hot"] = (trades["amount_wl"] > 1.30).astype(int)

    print(f"启动加仓模型 总样本: {fmt(stats(trades['ret']))}")
    for rule, g in trades.groupby("rule"):
        print(f"  {rule}: {fmt(stats(g['ret']))}")

    print("\n=== 按信号日成交额水位五分位（全部） ===")
    trades["q_amount"] = pd.qcut(trades["amount_wl"], 5, labels=False,
                                 duplicates="drop")
    for q in sorted(trades["q_amount"].dropna().unique()):
        sub = trades[trades["q_amount"] == q]
        rng = f"[{sub['amount_wl'].min():.2f}, {sub['amount_wl'].max():.2f}]"
        print(f"Q{q + 1} 水位{rng}: {fmt(stats(sub['ret']))}")

    print("\n=== 按 rule × 成交额水位五分位 ===")
    for rule in ["r7mA", "r9A"]:
        g = trades[trades["rule"] == rule].copy()
        g["q"] = pd.qcut(g["amount_wl"], 5, labels=False, duplicates="drop")
        for q in sorted(g["q"].dropna().unique()):
            sub = g[g["q"] == q]
            rng = f"[{sub['amount_wl'].min():.2f}, {sub['amount_wl'].max():.2f}]"
            print(f"{rule} Q{q + 1} 水位{rng}: {fmt(stats(sub['ret']))}")

    print("\n=== 成交额水位精细切分（全部） ===")
    bins = [(0, 0.90), (0.90, 1.00), (1.00, 1.10), (1.10, 1.20),
            (1.20, 1.30), (1.30, 1.50), (1.50, 1.80), (1.80, 9.99)]
    for lo, hi in bins:
        sub = trades[(trades["amount_wl"] >= lo) & (trades["amount_wl"] < hi)]
        if len(sub):
            print(f"[{lo:.2f},{hi:.2f}): {fmt(stats(sub['ret']))}")

    print("\n=== 按年份（放量>1.30 vs 非放量） ===")
    for y, g in trades.groupby("year"):
        h = g[g["hot"] == 1]
        c = g[g["hot"] == 0]
        hs = stats(h["ret"]) if len(h) else None
        cs = stats(c["ret"])
        if hs:
            print(f"{y}: 放量{len(h)}个({len(h)/len(g):.0%}) 胜率{hs['win']:.1%} "
                  f"均收{hs['mean']:+.2%} vs 非放量胜率{cs['win']:.1%} "
                  f"均收{cs['mean']:+.2%}")
        else:
            print(f"{y}: 无放量信号 vs 非放量胜率{cs['win']:.1%}")

    print("\n=== 按年份（低水位<1.00 vs 高水位>=1.00） ===")
    for y, g in trades.groupby("year"):
        lo = g[g["amount_wl"] < 1.00]
        hi = g[g["amount_wl"] >= 1.00]
        ls = stats(lo["ret"])
        hs = stats(hi["ret"])
        print(f"{y}: 低水位 n={ls['n']} 胜率{ls['win']:.1%} 均收{ls['mean']:+.2%} "
              f"| 高水位 n={hs['n']} 胜率{hs['win']:.1%} 均收{hs['mean']:+.2%}")

    print("\n=== 2026 年单独细看（衰减是否与量能有关） ===")
    g26 = trades[trades["year"] == "2026"]
    for q in sorted(g26["q_amount"].dropna().unique()):
        sub = g26[g26["q_amount"] == q]
        print(f"2026 Q{q + 1}: {fmt(stats(sub['ret']))}")

    print("\n=== 相关系数（启动加仓模型） ===")
    for col, label in [("amount_wl", "成交额水位"), ("vol_wl", "成交量水位"),
                       ("mkt_zt", "全市场涨停家数")]:
        p = trades[[col, "ret"]].dropna().corr(method="pearson").iloc[0, 1]
        s = trades[[col, "ret"]].dropna().corr(method="spearman").iloc[0, 1]
        pw = trades[[col, "win"]].dropna().corr(method="pearson").iloc[0, 1]
        print(f"{label}: 与收益 P={p:+.3f} S={s:+.3f} | 与胜负 P={pw:+.3f}")

    print("\n=== 过滤效果量化 ===")
    for name, mask in [
            ("全部", trades["amount_wl"] == trades["amount_wl"]),
            ("剔除低量(<1.00)", trades["amount_wl"] >= 1.00),
            ("剔除天量(>=1.50)", trades["amount_wl"] < 1.50),
            ("保留0.90~1.30", (trades["amount_wl"] >= 0.90)
             & (trades["amount_wl"] <= 1.30)),
            ("仅温和放量1.10~1.30", (trades["amount_wl"] > 1.10)
             & (trades["amount_wl"] <= 1.30))]:
        sub = trades[mask]
        if len(sub):
            print(f"{name}: {fmt(stats(sub['ret']))}")

    # ── 输出 markdown ──
    lines = [
        "# 启动加仓模型成功率与全市场成交量关系（2026-08-13）",
        "",
        f"> 样本：{len(trades)} 笔（r7mA {len(trades[trades['rule']=='r7mA'])} + "
        f"r9A {len(trades[trades['rule']=='r9A'])}，全市场，"
        f"信号日 {trades['S_date'].min()} ~ {trades['S_date'].max()}）。",
        "> 收益=策略输出 ret（完整模型：底仓+加仓，-7%止损/回落10%止盈/"
        "10日时间止损/退潮保护）。量能水位=信号日全市场成交额/20日均值。",
        "",
        f"**总口径：** {fmt(stats(trades['ret']))}",
        "",
        "## 1. 按信号日成交额水位五分位",
        "",
        "| 分组 | 成交额水位 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for q in sorted(trades["q_amount"].dropna().unique()):
        sub = trades[trades["q_amount"] == q]
        st = stats(sub["ret"])
        lines.append(
            f"| Q{q + 1} | {sub['amount_wl'].min():.2f}~{sub['amount_wl'].max():.2f} "
            f"| {st['n']} | {st['win']:.1%} | {st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 2. 按 rule × 成交额水位五分位",
        "",
        "| rule | 分组 | 成交额水位 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rule in ["r7mA", "r9A"]:
        g = trades[trades["rule"] == rule].copy()
        g["q"] = pd.qcut(g["amount_wl"], 5, labels=False, duplicates="drop")
        for q in sorted(g["q"].dropna().unique()):
            sub = g[g["q"] == q]
            st = stats(sub["ret"])
            lines.append(
                f"| {rule} | Q{q + 1} | {sub['amount_wl'].min():.2f}~"
                f"{sub['amount_wl'].max():.2f} | {st['n']} | {st['win']:.1%} | "
                f"{st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 3. 成交额水位精细切分（全部）",
        "",
        "| 成交额水位 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- |",
    ]
    for lo, hi in bins:
        sub = trades[(trades["amount_wl"] >= lo) & (trades["amount_wl"] < hi)]
        if len(sub):
            st = stats(sub["ret"])
            lines.append(f"| [{lo:.2f},{hi:.2f}) | {st['n']} | "
                         f"{st['win']:.1%} | {st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 4. 按年份（放量>1.30 vs 非放量）",
        "",
        "| 年份 | 放量信号数 | 占比 | 放量胜率/均收 | 非放量胜率/均收 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for y, g in trades.groupby("year"):
        h = g[g["hot"] == 1]
        c = g[g["hot"] == 0]
        hs = stats(h["ret"]) if len(h) else None
        cs = stats(c["ret"])
        if hs:
            lines.append(f"| {y} | {len(h)} | {len(h)/len(g):.0%} | "
                         f"{hs['win']:.1%}/{hs['mean']:+.2%} | "
                         f"{cs['win']:.1%}/{cs['mean']:+.2%} |")
        else:
            lines.append(f"| {y} | 0 | 0% | - | {cs['win']:.1%}/{cs['mean']:+.2%} |")
    lines += [
        "",
        "## 4b. 按年份（低水位<1.00 vs 高水位>=1.00）",
        "",
        "| 年份 | 低水位 n/胜率/均收 | 高水位 n/胜率/均收 |",
        "| --- | --- | --- |",
    ]
    for y, g in trades.groupby("year"):
        lo = g[g["amount_wl"] < 1.00]
        hi = g[g["amount_wl"] >= 1.00]
        ls = stats(lo["ret"])
        hs = stats(hi["ret"])
        lines.append(f"| {y} | {ls['n']}/{ls['win']:.1%}/{ls['mean']:+.2%} | "
                     f"{hs['n']}/{hs['win']:.1%}/{hs['mean']:+.2%} |")
    lines += [
        "",
        "## 5. 相关系数",
        "",
        "| 因子 | 与收益 Pearson | 与收益 Spearman | 与胜负 Pearson |",
        "| --- | --- | --- | --- | --- |",
    ]
    for col, label in [("amount_wl", "成交额水位"), ("vol_wl", "成交量水位"),
                       ("mkt_zt", "全市场涨停家数")]:
        p = trades[[col, "ret"]].dropna().corr(method="pearson").iloc[0, 1]
        s = trades[[col, "ret"]].dropna().corr(method="spearman").iloc[0, 1]
        pw = trades[[col, "win"]].dropna().corr(method="pearson").iloc[0, 1]
        lines.append(f"| {label} | {p:+.3f} | {s:+.3f} | {pw:+.3f} |")
    lines += [
        "",
        "## 6. 过滤效果量化",
        "",
        "| 规则 | n | 胜率 | 均收 | PF |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, mask in [
            ("全部", trades["amount_wl"] == trades["amount_wl"]),
            ("剔除低量(<1.00)", trades["amount_wl"] >= 1.00),
            ("剔除天量(>=1.50)", trades["amount_wl"] < 1.50),
            ("保留0.90~1.30", (trades["amount_wl"] >= 0.90)
             & (trades["amount_wl"] <= 1.30)),
            ("仅温和放量1.10~1.30", (trades["amount_wl"] > 1.10)
             & (trades["amount_wl"] <= 1.30))]:
        sub = trades[mask]
        if len(sub):
            st = stats(sub["ret"])
            lines.append(f"| {name} | {st['n']} | {st['win']:.1%} | "
                         f"{st['mean']:+.2%} | {st['pf']:.2f} |")
    lines += [
        "",
        "## 结论",
        "",
        "- **与 T0 模型相反：启动加仓模型是『量能越高越好』（正相关）**。"
        "成交额水位与收益 Pearson +0.41 / Spearman +0.50，与胜负 +0.32；"
        "水位五分位单调上升（Q1 37.8% → Q5 93.5%）。",
        "- **低量信号是坑**：信号日成交额水位 <1.00 时胜率 34~38%、均收为负"
        "（其中 0.90~1.00 最差 34.1%；2024 年低水位 2865 笔仅 37.3%）；"
        "缩量环境下涨停家数虽达标但无增量资金，反弹走不远。"
        "该模型赚的是『市场共振』的钱，需要量能确认。",
        "- **温和放量是最佳区**：水位 1.10~1.30 胜率 82.0%、均收 +13.24%、PF 15.93"
        "（1.20~1.30 达 85.0%）；≥1.30 的极端天量样本少（428 笔），"
        "整体仍 67~73%，与 T0 的『天量断崖』完全不同。",
        "- **过滤效果**：剔除低量(<1.00) 后 14,737 笔：胜率 78.3%、均收 +11.17%、"
        "PF 11.28（vs 全部 68.0%/8.26%/6.27）；建议作为前置条件"
        "（≥1.00，最好 ≥1.10）。",
        "- **2026 年衰减是环境问题，量能过滤救不了**：2026 年低水位 44.3%、"
        "高水位 40.4% 都差（>1.30 的 53 笔 52.8% 稍好但样本小），"
        "与 T0 模型 2026 放量日反而好形成对比——2026 年两套模型都应降档运行。",
        "- 补充说明：样本全市场 2021-02 ~ 2026-08；收益为策略输出 ret；"
        "仅供参考，不构成投资建议。",
        "",
    ]
    out = (Path(__file__).resolve().parent.parent / "docs"
           / "启动加仓模型与全市场成交量.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    import sys
    if "--strategy" in sys.argv:
        main_strategy()
    else:
        main()
