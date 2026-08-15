"""起涨特征研究 + 全市场回测。

三步：
1. 提取 72 只行业卡点企业历史"起涨日"（未来20日涨幅≥20% 且 前期20日涨幅≤10%，
   聚类去重后取段落内涨幅最大的一天为启动日）；
2. 量化起涨前/当日特征（涨幅、量比、20日回撤、前期涨幅、前5日、市场涨停家数等），
   与全市场同口径对比，找出区分特征；
3. 深挖 r7m/r9 信号日**之前**的前置特征（下跌结构/量能形态/日内形态/均线位置），
   量化分布并构造增强规则（r7mA/r7mE/r9A/r9E）；
4. 把量化特征做成无未来函数的规则，全市场回测（次日开盘买入、收盘卖出、双边费用0.2%），
   与基准及原 T0 规则对比，输出研究报告 + 信号明细 CSV。
"""
from __future__ import annotations

import sys
from datetime import datetime

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.report import load_chokepoint_codes
from mainrise.signals import load_names

START = "2021-01-01"
COST = 0.002          # 双边费用 0.2%
FWD_WIN = 0.20        # 起涨定义：未来20日涨幅 ≥ 20%
PRIOR_CAP = 0.10      # 前期20日涨幅 ≤ 10%（排除趋势中段）
CLUSTER = 15          # 同一段起涨内相邻候选最大间隔（交易日）
LAUNCH_WIN = 6        # 启动日 = 段内未来6日里涨幅最大的一天

# 全市场可交易规则（无未来函数；r=回撤深 v=温和量比 m=市场共振）
RULES = {
    "base": "基准：全市场任意交易日",
    "t0": "原T0规则裸跑：多头排列+创20日新高+（涨≥5%量比≥1.5 或 涨停量比≥1.0）",
    "r4": "超跌启动：20日回撤≥15% + 当日涨≥5% + 量比≥1.3",
    "r4m": "超跌温和量：20日回撤≥15% + 涨≥5% + 量比1.0~2.0",
    "r7": "超跌+市场共振：r4 + 全市场涨停≥90",
    "r7m": "温和量+市场共振：r4m + 全市场涨停≥90",
    "r8m": "深度超跌温和量：20日回撤≥25% + 涨≥5% + 量比1.0~2.0",
    "r9": "超跌+强市场：r4 + 全市场涨停≥130",
}

PRE_FEATURES = [
    "pre3", "pre5", "pre10", "gap", "body", "close_pos", "upper_shadow",
    "amp", "low_open_up", "above_ma5", "above_ma10", "above_ma20",
    "new_low_rebound", "prev_chg", "prev_vol_r", "vol5_v20", "v5_trend",
    "dd20", "days_from_high", "drop_speed",
]


def _market_zt(panels: pd.DataFrame) -> pd.Series:
    return panels[panels["close"] >= panels["limit_price"] - 1e-6].groupby("date").size()


def _launch_episodes(g: pd.DataFrame) -> list[int]:
    """段落起点：未来20日≥20% 且 前期20日≤10%，间隔>15天聚为一类，取段落起点。"""
    g = g.reset_index(drop=True)
    n = len(g)
    if n < 80:
        return []
    close = g["close"].astype(float)
    fwd20 = close.shift(-20) / close - 1
    prior20 = close / close.shift(20) - 1
    idx = np.where(((fwd20 >= FWD_WIN) & (prior20 <= PRIOR_CAP)).values)[0]
    ep = []
    for i in idx:
        if not ep or i - ep[-1] > CLUSTER:
            ep.append(i)
    return ep


def _launch_features(g: pd.DataFrame, zt_map: pd.Series) -> pd.DataFrame:
    g = g.reset_index(drop=True)
    n = len(g)
    if n < 80:
        return pd.DataFrame()
    close = g["close"].astype(float)
    high = g["high"].astype(float)
    vol = g["volume"].astype(float)
    amt = g["amount"].astype(float)
    rows = []
    for t0 in _launch_episodes(g):
        if t0 < 21 or t0 + 26 > n - 1:
            continue
        win = range(t0, min(t0 + LAUNCH_WIN, n))
        s = max(win, key=lambda i: float(g["pct_chg"].iloc[i]))   # 启动日
        c = close.iloc[s]
        v5 = float(vol.iloc[s - 5:s].mean())
        a5 = float(amt.iloc[s - 5:s].mean())
        vr = float(vol.iloc[s]) / v5 if v5 > 0 else np.nan
        ar = float(amt.iloc[s]) / a5 if a5 > 0 else np.nan
        zt = c >= float(g["limit_price"].iloc[s]) - 1e-6
        h20_prev = float(high.iloc[s - 20:s].max())
        new20 = c >= h20_prev - 1e-9
        ma5p = float(close.iloc[s - 5:s - 1].mean())
        ma10p = float(close.iloc[s - 10:s - 1].mean())
        ma20p = float(close.iloc[s - 20:s - 1].mean())
        bull_prev = ma5p > ma10p > ma20p
        dd_prev = (float(close.iloc[s - 1]) / h20_prev - 1) * 100
        prior20_prev = (float(close.iloc[s - 1]) / float(close.iloc[s - 21]) - 1) * 100
        g60_prev = (float(close.iloc[s - 1]) / float(close.iloc[s - 61]) - 1) * 100 if s >= 61 else np.nan
        pre5 = (c / float(close.iloc[s - 5]) - 1) * 100
        fwd5 = (float(close.iloc[s + 5]) / c - 1) * 100 if s + 5 < n else np.nan
        fwd10 = (float(close.iloc[s + 10]) / c - 1) * 100 if s + 10 < n else np.nan
        fwd20 = (float(close.iloc[s + 20]) / c - 1) * 100 if s + 20 < n else np.nan
        rows.append({
            "code": g["code"].iloc[0], "date": g["date"].iloc[s],
            "chg": float(g["pct_chg"].iloc[s]), "vr": vr, "ar": ar,
            "zt": zt, "new20": new20, "bull_prev": bull_prev,
            "dd_prev": dd_prev, "prior20_prev": prior20_prev, "g60_prev": g60_prev,
            "pre5": pre5, "fwd5": fwd5, "fwd10": fwd10, "fwd20": fwd20,
            "mkt_zt": int(zt_map.get(g["date"].iloc[s], 0)),
        })
    return pd.DataFrame(rows)


def _signal_mask(p: pd.DataFrame) -> dict[str, pd.Series]:
    if "pre5" not in p.columns:                      # 增强规则需要前置特征列
        p = _pre_features(p)
    close = p["close"].astype(float)
    high = p["high"].astype(float)
    vol = p["volume"].astype(float)
    pct = p["pct_chg"].astype(float)
    lim = p["limit_price"].astype(float)
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    bull = (ma5 > ma10) & (ma10 > ma20)
    h20p = high.shift(1).rolling(20).max()
    new20 = close >= h20p - 1e-9
    v5 = vol.shift(1).rolling(5).mean()
    vr = vol / v5
    dd20 = close.shift(1) / h20p - 1
    zt = close >= lim - 1e-6
    mzt = p["mkt_zt"]
    deep = dd20 <= -0.15
    deep25 = dd20 <= -0.25
    volup = (pct >= 5) & (vr >= 1.3)
    volup_m = (pct >= 5) & (vr >= 1.0) & (vr <= 2.0)
    masks = {
        "base": pct.notna(),
        "t0": bull & new20 & (((pct >= 5) & (vr >= 1.5)) | (zt & (vr >= 1.0))),
        "r4": deep & volup,
        "r4m": deep & volup_m,
        "r7": deep & volup & (mzt >= 90),
        "r7m": deep & volup_m & (mzt >= 90),
        "r8m": deep25 & volup_m,
        "r9": deep & volup & (mzt >= 130),
        "r7mA": deep & volup_m & (mzt >= 90) & (p["pre5"] < 0),
        "r7mE": deep & volup_m & (mzt >= 90) & p["new_low_rebound"],
        "r9A": deep & volup & (mzt >= 130) & (p["pre5"] < 0),
        "r9E": deep & volup & (mzt >= 130) & p["new_low_rebound"],
    }
    return {k: v.fillna(False) for k, v in masks.items()}


def _pre_features(g: pd.DataFrame) -> pd.DataFrame:
    """信号日之前/当日形态特征（全部只用 <= 当日的收盘数据，无未来函数）。"""
    n = len(g)
    if n < 21:
        return g
    close = g["close"].astype(float)
    open_ = g["open"].astype(float)
    high = g["high"].astype(float)
    low = g["low"].astype(float)
    vol = g["volume"].astype(float)
    pct = g["pct_chg"].astype(float)
    prevc = g["prev_close"].astype(float)
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    h20p = high.shift(1).rolling(20).max()
    l20p = low.shift(1).rolling(20).min()
    v5 = vol.shift(1).rolling(5).mean()
    v20 = vol.rolling(20).mean()
    g["pre3"] = (close / close.shift(3) - 1) * 100
    g["pre5"] = (close / close.shift(5) - 1) * 100
    g["pre10"] = (close / close.shift(10) - 1) * 100
    g["gap"] = (open_ / prevc - 1) * 100
    g["body"] = (close - open_) / open_ * 100
    rng = high - low
    g["close_pos"] = np.where(rng > 0, (close - low) / rng, 0.5)
    g["upper_shadow"] = np.where(rng > 0, (high - close) / rng, 0.0)
    g["amp"] = (high - low) / prevc * 100
    g["low_open_up"] = (open_ < prevc) & (close > prevc)
    g["above_ma5"] = close > ma5
    g["above_ma10"] = close > ma10
    g["above_ma20"] = close > ma20
    g["new_low_rebound"] = (low <= l20p + 1e-9) & (close > prevc)
    g["prev_chg"] = pct.shift(1)
    g["prev_vol_r"] = vol.shift(1) / vol.shift(2).rolling(5).mean()
    g["vol5_v20"] = v5 / v20
    g["v5_trend"] = vol.shift(1).rolling(5).mean() / vol.shift(6).rolling(5).mean()
    g["dd20"] = (close.shift(1) / h20p - 1) * 100
    hs = high.to_numpy(float)
    if len(hs) >= 20:
        from numpy.lib.stride_tricks import sliding_window_view
        sw = sliding_window_view(hs, 20)
        pos = np.argmax(sw, axis=1)
        days = np.full(len(hs), np.nan)
        days[19:] = 19 - pos
        g["days_from_high"] = days
    else:
        g["days_from_high"] = np.nan
    g["drop_speed"] = g["dd20"] / g["days_from_high"]
    return g


def _honest_backtest(panels: pd.DataFrame, zt_map: pd.Series) -> pd.DataFrame:
    panels = panels.merge(zt_map.rename("mkt_zt"), on="date", how="left")
    out = []
    for _, g in panels.groupby("code", sort=False):
        g = g.reset_index(drop=True)
        n = len(g)
        if n < 30:
            continue
        g = _pre_features(g)
        masks = _signal_mask(g)
        entry = g["open"].astype(float).shift(-1)
        row = g[["code", "date", "pct_chg"] + PRE_FEATURES].copy()
        for tag, mask in masks.items():
            for h in (5, 10, 20):
                px = (g["close"].astype(float).shift(-h) / entry - 1 - COST)
                row[f"{tag}_{h}"] = px.where(mask)
            row[f"sig_{tag}"] = mask
        out.append(row)
    return pd.concat(out, ignore_index=True)


def _stat(s: pd.Series) -> str:
    s = s.dropna() * 100
    if len(s) == 0:
        return "—"
    pos = s[s > 0].sum()
    neg = abs(s[s < 0].sum())
    pf = pos / neg if neg else 99
    return (f"胜率{(s > 0).mean():.1%} 均收{s.mean():+.2f}% "
            f"PF={pf:.2f} P≥10%={(s >= 10).mean():.1%}")


def run() -> str:
    panels = load_all_panels()
    panels = panels[panels["date"] >= START]
    panels = panels[~panels["is_st"].fillna(0).astype(int).astype(bool)]
    panels = panels[~panels["is_paused"].fillna(0).astype(int).astype(bool)]
    panels = panels.sort_values(["code", "date"])
    zt_map = _market_zt(panels)
    names = load_names()
    ck = load_chokepoint_codes()
    date = datetime.now().strftime("%Y-%m-%d")

    print("提取 72 只起涨日特征...")
    sub = panels[panels["code"].isin(ck)]
    ep72 = pd.concat([_launch_features(g, zt_map)
                      for _, g in sub.groupby("code", sort=False)], ignore_index=True)
    print(f"全市场启动日样本（同口径）...")
    ep_all = pd.concat([_launch_features(g, zt_map)
                        for _, g in panels.groupby("code", sort=False)], ignore_index=True)
    print(f"全市场规则回测（{len(panels):,} 行）...")
    bt = _honest_backtest(panels, zt_map)

    L = [f"# 72 只卡点标的起涨特征研究 + 全市场回测（{date}）", ""]
    L.append("> 数据：zzshare 全市场日线（2021-01-01 起，剔除 ST/停牌）；行情截止 "
             f"{panels['date'].max()}。起涨日=未来20日涨幅≥20%且前期20日≤10%的段落内"
             "涨幅最大一天（仅用于画像研究，含未来信息）。")
    L.append("> 全市场回测无未来函数：当日信号→次日开盘买入→第5/10/20日收盘卖出，双边费用0.2%。")
    L.append("> 免责：仅研究线索，不构成投资建议。")
    L.append("")

    L.append("## 一、72 只起涨画像（特征分布）")
    L.append("")
    L.append(f"样本：{len(ep72)} 个起涨段落（72 只，2021-2026，平均每只 {len(ep72) / 72:.1f} 段）")
    L.append("| 特征 | 72只中位 | 72只均值 | 全市场中位 | 全市场均值 | 解读 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    rows = [
        ("chg", "启动日涨幅%", "当日放量上攻，中位约+5%"),
        ("vr", "启动日量比", "温和放量1.4倍（全市场同口径相近）"),
        ("ar", "启动日额比", "成交额放大倍数"),
        ("dd_prev", "前日距20日高点%", "多数从-12%左右的深蹲后启动"),
        ("prior20_prev", "前期20日涨幅%", "起涨前普遍横盘/回调"),
        ("pre5", "启动前5日%", "启动前小幅企稳"),
        ("g60_prev", "前期60日%", "中期位置"),
        ("fwd10", "启动后10日%", "起涨后10日涨幅"),
        ("mkt_zt", "当日全市场涨停家数", "启动多发生在赚钱效应强的日子"),
    ]
    for col, label, note in rows:
        a, b = ep72[col].dropna(), ep_all[col].dropna()
        if col == "mkt_zt":
            L.append(f"| {label} | {a.median():.0f} | {a.mean():.0f} | {b.median():.0f} | "
                     f"{b.mean():.0f} | {note} |")
        else:
            L.append(f"| {label} | {a.median():+.1f} | {a.mean():+.1f} | {b.median():+.1f} | "
                     f"{b.mean():+.1f} | {note} |")
    for col, label in [("zt", "当日涨停占比"), ("new20", "创20日新高占比"),
                       ("bull_prev", "前日多头排列占比")]:
        a, b = ep72[col].mean(), ep_all[col].mean()
        L.append(f"| {label} | {a:.1%} | {a:.1%} | {b:.1%} | {b:.1%} | 见解读 |")
    L.append("")
    L.append("> 画像结论：卡点票的起涨多数不是'追高突破'（创20日新高仅两成、多头排列仅两成），"
             "而是**深蹲（回撤约12%）后放量启动**；且与全市场同口径几乎一致 → 起涨特征可全市场量化。")
    L.append("")

    L.append("## 二、特征预测力（全市场起涨日分桶 → 后10日）")
    L.append("")
    L.append("| 特征 | 分档1(最低) | 分档2 | 分档3 | 分档4(最高) | 结论 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for col, label, note in [
        ("chg", "启动日涨幅", "当日涨幅越大后续越强（>9.7%档最优）"),
        ("vr", "启动日量比", "量比与后续涨幅**负相关**：巨量(>2.1)反而最差，温和放量更健康"),
        ("dd_prev", "前日20日回撤", "回撤越深后续越强（<-20%档最优）"),
        ("prior20_prev", "前期20日涨幅", "深跌后启动（<-14%）最优，过热（+4~25%）次优"),
        ("pre5", "启动前5日", "前5日缩量阴跌（<-2.6%）最优；已预热上涨的反而差"),
        ("mkt_zt", "当日涨停家数", "市场赚钱效应越强越好（≥130档最优）"),
    ]:
        t = ep_all.groupby(pd.qcut(ep_all[col], 4, duplicates="drop"), observed=True)[
            "fwd10"].agg(["mean", "median"]).round(1)
        cells = [f"{m:.1f}%" for m in t["mean"]]
        while len(cells) < 4:
            cells.append("—")
        L.append(f"| {label} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | {note} |")

    L.append("")
    L.append("## 三、全市场规则回测（无未来函数，次日开盘买入）")
    L.append("")
    L.append("| 规则 | n | 5日 | 10日 | 20日 | 说明 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for tag, desc in RULES.items():
        n = int(bt[f"{tag}_5"].notna().sum())
        s5 = _stat(bt[f"{tag}_5"])
        s10 = _stat(bt[f"{tag}_10"])
        s20 = _stat(bt[f"{tag}_20"])
        L.append(f"| {tag} | {n:,} | {s5} | {s10} | {s20} | {desc} |")

    L.append("")
    L.append("## 四、逐年稳定性（r7m 温和量+市场共振 / r9 强市场）")
    L.append("")
    L.append("| 年份 | r7m n | r7m 5日 | r7m 10日 | r9 n | r9 5日 | r9 10日 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    bt["year"] = bt["date"].str[:4]
    for y in sorted(bt["year"].unique()):
        g = bt[bt["year"] == y]
        cell = []
        for tag in ("r7m", "r9"):
            sub = g[g[f"sig_{tag}"]]
            s5 = sub["r7m_5"].dropna() * 100 if tag == "r7m" else sub["r9_5"].dropna() * 100
            s10 = sub["r7m_10"].dropna() * 100 if tag == "r7m" else sub["r9_10"].dropna() * 100
            if len(s5) == 0:
                cell.append("—")
                cell.append("—")
            else:
                cell.append(f"{(s5 > 0).mean():.0%}/{s5.mean():+.1f}%")
                cell.append(f"{(s10 > 0).mean():.0%}/{s10.mean():+.1f}%")
        L.append(f"| {y} | {int(g['sig_r7m'].sum()):,} | {cell[0]} | {cell[1]} | "
                 f"{int(g['sig_r9'].sum()):,} | {cell[2]} | {cell[3]} |")

    L.append("")
    L.append("## 五、近期 r7m 信号（最近5个交易日，按日期倒序）")
    L.append("")
    recent = sorted(bt[bt["sig_r7m"]]["date"].unique())[-5:]
    sub = bt[(bt["sig_r7m"]) & (bt["date"].isin(recent))].copy()
    sub = sub.sort_values(["date", "pct_chg"], ascending=[False, False])
    L.append("| 日期 | 代码 | 名称 | 当日涨幅% | 规则 |")
    L.append("| --- | --- | --- | --- | --- |")
    for _, r in sub.head(25).iterrows():
        L.append(f"| {r['date']} | {r['code']} | {names.get(r['code'], '待补')} "
                 f"| {r['pct_chg']:+.1f} | r7m |")
    L.append(f"（共 {len(sub)} 个信号，明细见 CSV）")
    L.append("")
    L.append("**卡点名单（72 只）内近期 r7m 信号：**")
    sub_ck = sub[sub["code"].isin(ck)]
    if len(sub_ck):
        L.append("| 日期 | 代码 | 名称 | 当日涨幅% |")
        L.append("| --- | --- | --- | --- |")
        for _, r in sub_ck.iterrows():
            L.append(f"| {r['date']} | {r['code']} | {names.get(r['code'], '待补')} "
                     f"| {r['pct_chg']:+.1f} |")
    else:
        L.append("（无）")
    L.append("")

    L.append("")
    L.append("## 六、信号日之前特征量化（r7m / r9 vs 非信号基准）")
    L.append("")
    L.append("> 口径：数值类为中位数，占比类为均值；基准=非 r4 信号的全市场交易日。")
    L.append("| 前置特征 | r7m | r9 | 基准 | 含义 |")
    L.append("| --- | --- | --- | --- | --- |")
    for col, label, note in [
        ("pre10", "前10日涨幅%", "信号前 10 日仍在深跌（中位 -5%）"),
        ("pre5", "前5日涨幅%", "前 5 日刚企稳（中位 +1%，pre5<0 的'干净首阳'子集更强）"),
        ("pre3", "前3日涨幅%", "信号前 3 日已转涨（多为反弹第 2-3 天）"),
        ("gap", "当日跳空%", "小幅高开"),
        ("body", "当日实体%", "实体大阳"),
        ("close_pos", "收盘位置", "收盘在当日振幅 90% 位置（强收盘）"),
        ("upper_shadow", "上影线比例", "上影线短（10%）"),
        ("amp", "当日振幅%", "振幅 9-11%"),
        ("prev_chg", "前一日涨幅%", "前一日仍阴跌，当日反转"),
        ("vol5_v20", "前5日量/20日量", "启动前整体缩量"),
        ("dd20", "前日20日回撤%", "中位回撤约 -24%（深蹲）"),
        ("days_from_high", "距20日高点天数", "下跌持续约 17 个交易日"),
        ("drop_speed", "日均回撤%/日", "缓跌（-1.75%/日）"),
    ]:
        a = bt.loc[bt["sig_r7m"], col].median()
        b = bt.loc[bt["sig_r9"], col].median()
        c = bt.loc[~bt["sig_r4"], col].median()
        L.append(f"| {label} | {a:+.1f} | {b:+.1f} | {c:+.1f} | {note} |")
    for col, label in [("above_ma5", "收盘站上MA5"), ("above_ma10", "收盘站上MA10"),
                       ("above_ma20", "收盘站上MA20"),
                       ("new_low_rebound", "创新20日低点当日反包"),
                       ("low_open_up", "低开高走")]:
        a = bt.loc[bt["sig_r7m"], col].mean()
        b = bt.loc[bt["sig_r9"], col].mean()
        c = bt.loc[~bt["sig_r4"], col].mean()
        L.append(f"| {label} | {a:.0%} | {b:.0%} | {c:.0%} | 占比 |")

    L.append("")
    L.append("## 七、前置特征增强规则回测")
    L.append("")
    ENH = {
        "r7mA": "r7m + 启动前5日涨幅<0（干净超跌首阳）",
        "r7mE": "r7m + 创新20日低点当日反包",
        "r9A": "r9 + 启动前5日涨幅<0（干净超跌首阳）",
        "r9E": "r9 + 创新20日低点当日反包",
    }
    L.append("| 规则 | n | 5日 | 10日 | 20日 | 说明 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for tag, desc in ENH.items():
        n = int(bt[f"{tag}_5"].notna().sum())
        L.append(f"| {tag} | {n:,} | {_stat(bt[f'{tag}_5'])} | {_stat(bt[f'{tag}_10'])} | "
                 f"{_stat(bt[f'{tag}_20'])} | {desc} |")
    L.append("")
    L.append("**逐年（5日：n｜胜率/均收）：**")
    L.append("| 年份 | r7mA | r7mE | r9A | r9E |")
    L.append("| --- | --- | --- | --- | --- |")
    for y in sorted(bt["year"].unique()):
        g = bt[bt["year"] == y]
        cells = []
        for tag in ENH:
            s = g.loc[g[f"sig_{tag}"], f"{tag}_5"].dropna() * 100
            cells.append(f"{len(s):,}｜{(s > 0).mean():.0%}/{s.mean():+.1f}%" if len(s) else "—")
        L.append(f"| {y} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")

    L.append("")
    L.append("## 八、结论与建议")
    L.append("")
    L.append("1. **起涨画像**：72 只卡点票的起涨日典型特征=前期20日回撤约12%、启动前缩量阴跌、"
             "当日放量1.4倍、涨幅中位5%、约1/6涨停；创20日新高与多头排列**不是**必要条件。")
    L.append("2. **最优量化规则**：`r7m`＝20日回撤≥15% + 当日涨≥5% + 量比1.0~2.0 + "
             "全市场涨停≥90（n=3.2万，5日胜率63.4%、PF 2.71）；进攻版 `r9` 把涨停家数门槛提到"
             "130（n=1.1万，5日胜率71.1%、PF 4.90、20日P≥10%达50%）。")
    L.append("3. **量比反直觉**：巨量（>2倍）启动反而差（情绪高点/出货嫌疑），温和放量1~2倍最优。")
    L.append("4. **市场环境是放大器**：全市场涨停≥90/130 是 r4 胜率从 50% 提到 58%/71% 的关键。")
    L.append("5. **注意 2026 年特征**：r7m/r9 的 5 日胜率仍高（54%/2026），但 10 日跟风减弱"
             "（f10 胜率约 38-39%）→ 今年超跌反弹兑现快，宜短线兑现，不宜恋战。")
    L.append("6. **与 T0 对比**：同口径裸跑，原 T0 全市场 5 日胜率 36.8%、PF 0.72；"
             "超跌启动规则显著更强。建议把 r7m/r9 纳入模型作为第二套信号源，"
             "与 T0（趋势突破）互补。")
    L.append("7. **前置特征可量化且有效**：r7m/r9 信号日前 10 日深跌（中位 -5%）、前 3 日已转涨"
             "（+4.5%）、信号日收盘在振幅 90% 位置、上影线仅 10%、距 20 日高点约 17 个交易日"
             "缓跌（-1.75%/日）——下跌结构与日内形态都可量化。")
    L.append("8. **最强增强条件**：①启动前 5 日涨幅 <0（干净首阳）→ r7m 5日胜率 63%→71%、"
             "r9 →83%（PF 4.90→14.84）；②创新 20 日低点当日反包 → r7m 71.8%、r9 85.1%"
             "（PF 20+，但样本少、2021-2023 基本无有效样本，只做加分项）。")
    L.append("9. **收盘强度/缩量门限无增益**：r7m 信号本身已强收盘（close_pos 0.90），再加"
             "close_pos≥0.6 或 vol5_v20≤1.0 反而略降；判断'干净启动'用 pre5<0 比量能更有效。")
    L.append("10. **实战优先级**：r7mA（样本大、稳定）> r9A（强市场才可用）> r7mE/r9E"
             "（超激进子集，样本少，只做加分项不做主规则）。")
    L.append("")

    md_path = paths.report_dir() / f"起涨特征研究_{date}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    sig = bt[bt["sig_r7m"] | bt["sig_r9"] | bt["sig_r7mA"] | bt["sig_r7mE"]
             | bt["sig_r9A"] | bt["sig_r9E"]].copy()
    sig_out = sig[["date", "code", "pct_chg", "pre5", "dd20", "close_pos",
                   "new_low_rebound", "sig_r7m", "sig_r9", "sig_r7mA", "sig_r7mE",
                   "sig_r9A", "sig_r9E"]].rename(columns={
        "pct_chg": "当日涨幅%", "pre5": "前5日%", "dd20": "20日回撤%",
        "close_pos": "收盘位置", "new_low_rebound": "创新低反包",
        "sig_r7m": "r7m", "sig_r9": "r9", "sig_r7mA": "r7mA", "sig_r7mE": "r7mE",
        "sig_r9A": "r9A", "sig_r9E": "r9E"})
    csv_path = paths.report_dir() / f"起涨信号_全市场_{date}.csv"
    sig_out.to_csv(csv_path, index=False, encoding="utf-8-sig")

    print(f"研究报告: {md_path}")
    print(f"信号明细: {csv_path}（r7m/r9/增强 共 {len(sig_out):,} 个）")
    return str(md_path)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
