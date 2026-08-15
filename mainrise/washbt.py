# -*- coding: utf-8 -*-
"""洗盘提前止损回测验证（研究脚本，不改动固化模型）

背景（洗盘统计研究 2026-08-15）：
- 108 笔中 92 笔买入后先回调（洗盘）；盈利组洗盘 中位 3 天 / -3.2%，
  亏损组洗盘 中位 9 天 / -8.8%。
- 验证：在 MA20 退出之外，加"洗盘止损"（买入后收盘跌破买入价达
  阈值即提前卖出），是否改善总收益/回撤/PF。

规则（wash_stop 参数）：
  wash_stop=(mode, value)
  - mode="dd": 收盘价相对买入价回撤 <= -value（如 -0.07 即 -7%）→ 卖出
  - mode="days": 收盘价低于买入价连续 value 个交易日 → 卖出
  - mode="dd_days": 回撤 <= -value 或 连续 value 天未收复 → 卖出
  - mode=None: 原规则（仅 MA20 退出）

实现：fork portfolio_bt.simulate 的退出循环，通过 monkey-patch 不可行
（simulate 内部逻辑固定），因此复制核心循环到本脚本，保证口径一致。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mainrise.portfolio_bt import (  # noqa: E402
    build_info, metrics, load_chokepoint_codes, in_universe,
    market_features,
)
from mainrise.data import load_all_panels  # noqa: E402
import mainrise.bigtrend as bigtrend  # noqa: E402

COST = 0.002


def simulate_washstop(info: dict, hot_set: set, max_pos: int,
                      min_t0: int, mkt_ret20: dict | None = None,
                      downshift: str = "stop", exit_ma: int = 20,
                      rebuy: str = "none", score_min: int = 0,
                      hard_rule: bool = True,
                      hot_by_date: dict | None = None,
                      ban_overbought_weak: bool = True,
                      wash_stop: tuple | None = None) -> dict:
    """与 portfolio_bt.simulate 口径一致的副本，加 wash_stop 洗盘止损。"""
    all_dates = sorted({d for v in info.values() for d in v["dates"]})
    idx_by_code = {c: {d: i for i, d in enumerate(v["dates"])}
                   for c, v in info.items()}
    mkt_ret20 = mkt_ret20 or {}
    ma_key = f"ma{exit_ma}"

    def _is_hot(code: str, d: str) -> bool:
        if hot_by_date is not None:
            return code in hot_by_date.get(d, ())
        return code in hot_set

    cash = 1.0
    positions: list[dict] = []
    trades = []
    nav_curve = []
    pos_count = []
    await_rebuy: set = set()

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

    def _score(code: str, d: str) -> int:
        feat = info[code]["sig_feats"].get(d)
        if feat is None:
            return 0
        from mainrise.portfolio_bt import big_score
        return big_score(feat, _is_hot(code, d))

    def _try_buy(code, via, nav_now, sc):
        nonlocal cash
        if len(positions) >= max_pos:
            return
        px = _px(code, d)
        if px is None or px <= 0:
            return
        target = nav_now / max_pos * 1.0
        shares = target / px / (1 + COST)
        if shares * px * (1 + COST) > cash:
            shares = cash / px / (1 + COST)
        if shares * px <= 0.01:
            return
        cash -= shares * px * (1 + COST)
        positions.append({"code": code, "shares": shares,
                          "entry_px": px, "entry_date": d,
                          "peak_px": px, "via": via, "score": sc,
                          "below_days": 0})
        await_rebuy.discard(code)

    def _close_pos(pos, d, reason):
        nonlocal cash
        code = pos["code"]
        px = _px(code, d)
        if px is None:
            return False
        proceeds = pos["shares"] * px * (1 - COST)
        cash += proceeds
        ret = px / pos["entry_px"] - 1 - COST
        trades.append({
            "code": code, "entry_date": pos["entry_date"],
            "entry": pos["entry_px"], "exit_date": d, "exit": px,
            "ret": ret, "peak_gain": pos["peak_px"] / pos["entry_px"] - 1,
            "hold": (pd.Timestamp(d) - pd.Timestamp(pos["entry_date"])).days,
            "open": 0, "via": pos["via"], "score": pos["score"],
            "reason": reason,
        })
        positions.remove(pos)
        if rebuy != "none":
            await_rebuy.add(code)
        return True

    for d in all_dates:
        weak = (mkt_ret20.get(d, float("nan")) <= -5.0) if \
            mkt_ret20.get(d) is not None else False
        # ---- 退出 ----
        for pos in list(positions):
            code = pos["code"]
            px, ma = _px(code, d), _ma(code, d)
            if px is None:
                continue
            pos["peak_px"] = max(pos["peak_px"], px)
            # 洗盘止损（先于 MA20 检查）
            exit_now = False
            if wash_stop is not None and px < pos["entry_px"]:
                mode = wash_stop[0]
                dd_val = wash_stop[1] if len(wash_stop) > 1 else None
                days_val = wash_stop[2] if len(wash_stop) > 2 else None
                dd = px / pos["entry_px"] - 1
                if mode == "dd":
                    if dd <= -dd_val:
                        exit_now = True
                elif mode == "days":
                    pos["below_days"] += 1
                    if pos["below_days"] >= days_val:
                        exit_now = True
                elif mode == "dd_days":
                    pos["below_days"] += 1
                    if dd <= -dd_val or pos["below_days"] >= days_val:
                        exit_now = True
            else:
                pos["below_days"] = 0
            if exit_now:
                _close_pos(pos, d, "wash_stop")
                continue
            if ma is not None and px < ma:
                _close_pos(pos, d, "ma20")
        # ---- 入场 ----
        nav = cash + sum(p["shares"] * float(_px(p["code"], d) or
                         info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                         for p in positions)
        cands = []
        if not (downshift == "stop" and weak):
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
                sc = _score(code, d)
                if sc < score_min:
                    continue
                cands.append((sc, feat["cnt"], code))
        cands.sort(reverse=True)
        blocked = downshift == "stop" and weak
        if not blocked:
            for sc, _cnt, code in cands:
                _try_buy(code, "rule", nav, sc)
                nav = cash + sum(p["shares"] * float(
                    _px(p["code"], d) or
                    info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                    for p in positions)
        # ---- 日终净值 ----
        nav = cash + sum(p["shares"] * float(_px(p["code"], d) or
                         info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                         for p in positions)
        nav_curve.append((d, nav))
        pos_count.append(len(positions))
    # 期末未平仓
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="full", choices=["full", "2022plus"])
    ap.add_argument("--wash-stop", default=None,
                    help="洗盘止损: dd:0.07 | days:6 | dd_days:0.07,6（逗号分隔）")
    args = ap.parse_args()

    print("加载行情与热主题...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    ck = {c for c in load_chokepoint_codes()
          if not c.startswith("301") and not c.startswith("688")}
    panels = full[full["code"].isin(ck)].copy()
    print(f"范围：卡点企业 {len(ck)} 家，{len(panels):,} 行")

    mkt = market_features(full)
    del full
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))

    theme_map = bigtrend.load_theme()
    hot_set = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}
    print(f"热主题股票 {len(hot_set)} 只")

    info = build_info(panels, hot_set, min_t0=3)

    # 基线（无洗盘止损，固化口径）
    base = simulate_washstop(info, hot_set, max_pos=3, min_t0=3,
                             mkt_ret20=mkt_ret20, wash_stop=None)
    base_m = metrics(base, "基线(MA20退出)")

    variants = []
    if args.wash_stop:
        parts = args.wash_stop.split(",")
        if parts[0] == "dd":
            variants.append(("dd", float(parts[1]), None))
        elif parts[0] == "days":
            variants.append(("days", None, int(parts[1])))
        elif parts[0] == "dd_days":
            variants.append(("dd_days", float(parts[1]), int(parts[2])))
    else:
        # 默认网格
        for dd in (0.05, 0.06, 0.07, 0.08, 0.10):
            variants.append(("dd", dd, None))
        for days in (4, 5, 6, 8):
            variants.append(("days", None, days))
        for dd, days in ((0.05, 5), (0.06, 6), (0.07, 6), (0.08, 8)):
            variants.append(("dd_days", dd, days))

    print(f"\n{'规则':<28}{'笔数':>6}{'胜率':>7}{'总收益':>9}{'年化':>8}{'MDD':>8}{'PF':>7}{'洗盘止损笔':>10}")
    print("-" * 90)
    for label, m in [("基线", base_m)]:
        print(f"{label:<28}{m[1]:>6}{m[2]:>7}{m[3]:>9}{m[4]:>8}{m[5]:>8}{m[6]:>7}")

    results = []
    for mode, dd, days in variants:
        # 统一三元组 (mode, dd, days)，None 表示该维度不用
        ws = (mode, dd, days)
        sim = simulate_washstop(info, hot_set, max_pos=3, min_t0=3,
                                mkt_ret20=mkt_ret20, wash_stop=ws)
        m = metrics(sim, str(ws))
        n_wash = int((sim["trades"]["reason"] == "wash_stop").sum()) \
            if "reason" in sim["trades"].columns else 0
        if mode == "dd":
            label = f"回撤≤{dd*100:.0f}%"
        elif mode == "days":
            label = f"连{days}天未收复"
        else:
            label = f"回撤≤{dd*100:.0f}%或{days}天"
        print(f"{label:<28}{m[1]:>6}{m[2]:>7}{m[3]:>9}{m[4]:>8}{m[5]:>8}{m[6]:>7}{n_wash:>10}")
        results.append((label, m, sim))

    # 最优（PF 最高且收益不劣于基线过多）
    best = max(results, key=lambda r: r[1][5])  # 按 PF
    print("\n按 PF 最优:", best[0])
    out = ROOT / "output" / "reports" / f"洗盘止损回测_{dt.date.today():%Y-%m-%d}.md"
    lines = [
        f"# 洗盘止损回测验证（{dt.date.today()}）\n",
        "- 基线：MA20 退出 + 杀跌区停开 + 追高弱市禁入（固化口径）",
        "- 洗盘止损：买入后收盘跌破买入价达阈值 → 提前卖出（先于 MA20）\n",
        "| 规则 | 笔数 | 胜率 | 总收益 | 年化 | MDD | PF | 洗盘止损笔 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, m in [("基线", base_m)]:
        lines.append(f"| {label} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]} | {m[6]} | - |")
    for label, m, _sim in results:
        n_wash = int((_sim["trades"]["reason"] == "wash_stop").sum())
        lines.append(f"| {label} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]} | {m[6]} | {n_wash} |")
    lines.append("")
    lines.append("> 口径：全期 2021-08~2026-08；费用 0.2%/笔；1/3 仓×3 只；洗盘止损先于 MA20 检查。")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已输出: {out}")


if __name__ == "__main__":
    main()
