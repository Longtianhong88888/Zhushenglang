# -*- coding: utf-8 -*-
"""全市场放宽回测（研究脚本，2026-08-15）

用户要求：放宽到全市场（4727 只，不限 97 只卡点企业），且同一天多个
信号只选评分最高的 1 只买入（top1），看效果。

规则（其余沿用固化版）：
- 热主题：两种口径都测—— 全行业放宽（全部算热）/ 固定三主题
- 90日T0≥3 + 评分≥2 + MA20/8日洗盘退出 + 杀跌区停开 + 追高弱市禁入
- 同日只买评分最高 1 只（top1 变体）；max_pos=3（不同日可累积）

对比基线：卡点企业 97 只 + 固定热主题 + 同日多买（固化版，112 笔 +899%）
输出：output/reports/全市场放宽回测_<date>.md
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mainrise.portfolio_bt import (  # noqa: E402
    build_info, metrics, in_universe, market_features, MIN_T0_90, big_score,
)
from mainrise.data import load_all_panels  # noqa: E402
import mainrise.bigtrend as bigtrend  # noqa: E402

COST = 0.002


def simulate_top1(info: dict, hot_set: set, max_pos: int,
                  min_t0: int = MIN_T0_90, mkt_ret20: dict | None = None,
                  downshift: str = "stop", exit_ma: int = 20,
                  rebuy: str = "none", score_min: int = 2,
                  hard_rule: bool = True,
                  ban_overbought_weak: bool = True,
                  wash_exit_days: int = 8) -> dict:
    """同日多信号只买评分最高 1 只（top1）的组合模拟。其余与
    portfolio_bt.simulate 一致（MA20/洗盘8日退出 + 杀跌区 + 追高禁入）。"""
    all_dates = sorted({d for v in info.values() for d in v["dates"]})
    idx_by_code = {c: {d: i for i, d in enumerate(v["dates"])}
                   for c, v in info.items()}
    mkt_ret20 = mkt_ret20 or {}
    ma_key = f"ma{exit_ma}"

    def _is_hot(code: str, d: str) -> bool:
        return code in hot_set

    cash = 1.0
    positions: list[dict] = []
    trades = []
    nav_curve = []
    pos_count = []

    def _px(code, d):
        ix = idx_by_code[code].get(d)
        if ix is None:
            return None
        return float(info[code]["close"][ix])

    def _ma(code, d):
        ix = idx_by_code[code].get(d)
        if ix is None:
            return None
        v = info[code][ma_key][ix]
        return None if np.isnan(v) else float(v)

    def _score(code, d):
        feat = info[code]["sig_feats"].get(d)
        if feat is None:
            return 0
        return big_score(feat, _is_hot(code, d))

    def _close_pos(pos, d, reason):
        nonlocal cash
        px = _px(pos["code"], d)
        if px is None:
            return False
        cash += pos["shares"] * px * (1 - COST)
        ret = px / pos["entry_px"] - 1 - COST
        trades.append({
            "code": pos["code"], "entry_date": pos["entry_date"],
            "entry": pos["entry_px"], "exit_date": d, "exit": px,
            "ret": ret, "peak_gain": pos["peak_px"] / pos["entry_px"] - 1,
            "hold": (pd.Timestamp(d) - pd.Timestamp(pos["entry_date"])).days,
            "open": 0, "via": pos["via"], "score": pos["score"],
            "reason": reason,
        })
        positions.remove(pos)

    for d in all_dates:
        weak = (mkt_ret20.get(d, float("nan")) <= -5.0) if \
            mkt_ret20.get(d) is not None else False
        # 退出
        for pos in list(positions):
            px, ma = _px(pos["code"], d), _ma(pos["code"], d)
            if px is None:
                continue
            pos["peak_px"] = max(pos["peak_px"], px)
            if wash_exit_days > 0 and px < pos["entry_px"]:
                pos["below_days"] = pos.get("below_days", 0) + 1
                if pos["below_days"] >= wash_exit_days:
                    _close_pos(pos, d, "wash_exit")
                    continue
            else:
                pos["below_days"] = 0
            if ma is not None and px < ma:
                _close_pos(pos, d, "ma20")
        # 入场：同日只买评分最高 1 只
        nav = cash + sum(p["shares"] * float(_px(p["code"], d) or
                         info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                         for p in positions)
        if not (downshift == "stop" and weak):
            cands = []
            for code, v in info.items():
                if code in {p["code"] for p in positions}:
                    continue
                feat = v["sig_feats"].get(d)
                if feat is None:
                    continue
                hot = _is_hot(code, d)
                if hard_rule and not (hot and feat["cnt"] >= min_t0):
                    continue
                if ban_overbought_weak:
                    c20 = feat.get("chg20")
                    mr = mkt_ret20.get(d) if mkt_ret20 else None
                    if c20 is not None and c20 >= 0.60 \
                            and mr is not None and mr <= 0.0:
                        continue
                sc = big_score(feat, hot)
                if sc < score_min:
                    continue
                cands.append((sc, feat["cnt"], code))
            cands.sort(reverse=True)
            if cands and len(positions) < max_pos:
                # top1：只买评分最高（同分取 90日T0 高者）
                sc, _cnt, code = cands[0]
                px = _px(code, d)
                if px is not None and px > 0:
                    target = nav / max_pos
                    shares = target / px / (1 + COST)
                    if shares * px * (1 + COST) > cash:
                        shares = cash / px / (1 + COST)
                    if shares * px > 0.01:
                        cash -= shares * px * (1 + COST)
                        positions.append({
                            "code": code, "shares": shares,
                            "entry_px": px, "entry_date": d,
                            "peak_px": px, "via": "rule", "score": sc,
                            "below_days": 0,
                        })
        nav = cash + sum(p["shares"] * float(_px(p["code"], d) or
                         info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                         for p in positions)
        nav_curve.append((d, nav))
        pos_count.append(len(positions))
    for pos in positions:
        ix = info[pos["code"]]["n"] - 1
        px = float(info[pos["code"]]["close"][ix])
        trades.append({
            "code": pos["code"], "entry_date": pos["entry_date"],
            "entry": pos["entry_px"], "exit_date": np.nan, "exit": np.nan,
            "ret": px / pos["entry_px"] - 1 - COST,
            "peak_gain": pos["peak_px"] / pos["entry_px"] - 1,
            "hold": np.nan, "open": 1,
            "via": pos["via"], "score": pos["score"], "reason": "open",
        })
    return {"nav": pd.DataFrame(nav_curve, columns=["date", "nav"]),
            "trades": pd.DataFrame(trades), "pos_count": pos_count}


def main() -> None:
    t0 = time.time()
    print("加载全市场行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    full = full[~full["code"].astype(str).str.startswith(("301", "688"))]
    panels_all = full.copy()
    print(f"全市场 {len(full):,} 行, {full['code'].nunique()} 只 ({time.time()-t0:.0f}s)")

    mkt = market_features(full)
    del full
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))

    # 全市场热主题：固定三主题（bigtrend 主题映射只覆盖卡点企业，
    # 其他股票 theme 为空 → 不算热，等价"热主题门槛收紧到三主题"）
    theme_map = bigtrend.load_theme()
    hot_static = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}

    print("build_info 全市场（固定热）...")
    info_static = build_info(panels_all, hot_static, MIN_T0_90)
    print(f"  ({time.time()-t0:.0f}s)")

    base_kw = dict(max_pos=3, min_t0=MIN_T0_90, mkt_ret20=mkt_ret20,
                   downshift="stop", exit_ma=20, rebuy="none",
                   score_min=2, hard_rule=True,
                   ban_overbought_weak=True, wash_exit_days=8)

    # 方案1：全市场 + 固定热主题 + top1
    sim_hot = simulate_top1(info_static, hot_static, **base_kw)
    # 方案2：全市场 + 全热（放宽热）+ top1
    print("build_info 全市场（全热）...")
    all_codes = set(panels_all["code"])
    info_all = build_info(panels_all, all_codes, MIN_T0_90)
    print(f"  ({time.time()-t0:.0f}s)")
    sim_all = simulate_top1(info_all, all_codes, **base_kw)

    print(f"\n{'方案':<34}{'笔数':>6}{'胜率':>7}{'总收益':>9}{'年化':>8}"
          f"{'MDD':>8}{'PF':>7}{'≥60%':>7}")
    print("-" * 90)
    for label, sim in [("全市场+固定热+同日Top1", sim_hot),
                       ("全市场+全热+同日Top1", sim_all)]:
        m = metrics(sim, label)
        print(f"{label:<34}{m[1]:>6}{m[2]:>7}{m[3]:>9}{m[4]:>8}{m[5]:>8}"
              f"{m[6]:>7}{m[7]:>7}")

    # 分年度
    print("\n=== 分年度收益 ===")
    print(f"{'年份':<8}{'固定热+Top1':>14}{'全热+Top1':>14}")
    for sim, nm in [(sim_hot, "固定热+Top1"), (sim_all, "全热+Top1")]:
        pass
    nav_h = sim_hot["nav"]; nav_a = sim_all["nav"]
    for d in ["2021-12-31", "2022-12-30", "2023-12-29", "2024-12-31",
              "2025-12-31", nav_h["date"].iloc[-1]]:
        s = nav_h[nav_h["date"] <= d]; a = nav_a[nav_a["date"] <= d]
        if len(s) == 0 or len(a) == 0:
            continue
        print(f"{d[:4]:<8}{s['nav'].iloc[-1]/s['nav'].iloc[0]-1:>+13.0%}"
              f"{a['nav'].iloc[-1]/a['nav'].iloc[0]-1:>+14.0%}")

    out = ROOT / "output" / "reports" / f"全市场放宽回测_{dt.date.today():%Y-%m-%d}.md"
    n_all = panels_all["code"].nunique()
    L = [f"# 全市场放宽回测（{dt.date.today()}）\n",
         f"- 范围：全市场 {n_all} 只（不限 97 只卡点企业）",
         "- 同日多信号只买评分最高 1 只（top1）",
         "- 规则：90日T0≥3 + 评分≥2 + MA20/8日洗盘退出 + 杀跌区 + 追高禁入\n",
         "| 方案 | 笔数 | 胜率 | 总收益 | 年化 | MDD | PF | ≥60% |",
         "|---|---|---|---|---|---|---|---|",
         ]
    for label, sim in [("全市场+固定热+同日Top1", sim_hot),
                       ("全市场+全热+同日Top1", sim_all)]:
        m = metrics(sim, label)
        L.append(f"| {label} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]} | {m[6]} | {m[7]} |")
    L.append("")
    L.append("> 对比基线：卡点97只+固定热+多买 = 112笔 +899% / PF 3.21 / MDD -35%。")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n已输出: {out}")


if __name__ == "__main__":
    main()
