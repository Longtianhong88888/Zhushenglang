"""暴跌反转研究：20 日内高点回撤 >=30% 后首次放量涨停 + 次日不破 = 反转启动信号。

规则（研究版，2026-08-13）：
- 暴跌：当日收盘 <= 前 20 日最高价 x (1 - 30%)（近 10 日内出现过即视为暴跌窗口）
- 启动：暴跌窗口内首次涨停（收盘>=涨停价）且量比 >=1.0（放量涨停，缩量/一字不计）
- 确认：次日收盘 >= 涨停日收盘，且次日未再涨停（连板不计）
- 入场：确认后次日（T+2）开盘买入
- 输出：+3/+5/+10 日收益、胜率、按年份/回撤深度/量比分组，与主升浪基线对比

用法:
    python3 -m mainrise.cli reversal    # 生成 output/reports/暴跌反转研究_*.md + 信号 CSV
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.monitor import TZ_CN
from mainrise.report import load_chokepoint_codes, load_industry_info

MIN_CRASH = 0.30      # 20 日高点回撤 >=30%
VR_MIN = 1.0          # 涨停日量比门槛（与 T0 规则一致）
CRASH_WIN = 10        # 暴跌出现后 10 日内视为暴跌窗口
HORIZONS = (3, 5, 10)


def detect(panels: pd.DataFrame, min_crash: float = MIN_CRASH,
           vr_min: float = VR_MIN) -> pd.DataFrame:
    """全市场扫描暴跌反转信号。返回每只信号一行。"""
    rows = []
    for code, g in panels.groupby("code", sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        n = len(g)
        if n < 45:
            continue
        close = g["close"].to_numpy(float)
        high = g["high"].to_numpy(float)
        vol = g["volume"].to_numpy(float)
        lim = g["limit_price"].to_numpy(float)
        prev = g["prev_close"].to_numpy(float)
        dates = g["date"].tolist()
        open_p = g["open"].to_numpy(float)
        hi20 = pd.Series(high).rolling(20).max().shift(1).to_numpy()
        v5 = pd.Series(vol).rolling(5).mean().shift(1).to_numpy()
        crash = close <= hi20 * (1 - min_crash)
        crash_win = pd.Series(crash).rolling(CRASH_WIN, min_periods=1).max().to_numpy()
        limit_up = close >= lim - 1e-6
        vr = np.where(v5 > 0, vol / np.where(v5 > 0, v5, 1), np.nan)

        i = 0
        blocked_until: str | None = None
        while i < n:
            if blocked_until is not None and dates[i] < blocked_until:
                i += 1
                continue
            vr_ok = bool(np.isnan(vr[i]) or vr[i] >= vr_min)
            # 信号日仍需低于 20 日高点（未收复），保证"暴跌中反转"语义
            if (crash_win[i] and limit_up[i] and close[i] < hi20[i]
                    and vr_ok and i + 2 < n):
                c1 = g.iloc[i + 1]
                not_limit_next = c1["close"] < c1["limit_price"] - 1e-6
                if c1["close"] >= close[i] and not_limit_next:
                    rows.append({
                        "code": code,
                        "S_date": dates[i],
                        "confirm_date": dates[i + 1],
                        "buy_date": dates[i + 2],
                        "buy_i": i + 2,
                        "zt_chg": (close[i] / prev[i] - 1) * 100,
                        "vr": vr[i],
                        "crash_depth": (close[i] / hi20[i] - 1) * 100,
                        "buy_open": open_p[i + 2],
                    })
                    # 一个反转周期只计一次：跳过直到暴跌窗口结束
                    j = i + 1
                    while j < n and crash_win[j]:
                        j += 1
                    blocked_until = dates[j] if j < n else "9999-12-31"
                    i = j
                    continue
            i += 1
    return pd.DataFrame(rows)


def forward_returns(sig: pd.DataFrame, by_code: dict) -> pd.DataFrame:
    """从 buy_date 开盘入场，计算 +3/+5/+10 日收盘收益。"""
    out = sig.copy()
    for k in HORIZONS:
        out[f"ret{k}"] = np.nan
    for i, r in sig.iterrows():
        g = by_code.get(r["code"])
        if g is None or r["buy_i"] >= len(g):
            continue
        entry = g.iloc[int(r["buy_i"])]["open"]
        if not entry or entry <= 0:
            continue
        for k in HORIZONS:
            j = int(r["buy_i"]) + k
            if j < len(g):
                out.at[i, f"ret{k}"] = g.iloc[j]["close"] / entry - 1
    return out


def _stat(s: pd.DataFrame, label: str) -> str:
    if s.empty:
        return f"{label}: 无样本"
    r5 = s["ret5"].dropna()
    r10 = s["ret10"].dropna()
    return (f"{label}: n={len(s)} | +5日胜率{(r5 > 0).mean() * 100:5.1f}% "
            f"均收{r5.mean() * 100:+6.2f}% 中位{r5.median() * 100:+6.2f}% | "
            f"+10日胜率{(r10 > 0).mean() * 100:5.1f}% 均收{r10.mean() * 100:+6.2f}%")


def run(output_dir=None) -> str:
    """执行暴跌反转研究：全市场 + 行业卡点企业两组对比，写 CSV + Markdown。"""
    from mainrise.data import load_all_panels
    from mainrise.signals import in_universe

    panels = load_all_panels()
    panels = panels[panels["code"].map(in_universe)]
    panels = panels[~panels["is_st"].fillna(0).astype(int).astype(bool)]
    panels = panels[~panels["is_paused"].fillna(0).astype(int).astype(bool)]
    panels = panels.sort_values(["code", "date"])
    by_code = {c: g.reset_index(drop=True)
               for c, g in panels.groupby("code", sort=False)}

    print("扫描暴跌反转信号（全市场）...")
    sig_full = detect(panels)
    sig_full = forward_returns(sig_full, by_code)
    sig_full["year"] = sig_full["S_date"].astype(str).str[:4]
    chokepoint = load_chokepoint_codes()
    sig = sig_full[sig_full["code"].isin(chokepoint)].copy()
    sig["pos"] = sig["code"].map(
        lambda c: load_industry_info().get(c, {}).get("pos"))
    valid_full = sig_full.dropna(subset=["ret5"])
    valid = sig.dropna(subset=["ret5"])
    print(f"全市场信号 {len(sig_full)} 个（有效 {len(valid_full)}）；"
          f"行业卡点企业 {len(chokepoint)} 家，信号 {len(sig)} 个（有效 {len(valid)}）")

    out = Path(output_dir) if output_dir else paths.report_dir()
    out.mkdir(parents=True, exist_ok=True)
    date = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    csv_full = out / f"暴跌反转信号_全市场_{date}.csv"
    csv_path = out / f"暴跌反转信号_卡点_{date}.csv"
    sig_full.to_csv(csv_full, index=False, encoding="utf-8-sig")
    sig.to_csv(csv_path, index=False, encoding="utf-8-sig")

    lines = [f"# 暴跌反转研究（{date}）",
             f"> 规则：20 日内高点回撤 ≥30% 后，暴跌窗口（10 日）内首次放量涨停"
             f"（量比≥{VR_MIN:g}）+ 次日收盘不破涨停日收盘（且非连板）→ "
             "确认后次日（T+2）开盘买入",
             f"> **范围：模型只追踪行业卡点企业（{len(chokepoint)} 家，"
             "industry_info.csv），全市场结果仅作对比**",
             "> 免责：研究线索，不构成投资建议", ""]
    lines.append("## 总体：行业卡点企业 vs 全市场")
    lines.append(_stat(valid, "行业卡点企业"))
    lines.append(_stat(valid_full, "全市场（对比）"))
    lines.append("")
    lines.append("## 行业卡点企业 · 按年份")
    for y in sorted(valid["year"].unique()):
        lines.append(_stat(valid[valid["year"] == y], y))
    lines.append("")
    lines.append("## 行业卡点企业 · 按回撤深度")
    for lo, hi, lab in [(-100, -50, "回撤 50%+"), (-50, -40, "40-50%"),
                        (-40, -30, "30-40%"), (-30, 0, "0-30%")]:
        s = valid[(valid["crash_depth"] >= lo) & (valid["crash_depth"] < hi)]
        lines.append(_stat(s, lab))
    lines.append("")
    lines.append("## 行业卡点企业 · 按涨停日量比")
    for lo, hi in [(1.0, 1.5), (1.5, 2.5), (2.5, 99)]:
        s = valid[(valid["vr"] >= lo) & (valid["vr"] < hi)]
        lines.append(_stat(s, f"量比 {lo:g}-{hi:g}"))
    lines.append("")
    lines.append("## 行业卡点企业 · 按卡点强度（产业地位评分 pos）")
    for lo, hi, lab in [(85, 101, "核心卡点 pos≥85"), (0, 85, "一般 pos<85")]:
        s = valid[(valid["pos"] >= lo) & (valid["pos"] < hi)]
        lines.append(_stat(s, lab))
    lines.append("")
    lines.append("## 与主升浪基线对比（+5 日，全市场历史）")
    base = valid_full["ret5"]
    base_cp = valid["ret5"]
    lines.append(f"- 主升浪模型 +5 日胜率约 38.2%、均收 -1.31%（2022-2026，3 万信号）")
    lines.append(f"- 暴跌反转全市场 +5 日胜率 {(base > 0).mean() * 100:.1f}%、"
                 f"均收 {base.mean() * 100:+.2f}%")
    lines.append(f"- 暴跌反转卡点企业 +5 日胜率 {(base_cp > 0).mean() * 100:.1f}%、"
                 f"均收 {base_cp.mean() * 100:+.2f}%")
    lines.append("")
    recent = valid[valid["S_date"].astype(str) >= "2026-07-01"].sort_values("S_date")
    if not recent.empty:
        lines.append("## 近期信号（2026-07 起）")
        lines.append("| 日期 | 代码 | 涨停% | 量比 | 回撤% | +5日% | +10日% |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in recent.iterrows():
            lines.append(f"| {r['S_date']} | {r['code']} | {r['zt_chg']:.1f} | "
                         f"{r['vr']:.2f} | {r['crash_depth']:.0f} | "
                         f"{r['ret5'] * 100:+.1f} | {r['ret10'] * 100:+.1f} |")
        lines.append("")
    fh = valid[(valid["code"] == "000636") & (valid["S_date"] >= "2026-07-01")]
    if not fh.empty:
        lines.append("## 实例：风华高科（000636）")
        for _, r in fh.iterrows():
            lines.append(f"- {r['S_date']} 涨停 {r['zt_chg']:.1f}%（量比 {r['vr']:.2f}，"
                         f"距高点 {r['crash_depth']:.0f}%）→ {r['buy_date']} 开盘入场，"
                         f"+5 日 {r['ret5'] * 100:+.1f}%，+10 日 {r['ret10'] * 100:+.1f}%")
    lines.append(f"- 风华高科（000636）{'在' if '000636' in chokepoint else '**不在**'}卡点企业名单"
                 f"{'，模型正常追踪' if '000636' in chokepoint else '，按模型范围不追踪'}")
    lines.append("")
    lines.append("> 信号明细: " + csv_path.name)
    md = "\n".join(lines)
    md_path = out / f"暴跌反转研究_{date}.md"
    md_path.write_text(md, encoding="utf-8")
    print(md)
    print(f"研究报告: {md_path}")
    return str(md_path)


if __name__ == "__main__":
    run()
