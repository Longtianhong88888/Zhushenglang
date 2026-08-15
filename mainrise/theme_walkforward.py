"""硬规则主题 walk-forward 检验：2025 年初（只用当时数据）该定义什么主题？

问题（用户 2026-08-14）：模型硬规则热主题=AI硬件/半导体/存储 是 2021-2026
全期回测选出的事后最优；回到 2025 年初，基于当时可用数据（截至 2024-12-31）
能否事前选出接近的组合？还是纯后视镜？

方法：
- 候选组合：
  A 事后最优（全期回测）＝AI硬件/半导体/存储
  B 当时可观测（2024-12-31 周期卡主线）＝机器人/半导体/存储
  C 单主题 ×8（逐一作为唯一热主题）
- 每个组合跑同一模拟器（硬规则+评分≥2+1/3仓×3只+MA20退出+杀跌区停开），
  净值按 2025-01-01 重置，对比 2025 年 / 2025~2026-08 表现。
- 另：用 2021-2024 训练期选主题（每主题在训练期的 simulate 收益排名）作为
  "事前模型口径选择"，与事后最优对比。

用法:
    python3 -m mainrise.theme_walkforward
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

TRAIN_END = "2024-12-31"       # 训练期截止（2025 年初决策点）
EVAL_START = "2025-01-01"      # 评估起点

# 训练窗口：不同长度（站在 2024-12-31，用过去 N 个月数据选主题）
WINDOWS = [
    ("半年（2024-07 起）", "2024-07-01"),
    ("一年（2024-01 起）", "2024-01-01"),
    ("一年半（2023-07 起）", "2023-07-01"),
    ("两年（2023-01 起）", "2023-01-01"),
    ("四年（2021-01 起）", "2021-01-01"),   # 原参照
]
MIN_TRADES = 3        # 主题可信门槛：训练期至少 N 笔交易


def _info_upto(panels: pd.DataFrame, end_date: str) -> dict:
    """构建截至 end_date 的 info（信号只含该日前的历史，无前视）。"""
    sub = panels[panels["date"] <= end_date]
    return portfolio_bt.build_info(sub, set(), portfolio_bt.MIN_T0_90)


def _nav_from(sim: dict, start: str) -> pd.DataFrame:
    nv = sim["nav"].copy()
    nv = nv[nv["date"] >= start].reset_index(drop=True)
    if len(nv):
        nv["nav"] = nv["nav"] / float(nv["nav"].iloc[0])
    return nv


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

    mkt = market_features(full)
    del full
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))
    theme_map = bigtrend.load_theme()
    base = dict(mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20, rebuy="none")

    all_codes = set(theme_map)
    info_full = portfolio_bt.build_info(panels, all_codes, portfolio_bt.MIN_T0_90)
    print(f"build_info 完成（{len(info_full)} 只）")

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 硬规则主题 walk-forward 检验（多训练窗口，{dstr}）")
    L.append("")
    L.append(f"> 问题：2025 年初（{TRAIN_END}）定义硬规则热主题，用过去 半年/一年/"
             "一年半/两年/四年 数据各能选出什么？2025 年表现如何？")
    L.append("> 口径：同一模拟器（硬规则+评分≥2+1/3仓×3只+MA20退出+杀跌区停开）；"
             "训练期每主题作为唯一热主题跑收益排名（交易数≥3 才可信）；选 Top3 后"
             "全期跑，净值按 2025-01-01 重置。")
    L.append("")

    # 各窗口选主题 + 2025 表现
    L.append("## 一、各训练窗口选出的主题与 2025 表现")
    L.append("")
    L.append("| 训练窗口 | 选出主题(训练期收益/笔) | 2025收益 | 2025回撤 | 2026 |")
    L.append("| --- | --- | --- | --- | --- |")
    results = {}
    for label, wstart in WINDOWS:
        pw = panels[(panels["date"] >= wstart) & (panels["date"] <= TRAIN_END)]
        info_w = portfolio_bt.build_info(pw, set(), portfolio_bt.MIN_T0_90)
        rank = []
        for theme in sorted({th for th in theme_map.values() if th != "其他"}):
            hs = {c for c, th in theme_map.items() if th == theme}
            if len(hs) < 3:
                continue
            sim = portfolio_bt.simulate(info_w, hs, 3, hard_rule=True,
                                        score_min=2, **base)
            nv = sim["nav"]
            r = nv["nav"].iloc[-1] / nv["nav"].iloc[0] - 1 if len(nv) else np.nan
            rank.append((theme, r, len(sim["trades"])))
        rank.sort(key=lambda x: -x[1])
        # 选主题：交易数≥3 的收益排名；不足则补收益最高者
        credible = [t for t, r, n in rank if n >= MIN_TRADES]
        chosen = credible[:3]
        if len(chosen) < 3:
            for t, r, n in rank:
                if t not in chosen:
                    chosen.append(t)
                if len(chosen) == 3:
                    break
        hs = {c for c, th in theme_map.items() if th in set(chosen)}
        sim = portfolio_bt.simulate(info_full, hs, 3, hard_rule=True,
                                    score_min=2, **base)
        nv25 = _nav_from(sim, EVAL_START)
        nv26 = _nav_from(sim, "2026-01-01")
        r25 = nv25["nav"].iloc[-1] / nv25["nav"].iloc[0] - 1
        dd25 = (nv25["nav"] / nv25["nav"].cummax() - 1).min()
        r26 = (nv26["nav"].iloc[-1] / nv26["nav"].iloc[0] - 1
               if len(nv26) else np.nan)
        results[label] = (chosen, r25, dd25, r26)
        sel = "、".join(f"{t}({r:+.0%}/{n}笔)" for t, r, n in rank
                        if t in chosen)
        L.append(f"| {label} | {sel} | {r25:+.0%} | {dd25:.0%} | "
                 f"{r26:+.0%}" if r26 == r26 else f"| {label} | {sel} | {r25:+.0%} | {dd25:.0%} | - |")
    L.append("")
    L.append("| 参照：事后最优组合 | AI硬件/半导体/存储 | +390% | -22% | +102% |")
    L.append("")

    # 各窗口训练期排名明细（关键窗口展示）
    L.append("## 二、各窗口训练期主题排名（当时可观测）")
    L.append("")
    for label, wstart in WINDOWS:
        pw = panels[(panels["date"] >= wstart) & (panels["date"] <= TRAIN_END)]
        info_w = portfolio_bt.build_info(pw, set(), portfolio_bt.MIN_T0_90)
        rank = []
        for theme in sorted({th for th in theme_map.values() if th != "其他"}):
            hs = {c for c, th in theme_map.items() if th == theme}
            if len(hs) < 3:
                continue
            sim = portfolio_bt.simulate(info_w, hs, 3, hard_rule=True,
                                        score_min=2, **base)
            nv = sim["nav"]
            r = (nv["nav"].iloc[-1] / nv["nav"].iloc[0] - 1 if len(nv) else np.nan)
            rank.append((theme, r, len(sim["trades"])))
        rank.sort(key=lambda x: -x[1])
        L.append(f"**{label}**（{wstart} ~ {TRAIN_END}）")
        L.append("")
        L.append("| 排名 | 主题 | 训练期收益 | 交易 |")
        L.append("| --- | --- | --- | --- |")
        for i, (t, r, n) in enumerate(rank, 1):
            L.append(f"| {i} | {t} | {r:+.0%} | {n} |")
        L.append("")

    L.append("## 三、结论")
    L.append("")
    best = max(results, key=lambda k: results[k][1])
    L.append(f"- 2025 年最优训练窗口：**{best}**（{results[best][1]:+.0%}）；"
             f"事后最优 A（AI硬件/半导体/存储）为 +390%。")
    L.append("- 窗口越短，主题收益排名噪音越大（交易数少、单笔权重高）；窗口≥1 年"
             "能否稳定选出 AI硬件/半导体？对比各窗口选出主题与 2025 实际收益，"
             "判断最短可靠窗口。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"主题walkforward_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


if __name__ == "__main__":
    run()
