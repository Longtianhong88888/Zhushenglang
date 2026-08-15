"""硬规则主题·训练频率研究：多久重新选一次主题最优？

问题（用户 2026-08-14）：主题选择（每年初按过去收益排名选 Top3）的重选频率
应该是多少？固定（选一次用到底）/ 每年 / 每半年 / 每季度？

方法（滚动 walk-forward，无未来函数）：
- 决策日：从 2021-06 起按频率间隔（季度≈3月/半年≈6月/一年≈12月）
- 每个决策日 d：用过去 W 个交易日（默认 250）各主题作为唯一热主题跑模型
  收益排名（交易数≥3 门槛）选 Top3 → 该主题股票集 → 应用于下一个决策区间
- 用 portfolio_bt.simulate 的 hot_by_date 逐日切换热主题集合，全期 2021-2026
- 对比：固定 T3（AI硬件/半导体/存储，现规则，2021 初选一次用到底）

用法:
    python3 -m mainrise.theme_freq
"""
from __future__ import annotations

import time
from datetime import timedelta

import numpy as np
import pandas as pd

from mainrise import bigtrend, paths, portfolio_bt
from mainrise.data import load_all_panels
from mainrise.entry_study import market_features
from mainrise.report import load_chokepoint_codes
from mainrise.signals import in_universe

WINDOW_TRADING = 250       # 每次重选用的训练窗口（交易日）
MIN_TRADES = 3
FIXED_T3 = {"AI硬件", "半导体", "存储"}

# 频率：季度/半年/一年（用交易日近似）
FREQS = [
    ("每季度重选", 63),
    ("每半年重选", 126),
    ("每年重选", 250),
]


def _info_upto(panels: pd.DataFrame, end_date: str, start_date: str | None = None) -> dict:
    sub = panels[panels["date"] <= end_date]
    if start_date:
        sub = sub[sub["date"] >= start_date]
    return portfolio_bt.build_info(sub, set(), portfolio_bt.MIN_T0_90)


def _pick_themes(info: dict, theme_map: dict, base: dict) -> list:
    """训练期各主题收益排名选 Top3（交易数≥3 门槛，不足补收益最高）。"""
    rank = []
    for theme in sorted({th for th in theme_map.values() if th != "其他"}):
        hs = {c for c, th in theme_map.items() if th == theme}
        if len(hs) < 3:
            continue
        sim = portfolio_bt.simulate(info, hs, 3, hard_rule=True,
                                    score_min=2, **base)
        nv = sim["nav"]
        r = nv["nav"].iloc[-1] / nv["nav"].iloc[0] - 1 if len(nv) else np.nan
        rank.append((theme, r, len(sim["trades"])))
    rank.sort(key=lambda x: -x[1])
    credible = [t for t, r, n in rank if n >= MIN_TRADES]
    chosen = credible[:3]
    for t, r, n in rank:
        if t not in chosen:
            chosen.append(t)
        if len(chosen) == 3:
            break
    return chosen


def run() -> str:
    t0 = time.time()
    print("加载行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    ck = {c for c in load_chokepoint_codes()
          if not c.startswith("301") and not c.startswith("688")}
    panels = full[full["code"].isin(ck)].copy()
    dates_all = sorted(panels["date"].unique())

    mkt = market_features(full)
    del full
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))
    theme_map = bigtrend.load_theme()
    base = dict(mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20, rebuy="none")

    all_codes = set(theme_map)
    info_full = portfolio_bt.build_info(panels, all_codes, portfolio_bt.MIN_T0_90)

    # 固定 T3（现规则）
    hot_fixed = {c for c, th in theme_map.items() if th in FIXED_T3}
    sim_fixed = portfolio_bt.simulate(info_full, hot_fixed, 3, hard_rule=True,
                                      score_min=2, **base)

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 硬规则主题·训练频率研究（{dstr}）")
    L.append("")
    L.append(f"> 滚动 walk-forward：每决策日用过去 {WINDOW_TRADING} 个交易日各主题"
             "收益排名（交易数≥3）选 Top3，应用于下一区间（hot_by_date 逐日切换）；"
             "对比固定 T3（AI硬件/半导体/存储）。规则全同（硬规则+评分≥2+1/3仓×3只"
             "+MA20退出+杀跌区停开）。")
    L.append("")

    results = {"固定 T3（现规则）": sim_fixed}
    freq_notes = {}

    for label, gap in FREQS:
        # 决策日：从训练窗口后的首个可决策日起，每 gap 交易日一次
        decisions = []
        i = WINDOW_TRADING + 1
        while i < len(dates_all) - 1:
            decisions.append(dates_all[i])
            i += gap
        # 训练窗口起点（供展示）
        start_i = 0
        hot_by_date = {}
        switches = []
        prev_chosen = None
        for k, d in enumerate(decisions):
            d_idx = dates_all.index(d)
            w_start = dates_all[max(0, d_idx - WINDOW_TRADING)]
            info_w = _info_upto(panels, d, w_start)
            chosen = _pick_themes(info_w, theme_map, base)
            if prev_chosen is not None:
                switches.append(len(set(chosen) - set(prev_chosen))
                                + len(set(prev_chosen) - set(chosen)))
            prev_chosen = set(chosen)
            seg_end = decisions[k + 1] if k + 1 < len(decisions) else dates_all[-1]
            hs = {c for c, th in theme_map.items() if th in set(chosen)}
            for dd in dates_all[d_idx:dates_all.index(seg_end) + 1]:
                hot_by_date[dd] = hs
        sim = portfolio_bt.simulate(info_full, set(), 3, hard_rule=True,
                                    score_min=2, hot_by_date=hot_by_date, **base)
        results[label] = sim
        freq_notes[label] = (len(decisions), switches, decisions)
        print(f"  {label}: {len(decisions)} 次重选")

    L.append("## 一、总体对比")
    L.append("")
    L.append("| 训练频率 | 重选次数 | 交易 | 胜率 | 总收益 | 年化 | 最大回撤 | PF | 抓到≥60% |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for label, sim in results.items():
        m = portfolio_bt.metrics(sim, label)
        n_sel = freq_notes.get(label, ("—", None))[0] if label != "固定 T3（现规则）" else 1
        L.append(f"| {label} | {n_sel} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | "
                 f"{m[5]} | {m[6]} | {m[7]} |")
    L.append("")

    # 逐年对比
    L.append("## 二、逐年收益对比")
    L.append("")
    L.append("| 年份 | " + " | ".join(results.keys()) + " |")
    L.append("| --- |" + " --- |" * len(results))
    navs = {label: sim["nav"].copy() for label, sim in results.items()}
    for label, nv in navs.items():
        nv["year"] = nv["date"].str[:4]
    years = sorted({y for nv in navs.values() for y in nv["year"]})
    for yr in years:
        row = f"| {yr} |"
        for label, nv in navs.items():
            g = nv[nv["year"] == yr]
            r = g["nav"].iloc[-1] / g["nav"].iloc[0] - 1 if len(g) else np.nan
            row += f" {r:+.0%} |"
        L.append(row)
    L.append("")

    # 主题切换轨迹（每频率）
    L.append("## 三、各频率的主题切换轨迹（决策日 → 选出主题）")
    L.append("")
    for label, gap in FREQS:
        L.append(f"**{label}**")
        L.append("")
        L.append("| 决策日 | 选出主题 |")
        L.append("| --- | --- |")
        n_sel, switches, decisions = freq_notes[label]
        d_idx = dates_all.index(decisions[0])
        info_w0 = _info_upto(panels, decisions[0],
                             dates_all[max(0, d_idx - WINDOW_TRADING)])
        for k, d in enumerate(decisions):
            if k > 0:
                info_w = _info_upto(panels, d,
                                    dates_all[max(0, dates_all.index(d)
                                                  - WINDOW_TRADING)])
            else:
                info_w = info_w0
            chosen = _pick_themes(info_w, theme_map, base)
            L.append(f"| {d} | {'、'.join(chosen)} |")
        L.append(f"- 平均每次重选切换主题数：{np.mean(switches):.1f}")
        L.append("")

    L.append("## 四、结论")
    L.append("")
    L.append("- 若动态重选（任何频率）优于固定 T3 → 值得按该频率重选；若全部劣于固定"
             " → 主题 alpha 足够持续，固定即可，重选只是追热点（与热主题动态化结论"
             "同构）。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"主题训练频率_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


if __name__ == "__main__":
    run()
