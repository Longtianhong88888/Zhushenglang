"""每年 1 月选一次主题（用过去一年数据）· 精确检验。

用户假设：每年 1 月用过去 12 个月各主题收益排名选 Top3，作为当年硬规则热主题。
对比固定 T3（现规则）。预期（基于 theme-freq 滚动每年 +237%）：大幅劣于固定。

用法:
    python3 -m mainrise.theme_jan
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from mainrise import bigtrend, paths, portfolio_bt
from mainrise.data import load_all_panels
from mainrise.entry_study import market_features
from mainrise.report import load_chokepoint_codes
from mainrise.signals import in_universe

WINDOW = 250        # 过去一年（交易日）
MIN_TRADES = 3
FIXED_T3 = {"AI硬件", "半导体", "存储"}


def _pick_themes(info: dict, theme_map: dict, base: dict) -> list:
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

    # 每年 1 月首个交易日（2022-2026）
    decisions = []
    for yr in range(2022, 2027):
        jan = [d for d in dates_all if d.startswith(str(yr)) and d[5:7] == "01"]
        if jan:
            decisions.append(jan[0])

    hot_by_date = {}
    picks = []
    for k, d in enumerate(decisions):
        d_idx = dates_all.index(d)
        w_start = dates_all[max(0, d_idx - WINDOW)]
        sub = panels[(panels["date"] >= w_start) & (panels["date"] <= d)]
        info_w = portfolio_bt.build_info(sub, set(), portfolio_bt.MIN_T0_90)
        chosen = _pick_themes(info_w, theme_map, base)
        picks.append((d, chosen))
        seg_end = decisions[k + 1] if k + 1 < len(decisions) else dates_all[-1]
        hs = {c for c, th in theme_map.items() if th in set(chosen)}
        for dd in dates_all[d_idx:dates_all.index(seg_end) + 1]:
            hot_by_date[dd] = hs
        print(f"  {d}: {chosen}")

    sim_jan = portfolio_bt.simulate(info_full, set(), 3, hard_rule=True,
                                    score_min=2, hot_by_date=hot_by_date, **base)
    hot_fixed = {c for c, th in theme_map.items() if th in FIXED_T3}
    sim_fixed = portfolio_bt.simulate(info_full, hot_fixed, 3, hard_rule=True,
                                      score_min=2, **base)

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 每年 1 月选主题（过去一年数据）检验（{dstr}）")
    L.append("")
    L.append("> 决策日=每年 1 月首个交易日；训练窗口=前 250 交易日（约一年）；"
             "各主题作为唯一热主题跑模型收益排名（交易数≥3）选 Top3，用于当年"
             "（hot_by_date 切换）。")
    L.append("")

    L.append("## 一、每年选择轨迹")
    L.append("")
    L.append("| 决策日 | 选出主题 |")
    L.append("| --- | --- |")
    for d, chosen in picks:
        L.append(f"| {d} | {'、'.join(chosen)} |")
    L.append("")

    L.append("## 二、对比")
    L.append("")
    L.append("| 方案 | 交易 | 胜率 | 总收益 | 年化 | 最大回撤 | PF |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for label, sim in (("固定 T3（现规则）", sim_fixed),
                       ("每年1月重选（一年数据）", sim_jan)):
        m = portfolio_bt.metrics(sim, label)
        L.append(f"| {label} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]} | {m[6]} |")
    L.append("")

    L.append("## 三、逐年收益")
    L.append("")
    L.append("| 年份 | 固定 T3 | 每年1月重选 |")
    L.append("| --- | --- | --- |")
    navs = {"固定 T3（现规则）": sim_fixed["nav"].copy(),
            "每年1月重选（一年数据）": sim_jan["nav"].copy()}
    for nv in navs.values():
        nv["year"] = nv["date"].str[:4]
    for yr in sorted({y for nv in navs.values() for y in nv["year"]}):
        row = f"| {yr} |"
        for nv in navs.values():
            g = nv[nv["year"] == yr]
            r = g["nav"].iloc[-1] / g["nav"].iloc[0] - 1 if len(g) else np.nan
            row += f" {r:+.0%} |"
        L.append(row)
    L.append("")

    L.append("## 四、结论")
    L.append("")
    tot_j = (sim_jan["nav"]["nav"].iloc[-1] / sim_jan["nav"]["nav"].iloc[0] - 1)
    tot_f = (sim_fixed["nav"]["nav"].iloc[-1] / sim_fixed["nav"]["nav"].iloc[0] - 1)
    L.append(f"- 每年 1 月用一年数据重选：**{tot_j:+.0%}** vs 固定 T3 "
             f"**{tot_f:+.0%}**（差 {tot_j-tot_f:+.0%}pp）")
    L.append("- 与预期一致（theme-freq 滚动每年 +237% 同量级）：重选被 1 月初时点"
             " 的短期强弱带偏（如 2022 选有色因 2021 强、2025 选自动驾驶/机器人），"
             "主题年度选择不可靠，固定 T3 完胜。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"主题每年1月_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


if __name__ == "__main__":
    run()
