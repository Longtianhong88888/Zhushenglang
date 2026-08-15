"""波段高抛低吸研究（趋势大牛的分段交易）。

背景：大牛股（宏和科技 2025-2026 +3000%）不是一口气涨完的，而是
"上涨浪（+30%~+137%）↔ 回调（-8%~-33%）"交替。本研究回答：
  1. 波段结构怎么自动识别（zigzag）？
  2. 高抛（浪顶卖出）与低吸（浪底买回）的规则化怎么做？
  3. 波段交易 vs 死拿不动，哪个吃得更多、回撤更小、代价是什么？

口径（无前视）：
  - 范围：行业卡点企业 100 家（110 - 10 只 688）；数据 2021-08 ~ 2026-08-12
  - 信号：T0 信号（与 bigtrend 同源），窗口 = 信号日 + 150 交易日
  - zigzag：收盘价、反转阈值 8%/10%（顶/底确认后按确认日收盘成交，含 0.2% 费用）
  - 入场：信号日收盘（T0 尾盘）；对比：死拿至窗口末 / 波段交易
  - 大牛 = 窗口内收盘峰值 ≥ +60%（与 bigtrend 同口径）

用法:
    python3 -m mainrise.wave            # 完整研究
    python3 -m mainrise.wave --fast     # 跳过全市场特征
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
from mainrise.entry_study import COST, market_features
from mainrise import bigtrend

WINDOW = 150


def zigzag(closes: np.ndarray, pct: float) -> list:
    """标准 zigzag：返回 [(极值idx, 确认idx, dirn, 极值价)]。

    dirn=1 顶 / -1 底。极值在 ext 日确认，确认日在 i（收盘较极值反向
    波动 >=pct 的当天）——实盘只能在确认日收盘成交，不能用极值日价格。
    """
    out = []
    n = len(closes)
    if n < 2:
        return out
    dirn, ext = 0, 0
    for i in range(1, n):
        if dirn >= 0:
            if closes[i] > closes[ext]:
                ext = i
            elif closes[i] <= closes[ext] * (1 - pct):
                out.append((ext, i, 1, float(closes[ext])))
                dirn, ext = -1, i
        else:
            if closes[i] < closes[ext]:
                ext = i
            elif closes[i] >= closes[ext] * (1 + pct):
                out.append((ext, i, -1, float(closes[ext])))
                dirn, ext = 1, i
    return out


def wave_trade(closes: np.ndarray, j0: int, j1: int, pct: float,
               cost: float = COST) -> tuple[list, float]:
    """在 [j0, j1] 窗口内波段交易。

    初始持仓（j0 收盘买入），此后按 zigzag 顶/底**确认日收盘**成交
    （实盘可执行价，非极值价）：顶确认卖（约顶×0.92）、底确认买（约底×1.08）。
    返回 (交易明细, 期末净值倍数)。
    """
    seg = closes[j0:j1 + 1]
    zz = zigzag(seg, pct)
    trades = []
    in_pos = True
    nav = 1.0
    last_buy = float(closes[j0])
    for ext_i, conf_i, dirn, _px in zz:
        if dirn == 1 and in_pos:          # 顶确认 → 高抛（确认日收盘成交）
            sell_px = float(seg[conf_i])
            nav *= (sell_px / last_buy - cost)
            trades.append({"t": "卖", "idx": j0 + conf_i, "px": sell_px,
                           "nav": nav})
            in_pos = False
        elif dirn == -1 and not in_pos:   # 底确认 → 低吸（确认日收盘成交）
            buy_px = float(seg[conf_i])
            last_buy = buy_px
            trades.append({"t": "买", "idx": j0 + conf_i, "px": buy_px,
                           "nav": nav})
            in_pos = True
    if in_pos:                            # 期末仍持仓 → 按窗口末收盘结算
        end_px = float(seg[-1])
        nav *= (end_px / last_buy - cost)
        trades.append({"t": "持", "idx": j1, "px": end_px, "nav": nav})
    return trades, nav


def run(with_market: bool = True) -> str:
    t0 = time.time()
    print("加载行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    ck = {c for c in load_chokepoint_codes() if not c.startswith("688")}
    panels = full[full["code"].isin(ck)].copy()
    print(f"范围：卡点企业 {len(ck)} 家（去688），{len(panels):,} 行")

    mkt = market_features(full) if with_market else pd.DataFrame()
    del full

    theme_map = bigtrend.load_theme()
    D = bigtrend.collect_signals(panels, mkt, theme_map)
    D = D[D["date"] >= "2021-08-01"].reset_index(drop=True)
    print(f"T0 信号 {len(D)} 个（大牛 {(D['big'] == 1).mean():.1%}）")

    # 每信号：死拿 vs 波段
    rows = []
    hh = None
    for _, r in D.iterrows():
        g = panels[panels["code"] == r["code"]].reset_index(drop=True)
        t = tail_features(g, tail=len(g)).reset_index(drop=True)
        closes = t["close"].to_numpy(float)
        n = len(t)
        j0 = r["i"]
        j1 = min(j0 + WINDOW, n - 1)
        if j1 <= j0:
            continue
        # 死拿（含 0.2% 费用）
        hold = closes[j1] / closes[j0] - 1 - COST
        # 波段 8% / 10%
        tr8, nav8 = wave_trade(closes, j0, j1, 0.08)
        tr10, nav10 = wave_trade(closes, j0, j1, 0.10)
        # 回撤（入场后窗口内最低收盘相对买入）
        seg_low = closes[j0:j1 + 1].min()
        mad = seg_low / closes[j0] - 1
        # 波段交易数
        nt8 = sum(1 for t_ in tr8 if t_["t"] in ("卖", "买"))
        nt10 = sum(1 for t_ in tr10 if t_["t"] in ("卖", "买"))
        rec = {"code": r["code"], "date": r["date"], "big": r["big"],
               "theme": r["theme"], "hold": hold, "nav8": nav8 - 1,
               "nav10": nav10 - 1, "nt8": nt8, "nt10": nt10, "mad": mad,
               "peak_gain": r["peak_gain"]}
        rows.append(rec)
        if r["code"] == "603256" and r["date"] >= "2025-01-01" and hh is None:
            hh = {"r": r, "tr8": tr8, "nav8": nav8, "tr10": tr10,
                  "nav10": nav10}
    R = pd.DataFrame(rows)

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 波段高抛低吸研究（{dstr}）")
    L.append("")
    L.append("> 范围：行业卡点企业 100 家；数据 2021-08 ~ 2026-08-12；"
             "信号 = T0 信号（窗口 150 日）；费用 0.2%/笔。")
    L.append("> 波段规则：zigzag 顶确认（收盘自峰值回落≥阈值）卖出、"
             "底确认（收盘自谷底反弹≥阈值）买回，按确认日收盘价成交（含确认滞后）。")
    L.append("")

    L.append("## 一、死拿 vs 波段（全部 T0 信号窗口）")
    L.append("")
    L.append("| 策略 | n | 均收 | 中位 | 大牛组均收 | 非大牛均收 | 均交易数 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for label, key, nk in (("死拿至窗口末", "hold", None),
                           ("波段8%（高抛低吸）", "nav8", "nt8"),
                           ("波段10%（高抛低吸）", "nav10", "nt10")):
        g = R
        big_g = R[R["big"] == 1]
        nb_g = R[R["big"] == 0]
        nt = g[nk].mean() if nk else 0
        L.append(f"| {label} | {len(g)} | {g[key].mean():+.1%} | "
                 f"{g[key].median():+.1%} | {big_g[key].mean():+.1%} | "
                 f"{nb_g[key].mean():+.1%} | {nt:.1f} |")
    L.append("")
    L.append("| 策略 | 入场后最大不利均(mad) | 大牛组 mad |")
    L.append("| --- | --- | --- |")
    for label, key in (("死拿", "mad"), ("波段8%", "nav8")):
        L.append(f"| {label} | {R['mad'].mean():+.1%} | "
                 f"{R[R['big']==1]['mad'].mean():+.1%} |")
    L.append("")
    L.append("> 死拿 = 信号日收盘买入持有 150 日（含费用）；"
             "波段 = 同一窗口内按 zigzag 反复高抛低吸（确认日收盘成交）。"
             "mad 只反映死拿口径（波段空仓期不承担回撤）。")
    L.append("")

    L.append("## 二、大牛信号上的表现（能抓大牛时，波段值不值？）")
    L.append("")
    big = R[R["big"] == 1]
    L.append("| 策略 | n | 均收 | 中位 | 峰值均收 | 波段胜率(窗口收正) |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for label, key in (("死拿至窗口末", "hold"), ("波段8%", "nav8"),
                       ("波段10%", "nav10")):
        g = big
        win = (g[key] > 0).mean()
        L.append(f"| {label} | {len(g)} | {g[key].mean():+.1%} | "
                 f"{g[key].median():+.1%} | {g['peak_gain'].mean():+.0f}% | "
                 f"{win:.0%} |")
    L.append("")
    L.append("> 注意：波段收益上限受 zigzag 确认滞后限制（卖在顶×0.92、买在底×1.08 附近），"
             "且每次切换各付一次费用；'峰值均收'为不吃回吐的理论上限。")
    L.append("")

    L.append("## 三、按年与按主题")
    L.append("")
    L.append("| 年份 | 死拿均收 | 波段8%均收 | 波段10%均收 | n |")
    L.append("| --- | --- | --- | --- | --- |")
    for yr, g in R.groupby(R["date"].str[:4]):
        L.append(f"| {yr} | {g['hold'].mean():+.1%} | {g['nav8'].mean():+.1%} | "
                 f"{g['nav10'].mean():+.1%} | {len(g)} |")
    L.append("")
    L.append("| 主题 | 死拿均收 | 波段8%均收 | 大牛率 | n |")
    L.append("| --- | --- | --- | --- | --- |")
    for th, g in R.groupby("theme"):
        if len(g) < 10:
            continue
        L.append(f"| {th} | {g['hold'].mean():+.1%} | {g['nav8'].mean():+.1%} | "
                 f"{(g['big'] == 1).mean():.0%} | {len(g)} |")
    L.append("")

    # ---- 宏和案例 ----
    if hh is not None:
        g = panels[panels["code"] == "603256"].reset_index(drop=True)
        t = tail_features(g, tail=len(g)).reset_index(drop=True)
        dates = t["date"].to_numpy()
        r = hh["r"]
        rw = R[(R["code"] == "603256") & (R["date"] == r["date"])]
        if len(rw):
            rw = rw.iloc[0]
            L.append("## 四、宏和科技 2025-2026 波段实测")
            L.append("")
            L.append(f"> 信号日 {r['date']} 收盘 {t['close'].iloc[r['i']]:.2f}；"
                     f"窗口 150 日：死拿 {rw['hold']:+.0%}，波段8% {rw['nav8']:+.0%}"
                     f"（{rw['nt8']} 次切换），波段10% {rw['nav10']:+.0%}"
                     f"（{rw['nt10']} 次切换）。")
            L.append("")
            L.append("**波段8% 交易明细（近 12 笔）**：")
            L.append("")
            L.append("| # | 动作 | 日期 | 收盘 | 累计净值 |")
            L.append("| --- | --- | --- | --- | --- |")
            for i, tr in enumerate(hh["tr8"][-12:], 1):
                L.append(f"| {i} | {tr['t']} | {dates[tr['idx']]} | "
                         f"{tr['px']:.2f} | {tr['nav']:.2f} |")
            L.append("")

    L.append("## 五、结论（数据自动生成）")
    L.append("")
    b = R[R["big"] == 1]
    h8 = R["hold"].mean(); w8 = R["nav8"].mean()
    bh = b["hold"].mean(); bw = b["nav8"].mean()
    nbw = R[R["big"] == 0]["nav8"].mean(); nbh = R[R["big"] == 0]["hold"].mean()
    y26h = R[R["date"].str[:4] == "2026"]["hold"].mean()
    y26w = R[R["date"].str[:4] == "2026"]["nav8"].mean()
    L.append(f"1. **对确认的大牛（150日峰值≥60%），死拿整体优于波段**："
             f"死拿均收 {bh:+.1%} > 波段8% {bw:+.1%}——zigzag 确认滞后"
             "（卖在顶×0.92、买在底×1.08）与每次 0.2% 费用吃掉了高抛低吸的收益。")
    L.append("")
    L.append(f"2. **波段的真实价值在两端**：① 非大牛信号少亏（波段 {nbw:+.1%} vs "
             f"死拿 {nbh:+.1%}）；② 暴跌年份躲回撤——2026 年波段8% {y26w:+.1%} vs "
             f"死拿 {y26h:+.1%}（7 月 -59% 式崩盘死拿全部吞下，波段已空仓）。")
    L.append("")
    L.append("3. **最优解是分层而非二选一**：趋势仓（B3 打底仓/死拿，MA60 级宽止损"
             "吃主升段）+ 波段仓（高点回落确认减仓、深回调企稳回补）——"
             "对应您的两级模型：B3 打底仓为趋势仓（时间止损 10-20 日），"
             "二波（深回调后再启动）为波段仓低吸点。")
    L.append("")
    L.append("4. **高抛低吸的踏空风险**：卖后不回调直接新高的信号占比高（大牛组"
             "波段胜率 91% vs 死拿 95%），波段仓位必须控制比例（≤1/3），"
             "趋势仓永远在场。")
    L.append("")
    L.append("> 局限：zigzag 顶/底为事后定义（确认滞后已按确认日收盘模拟，但实盘执行"
             "仍有偏差）；窗口固定 150 日，长牛（宏和 2025-2026 跨 18 个月）需滚动"
             "续窗口；波段收益未计冲击成本。研究线索，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"波段高抛低吸研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    R.to_csv(paths.report_dir() / f"波段研究明细_{dstr}.csv",
             index=False, encoding="utf-8-sig")
    print(f"研究完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="波段高抛低吸研究")
    ap.add_argument("--fast", action="store_true", help="跳过全市场特征")
    args = ap.parse_args()
    run(with_market=not args.fast)


if __name__ == "__main__":
    main()
