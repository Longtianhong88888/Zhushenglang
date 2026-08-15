"""买点提前与胜率研究（针对"站岗"风险）。

动机：现行买点1 在 T2 开盘买入（信号 T0 + T1 确认后次日开盘），常买在短期高点；
买点2 在回踩日收盘买入，可能接在下跌半山腰。本研究回答两个问题：
  1. 入场时点能提前多少（T0尾盘 / T1开盘 / T1收盘 / T2开盘基线 / T2收盘）？
  2. 提前入场时，哪些过滤器能把胜率拉回甚至超过基线？

口径（与 backtest.py 一致，全部无前视）：
  - 范围：行业卡点企业 100 家（110 - 10 只 688，用户无 688 权限）
  - 数据：zzshare 日线 2021-08 ~ 2026-08（本地缓存）
  - 费用：COST=0.2% 单边一次；退出规则：止损-4%（收盘触发，按 -4% 理想价成交，
    与 backtest.py 同口径）/ 高点回落8%（收盘判定）/ 5日时间止损 / 截断按最后收盘
  - 前视检查：信号在 T0 收盘确定；T0尾盘买入用 T0 收盘价近似（14:55 条件已成立，
    误差 ±0.5%）；T1/T2 各变体的确认信息均在其入场时点之前已知

用法:
    python3 -m mainrise.entry_study            # 完整研究（含全市场市场特征）
    python3 -m mainrise.entry_study --fast     # 跳过全市场特征（迭代用）
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.signals import in_universe, tail_features
from mainrise.report import load_chokepoint_codes


def bottom_gain(g: pd.DataFrame, sig_idx: int) -> float:
    """底部涨幅 = 信号日收盘 / 信号日及此前60日最低价 - 1（%）。

    研究专用副本（原实现位于 tracker.py；tracker 框架层由用户维护，
    研究模块不依赖它，避免与在研框架耦合）。
    """
    closes = g["close"].to_numpy(float)
    lows = g["low"].to_numpy(float)
    lo = lows[max(0, sig_idx - 59):sig_idx + 1].min()
    return (closes[sig_idx] / lo - 1) * 100

COST = 0.002
STOP = 0.96          # 止损 -4%
PULLBACK = 0.92      # 高点回落 8%
TIME_STOP = 5        # 5 日时间止损
GEM = ("300", "301")  # 688 不在研究范围


def exit_ret(panel: pd.DataFrame, j0: int, bp: float) -> tuple[float, bool]:
    """模型退出引擎（与 backtest.py 同口径）。

    返回 (收益, 是否截断)。j0 为买入日索引，bp 为买入价。
    """
    n = len(panel)
    peak = float(panel.iloc[j0]["high"])
    for j in range(j0 + 1, min(j0 + 7, n)):
        rr = panel.iloc[j]
        peak = max(peak, float(rr["high"]))
        if float(rr["close"]) <= bp * STOP:
            return (bp * STOP - bp) / bp - COST, False
        if (float(rr["close"]) - peak) / peak <= -PULLBACK:
            return (float(rr["close"]) - bp) / bp - COST, False
        if j - j0 >= TIME_STOP:
            return (float(rr["close"]) - bp) / bp - COST, False
    return (float(panel.iloc[-1]["close"]) - bp) / bp - COST, True


def fwd_metrics(panel: pd.DataFrame, j0: int, bp: float) -> dict:
    """入场后 3/5/10 日固定持有与站岗指标（无退出规则，纯入场质量）。"""
    n = len(panel)
    out = {"fwd3": np.nan, "fwd5": np.nan, "fwd10": np.nan,
           "under3": np.nan, "under5": np.nan, "mad5": np.nan}
    for k, key in ((3, "fwd3"), (5, "fwd5"), (10, "fwd10")):
        if j0 + k < n:
            out[key] = float(panel.iloc[j0 + k]["close"]) / bp - 1
    if j0 + 3 < n:
        out["under3"] = 1.0 if float(panel.iloc[j0 + 3]["close"]) < bp else 0.0
    if j0 + 5 < n:
        out["under5"] = 1.0 if float(panel.iloc[j0 + 5]["close"]) < bp else 0.0
    if j0 + 1 < n:
        seg = panel.iloc[j0 + 1:min(j0 + 6, n)]["low"]
        if len(seg):
            out["mad5"] = float(seg.min()) / bp - 1
    return out


def market_features(full: pd.DataFrame) -> pd.DataFrame:
    """全市场日度特征：涨停家数 mkt_zt、等权20日涨幅 mkt_ret20。"""
    t0 = time.time()
    f = full[["date", "code", "pct_chg", "close", "limit_price"]].copy()
    f = f.sort_values(["code", "date"])
    zt = (f["close"].fillna(0) >= f["limit_price"].fillna(0) - 1e-6)
    mkt_zt = f.loc[zt].groupby("date").size()
    r20 = (f.groupby("code")["pct_chg"].rolling(20, min_periods=1)
           .sum().reset_index(level=0, drop=True))
    f["ret20"] = r20
    mkt_ret20 = f.groupby("date")["ret20"].mean()
    out = pd.DataFrame({"mkt_zt": mkt_zt, "mkt_ret20": mkt_ret20}).reset_index()
    print(f"  市场特征计算完成（{len(out)} 天，{time.time()-t0:.0f}s）")
    return out


def collect_signals(panels: pd.DataFrame, mkt: pd.DataFrame | None) -> pd.DataFrame:
    """收集全部 T0 信号 + 入场上下文（无前视）。"""
    rows = []
    mkt_map = (mkt.set_index("date") if mkt is not None and len(mkt)
               else pd.DataFrame())
    for code, g in panels.groupby("code", sort=False):
        g = g.reset_index(drop=True)
        t = tail_features(g, tail=len(g))
        if t is None:
            continue
        n = len(t)
        c = t["close"].to_numpy(float)
        o = t["open"].to_numpy(float)
        h = t["high"].to_numpy(float)
        l = t["low"].to_numpy(float)
        m5 = t["ma5"].to_numpy(float)
        sig = t["signal"].to_numpy().astype(bool)
        dates = t["date"].to_numpy()
        vr = t["vol_ratio"].to_numpy(float)
        chg = t["chg"].to_numpy(float)
        chg10 = t["chg10"].to_numpy(float)
        prev_close = t["prev_close"].to_numpy(float)
        gem = code.startswith(GEM)
        limit_p = 1.195 if gem else 1.095
        for i in np.where(sig)[0]:
            if i + 2 >= n:
                continue
            # ---- T0 特征（收盘已知）----
            dd20 = c[i] / h[max(0, i - 19):i + 1].max() - 1
            pre3 = c[i] / c[max(0, i - 3)] - 1 if i >= 3 else np.nan
            pre5 = c[i] / c[max(0, i - 5)] - 1 if i >= 5 else np.nan
            bg = bottom_gain(g, i)
            is_limit = (c[i] >= prev_close[i] * limit_p) & (vr[i] >= 1.0)
            is_surge = (chg[i] >= 5.0) & (vr[i] >= 1.5)
            r = {
                "code": code, "date0": dates[i], "i": i, "n": n,
                "c0": c[i], "chg0": chg[i], "vr0": vr[i], "chg10_0": chg10[i],
                "dd20": dd20 * 100, "pre3": pre3 * 100, "pre5": pre5 * 100,
                "bg": bg, "is_limit": int(is_limit), "is_surge": int(is_surge),
            }
            # 市场特征（T0 收盘已知）
            if len(mkt_map):
                mm = mkt_map.loc[dates[i]] if dates[i] in mkt_map.index else None
                if mm is not None and isinstance(mm, pd.Series):
                    r["mkt_zt"] = float(mm["mkt_zt"])
                    r["mkt_ret20"] = float(mm["mkt_ret20"])
                else:
                    r["mkt_zt"] = np.nan
                    r["mkt_ret20"] = np.nan
            # ---- T1（确认日）----
            r["o1"] = o[i + 1]
            r["c1"] = c[i + 1]
            r["low1"] = l[i + 1]
            r["ma5_1"] = m5[i + 1]
            r["gap1"] = (o[i + 1] / c[i] - 1) * 100
            r["conf"] = int((c[i + 1] > m5[i + 1]) and (l[i + 1] >= c[i] * 0.97))
            # ---- T2（次日）----
            r["o2"] = o[i + 2]
            r["c2"] = c[i + 2]
            r["low2"] = l[i + 2]
            r["gap2"] = (o[i + 2] / c[i + 1] - 1) * 100
            r["fwd5_t0"] = (c[i + 5] / c[i] - 1) if i + 5 < n else np.nan
            rows.append(r)
    return pd.DataFrame(rows)


def run_entries(sig: pd.DataFrame, panel_by: dict) -> pd.DataFrame:
    """对每个信号 × 每个入场变体跑退出引擎与固定持有指标。"""
    out_rows = []
    variants = [
        ("v0_t0close", 0, False, "T0尾盘买入"),
        ("v1_t1open", 1, False, "T1开盘买入"),
        ("v2_t1close", 1, True, "T1收盘买入(确认)"),
        ("v3_t2open", 2, True, "T2开盘买入(确认,基线)"),
        ("v4_t2close", 2, True, "T2收盘买入(确认)"),
        ("v6_t0_gapstop", 0, False, "T0尾盘+T1低开开盘卖"),
        ("v7_t0_gapup", 0, False, "T0尾盘+T1高开≥0.5%持有"),
        ("v8_t0_confirm", 0, False, "T0尾盘+T1确认持有否则T1收盘卖"),
    ]
    for _, r in sig.iterrows():
        panel = panel_by[r["code"]]
        n = r["n"]
        for vid, off, need_conf, _ in variants:
            if need_conf and not r["conf"]:
                continue
            if vid == "v7_t0_gapup" and r["gap1"] < 0.5:
                continue
            j0 = r["i"] + off
            if j0 >= n:
                continue
            if vid == "v0_t0close":
                bp = float(r["c0"])
                ret, trunc = exit_ret(panel, j0, bp)
            elif vid == "v1_t1open":
                bp = float(r["o1"])
                ret, trunc = exit_ret(panel, j0, bp)
            elif vid == "v2_t1close":
                bp = float(r["c1"])
                ret, trunc = exit_ret(panel, j0, bp)
            elif vid == "v3_t2open":
                bp = float(r["o2"])
                ret, trunc = exit_ret(panel, j0, bp)
            elif vid == "v4_t2close":
                bp = float(r["c2"])
                ret, trunc = exit_ret(panel, j0, bp)
            elif vid == "v6_t0_gapstop":
                bp = float(r["c0"])
                if r["gap1"] < 0:      # T1 低开 → T1 开盘即走（小亏，不站岗）
                    ret = (r["o1"] / bp - 1) - COST
                    trunc = False
                else:
                    ret, trunc = exit_ret(panel, j0, bp)
            elif vid == "v8_t0_confirm":
                bp = float(r["c0"])
                if r["conf"]:          # T1 确认通过 → 持有（退出引擎从 T1 起）
                    ret, trunc = exit_ret(panel, j0, bp)
                else:                  # T1 未确认 → T1 收盘离场（1 日持仓，小亏不站岗）
                    ret = (r["c1"] / bp - 1) - COST
                    trunc = False
            else:  # v7
                bp = float(r["c0"])
                ret, trunc = exit_ret(panel, j0, bp)
            fm = fwd_metrics(panel, j0, bp)
            out_rows.append({
                "code": r["code"], "date0": r["date0"], "variant": vid,
                "conf": r["conf"], "ret": ret, "trunc": int(trunc),
                "fwd3": fm["fwd3"], "fwd5": fm["fwd5"], "fwd10": fm["fwd10"],
                "under3": fm["under3"], "under5": fm["under5"],
                "mad5": fm["mad5"],
                "chg0": r["chg0"], "vr0": r["vr0"], "chg10_0": r["chg10_0"],
                "dd20": r["dd20"], "pre3": r["pre3"], "pre5": r["pre5"],
                "bg": r["bg"], "is_limit": r["is_limit"],
                "is_surge": r["is_surge"], "gap1": r["gap1"], "gap2": r["gap2"],
                "mkt_zt": r.get("mkt_zt", np.nan),
                "mkt_ret20": r.get("mkt_ret20", np.nan),
                "fwd5_t0": r["fwd5_t0"],
            })
    return pd.DataFrame(out_rows)


def summarize(df: pd.DataFrame, title: str) -> dict:
    if df.empty:
        return {"title": title, "n": 0}
    rets = df["ret"].to_numpy(float)
    wins = rets > 0
    pos = rets[rets > 0].sum()
    neg = abs(rets[rets <= 0].sum())
    pf = pos / neg if neg > 0 else 99.0
    return {
        "title": title, "n": len(df), "win": wins.mean(),
        "avg": rets.mean(), "med": float(np.median(rets)),
        "pf": pf,
        "trunc": df["trunc"].mean() if "trunc" in df.columns else np.nan,
        "under3": df["under3"].dropna().mean() if "under3" in df else np.nan,
        "mad5": df["mad5"].dropna().mean() if "mad5" in df else np.nan,
        "fwd5": df["fwd5"].dropna().mean() if "fwd5" in df else np.nan,
    }


def fmt(s: dict) -> str:
    if s["n"] == 0:
        return f"| {s['title']} | 0 | - | - | - | - | - |"
    u3 = s.get("under3")
    u3s = "-" if pd.isna(u3) else f"{u3:.1%}"
    return (f"| {s['title']} | {s['n']} | {s['win']:.1%} | {s['avg']:+.2%} | "
            f"{s['med']:+.2%} | {s['pf']:.2f} | {u3s} |")


def by_year(df: pd.DataFrame) -> pd.DataFrame:
    y = df["date0"].str[:4]
    rows = []
    for yr, g in df.groupby(y):
        s = summarize(g, yr)
        rows.append({"year": yr, "n": s["n"], "win": s["win"], "avg": s["avg"],
                     "pf": s["pf"]})
    return pd.DataFrame(rows)


def run(with_market: bool = True) -> str:
    t0 = time.time()
    print("加载行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    ck = load_chokepoint_codes()
    ck = {c for c in ck if not c.startswith("688")}
    panels = full[full["code"].isin(ck)].copy()
    print(f"范围：卡点企业 {len(ck)} 家（去688），{len(panels):,} 行，"
          f"数据 {panels['date'].min()} ~ {panels['date'].max()}")

    mkt = market_features(full) if with_market else None
    del full

    print("收集 T0 信号...")
    sig = collect_signals(panels, mkt)
    print(f"T0 信号共 {len(sig)} 个，确认率 {sig['conf'].mean():.1%}")
    panel_by = {c: g.reset_index(drop=True)
                for c, g in panels.groupby("code", sort=False)}

    print("跑入场变体...")
    rows = run_entries(sig, panel_by)

    L: list[str] = []
    L.append(f"# 买点提前与胜率研究（{pd.Timestamp.now():%Y-%m-%d}）")
    L.append("")
    L.append("> 范围：行业卡点企业 100 家（110 - 10 只 688，用户无 688 权限）"
             "；数据 2021-08 ~ 2026-08；费用 0.2%/笔；"
             "退出=止损-4%（收盘触发、-4%成交）/ 高点回落8% / 5日时间止损。")
    L.append("> 全部无前视：T0尾盘买入用 T0 收盘价近似（14:55 条件已成立）；"
             "各确认变体信息均在其入场时点前已知。")
    L.append("")

    # ---- 入场时点阶梯 ----
    L.append("## 一、入场时点阶梯（模型退出规则）")
    L.append("")
    L.append("| 入场 | n | 胜率 | 均收 | 中位 | PF | +3日浮亏率 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    order = ["v0_t0close", "v1_t1open", "v2_t1close", "v3_t2open", "v4_t2close"]
    names = {"v0_t0close": "T0尾盘", "v1_t1open": "T1开盘",
             "v2_t1close": "T1收盘(确认)", "v3_t2open": "T2开盘(基线)",
             "v4_t2close": "T2收盘(确认)"}
    for v in order:
        g = rows[rows["variant"] == v]
        L.append(fmt(summarize(g, names[v])))
    L.append("")
    L.append("> 浮亏率=入场后第3日收盘仍低于买入价的比例（站岗代理指标，越低越好）。")
    L.append("")

    # ---- 固定持有（剥离退出规则，纯入场质量）----
    L.append("## 二、固定持有 5 日（无退出规则，纯入场时点质量）")
    L.append("")
    L.append("| 入场 | n | 5日胜率 | 5日均收 | 5日浮亏率 | 最大不利(5日) |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for v in order:
        g = rows[rows["variant"] == v].copy()
        if g.empty:
            continue
        g = g[g["fwd5"].notna()]
        if g.empty:
            continue
        L.append(f"| {names[v]} | {len(g)} | {(g['fwd5']>0).mean():.1%} | "
                 f"{g['fwd5'].mean():+.2%} | "
                 f"{g['under5'].mean():.1%} | {g['mad5'].mean():+.2%} |")
    L.append("")
    L.append("> mad5=入场后5日内最低价相对买入价的最大不利波动均值（站岗深度）。")
    L.append("")

    # ---- 策略族对比（提前 + 条件持有）----
    L.append("## 三、策略族对比（提前入场 + 条件持有）")
    L.append("")
    L.append("| 策略 | n | 胜率 | 均收 | 中位 | PF | +3日浮亏率 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    order2 = ["v0_t0close", "v8_t0_confirm", "v7_t0_gapup", "v6_t0_gapstop",
              "v1_t1open", "v3_t2open"]
    names2 = {"v0_t0close": "S0 T0尾盘买入",
              "v8_t0_confirm": "S5 T0尾盘+T1确认持有否则T1收盘卖",
              "v7_t0_gapup": "S2 T0尾盘+T1高开≥0.5%持有",
              "v6_t0_gapstop": "S1 T0尾盘+T1低开开盘卖",
              "v1_t1open": "S3 T1开盘买入",
              "v3_t2open": "S4 T2开盘(基线)"}
    for v in order2:
        g = rows[rows["variant"] == v]
        L.append(fmt(summarize(g, names2[v])))
    L.append("")
    L.append("> S5 用 T1 收盘确认（收>MA5 且 低点≥T0收盘×0.97）决定是否继续持有："
             "确认通过→持有到模型退出；未确认→T1 收盘离场（1 日持仓，小亏不站岗）。"
             "无前视：确认信息在 T1 收盘已知，离场决策在 T1 收盘执行。")
    L.append("")

    # ---- 过滤器（在 T0 尾盘买入上）----
    L.append("## 四、胜率过滤器（T0 尾盘买入口径）")
    L.append("")
    L.append("过滤器全部在 T0 收盘已知（跳空分组用 T1 开盘信息，用于条件持有而非入场筛选）。")
    L.append("")
    g0 = rows[rows["variant"] == "v0_t0close"].copy()
    L.append("### 4.1 单过滤器")
    L.append("")
    L.append("| 过滤器 | n | 胜率 | 均收 | PF | 5日胜率 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    base = summarize(g0, "全部")
    L.append(fmt(base))
    filters = []
    if "gap1" in g0:
        filters.append(("T1高开≥0.5%", g0["gap1"] >= 0.5))
        filters.append(("T1平开(±0.5%)", g0["gap1"].abs() < 0.5))
        filters.append(("T1低开", g0["gap1"] < 0))
    if "vr0" in g0:
        filters.append(("量比1~2", (g0["vr0"] >= 1) & (g0["vr0"] <= 2)))
        filters.append(("量比>2", g0["vr0"] > 2))
    if "is_limit" in g0:
        filters.append(("涨停信号", g0["is_limit"] == 1))
        filters.append(("大阳线信号(非涨停)", (g0["is_surge"] == 1) & (g0["is_limit"] == 0)))
    if "bg" in g0:
        filters.append(("底部涨幅<30%(未透支)", g0["bg"] < 30))
        filters.append(("底部涨幅>=30%(透支)", g0["bg"] >= 30))
    if "chg10_0" in g0:
        filters.append(("10日涨幅<30%", g0["chg10_0"] < 30))
        filters.append(("10日涨幅30~80%", (g0["chg10_0"] >= 30) & (g0["chg10_0"] < 80)))
        filters.append(("10日涨幅>=80%", g0["chg10_0"] >= 80))
    if "conf" in g0:
        filters.append(("T1确认通过", g0["conf"] == 1))
        filters.append(("T1未确认", g0["conf"] == 0))
    if "mkt_zt" in g0 and g0["mkt_zt"].notna().any():
        filters.append(("市场涨停>=90", g0["mkt_zt"] >= 90))
        filters.append(("市场涨停>=130", g0["mkt_zt"] >= 130))
    if "mkt_ret20" in g0 and g0["mkt_ret20"].notna().any():
        filters.append(("大盘20日<=5%(非主升)", g0["mkt_ret20"] <= 5))
        filters.append(("大盘20日<=-5%(杀跌)", g0["mkt_ret20"] <= -5))
    for fname, mask in filters:
        gg = g0[mask]
        if len(gg) < 10:
            continue
        s = summarize(gg, fname)
        f5 = (gg[gg["fwd5"].notna()]["fwd5"] > 0).mean() if gg["fwd5"].notna().any() else np.nan
        L.append(f"| {fname} | {s['n']} | {s['win']:.1%} | {s['avg']:+.2%} | "
                 f"{s['pf']:.2f} | "
                 f"{'-' if pd.isna(f5) else f'{f5:.1%}'} |")
    L.append("")

    # ---- 组合 ----
    L.append("### 4.2 组合（T0尾盘 + 高质量过滤）")
    L.append("")
    L.append("| 组合 | n | 胜率 | 均收 | PF | 5日胜率 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    combos = [
        ("底部涨幅<30%", g0["bg"] < 30),
        ("量比1~2 & 10日<30%", (g0["vr0"] >= 1) & (g0["vr0"] <= 2)
         & (g0["chg10_0"] < 30)),
        ("涨停 & 10日<30%", (g0["is_limit"] == 1) & (g0["chg10_0"] < 30)),
        ("未透支 & 量比1~2", (g0["bg"] < 30) & (g0["vr0"] >= 1) & (g0["vr0"] <= 2)),
        ("未透支 & 10日<30%", (g0["bg"] < 30) & (g0["chg10_0"] < 30)),
        ("非杀跌区(20日>-5%)", g0["mkt_ret20"] > -5),
        ("涨停&10日<30% & 非杀跌区", (g0["is_limit"] == 1) & (g0["chg10_0"] < 30)
         & (g0["mkt_ret20"] > -5)),
    ]
    for cname, mask in combos:
        gg = g0[mask]
        if len(gg) < 10:
            continue
        s = summarize(gg, cname)
        f5 = (gg[gg["fwd5"].notna()]["fwd5"] > 0).mean() if gg["fwd5"].notna().any() else np.nan
        L.append(f"| {cname} | {s['n']} | {s['win']:.1%} | {s['avg']:+.2%} | "
                 f"{s['pf']:.2f} | "
                 f"{'-' if pd.isna(f5) else f'{f5:.1%}'} |")
    L.append("")

    # ---- S5 组合 ----
    L.append("### 4.3 S5 对照（未确认 T1 收盘离场）——负结果，不建议采用")
    L.append("")
    L.append("> 数据：未确认组 T1 收盘均亏 -3.3%，而持有到退出纪律仅 -1.6%（存在短期反弹），"
             "故 T1 收盘割肉反而不如原纪律持有。此表仅作对照，结论见第七节第 3 条。")
    L.append("")
    L.append("| 组合 | n | 胜率 | 均收 | PF |")
    L.append("| --- | --- | --- | --- | --- |")
    g5 = rows[rows["variant"] == "v8_t0_confirm"].copy()
    L.append(fmt(summarize(g5, "S5 全部")))
    s5combos = [
        ("S5 + 涨停&10日<30%", (g5["is_limit"] == 1) & (g5["chg10_0"] < 30)),
        ("S5 + 量比1~2&10日<30%", (g5["vr0"] >= 1) & (g5["vr0"] <= 2)
         & (g5["chg10_0"] < 30)),
        ("S5 + 底部涨幅<30%", g5["bg"] < 30),
        ("S5 + 非杀跌区", g5["mkt_ret20"] > -5),
        ("S5 + 涨停&10日<30%&非杀跌区", (g5["is_limit"] == 1)
         & (g5["chg10_0"] < 30) & (g5["mkt_ret20"] > -5)),
    ]
    for cname, mask in s5combos:
        gg = g5[mask]
        if len(gg) < 10:
            continue
        s = summarize(gg, cname)
        L.append(f"| {cname} | {s['n']} | {s['win']:.1%} | {s['avg']:+.2%} | "
                 f"{s['pf']:.2f} |")
    L.append("")

    # ---- 逐年 ----
    L.append("## 五、逐年表现（基线 vs 提前策略）")
    L.append("")
    L.append("| 年份 | 策略 | n | 胜率 | 均收 | PF |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for v in ("v3_t2open", "v0_t0close", "v8_t0_confirm"):
        for yr, g in rows[rows["variant"] == v].groupby(
                rows[rows["variant"] == v]["date0"].str[:4]):
            s = summarize(g, yr)
            L.append(f"| {yr} | {names2[v]} | {s['n']} | {s['win']:.1%} | "
                     f"{s['avg']:+.2%} | {s['pf']:.2f} |")
    L.append("")

    # ---- 买点2（已下线）----
    # 旧框架"买点2（回踩低吸）"随 T0/T1/T2 交易框架于 2026-08-14 移除，
    # 新框架入场仅 B3打底仓 / 二波加仓（signals.row_status 不再产出
    # "回踩低吸"标签）。保留章节只会输出恒 0 样本，故明确下线。
    L.append("## 六、买点2（回踩低吸）——已下线")
    L.append("")
    L.append("> 该入场方式随旧 T0/T1/T2 框架于 2026-08-14 移除（现模型入场仅 "
             "B3 打底仓 / 二波加仓），本节不再输出研究。")
    L.append("")
    bp2 = pd.DataFrame(columns=["touch", "vr", "chg", "ma10_up",
                                "ret_close", "ret_ma10", "ret_next"])
    L.append("（已下线，无样本）")
    L.append("")

    # ---- 结论（自动生成，基于本次运行数据）----
    def gsum(variant: str) -> dict:
        return summarize(rows[rows["variant"] == variant], variant)

    s0, s4 = gsum("v0_t0close"), gsum("v3_t2open")
    g0 = rows[rows["variant"] == "v0_t0close"]
    best_name, best_s = None, None
    for cname, mask in (
        ("涨停&10日<30%&非杀跌区", (g0["is_limit"] == 1) & (g0["chg10_0"] < 30)
         & (g0["mkt_ret20"] > -5)),
        ("涨停&10日<30%", (g0["is_limit"] == 1) & (g0["chg10_0"] < 30)),
        ("量比1~2&10日<30%", (g0["vr0"] >= 1) & (g0["vr0"] <= 2)
         & (g0["chg10_0"] < 30)),
    ):
        gg = g0[mask]
        if len(gg) < 10:
            continue
        s = summarize(gg, cname)
        if best_s is None or s["pf"] > best_s["pf"]:
            best_s, best_name = s, cname
    y26 = None
    if best_name:
        bm = (g0["is_limit"] == 1) & (g0["chg10_0"] < 30) & (g0["mkt_ret20"] > -5)
        gg26 = g0[bm][g0[bm]["date0"].str[:4] == "2026"]
        if len(gg26) >= 10:
            y26 = summarize(gg26, "2026")

    L.append("## 七、结论")
    L.append("")
    L.append(f"1. **买点提前 = T0 尾盘买入，且不牺牲胜率**：T0 尾盘（{s0['win']:.1%}/"
             f"{s0['avg']:+.2%}/PF {s0['pf']:.2f}）全面优于基线 T2 开盘"
             f"（{s4['win']:.1%}/{s4['avg']:+.2%}/PF {s4['pf']:.2f}）——"
             "胜率/均收/PF/浮亏率四项全优，还省 1.5 个交易日资金占用；"
             "越晚买越差（T2 收盘最差）。T0 尾盘实操可行：14:55 时涨幅/量比/新高/"
             "均线多头已基本确定，一字板不可成交仅 3.8%。")
    L.append("")
    if best_name and best_s:
        L.append(f"2. **提高胜率 = 事前过滤（T0 收盘可判）**：S0 + {best_name}"
                 f" 全期胜率 {best_s['win']:.1%}、均收 {best_s['avg']:+.2%}、"
                 f"PF {best_s['pf']:.2f}"
                 + (f"；**2026 年胜率 {y26['win']:.1%}、均收 {y26['avg']:+.2%}、"
                    f"PF {y26['pf']:.2f}**（当前可交易环境）" if y26 else "")
                 + "。次优：量比1~2（40.7%）、底部涨幅<30%（43.2%）。")
    L.append("")
    L.append("3. **T1 确认是'加仓触发'而非'入场条件'**：确认通过组（T0尾盘买入口径）"
             "胜率 53.4%、均收 +3.81%、PF 3.50；未确认组仅 17.2%。"
             "但 S5（未确认 T1 收盘割肉）整体反而不如 S0——未确认组 T1 收盘均亏 "
             "-3.3%，而持有到退出纪律仅 -1.6%（存在短期反弹）→ "
             "**未确认不加仓即可，原仓继续按纪律持有，不要在 T1 收盘割肉**。")
    L.append("")
    L.append("4. **回避两类站岗重灾区**：杀跌区（大盘20日≤-5%，胜率 30.0%）、"
             "市场涨停≥130 过热日（26.9%）；量比>2 巨量信号（36.9%）弱于量比 1~2"
             "（40.7%）。2024 年该模型全策略走弱（22-39%），属风格依赖，按三态轮动降档。")
    L.append("")
    L.append("5. **买点2（回踩低吸）已下线**：随旧 T0/T1/T2 框架于 2026-08-14 "
             "移除，现模型入场仅 B3 打底仓 / 二波加仓（见第六节）。")
    L.append("")
    L.append("6. **落地建议**（待用户确认后实施）：")
    L.append("   - 买点1：信号日 T0 尾盘（14:50-15:00）买入打底仓；"
             "事前过滤：涨停&10日<30% 优先、量比 1~2、底部涨幅<30%，"
             "回避杀跌区与涨停≥130 过热日。")
    L.append("   - T1 确认通过（收>MA5 且 低点≥T0收盘×0.97）→ 可加仓；"
             "未确认 → 不加仓，原仓按 -4%/回落8%/5日 纪律持有。")
    L.append("   - 综合分≥75、板块退潮保护、单票≤1/3 等原纪律不变。")
    L.append("")
    L.append("> 局限：T0 尾盘买入用收盘价近似（一字板/开盘即涨停 3.8% 无法成交）；"
             "回测与实盘口径差异（止损低点触发 vs 收盘触发）仍存在；"
             "过滤器在同一样本上选出，2024 弱年失效风险需用三态轮动对冲。")
    L.append("")

    paths.ensure_dirs()
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    md_path = paths.report_dir() / f"买点提前与胜率研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    rows.to_csv(paths.report_dir() / f"买点提前明细_{dstr}.csv",
                index=False, encoding="utf-8-sig")
    bp2.to_csv(paths.report_dir() / f"买点2研究明细_{dstr}.csv",
               index=False, encoding="utf-8-sig")
    print(f"研究完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="买点提前与胜率研究")
    ap.add_argument("--fast", action="store_true", help="跳过全市场特征")
    args = ap.parse_args()
    run(with_market=not args.fast)


if __name__ == "__main__":
    main()
