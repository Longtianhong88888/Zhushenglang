# -*- coding: utf-8 -*-
"""Walk-forward 稳健性验证（研究脚本，2026-08-15）

背景（审计 M8）：wash_exit=8 / top1 / 评分≥2 / 追高禁入 等参数全部在
2021-2026 同一样本上选定，+998% 可能是样本内最大值。本脚本做：
1. 分年度 / 分段（2021-2023 训练段 vs 2024-2026 样本外段）回测同一套
   固化规则，看参数是否在不同时段稳定（非过拟合）
2. 关键参数敏感性：wash_exit_days ∈ {0,6,8,10} × top1 ∈ {True,False}
   在 训练段（≤2023）与 样本外段（≥2024）分别评估，确认 8/top1 不是
   只在全期偶然最优
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mainrise.portfolio_bt import (  # noqa: E402
    simulate, build_info, metrics, in_universe, market_features, MIN_T0_90,
)
from mainrise.data import load_all_panels  # noqa: E402
import mainrise.bigtrend as bigtrend  # noqa: E402


def load_info_upto(full: pd.DataFrame, hot_set: set, end_date: str | None):
    """构建截至 end_date（含）的全市场 info（样本外无前视）。"""
    if end_date:
        panels = full[full["date"] <= end_date].copy()
    else:
        panels = full.copy()
    return build_info(panels, hot_set, MIN_T0_90), panels


def main() -> None:
    t0 = time.time()
    print("加载全市场行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full[~full["code"].astype(str).str.startswith(("301", "688"))]
    full = full.sort_values(["code", "date"])
    print(f"全市场（排除 301/688）{full['code'].nunique()} 只 "
          f"({time.time()-t0:.0f}s)")

    mkt = market_features(full)
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))
    theme_map = bigtrend.load_theme()
    hot_set = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}

    segments = {
        "训练段 2021-2023": "2023-12-29",
        "样本外 2024-2026": None,          # 全期减去训练段的对比看分段表现
        "全期 2021-2026": None,
    }
    # 用"截至训练段"的 info 跑训练段；样本外段 = 全期 info（模型参数已定，
    # 但注意 mkt_ret20 是全期算的——影响小，杀跌区判定用当日大盘）
    info_all, _ = load_info_upto(full, hot_set, None)
    info_train, _ = load_info_upto(full, hot_set, "2023-12-29")

    # 参数网格
    grid = [(0, False), (0, True), (6, True), (8, True), (10, True)]

    def _seg_metrics(info, tag):
        """用截断到 end_date 的 info 跑 simulate（simulate 只在该段内有数据）。"""
        rows = []
        for wd, top in grid:
            sim = simulate(info, hot_set, max_pos=3, min_t0=3,
                           mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20,
                           rebuy="none", score_min=2, hard_rule=True,
                           ban_overbought_weak=True, wash_exit_days=wd,
                           top1=top)
            m = metrics(sim, f"{tag} wd={wd} top1={top}")
            rows.append((wd, top, m))
        return rows

    print(f"\n{'时段':<16}{'参数':<20}{'笔数':>5}{'胜率':>7}{'总收益':>9}"
          f"{'年化':>8}{'MDD':>8}{'PF':>7}")
    print("-" * 90)

    # 训练段：info 只构建到 2023-12-29 → simulate 只产生 2021-2023 交易
    train_rows = _seg_metrics(info_train, "训练段")
    for wd, top, m in train_rows:
        tag = f"wd={wd} top1={top}"
        print(f"{'2021-2023':<16}{tag:<20}{m[1]:>5}{m[2]:>7}{m[3]:>9}"
              f"{m[4]:>8}{m[5]:>8}{m[6]:>7}")

    print()
    out_rows = []
    for wd, top, m in train_rows:
        out_rows.append({"段": "2021-2023", "参数": f"wd={wd} top1={top}",
                         "笔数": m[1], "胜率": m[2], "总收益": m[3],
                         "年化": m[4], "MDD": m[5], "PF": m[6]})
    # 样本外/全期（固化参数 8/top1 与对照组）
    for wd, top in [(0, False), (0, True), (6, True), (8, True), (10, True)]:
        sim = simulate(info_all, hot_set, max_pos=3, min_t0=3,
                       mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20,
                       rebuy="none", score_min=2, hard_rule=True,
                       ban_overbought_weak=True, wash_exit_days=wd, top1=top)
        m = metrics(sim, "x")
        tag = f"wd={wd} top1={top}"
        print(f"{'全期':<16}{tag:<20}{m[1]:>5}{m[2]:>7}{m[3]:>9}"
              f"{m[4]:>8}{m[5]:>8}{m[6]:>7}")
        out_rows.append({"段": "全期", "参数": tag, "笔数": m[1],
                         "胜率": m[2], "总收益": m[3], "年化": m[4],
                         "MDD": m[5], "PF": m[6]})

    # 分年度
    print("\n=== 分年度净值（固化规则 wd=8 top1=True）===")
    sim8 = simulate(info_all, hot_set, max_pos=3, min_t0=3,
                    mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20,
                    rebuy="none", score_min=2, hard_rule=True,
                    ban_overbought_weak=True, wash_exit_days=8, top1=True)
    nav = sim8["nav"]
    for d in ["2021-12-31", "2022-12-30", "2023-12-29", "2024-12-31",
              "2025-12-31", nav["date"].iloc[-1]]:
        s = nav[nav["date"] <= d]
        if len(s) == 0:
            continue
        y = s["nav"].iloc[-1] / s["nav"].iloc[0] - 1
        print(f"  {d[:4]}: 累计 {y:+.0%}")

    out = ROOT / "output" / "reports" / f"walkforward_验证_{time.strftime('%Y-%m-%d')}.md"
    L = [f"# Walk-forward 稳健性验证（{time.strftime('%Y-%m-%d')}）\n",
         "- 固化规则：全市场（排除301/688）+ 固定热 + 90日T0≥3 + 评分≥2",
         "- 退出：MA20 + 洗盘8日止损 + 杀跌区 + 追高禁入 + 同日Top1\n",
         "| 时段 | 参数 | 笔数 | 胜率 | 总收益 | 年化 | MDD | PF |",
         "|---|---|---|---|---|---|---|---|"]
    for r in out_rows:
        L.append(f"| {r['段']} | {r['参数']} | {r['笔数']} | {r['胜率']} | "
                 f"{r['总收益']} | {r['年化']} | {r['MDD']} | {r['PF']} |")
    L.append("")
    L.append("> 说明：训练段=2021-2023（参数选定前）；全期=2021-2026。")
    L.append("> 若 wd=8/top1 在训练段也优于 wd=0/top1=False，说明参数非全期过拟合。")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n已输出: {out}")


if __name__ == "__main__":
    main()
