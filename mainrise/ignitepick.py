# -*- coding: utf-8 -*-
"""同日多点火选择规则回测（研究脚本，2026-08-15）

背景：候选池（动态，非固定3只）每只独立做盘中点火检测；同一天可能
多只同时点火。用户指出：评分最高的当天未必点火，评分最低的可能是
当天唯一点火——不能用评分过滤点火。本脚本用历史 5 分钟数据回测：

1. 逐日重算"历史候选池"（bigbull 固化口径：全市场+固定热+90日T0≥3+
   评分≥2，按历史日期无前视）
2. 用当日 m5daily 5分钟数据检测点火（与 ignite5 同口径）
3. 对"同日多点火"的日子，比较不同选择规则的后续收益：
   - 规则A：全部买（点火几只买几只）
   - 规则B：选评分最高
   - 规则C：选点火强度最高（量比最大）
   - 规则D：选当日涨幅最高
   后续收益 = 点火日次日开盘买 → N 日后收盘卖（1/3/5 日）
"""
from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mainrise.portfolio_bt import (  # noqa: E402
    build_info, in_universe, market_features, MIN_T0_90, big_score,
)
from mainrise.data import load_all_panels  # noqa: E402
import mainrise.bigtrend as bigtrend  # noqa: E402

M5_DIR = ROOT / "data" / "m5daily"
IGNITE_VOL_MULT = 2.0
IGNITE_MIN_CHG = 0.03
IGNITE_NEW_HI_CHG = 0.05


def load_m5(code: str) -> pd.DataFrame:
    p = M5_DIR / f"{code}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    df["datetime"] = df["datetime"].str.strip()
    df["date"] = df["datetime"].str[:10]
    return df


def detect_ignite_day(day_m5: pd.DataFrame, hi20: float, prev_close: float,
                      base_vol: float) -> dict | None:
    """单日 5 分钟序列点火检测（与 ignite5.detect_ignite 同口径）。"""
    if day_m5.empty or hi20 <= 0 or prev_close <= 0 or base_vol <= 0:
        return None
    closes = day_m5["close"].to_numpy(float)
    highs = day_m5["high"].to_numpy(float)
    vols = day_m5["volume"].to_numpy(float)
    for i in range(len(closes)):
        vm = vols[i] / base_vol
        if vm < IGNITE_VOL_MULT:
            continue
        h = highs[i]
        chg = h / prev_close - 1
        new_hi = h > hi20
        if chg >= IGNITE_MIN_CHG and (new_hi or chg >= IGNITE_NEW_HI_CHG):
            return {"time": str(day_m5["datetime"].iloc[i])[11:16],
                    "px": float(closes[i]), "vol_mult": round(vm, 1),
                    "chg": round(chg * 100, 1), "new_hi": bool(new_hi)}
    return None


def load_daily_series(full: pd.DataFrame, code: str):
    g = full[full["code"] == code].sort_values("date").reset_index(drop=True)
    return g


def main() -> None:
    print("加载全市场日线...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])

    mkt = market_features(full)
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))

    theme_map = bigtrend.load_theme()
    hot_set = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}

    # 一次性 build_info 全市场（sig_feats 含每个信号日的特征，无前视：
    # feat 在信号日当天计算，只用到当日及之前的数据）
    print("build_info 全市场（一次）...")
    info = build_info(full, hot_set, MIN_T0_90)

    # 历史候选池：对每个有 m5 数据的交易日，从 sig_feats 取当日信号
    m5_days = sorted({d for p in M5_DIR.glob("*.csv")
                      for d in _days_in(p)})
    print(f"有 5 分钟数据的交易日: {m5_days}")

    # 日期索引（全市场）
    all_dates = sorted(full["date"].unique())
    date_idx = {d: i for i, d in enumerate(all_dates)}

    results = []          # 每个点火信号一条
    for day in m5_days:
        if day not in date_idx:
            continue
        # 候选池（bigbull 口径，无前视）= 截至 day 的 45 个自然日内
        # 硬规则信号（热主题 + 90日T0≥3 + 评分≥2 + 追高禁入），按信号日倒序
        cutoff = (pd.Timestamp(day) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
        day_cands = []
        for code, v in info.items():
            for d, feat in v["sig_feats"].items():
                if d < cutoff or d > day:
                    continue
                hot = code in hot_set
                if not (hot and feat["cnt"] >= MIN_T0_90):
                    continue
                c20 = feat.get("chg20")
                mr = mkt_ret20.get(d) if mkt_ret20 else None
                if c20 is not None and c20 >= 0.60 and mr is not None and mr <= 0.0:
                    continue
                sc = big_score(feat, hot)
                if sc < 2:
                    continue
                day_cands.append({"code": code, "score": sc, "cnt": feat["cnt"],
                                  "date": d})
        # 按代码去重（同票取最近信号）
        seen = set()
        uniq = []
        for c in sorted(day_cands, key=lambda x: x["date"], reverse=True):
            if c["code"] in seen:
                continue
            seen.add(c["code"])
            uniq.append(c)
        day_cands = uniq
        if not day_cands:
            continue
        print(f"\n=== {day} 候选池 {len(day_cands)} 只 ===")
        for c in day_cands:
            print(f"  {c['code']} 评分{c['score']} T0={c['cnt']}")

        # 点火检测
        ignited = []
        for c in day_cands:
            code = c["code"]
            m5 = load_m5(code)
            if m5.empty:
                continue
            day_m5 = m5[m5["date"] == day]
            if day_m5.empty:
                continue
            # 日线前20高/昨收/单根均量（截至 day）
            g = load_daily_series(full, code)
            g = g[g["date"] <= day]      # 只用点火日及之前的数据（无前视）
            if len(g) < 22:
                continue
            prev_close = float(g["close"].iloc[-2])
            hi20 = float(g["high"].iloc[-21:-1].max())
            v5 = float(g["volume"].tail(5).mean())
            base_vol = v5 / 48
            sig = detect_ignite_day(day_m5, hi20, prev_close, base_vol)
            if sig:
                # 当日涨幅（收盘口径）
                chg_close = float(g["close"].iloc[-1]) / prev_close - 1
                c2 = dict(c)
                c2["ignite"] = sig
                c2["chg_close"] = chg_close
                c2["entry_px"] = sig["px"]
                ignited.append(c2)
                print(f"  🔥 {code} 点火 {sig['time']} {sig['px']} "
                      f"量比{sig['vol_mult']}× 涨{sig['chg']}%")
        if not ignited:
            print("  （当日无点火）")
            continue
        # 后续收益：次日开盘买 → 1/3/5 日后收盘卖
        i_day = date_idx[day]
        for c in ignited:
            code = c["code"]
            g = load_daily_series(full, code)
            g = g[g["date"] > day].reset_index(drop=True)
            if len(g) < 1:
                continue
            entry = float(g["open"].iloc[0])     # 次日开盘
            rets = {}
            # ret1 = 次日收盘（同日，索引0）；ret3/ret5 = 第3/5个交易日收盘
            for n, label in ((0, 1), (2, 3), (4, 5)):
                if len(g) > n:
                    exit_px = float(g["close"].iloc[n])
                    rets[label] = exit_px / entry - 1
            results.append({"date": day, "code": code,
                            "score": c["score"], "vol_mult": c["ignite"]["vol_mult"],
                            "chg": c["ignite"]["chg"], "chg_close": c["chg_close"],
                            "ret1": rets.get(1), "ret3": rets.get(3),
                            "ret5": rets.get(5)})

    if not results:
        print("\n无点火信号样本")
        return
    df = pd.DataFrame(results)
    print("\n" + "=" * 70)
    print(f"点火信号样本：{len(df)} 条（{df['date'].nunique()} 个交易日）")
    print("\n=== 全部点火信号（含后续收益）===")
    print(df.to_string(index=False))
    print("\n=== 按评分分组（同日多点火时，高分 vs 低分收益）===")
    for sc in sorted(df["score"].unique(), reverse=True):
        sub = df[df["score"] == sc]
        print(f"  评分{sc}: {len(sub)}条, ret1 {sub['ret1'].mean():+.2%} "
              f"ret3 {sub['ret3'].mean():+.2%} ret5 {sub['ret5'].mean():+.2%}")
    print("\n=== 按量比分组（点火强度）===")
    df["vm_g"] = df["vol_mult"].apply(lambda v: "≥5×" if v >= 5 else
                                      ("3-5×" if v >= 3 else "2-3×"))
    for g, sub in df.groupby("vm_g"):
        print(f"  {g}: {len(sub)}条, ret1 {sub['ret1'].mean():+.2%} "
              f"ret3 {sub['ret3'].mean():+.2%} ret5 {sub['ret5'].mean():+.2%}")

    out = ROOT / "output" / "reports" / f"同日多点火选择回测_{dt.date.today():%Y-%m-%d}.md"
    lines = [f"# 同日多点火选择规则回测（{dt.date.today()}）\n",
             f"- 样本：{len(df)} 条点火信号 / {df['date'].nunique()} 个交易日（2026-08-06~08-14）",
             "- 后续收益 = 点火次日开盘买 → N 日后收盘卖\n",
             "| 日期 | 代码 | 评分 | 量比 | 点火涨幅% | 收盘涨幅% | ret1 | ret3 | ret5 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for _, r in df.sort_values(["date", "score"], ascending=[True, False]).iterrows():
        lines.append(f"| {r['date']} | {r['code']} | {r['score']} | {r['vol_mult']}× | "
                     f"{r['chg']} | {r['chg_close']*100:.1f} | "
                     f"{(r['ret1'] if pd.notna(r['ret1']) else '—')} | "
                     f"{(r['ret3'] if pd.notna(r['ret3']) else '—')} | "
                     f"{(r['ret5'] if pd.notna(r['ret5']) else '—')} |")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已输出: {out}")


def _days_in(p: Path) -> list[str]:
    days = set()
    with open(p) as f:
        for row in csv.reader(f):
            if row and len(row[0]) >= 10 and row[0][4] == "-" and row[0][:4].isdigit():
                days.add(row[0][:10])
    return list(days)


if __name__ == "__main__":
    main()
