"""月度买卖点优化：用归档 5 分钟数据（data/m5daily）重新校准点火信号阈值。

背景（2026-08-15 用户方向）：候选池标的 5 分钟级"资金点火进攻"买入信号，
每日归档 data/m5daily/<code>.csv。每月用累计的 5 分钟历史评估当前
VOL_MULT / MIN_CHG / NEW_HI_CHG 阈值是否最优，并对比"点火买 vs 收盘买"
（同月样本），输出优化建议。

用法:
    python3 -m mainrise.m5optimize              # 用全部归档数据评估当前阈值
    python3 -m mainrise.m5optimize --vol 2.5 --min-chg 0.04   # 试特定阈值
    python3 -m mainrise.m5optimize --grid       # 网格搜索最优阈值
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.m5data import load_m5
from mainrise.report import load_chokepoint_codes


def _candidate_codes() -> list[str]:
    """点火追踪候选：bigbull_cands + 卡点企业（供评估，取有归档数据的）。"""
    codes = set()
    try:
        p = paths.state_dir() / "bigbull_cands.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        codes |= {str(c.get("code")) for c in (data.get("cands") or [])}
    except Exception:  # noqa: BLE001
        pass
    codes |= load_chokepoint_codes()
    return sorted(codes)


def _evaluate(vol_mult: float, min_chg: float, new_hi_chg: float) -> dict:
    """用归档 5 分钟数据评估：点火信号数、点火价 vs 收盘价差、后续表现。

    对每个 (code, 交易日)：用当日 5 分钟检测点火（IGNITE 判定），
    记录点火价与当日收盘价差；若归档数据延伸到次日，统计点火后 1/3/5 日收益。
    """
    from mainrise import ignite5
    codes = _candidate_codes()
    # 归档数据按 code 加载（可能很大，逐 code 处理）
    total_sig = 0
    gaps = []
    fwd = {1: [], 3: [], 5: []}
    for code in codes:
        df = load_m5(code)
        if len(df) < 100:
            continue
        # 按日分组
        df["day"] = df["datetime"].dt.strftime("%Y-%m-%d")
        # 前20日高点 / 前收（用日线数据，m5 归档需与日线对齐）
        # 简化：用 5 分钟数据自身算前20日高点（跨日）
        days = sorted(df["day"].unique())
        hi_hist: dict[str, float] = {}
        for i, d in enumerate(days):
            day_rows = df[df["day"] == d]
            if i == 0:
                continue
            # 前收 = 前一交易日收盘
            prev_close = float(df[df["day"] == days[i - 1]]["close"].iloc[-1])
            # 前20日高（不含当日）
            hist = df[df["day"] < d]
            hi20 = float(hist["high"].max()) if len(hist) else prev_close
            # 近5日单根均量
            recent = hist.tail(240)  # 5 个交易日 × 48 根
            base_vol = float(recent["volume"].mean()) if len(recent) else 0
            # 检测点火
            rows = [[str(t), o, c, h, l, v / 100]
                    for t, o, c, h, l, v in zip(
                        day_rows["datetime"].dt.strftime("%Y%m%d%H%M"),
                        day_rows["open"], day_rows["close"], day_rows["high"],
                        day_rows["low"], day_rows["volume"])]
            sig = ignite5.detect_ignite(code, rows, hi20, prev_close, base_vol)
            if sig:
                total_sig += 1
                gaps.append(sig["px"] / float(day_rows["close"].iloc[-1]) - 1)
                # 后续表现：点火价 → 之后 1/3/5 交易日收盘
                for k in (1, 3, 5):
                    if i + k < len(days):
                        fut_close = float(
                            df[df["day"] == days[i + k]]["close"].iloc[-1])
                        fwd[k].append(fut_close / sig["px"] - 1)
    return {"signals": total_sig, "gap_mean": float(np.mean(gaps)) if gaps else None,
            "gap_neg_pct": float(np.mean(np.array(gaps) < 0)) if gaps else None,
            "fwd1": float(np.mean(fwd[1])) if fwd[1] else None,
            "fwd3": float(np.mean(fwd[3])) if fwd[3] else None,
            "fwd5": float(np.mean(fwd[5])) if fwd[5] else None}


def run(vol_mult: float = 2.0, min_chg: float = 0.03,
        new_hi_chg: float = 0.05, grid: bool = False) -> str:
    d = paths.data_dir() / "m5daily"
    n_files = len(list(d.glob("*.csv"))) if d.exists() else 0
    print(f"归档 5 分钟数据: {n_files} 只")

    if grid:
        print("\n网格搜索（vol_mult × min_chg）：")
        best = None
        for vm in (1.5, 2.0, 2.5, 3.0):
            for mc in (0.03, 0.04, 0.05):
                r = _evaluate(vm, mc, new_hi_chg)
                score = (r["gap_neg_pct"] or 0) * 10 + (r["fwd3"] or 0)
                print(f"  vol={vm} min_chg={mc:.0%}: 信号{r['signals']} "
                      f"买价低{(r['gap_neg_pct'] or 0):.0%} 3日{(r['fwd3'] or 0):+.1%} "
                      f"score={score:.3f}")
                if best is None or score > best[0]:
                    best = (score, vm, mc)
        print(f"\n最优: vol_mult={best[1]} min_chg={best[2]:.0%}")
        return "grid"

    r = _evaluate(vol_mult, min_chg, new_hi_chg)
    L = [f"# 月度买卖点优化（{pd.Timestamp.now():%Y-%m}）", "",
         f"> 归档 5 分钟 {n_files} 只；当前阈值 vol_mult={vol_mult} "
         f"min_chg={min_chg:.0%} new_hi_chg={new_hi_chg:.0%}", ""]
    L.append(f"## 点火信号评估")
    L.append("")
    L.append(f"- 信号数：{r['signals']}")
    L.append(f"- 点火价 vs 收盘价：均差 {r['gap_mean']:+.1%}，"
             f"{r['gap_neg_pct']:.0%} 低于收盘（买入更优）")
    L.append(f"- 点火后 1/3/5 日收益：{r['fwd1']:+.1%} / {r['fwd3']:+.1%} / "
             f"{r['fwd5']:+.1%}")
    L.append("")
    L.append("> 评估：点火价低于收盘占比越高、后续收益越正 → 当前阈值越有效。"
             "若信号过少（<20/月）或假点火多（fwd 为负），调大 vol_mult/min_chg。")
    L.append("")
    L.append("> 研究线索，不构成投资建议。")
    md = paths.report_dir() / f"月度买卖点优化_{pd.Timestamp.now():%Y-%m}.md"
    md.write_text("\n".join(L), encoding="utf-8")
    print(f"优化报告: {md}")
    return str(md)


def main() -> None:
    ap = argparse.ArgumentParser(description="月度买卖点优化（5分钟归档数据）")
    ap.add_argument("--vol", type=float, default=2.0, help="量比阈值（默认2.0）")
    ap.add_argument("--min-chg", type=float, default=0.03, help="最小涨幅（默认0.03）")
    ap.add_argument("--new-hi-chg", type=float, default=0.05, help="突破型涨幅")
    ap.add_argument("--grid", action="store_true", help="网格搜索最优阈值")
    args = ap.parse_args()
    run(args.vol, args.min_chg, args.new_hi_chg, args.grid)


if __name__ == "__main__":
    main()
