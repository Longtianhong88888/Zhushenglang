"""两级模型回测（B3 打底仓 / 二波加仓），无前视，带纪律模拟。

信号（2026-08-14 两级模型，替代原 T0/T1/T2）：
- B3：均线粘合≤3% + 阳线 + 量比≥2 + 涨幅≥1% + 站上三均线 + 距60日低点<30%
  → 打底仓；信号日 S → S+1 不破低点确认 → S+2 开盘买
- 二波：B3 后 3~30 日内 深回调2-12% + 均线再次粘合≤2% + 缩量 →
  放量阳线再启动（触发日 T 收盘判定）→ T+1 开盘买（加仓）
- 纪律：止损-4%、高点回落8%、时间止损（10/20日），成本 0.2%

输出 output/reports/mainrise_trades.csv（B3/二波 信号明细，供 evaluate/观察池）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.report import load_chokepoint_codes

START = "2022-01-01"
COST = 0.002


def _signal_flags(panel: pd.DataFrame) -> pd.DataFrame:
    """计算 B3 / 二波 信号列（与 signals.tail_features 同口径）。"""
    t = panel.copy()
    t["ma5"] = t["close"].rolling(5).mean()
    t["ma10"] = t["close"].rolling(10).mean()
    t["ma20"] = t["close"].rolling(20).mean()
    t["v5"] = t["volume"].rolling(5).mean().shift(1)
    t["vr"] = t["volume"] / t["v5"]
    t["lo60"] = t["low"].rolling(60).min().shift(1)
    t["spread"] = (t[["ma5", "ma10", "ma20"]].max(axis=1)
                   - t[["ma5", "ma10", "ma20"]].min(axis=1)) / t["ma20"]
    t["cross"] = (t["close"] > t["ma5"]) & (t["close"] > t["ma10"]) \
        & (t["close"] > t["ma20"])
    t["yang"] = t["close"] > t["open"]
    t["chg"] = (t["close"] / t["prev_close"] - 1) * 100
    b3 = ((t["spread"] <= 0.03) & t["yang"] & (t["chg"] >= 1.0)
          & (t["vr"] >= 2.0) & t["cross"]
          & (t["lo60"] > 0) & (t["close"] / t["lo60"] - 1 < 0.30)).to_numpy()
    wave2 = np.zeros(len(t), dtype=bool)
    b3_idx = [i for i in range(len(t)) if b3[i]]
    for i in range(len(t)):
        b = None
        for bi in reversed(b3_idx):
            d = i - bi
            if d > 30:
                break
            if d >= 3:
                b = bi
                break
        if b is None or not (t.iloc[i]["vr"] >= 1.5 and t.iloc[i]["yang"]
                             and t.iloc[i]["chg"] >= 2.0 and t.iloc[i]["cross"]):
            continue
        t2 = (t.iloc[i - 1]["spread"] <= 0.02 if i >= 1 else False) or \
             (t.iloc[i - 2]["spread"] <= 0.02 if i >= 2 else False)
        if not t2:
            continue
        hi_since = t.iloc[b + 1:i]["high"].max() if i > b + 1 else 0.0
        if hi_since <= 0:
            continue
        depth = t.iloc[b]["close"] / hi_since - 1
        if not (-0.12 <= depth <= -0.02):
            continue
        if t.iloc[b + 1:i]["volume"].mean() >= t.iloc[b]["volume"]:
            continue
        wave2[i] = True
    t["b3"] = b3
    t["wave2"] = wave2
    return t


def _sim(entries, by, time_stop):
    rets = []
    for code, bi in entries:
        panel = by[code]
        if bi >= len(panel):
            continue
        buy = panel.iloc[bi]["open"]
        if buy <= 0:
            continue
        peak = panel.iloc[bi]["high"]
        out = None
        for j in range(bi + 1, min(bi + time_stop + 1, len(panel))):
            rr = panel.iloc[j]
            peak = max(peak, rr["high"])
            if rr["close"] <= buy * 0.96:
                out = (buy * 0.96 - buy) / buy - COST
                break
            if (rr["close"] - peak) / peak <= -0.08:
                out = (rr["close"] - buy) / buy - COST
                break
        if out is None:
            out = (panel.iloc[min(bi + time_stop, len(panel) - 1)]["close"]
                   - buy) / buy - COST
        rets.append(out)
    rets = np.array(rets)
    if len(rets) == 0:
        return
    rets_pct = rets * 100
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() else 99
    ratio = wins.mean() / abs(losses.mean()) if len(losses) else 99
    print(f"  n={len(rets):4d} 胜率{len(wins)/len(rets):5.1%} "
          f"均收{rets_pct.mean():+5.2f}% PF={pf:5.2f} 盈亏比={ratio:5.2f} "
          f"P≥20%{np.mean(rets >= 0.20):4.1%} 最差{rets_pct.min():+.0f}%")


def run(grid: bool = False) -> None:
    """两级模型回测：B3 打底仓 / 二波加仓（无前视，带纪律）。"""
    panels = load_all_panels()
    if panels.empty:
        raise SystemExit("本地无行情数据，请先运行: mainrise init")
    panels = panels[panels["date"] >= "2021-01-01"]
    panels = panels[~panels["is_st"].fillna(0).astype(int).astype(bool)]
    panels = panels[~panels["is_paused"].fillna(0).astype(int).astype(bool)]
    ck = load_chokepoint_codes()
    panels = panels[panels["code"].isin(ck)].sort_values(["code", "date"])
    print(f"两级模型回测范围：行业卡点企业 {len(ck)} 家")
    by = {c: g.reset_index(drop=True) for c, g in panels.groupby("code")}

    b3_entries, w2_entries, trades = [], [], []
    for code, panel in by.items():
        t = _signal_flags(panel)
        b3 = t["b3"].to_numpy()
        for i in range(len(panel) - 2):
            if b3[i] and t.iloc[i + 1]["low"] >= t.iloc[i]["low"]:
                b3_entries.append((code, i + 2))
                if t.iloc[i]["date"] >= START:
                    trades.append({"code": code, "S_date": t.iloc[i]["date"],
                                   "buy_date": t.iloc[i + 2]["date"],
                                   "kind": "B3", "chg": t.iloc[i]["chg"],
                                   "vol_ratio": t.iloc[i]["vr"]})
        b3_idx = [i for i in range(len(panel)) if b3[i]]
        for b in b3_idx:
            for i in range(b + 3, min(b + 31, len(panel) - 1)):
                pb = t.iloc[b]
                hi_s = t.iloc[b + 1:i + 1]["high"].max()
                depth = pb["close"] / hi_s - 1 if hi_s > 0 else 0
                t2 = (t.iloc[i]["spread"] <= 0.02
                      or t.iloc[i - 1]["spread"] <= 0.02)
                shrink = t.iloc[b + 1:i + 1]["volume"].mean() < pb["volume"]
                if not (-0.12 <= depth <= -0.02 and t2 and shrink):
                    continue
                tr = t.iloc[i + 1]
                if (tr["vr"] >= 1.5 and tr["yang"] and tr["chg"] >= 2.0
                        and tr["cross"]):
                    w2_entries.append((code, i + 2))
                    if t.iloc[i + 1]["date"] >= START:
                        trades.append({"code": code,
                                       "S_date": t.iloc[i + 1]["date"],
                                       "buy_date": t.iloc[i + 2]["date"],
                                       "kind": "二波", "chg": tr["chg"],
                                       "vol_ratio": tr["vr"]})
                    # M6 修复：与 signals.py wave2 一致，逐日判定——
                    # 同一 B3 窗口内所有满足条件的日都记（原 break 只记第一个，
                    # 回测样本与实盘信号语义不一致）

    print("=== 两级模型（卡点名单 2022 起，无前视；止损-4%/回落8%） ===")
    for ts in (10, 20):
        print(f"--- 时间止损 {ts} 日 ---")
        print("B3 打底仓（S+1确认→S+2开）:", end="")
        _sim(b3_entries, by, ts)
        print("二波加仓（T+1开）:", end="")
        _sim(w2_entries, by, ts)
    print(f"样本：B3 {len(b3_entries)} 笔 / 二波 {len(w2_entries)} 笔（按交易日）")

    paths.ensure_dirs()
    if trades:
        pd.DataFrame(trades).to_csv(
            paths.report_dir() / "mainrise_trades.csv", index=False)
        print(f"信号明细（供 evaluate/观察池）: "
              f"{paths.report_dir() / 'mainrise_trades.csv'}")


def two_stage_run() -> None:
    run()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true",
                    help="（已废弃，保留兼容）")
    ap.add_argument("--two-stage", action="store_true",
                    help="两级模型验证（默认即两级模型）")
    args = ap.parse_args()
    try:
        run(args.grid)
    except SystemExit as e:
        print(e)
        raise


if __name__ == "__main__":
    main()
