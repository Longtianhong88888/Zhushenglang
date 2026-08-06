"""主升浪信号回测（2022-01-01 起，v3 纪律：回落8%/止损5%/5日时间止损）。"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
from tqdm import tqdm

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.signals import in_universe, tail_features

START = "2022-01-01"
END = None
COST = 0.002


def scan(panels: pd.DataFrame, min_chg: float, min_vr: float, mkt_min: int,
         zt_map: dict) -> pd.DataFrame:
    rows = []
    for code, g in tqdm(panels.groupby("code", sort=False), desc="主升扫描", leave=False):
        if len(g) < 40:
            continue
        t = tail_features(g, tail=len(g))
        if t is None:
            continue
        c = t["close"].to_numpy(float)
        o = t["open"].to_numpy(float)
        h = t["high"].to_numpy(float)
        l = t["low"].to_numpy(float)
        dates = t["date"].to_numpy()
        chg = t["chg"].to_numpy()
        vr = t["vol_ratio"].to_numpy()
        signal = t["signal"].to_numpy().astype(bool)
        m5 = t["ma5"].to_numpy()
        if mkt_min:
            zt_arr = t["date"].map(zt_map).to_numpy()
            signal = signal & (zt_arr >= mkt_min)
        n = len(t)
        i = 0
        while i < n:
            if not signal[i]:
                i += 1
                continue
            if i + 2 < n:
                c1 = t.iloc[i + 1]
                if c1["close"] > m5[i + 1] and c1["low"] >= c[i] * 0.97:
                    rows.append({"code": code, "S": i, "S_date": dates[i],
                                 "buy_date": dates[i + 2], "buy_i": i + 2,
                                 "chg": chg[i], "vol_ratio": vr[i],
                                 "zt": zt_arr[i] if mkt_min else 0})
                    i += 2
                    continue
            i += 1
    return pd.DataFrame(rows)


def run(grid: bool = False) -> None:
    panels = load_all_panels()
    if panels.empty:
        raise SystemExit("本地无行情数据，请先运行: mainrise init")
    panels = panels[panels["date"] >= "2021-01-01"]
    panels = panels[~panels["is_st"].fillna(0).astype(int).astype(bool)]
    panels = panels[~panels["is_paused"].fillna(0).astype(int).astype(bool)]
    panels = panels[panels["code"].map(in_universe)]
    panels = panels.sort_values(["code", "date"])
    by_code = {c: g.reset_index(drop=True) for c, g in panels.groupby("code", sort=False)}
    zt_map = panels[panels["close"] >= panels["limit_price"] - 1e-6].groupby("date").size().to_dict()

    print("=== 主升浪启动信号：参数敏感性 ===")
    best = None
    end = END or panels["date"].max()
    if grid:
        chgs = [round(x, 1) for x in np.arange(2.0, 7.01, 0.5)]  # 2.0~7.0 步长0.5
        vrs = [1.1, 1.2, 1.3, 1.4, 1.5, 1.8]
        mkts = [0, 30, 60]
        print(f"精细扫描: 涨幅{chgs} x 量比{vrs} x 涨停{mkts}"
              f"（{len(chgs)*len(vrs)*len(mkts)} 组，约 30-40 分钟，建议隔夜运行）")
    else:
        chgs, vrs, mkts = [2.0, 3.0, 5.0], [1.2, 1.5], [0, 40, 60]
    for min_chg in chgs:
        for min_vr in vrs:
            for mkt_min in mkts:
                sig = scan(panels, min_chg, min_vr, mkt_min, zt_map)
                sig = sig[(sig["S_date"] >= START) & (sig["S_date"] <= end)]
                if sig.empty:
                    continue
                rets = []
                for _, r in sig.iterrows():
                    panel = by_code[r["code"]]
                    bi = r["buy_i"]
                    if bi >= len(panel):
                        continue
                    buy = panel.iloc[bi]["open"]
                    if buy <= 0:
                        continue
                    peak = panel.iloc[bi]["high"]
                    for j in range(bi + 1, min(bi + 7, len(panel))):
                        rr = panel.iloc[j]
                        peak = max(peak, rr["high"])
                        if rr["close"] <= buy * 0.95:
                            rets.append((buy * 0.95 - buy) / buy - COST)
                            break
                        if (rr["close"] - peak) / peak <= -0.08:
                            rets.append((rr["close"] - buy) / buy - COST)
                            break
                        if j - bi >= 5:
                            rets.append((rr["close"] - buy) / buy - COST)
                            break
                    else:
                        rets.append((panel.iloc[-1]["close"] - buy) / buy - COST)
                rets = np.array(rets)
                pos = rets[rets > 0].sum()
                neg = abs(rets[rets < 0].sum())
                pf = pos / neg if neg else 99
                line = f"涨幅≥{min_chg:.0f}% 量比≥{min_vr:.1f} 涨停≥{mkt_min}: n={len(rets):5d} 胜率{(rets>0).mean():.1%} 均收{rets.mean():+.2%} PF={pf:.3f}"
                print(line)
                if best is None or pf > best[0]:
                    best = (pf, min_chg, min_vr, mkt_min, sig, rets)
    if best:
        pf, min_chg, min_vr, mkt_min, sig, rets = best
        print(f"\n最优: PF={pf:.3f} (涨幅≥{min_chg}% 量比≥{min_vr} 涨停≥{mkt_min})")
        paths.ensure_dirs()
        sig.to_csv(paths.report_dir() / "mainrise_trades.csv", index=False)
        print(f"交易明细: {paths.report_dir() / 'mainrise_trades.csv'}")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true",
                    help="精细参数扫描（约72组，耗时15-20分钟）")
    args = ap.parse_args()
    try:
        run(args.grid)
    except SystemExit as e:
        print(e)
        sys.exit(1)
