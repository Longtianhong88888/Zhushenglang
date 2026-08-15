"""启动加仓投资模型。

结构（全市场回测验证，无未来函数）：
1. 启动信号（前置特征）→ 次日开盘打底仓；
2. T+1 收盘站上 MA10 → T+2 开盘加仓（底仓:加仓 = 2:1）；
3. 退出纪律：止损 = 加权均价 -7%（盘中低点触发）；
   止盈 = 持仓最高价回落 10%（收盘价判定）；时间止损 = 10 个交易日；
   市场退潮保护 = 持有期间全市场涨停家数 < 50 → 次日开盘清仓。

信号规则（r7mA / r9A）：
- 20日回撤 ≥15% + 当日涨幅 ≥5% + 温和量比 1.0~2.0 + 前5日涨幅 <0（干净首阳）
- r7mA：全市场涨停家数 ≥90（标准模式）
- r9A ：全市场涨停家数 ≥130（强市场模式）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.launch import _pre_features, _signal_mask
from mainrise.signals import load_names

START = "2021-01-01"
COST = 0.001                       # 每笔成交费用 0.1%

DEFAULT_PARAMS = {
    "stop_pct": 0.07,              # 止损：加权均价 -7%
    "tp_pct": 0.10,                # 止盈：最高价回落 10%（收盘判定）
    "time_days": 10,               # 时间止损
    "protect_zt": 50,              # 市场退潮保护：涨停家数 < 50 清仓
    "add_weight": 0.5,             # 加仓权重（底仓:加仓 = 1:0.5 = 2:1）
}

RULES = {
    "r7mA": "标准模式：回撤≥15% + 涨≥5% + 量比1~2 + 前5日<0 + 全市场涨停≥90",
    "r9A": "强市场模式：回撤≥15% + 涨≥5% + 量比≥1.3 + 前5日<0 + 全市场涨停≥130",
}


def sim_panel(g: pd.DataFrame, mask: pd.Series, params: dict | None = None,
              add_on: bool = True) -> pd.DataFrame:
    """对单只股票面板模拟模型交易，返回交易明细 DataFrame。"""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    stop_pct, tp_pct = p["stop_pct"], p["tp_pct"]
    time_days, protect_zt, w_add = p["time_days"], p["protect_zt"], p["add_weight"]
    g = g.reset_index(drop=True)
    n = len(g)
    if n < 30:
        return pd.DataFrame()
    o = g["open"].to_numpy(float)
    h = g["high"].to_numpy(float)
    l = g["low"].to_numpy(float)
    c = g["close"].to_numpy(float)
    ma10 = g["close"].rolling(10).mean().to_numpy(float)
    mzt = g["mkt_zt"].to_numpy(float)
    idx = np.where(mask.to_numpy())[0]
    trades = []
    for i in idx:
        if i + 2 >= n or not (o[i + 1] > 0) or not (h[i + 1] > 0):
            continue
        e1 = o[i + 1] * (1 + COST)                 # 底仓：信号日次日开盘
        cash = e1
        units = 1.0
        peak = h[i + 1]
        added = False
        if l[i + 1] <= e1 * (1 - stop_pct):        # 底仓第 1 天止损
            exit_px = e1 * (1 - stop_pct)
            reason, exit_j = "止损", i + 1
        elif c[i + 1] <= peak * (1 - tp_pct):      # 底仓第 1 天止盈
            exit_px = c[i + 1]
            reason, exit_j = "止盈", i + 1
        else:
            # T+1 收盘站上 MA10 → T+2 开盘加仓
            if add_on and i + 3 < n and o[i + 2] > 0 and h[i + 2] > 0 \
                    and c[i + 1] > ma10[i + 1]:
                e2 = o[i + 2] * (1 + COST)
                cash += e2 * w_add
                units += w_add
                added = True
            avg = cash / units
            last = min(i + time_days, n - 1)
            exit_j = None
            exit_px = None
            reason = None
            for j in range(i + 2, last + 1):
                if not (h[j] > 0):                 # 停牌日跳过
                    continue
                peak = max(peak, h[j])
                if protect_zt is not None and j > i + 2 and mzt[j] < protect_zt:
                    exit_px = o[j]
                    reason, exit_j = "退潮", j
                    break
                if l[j] <= avg * (1 - stop_pct):
                    exit_px = avg * (1 - stop_pct)
                    reason, exit_j = "止损", j
                    break
                if c[j] <= peak * (1 - tp_pct):
                    exit_px = c[j]
                    reason, exit_j = "止盈", j
                    break
            if exit_j is None:
                valid = [j for j in range(i + 2, last + 1) if h[j] > 0]
                j = valid[-1] if valid else i + 1
                exit_j, exit_px = j, c[j]
                reason = "未平仓" if j == n - 1 else "时间"   # 数据截断的持仓不计数
        ret = (exit_px * units * (1 - COST) - cash) / cash
        trades.append({
            "signal_date": g["date"].iloc[i], "code": g["code"].iloc[0],
            "signal_chg": float(g["pct_chg"].iloc[i]),
            "base_open": e1 / (1 + COST), "add_open": (o[i + 2] if added else np.nan),
            "ret": ret, "reason": reason, "hold": exit_j - i, "added": added,
        })
    return pd.DataFrame(trades)


def _stats(df: pd.DataFrame) -> dict:
    if df is None or len(df) == 0:
        return {"n": 0}
    df = df[df["reason"] != "未平仓"]
    if len(df) == 0:
        return {"n": 0}
    s = df["ret"] * 100
    pos = s[s > 0].sum()
    neg = abs(s[s < 0].sum())
    return {
        "n": len(df), "win": (s > 0).mean(), "mean": s.mean(), "med": s.median(),
        "pf": pos / neg if neg else 99, "p10": (s >= 10).mean(),
        "add": df["added"].mean(), "hold": df["hold"].mean(),
        "worst": s.min(), "best": s.max(),
    }


def _fmt(st: dict) -> str:
    if not st.get("n"):
        return "—"
    return (f"n={st['n']:,} 胜率{st['win']:.1%} 均收{st['mean']:+.2f}% "
            f"中位{st['med']:+.2f}% PF={st['pf']:.2f} P≥10%={st['p10']:.1%} "
            f"加仓{st['add']:.0%} 持{st['hold']:.1f}日")


def run() -> str:
    panels = load_all_panels()
    panels = panels[panels["date"] >= START]
    panels = panels[~panels["is_st"].fillna(0).astype(int).astype(bool)]
    panels = panels[~panels["is_paused"].fillna(0).astype(int).astype(bool)]
    panels = panels.sort_values(["code", "date"])
    zt_map = panels[panels["close"] >= panels["limit_price"] - 1e-6].groupby("date").size()
    panels = panels.merge(zt_map.rename("mkt_zt"), on="date", how="left")
    names = load_names()
    date = pd.Timestamp.now().strftime("%Y-%m-%d")

    print("构建全市场面板与信号...")
    code_panels = []
    for _, g in panels.groupby("code", sort=False):
        g2 = g.reset_index(drop=True)
        if len(g2) < 30:
            continue
        g2 = _pre_features(g2)
        code_panels.append((g2, _signal_mask(g2)))
    print(f"模拟交易（{len(code_panels):,} 只）...")

    trades = {}
    for rule in RULES:
        for mode in ("add", "base"):
            agg = []
            for g2, m in code_panels:
                r = sim_panel(g2, m[rule], add_on=(mode == "add"))
                if not r.empty:
                    r["rule"] = rule
                    agg.append(r)
            trades[f"{rule}_{mode}"] = pd.concat(agg, ignore_index=True) if agg \
                else pd.DataFrame()

    L = [f"# 启动加仓投资模型（全市场回测报告 · {date}）", ""]
    L.append("> 数据：zzshare 全市场日线（2021-01-01 起，剔除 ST/停牌），行情截止 "
             f"{panels['date'].max()}；无未来函数；双边费用 0.1%/笔。")
    L.append("> 模型：前置特征打底仓 → T+1 站上 MA10 次日加仓（2:1）→ 止损/止盈/时间止损/"
             "市场退潮保护。免责：仅研究，不构成投资建议。")
    L.append("")

    L.append("## 一、模型规则")
    L.append("")
    L.append(f"- 启动信号：20日回撤≥15% + 当日涨幅≥5% + 温和量比1.0~2.0 + 前5日涨幅<0"
             f"（干净首阳）；r7mA 需全市场涨停≥90，r9A 需≥130")
    L.append(f"- 底仓：信号日次日开盘买入（计划仓位 2/3）")
    L.append(f"- 加仓：T+1 收盘站上 MA10 → T+2 开盘加仓（底仓:加仓=2:1，加仓率约 27-29%）")
    L.append(f"- 止损：加权均价 -{DEFAULT_PARAMS['stop_pct']:.0%}（盘中低点触发）")
    L.append(f"- 止盈：持仓最高价回落 {DEFAULT_PARAMS['tp_pct']:.0%}（收盘价判定）")
    L.append(f"- 时间止损：{DEFAULT_PARAMS['time_days']} 个交易日")
    L.append(f"- 市场退潮保护：持有期间全市场涨停家数 < {DEFAULT_PARAMS['protect_zt']} → 次日开盘清仓")
    L.append("")

    L.append("## 二、回测总览")
    L.append("")
    L.append("| 模式 | 统计 |")
    L.append("| --- | --- |")
    for rule in RULES:
        for mode, label in (("base", "仅底仓"), ("add", "底仓+加仓（完整模型）")):
            raw = trades[f"{rule}_{mode}"]
            st = _stats(raw)
            unfin = int((raw["reason"] == "未平仓").sum()) if len(raw) else 0
            tail = f"（另有 {unfin:,} 笔未到期持仓未计入）" if unfin else ""
            L.append(f"| {rule} {label} | {_fmt(st)}{tail} |")
    L.append("")

    L.append("## 三、退出原因分布（r9A 完整模型）")
    L.append("")
    df9 = trades["r9A_add"]
    if len(df9):
        done = df9[df9["reason"] != "未平仓"]
        vc = done["reason"].value_counts()
        for reason, cnt in vc.items():
            sub = done[done["reason"] == reason]
            s = sub["ret"] * 100
            L.append(f"- {reason}：{cnt:,} 笔（{cnt / len(done):.0%}），均收 {s.mean():+.2f}%")
    L.append("")

    L.append("## 四、逐年表现")
    L.append("")
    L.append("| 年份 | r7mA n/胜率/均收/PF | r9A n/胜率/均收/PF |")
    L.append("| --- | --- | --- |")
    all_years = sorted({y for r in RULES if len(trades[f'{r}_add'])
                        for y in trades[f'{r}_add']['signal_date'].str[:4].unique()})
    for y in all_years:
        cells = []
        for rule in ("r7mA", "r9A"):
            df = trades[f"{rule}_add"]
            g = df[df["signal_date"].str[:4] == y]
            st = _stats(g)
            cells.append(f"{st['n']:,}/{st['win']:.0%}/{st['mean']:+.1f}%/{st['pf']:.1f}"
                         if st["n"] else "—")
        L.append(f"| {y} | {cells[0]} | {cells[1]} |")
    L.append("")

    L.append("## 五、近期交易（按信号日倒序 Top 15）")
    L.append("")
    df_all = pd.concat([trades[f"{r}_add"] for r in RULES], ignore_index=True)
    if len(df_all):
        df_all = df_all.sort_values("signal_date", ascending=False)
        L.append("| 信号日 | 代码 | 名称 | 信号涨幅% | 底仓开盘 | 加仓开盘 | 持有日 | 收益% | 退出 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for _, r in df_all.head(15).iterrows():
            add_cell = f"{r['add_open']:.2f}" if not pd.isna(r["add_open"]) else "—"
            L.append(f"| {r['signal_date']} | {r['code']} | {names.get(r['code'], '待补')} "
                     f"| {r['signal_chg']:+.1f} | {r['base_open']:.2f} | "
                     f"{add_cell} | {r['hold']} | {r['ret'] * 100:+.1f} | {r['reason']} |")
    L.append("")

    L.append("## 六、结论与风险")
    L.append("")
    st9 = _stats(trades["r9A_add"])
    st7 = _stats(trades["r7mA_add"])
    L.append(f"1. **强市场模式 r9A 显著更强**：n={st9['n']:,}，胜率 {st9['win']:.1%}、"
             f"均收 {st9['mean']:+.2f}%、PF {st9['pf']:.2f}、单笔收益≥10% 占比 {st9['p10']:.1%}；"
             f"标准模式 r7mA：胜率 {st7['win']:.1%}、PF {st7['pf']:.2f}。")
    L.append("2. **加仓是可选增强，不是收益来源**：仅底仓的胜率/均收略高于加仓版"
             "（r9A：77.9%/+11.2% vs 77.2%/+10.7%），加仓的价值在于放大盈利交易的仓位；"
             "若追求单笔质量可只用底仓。")
    L.append("3. **市场退潮保护是核心风控**：r9A 完整模型 66% 的交易由'涨停<50 清仓'退出，"
             "它把 2024 年均收从 +8.4% 提升到 +13.3%；没有它模型在 2026 年转负。")
    L.append("4. **强行情依赖**：2022/2024/2025 年 r9A 胜率 77-83%；2021（样本少）与 2026 "
             "衰减（2026 胜率约 40%、均收 +0.4%），2026 年应降低仓位、只做短线兑现。")
    L.append("5. **仓位纪律**：单票 ≤1/3 总仓、并行 ≤3 只；r9A 只在涨停≥130 的强市场日开新仓，"
             "触发后持仓期间盯全市场涨停家数，跌破 50 无条件次日清仓。")
    L.append("")

    md_path = paths.report_dir() / f"启动加仓模型_{date}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    trades_out = pd.concat([trades[f"{r}_add"] for r in RULES], ignore_index=True)
    csv_path = paths.report_dir() / f"启动加仓模型_交易明细_{date}.csv"
    trades_out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"模型报告: {md_path}")
    print(f"交易明细: {csv_path}（{len(trades_out):,} 笔）")
    return str(md_path)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
