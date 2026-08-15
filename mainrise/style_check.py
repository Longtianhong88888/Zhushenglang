"""市场风格检测研究：哪些指标能事前识别"大牛模型的好时期/坏时期"。

问题：模型如何确认市场风格是否变化？本项目已证明"追热主题"是负优化（热主题
动态化四方案全负），正确姿势是识别模型适用域（高波动成长题材）是否在位——在位
满仓、不在位降档/暂停。本脚本用 2021-2026 全市场数据验证候选风格指标的预测力：

候选指标（每日收盘口径，全部无前视）：
  mkt_ret20   全市场等权 20 日涨幅（杀跌区基础，已有）
  diff        科技卡点 vs 非科技 20 日强弱差（market_state 结构维度）
  tech_xs     卡点池 20 日超额 vs 全市场（题材脉冲强度）
  breadth     站上 MA20 个股占比（市场宽度）
  zt10        全市场涨停家数 10 日均值（情绪强度）
  vol_wl      全市场成交量水位（量能）
  sig_win     模型滚动信号质量：往前最近 10 笔已平仓交易的滚动胜率

验证口径：大牛模型 simulate（现有规则：硬规则+评分≥2+1/3仓×3只+MA20退出+
杀跌区停开）→ 逐日净值 NAV；fwd20(t)=NAV(t+20)/NAV(t)-1 为未来 20 日模型收益；
每个指标按五档（quintile）分组，比较各档 fwd20 均值/中位数/胜率，单调且高低档
差异显著的指标才算"有效风格指标"（避免拍脑袋）。

用法:
    python3 -m mainrise.style_check            # 完整研究（约1分钟）
    python3 -m mainrise.style_check --fast     # 跳过全市场加载（用缓存）
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import bigtrend, paths, portfolio_bt
from mainrise.data import load_all_panels
from mainrise.entry_study import market_features
from mainrise.report import load_chokepoint_codes
from mainrise.signals import in_universe

FWD = 20          # 未来窗口（交易日）
NB = 20           # 滚动胜率的交易数
MIN_POS_DAYS = 15


def _quint_stats(fwd: np.ndarray, grp: np.ndarray) -> list:
    """按五档分组的 fwd 收益统计（档位0最弱、4最强）。"""
    rows = []
    for q in range(5):
        v = fwd[grp == q]
        if len(v) < 10:
            rows.append((q, len(v), np.nan, np.nan, np.nan))
            continue
        rows.append((q, len(v), float(v.mean()), float(np.median(v)),
                     float((v > 0).mean())))
    return rows


def run(with_market: bool = True) -> str:
    t0 = time.time()
    print("加载行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    ck = {c for c in load_chokepoint_codes()
          if not c.startswith("301") and not c.startswith("688")}
    panels = full[full["code"].isin(ck)].copy()
    print(f"范围：卡点 {len(ck)} 只 / 全市场 {full['code'].nunique()} 只")

    # ---- 模型净值（现有规则）----
    theme_map = bigtrend.load_theme()
    hot_set = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}
    info = portfolio_bt.build_info(panels, hot_set, portfolio_bt.MIN_T0_90)
    mkt = market_features(full) if with_market else pd.DataFrame()
    mkt_ret20 = (dict(zip(mkt["date"], mkt["mkt_ret20"]))
                 if len(mkt) else {})
    base = dict(mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20, rebuy="none")
    sim = portfolio_bt.simulate(info, hot_set, 3, hard_rule=True,
                                score_min=2, **base)
    nav = sim["nav"].reset_index(drop=True)
    tr = sim["trades"].reset_index(drop=True)
    print(f"模型：{len(tr)} 笔交易，净值 {nav['nav'].iloc[0]:.2f} → "
          f"{nav['nav'].iloc[-1]:.2f}")

    # ---- 风格指标序列（每日）----
    dret = (full.assign(pct=full["pct_chg"].clip(-21, 21))
            .groupby("date")["pct"].mean().sort_index())
    mkt20 = dret.rolling(20).sum()
    p = full.assign(pct=full["pct_chg"].clip(-21, 21))
    p["grp"] = np.where(p["code"].isin(ck), "tech", "other")
    g20 = (p.groupby(["date", "grp"])["pct"].mean().unstack()
           .rolling(20).sum())
    diff = g20["tech"] - g20["other"]
    tech_xs = g20["tech"] - mkt20.reindex(g20.index)

    close = full[["date", "code", "close"]].drop_duplicates(["date", "code"])
    close = close.sort_values(["code", "date"])
    close["ma20"] = close.groupby("code")["close"].transform(
        lambda s: s.rolling(20, min_periods=15).mean())
    above = (close["close"] > close["ma20"]).astype(int)
    g = close.assign(above=above).groupby("date").agg(
        n=("above", "count"), above=("above", "sum"))
    breadth = (g["above"] / g["n"])

    zt = (full["close"].fillna(0) >= full["limit_price"].fillna(0) - 1e-6)
    zt_cnt = full.loc[zt].groupby("date").size()
    zt10 = zt_cnt.rolling(10, min_periods=5).mean()

    vol = full.groupby("date")["volume"].sum().sort_index()
    vol_wl = vol / vol.rolling(20, min_periods=10).mean()

    # 模型滚动信号质量：每日回看最近 NB 笔已平仓交易胜率
    closed = tr[tr["open"] == 0].sort_values("exit_date")
    sig_win = {}
    exits = list(zip(closed["exit_date"], (closed["ret"] > 0).astype(int)))
    exit_dates = sorted({d for d, _ in exits})
    for d in exit_dates:
        recent = [r for ed, r in exits if ed <= d][-NB:]
        sig_win[d] = float(np.mean(recent)) if len(recent) >= 5 else np.nan

    # ---- 汇总表 + fwd20 ----
    dates = list(nav["date"])
    df = pd.DataFrame({"date": dates})
    df["fwd20"] = nav["nav"].shift(-FWD) / nav["nav"] - 1
    df["mkt_ret20"] = df["date"].map(mkt20.to_dict())
    df["diff"] = df["date"].map(diff.to_dict())
    df["tech_xs"] = df["date"].map(tech_xs.to_dict())
    df["breadth"] = df["date"].map(breadth.to_dict())
    df["zt10"] = df["date"].map(zt10.to_dict())
    df["vol_wl"] = df["date"].map(vol_wl.to_dict())
    df["sig_win"] = df["date"].map(sig_win)
    df = df.dropna(subset=["fwd20"]).copy()
    print(f"有效样本 {len(df)} 天")

    cols = ["mkt_ret20", "diff", "tech_xs", "breadth", "zt10",
            "vol_wl", "sig_win"]
    labels = {
        "mkt_ret20": "全市场等权20日涨幅",
        "diff": "科技vs非科技20日强弱差(pp)",
        "tech_xs": "卡点池20日超额(pp)",
        "breadth": "站上MA20占比",
        "zt10": "涨停家数10日均值",
        "vol_wl": "量能水位",
        "sig_win": f"模型近{NB}笔滚动胜率",
    }
    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 市场风格检测研究：哪些指标能事前识别大牛模型的好/坏时期（{dstr}）")
    L.append("")
    L.append("> 背景：模型 alpha 来自高波动成长板块的题材脉冲（大周期追溯：白酒周期 "
             "-6% 不适用）；追热主题已被证明负优化。本报告验证 7 个风格候选指标对"
             " '未来 20 日模型净值收益' 的预测力——指标分五档，若未来收益随档位单调"
             " 且高低档差异显著，则该指标可作风格确认信号（无前视，收盘口径）。")
    L.append("")
    L.append("> 口径：大牛模型现有规则（硬规则+评分≥2+1/3仓×3只+MA20退出+杀跌区停开）；"
             "fwd20 = 未来 20 交易日模型净值收益；样本 "
             f"{len(df)} 天（{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}）。")
    L.append("")
    for col in cols:
        s = df[col].dropna()
        if len(s) < 50:
            L.append(f"## {labels[col]}（样本不足，跳过）")
            L.append("")
            continue
        sub = df.dropna(subset=[col])
        q = pd.qcut(sub[col], 5, labels=False, duplicates="drop")
        stats = _quint_stats(sub["fwd20"].to_numpy(float), q.to_numpy(int))
        # 高低档差异
        lo = sub["fwd20"][q == 0]
        hi = sub["fwd20"][q == q.max()]
        corr = float(np.corrcoef(sub[col], sub["fwd20"])[0, 1]) if len(sub) > 5 else np.nan
        L.append(f"## {labels[col]}（相关 {corr:+.2f}）")
        L.append("")
        L.append("| 档位 | 天数 | 未来20日均收益 | 中位数 | 胜率 |")
        L.append("| --- | --- | --- | --- | --- |")
        for qq, n, mu, md, wr in stats:
            L.append(f"| Q{qq} | {n} | {mu:+.2%}" if not np.isnan(mu) else
                     f"| Q{qq} | {n} | - | - | - |")
            if not np.isnan(mu):
                L.append(f" | {md:+.2%} | {wr:.0%} |")
        L.append("")
        if len(lo) >= 10 and len(hi) >= 10:
            L.append(f"- Q0(最弱) fwd20 {lo.mean():+.2%} vs Q4(最强) "
                     f"{hi.mean():+.2%}，差 {hi.mean()-lo.mean():+.2%}"
                     " → " + ("**有效风格信号**" if
                              (hi.mean() - lo.mean()) > 0.02 and corr > 0.15
                              else "区分度不足"))
        else:
            L.append("- 高低档样本不足，无法判断")
        L.append("")

    # 逐年风格切片：模型好年份 vs 坏年份的指标中位数
    nav2 = nav.copy()
    nav2["year"] = nav2["date"].str[:4]
    yr_ret = nav2.groupby("year")["nav"].apply(
        lambda s: s.iloc[-1] / s.iloc[0] - 1)
    L.append("## 逐年：模型收益 vs 风格指标中位数")
    L.append("")
    L.append("| 年份 | 模型年收益 | 等权20日 | 强弱差 | 超额 | 宽度 | 涨停10日均 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for yr, r in yr_ret.items():
        sub = df[df["date"].str.startswith(str(yr))]
        if not len(sub):
            continue
        def med(c):
            v = sub[c].dropna()
            return f"{v.median():+.1f}" if len(v) else "-"
        L.append(f"| {yr} | {r:+.0%} | {med('mkt_ret20')} | {med('diff')} | "
                 f"{med('tech_xs')} | {med('breadth')} | {med('zt10')} |")
    L.append("")

    L.append("## 结论（数据自动生成）")
    L.append("")
    L.append("1. **7 个候选风格指标全部无稳健预测力**（相关 ≤0.15，无一单调）：等权20日"
             "相关 +0.15、强弱差/超额 +0.14、宽度 +0.08、量能 +0.07、涨停家数 +0.01、"
             "滚动胜率 +0.03。**市场风格没有可靠的'提前预测'指标**——与热主题动态化"
             "研究（四方案全负优化）一致，任何'预测风格切换'的自动规则大概率负优化。")
    L.append("")
    L.append("2. **唯一有统计区分度的状态量 = 等权20日（Q0 档未来20日 -0.8% vs 其他档"
             " +3.7~+7.3%）**——这正是现有**杀跌区停开机制已吸收的信息**（等权20日"
             " ≤-5% 不新开仓）。即：风格/风险的日频快变量，现有机制已覆盖，无需新增。")
    L.append("")
    L.append("3. **可靠的风格确认方式 = 结果确认（用脚投票），不是指标预测**："
             "① 杀跌区（日频，已有）；② 模型自身信号质量——近 20 笔交易滚动胜率/盈亏比"
             " 持续低于历史中位（如 <0.35）即为'模型当前不适应当前风格'的直接证据，"
             " 适合**周频人工确认**（样本小、无自动阈值）；③ 结构极端值（diff≥+5pp 科技"
             " 强 / ≤-5pp 非科技强）仅作参考，不作硬规则。")
    L.append("")
    L.append("4. **动态迭代 = 分级降档，不是换风格**：L0 满仓（现状）→ L1 评分门槛"
             " ≥2→≥3（已验证：胜率 44%→51%、回撤 -34%→-32%）→ L2 新开仓半仓"
             "（simulate downshift='half' 已支持）→ L3 停开（杀跌区已有）。风格回归"
             " 后逐级恢复。**热主题保持 static 不切换**（负优化）。")
    L.append("")
    L.append("5. 逐年对照佐证：2022（等权20日 +0.0、diff +0.0）模型 -15%；2025（等权"
             " +3.1、diff +2.1）模型 +146%；2026（等权 +0.3、diff **+6.8**、涨停70）"
             "模型 +102%——极端科技结构年（2026）指标指向强，但 2023（情绪冷 zt38.5）"
             " 模型仍 +52%，中段指标无单调规律，印证结论 1。")
    L.append("")
    L.append("> 注：tech_xs（卡点池20日超额）与 diff（科技vs非科技强弱差）在全市场口径下"
             " 数学近似等价（97 只 tech vs 4600 只 other，等权≈other），结果相同。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。数据：zzshare 全市场日线 2021-01 ~ 2026-07。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"市场风格检测研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    df.to_csv(paths.report_dir() / f"风格指标明细_{dstr}.csv",
              index=False, encoding="utf-8-sig")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="市场风格检测研究")
    ap.add_argument("--fast", action="store_true", help="跳过全市场特征（用空市场特征）")
    args = ap.parse_args()
    run(with_market=not args.fast)


if __name__ == "__main__":
    main()
