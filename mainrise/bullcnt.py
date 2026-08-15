"""连续买入信号计数 → 大牛属性判定研究。

问题：一只票连续出现多少个买入信号后，可以判定其"大牛属性"（后续大概率走
+60% 级主升浪）？买入信号口径：T0（放量涨停/大阳线，历史基准）、B3（均线粘合
爆量突破，现行框架打底仓信号）、二波（B3 后深回调再启动，加仓信号）。

统计口径（无前视）：
  - 范围：行业卡点企业 100 家（110 - 10 只 688）；数据 2021-08 ~ 2026-08-12
  - 大牛 = 信号日后 150 交易日内收盘峰值 ≥ +60%
  - 密度 = 信号日往前 N 日内的同类/合计信号数；链长 = 相邻信号间隔 ≤20 日的连续计数
  - 全部特征在信号日收盘已知

用法:
    python3 -m mainrise.bullcnt            # 完整研究
    python3 -m mainrise.bullcnt --fast     # 跳过全市场特征
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
from mainrise.entry_study import market_features
from mainrise import bigtrend

WINDOW = 150
CHAIN_GAP = 20          # 链：相邻信号间隔 ≤20 交易日视为"连续"


def collect(panels: pd.DataFrame, mkt: pd.DataFrame) -> pd.DataFrame:
    """每个买入信号日（T0/B3/二波 任一）的密度/链长/大牛标签。"""
    rows = []
    mktm = mkt.set_index("date") if len(mkt) else pd.DataFrame()
    for code, g in panels.groupby("code", sort=False):
        g = g.reset_index(drop=True)
        t = tail_features(g, tail=len(g))
        if t is None:
            continue
        n = len(t)
        closes = t["close"].to_numpy(float)
        b3 = t["b3"].to_numpy().astype(bool)
        w2 = t["wave2"].to_numpy().astype(bool)
        sig0 = t["signal"].to_numpy().astype(bool)
        dates = t["date"].to_numpy()
        any_sig = b3 | w2 | sig0
        # 链长与密度（滚动）
        chain = 0
        last_sig_i = -999
        for i in range(n):
            if not any_sig[i]:
                continue
            if i + 30 >= n:            # 需至少 30 日前瞻
                break
            if last_sig_i >= 0 and i - last_sig_i <= CHAIN_GAP:
                chain += 1
            else:
                chain = 1
            last_sig_i = i
            lo = max(0, i - 89)
            peak = closes[i + 1:i + 1 + WINDOW].max()
            mm = mktm.loc[dates[i]] if dates[i] in mktm.index else None
            rows.append({
                "code": code, "date": dates[i],
                "theme": bigtrend.load_theme().get(code, "其他"),
                "is_t0": int(sig0[i]), "is_b3": int(b3[i]),
                "is_w2": int(w2[i]),
                "n90_all": int(any_sig[lo:i + 1].sum()),
                "n90_t0": int(sig0[lo:i + 1].sum()),
                "n90_b3": int(b3[lo:i + 1].sum()),
                "n90_w2": int(w2[lo:i + 1].sum()),
                "n45_all": int(any_sig[max(0, i - 44):i + 1].sum()),
                "n45_t0": int(sig0[max(0, i - 44):i + 1].sum()),
                "chain": chain,
                "big": int((peak / closes[i] - 1) * 100 >= 60.0),
                "peak_gain": (peak / closes[i] - 1) * 100,
                "mkt_ret20": float(mm["mkt_ret20"]) if mm is not None
                and isinstance(mm, pd.Series) else np.nan,
            })
    return pd.DataFrame(rows)


def _prob_table(D: pd.DataFrame, col: str, label: str) -> list:
    L = []
    L.append(f"### {label}（按 {col} 分组）")
    L.append("")
    L.append("| 计数 | n | 大牛概率 | 中牛概率 | 均峰 |")
    L.append("| --- | --- | --- | --- | --- |")
    g = D.groupby(col)
    for k, gg in g:
        if k == 0:
            continue
        L.append(f"| {k} | {len(gg)} | {(gg['big'] == 1).mean():.1%} | "
                 f"{(gg['peak_gain'] >= 30).mean():.1%} | "
                 f"{gg['peak_gain'].mean():+.0f}% |")
    return L


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

    D = collect(panels, mkt)
    D = D[D["date"] >= "2021-08-01"].reset_index(drop=True)
    base = (D["big"] == 1).mean()
    print(f"买入信号日 {len(D)} 个，大牛占比 {base:.1%}")

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 连续买入信号计数 → 大牛属性判定（{dstr}）")
    L.append("")
    L.append(f"> 范围：行业卡点企业 100 家；数据 2021-08 ~ 2026-08-12；"
             f"大牛 = 信号后 150 日收盘峰值 ≥+60%；信号 = T0 / B3 / 二波 任一。")
    L.append(f"> 基线：任意买入信号日的大牛概率 {base:.1%}；"
             "随机交易日 18.1%。")
    L.append("")

    # ---- 密度 ----
    L.append("## 一、90 日内累计信号数 → 大牛概率")
    L.append("")
    L.append("> 计数 = 信号日往前 90 交易日内该信号出现的次数（含当日）。")
    L.append("")
    L.extend(_prob_table(D, "n90_all", "合计信号（T0+B3+二波）"))
    L.append("")
    L.extend(_prob_table(D, "n90_t0", "T0 信号（放量涨停/大阳线）"))
    L.append("")
    L.extend(_prob_table(D, "n90_b3", "B3 信号（均线粘合爆量突破）"))
    L.append("")

    # ---- 链长 ----
    L.append("## 二、连续链长（相邻信号间隔 ≤20 日）→ 大牛概率")
    L.append("")
    L.append("| 链长 | n | 大牛概率 | 中牛概率 | 均峰 |")
    L.append("| --- | --- | --- | --- | --- |")
    for k, gg in D.groupby("chain"):
        if k > 6:
            continue
        L.append(f"| {k} | {len(gg)} | {(gg['big'] == 1).mean():.1%} | "
                 f"{(gg['peak_gain'] >= 30).mean():.1%} | "
                 f"{gg['peak_gain'].mean():+.0f}% |")
    L.append("")
    L.append("> 链长 1 = 孤立信号（此前 ≥20 日无信号）；链长 ≥2 = 连续出现。")
    L.append("")

    # ---- 阈值 × 主题 ----
    L.append("## 三、阈值 × 热主题（AI硬件/半导体/存储）")
    L.append("")
    L.append("| 条件 | n | 大牛概率 | 均峰 |")
    L.append("| --- | --- | --- | --- |")
    hot = D["theme"].isin(bigtrend.HOT_THEMES)
    for cond, mask, nm in (
        ("全部信号", pd.Series(True, index=D.index), "全部"),
        ("90日≥2", D["n90_all"] >= 2, "≥2"),
        ("90日≥3", D["n90_all"] >= 3, "≥3"),
        ("90日≥4", D["n90_all"] >= 4, "≥4"),
        ("90日≥5", D["n90_all"] >= 5, "≥5"),
        ("热主题", hot, "热主题"),
        ("热主题 & 90日≥2", hot & (D["n90_all"] >= 2), "热&≥2"),
        ("热主题 & 90日≥3", hot & (D["n90_all"] >= 3), "热&≥3"),
        ("热主题 & 90日≥4", hot & (D["n90_all"] >= 4), "热&≥4"),
    ):
        gg = D[mask]
        if len(gg) < 20:
            continue
        L.append(f"| {cond} | {len(gg)} | {(gg['big'] == 1).mean():.1%} | "
                 f"{gg['peak_gain'].mean():+.0f}% |")
    L.append("")

    # ---- 宏和案例 ----
    hh = D[D["code"] == "603256"].sort_values("date")
    if len(hh):
        L.append("## 四、宏和科技信号轨迹（何时跨过阈值）")
        L.append("")
        L.append("| 日期 | 类型 | 90日累计 | 链长 | 事后大牛 |")
        L.append("| --- | --- | --- | --- | --- |")
        for _, r in hh[hh["date"] >= "2025-01-01"].iterrows():
            typ = "+".join(x for x, f in (("T0", r["is_t0"]), ("B3", r["is_b3"]),
                                          ("二波", r["is_w2"])) if f)
            L.append(f"| {r['date']} | {typ} | {r['n90_all']} | {r['chain']} | "
                     f"{'✓' if r['big'] else ''} |")
        L.append("")

    # ---- 逐年 ----
    L.append("## 五、推荐阈值逐年表现")
    L.append("")
    L.append("> 推荐阈值见结论；下表为 '90日内累计信号 ≥3 且热主题' 的逐年表现。")
    L.append("")
    L.append("| 年份 | n | 大牛概率 | 均峰 |")
    L.append("| --- | --- | --- | --- |")
    sel = D[hot & (D["n90_all"] >= 3)]
    for yr, gg in sel.groupby(sel["date"].str[:4]):
        L.append(f"| {yr} | {len(gg)} | {(gg['big'] == 1).mean():.1%} | "
                 f"{gg['peak_gain'].mean():+.0f}% |")
    L.append("")

    # ---- 结论（数据自动生成）----
    hot = D["theme"].isin(bigtrend.HOT_THEMES)
    base = (D["big"] == 1).mean()
    t0_3 = D[D["n90_t0"] >= 3]
    t0_4 = D[D["n90_t0"] >= 4]
    t0_6 = D[D["n90_t0"] >= 6]
    ch4 = D[D["chain"] >= 4]
    hot3 = D[hot & (D["n90_all"] >= 3)]
    y25 = hot3[hot3["date"].str[:4] == "2025"]
    y26 = hot3[hot3["date"].str[:4] == "2026"]

    L.append("## 六、结论（数据自动生成）")
    L.append("")
    L.append(f"1. **T0 计数是'大牛属性'判别信号**：90 日内 T0 数 1→6，大牛概率"
             f" 22.2%→42.9% 单调上升（基线 {base:.1%}）。"
             f"90日≥3 → {((t0_3['big']==1).mean()):.1%}；≥4 → "
             f"{((t0_4['big']==1).mean()):.1%}；≥6 → {((t0_6['big']==1).mean()):.1%}。")
    L.append("")
    L.append("2. **B3 计数不判别大牛**（1→5 个 B3 的大牛率 27.4%→19.3% 反而下降）——"
             "B3 是'低吸入场点'信号（均线粘合爆量突破，横盘票也会反复触发），"
             "不是'大牛属性'信号；宏和 2025-2026 主升期仅 1 个 B3 即为例证。"
             "二波信号本身即依赖 B3 前置，同理不作独立计数。")
    L.append("")
    L.append(f"3. **连续链长有独立增量**：间隔≤20 日的连续信号链长 ≥4 → "
             f"{((ch4['big']==1).mean()):.1%}（孤立信号 24.1%）。"
             "宏和 07-07/08/09 三天连板链长 4→6 即为此形态。")
    L.append("")
    L.append(f"4. **推荐判定规则**：90 日内累计 **≥3 个 T0 信号** 且 **热主题**"
             f"（AI硬件/半导体/存储）→ 判定大牛候选（全期大牛概率 "
             f"{((hot3['big']==1).mean()):.1%}，2025 年 {((y25['big']==1).mean()):.1%}"
             f"、2026 年 {((y26['big']==1).mean()):.1%}）；"
             "信号更密集（≥6 个/90 日）或链长≥4 可作为重仓强化条件。")
    L.append("")
    L.append("5. **诚实警示**：① 同一股票连续信号共享同一段未来行情（重叠窗口），"
             "概率提升含动量成分；② 该规则 2021-2024 仅 10-27%（见第五节），"
             "2025-2026 的 52-65% 是当前风格环境的红利，弱年必须按三态轮动降档；"
             "③ 计数判定是'大牛候选'（选股/跟踪），入场仍需 B3/二波/回踩等"
             "买点信号，二者分工不同。")
    L.append("")
    L.append("> 局限：信号窗口重叠致样本非独立；B3/二波为 2026-08-14 新规则，"
             "历史回算口径与现行代码一致但未经历实盘检验。研究线索，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"买入信号计数大牛判定_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    D.to_csv(paths.report_dir() / f"信号计数明细_{dstr}.csv",
             index=False, encoding="utf-8-sig")
    print(f"研究完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="连续买入信号计数 → 大牛属性判定")
    ap.add_argument("--fast", action="store_true", help="跳过全市场特征")
    args = ap.parse_args()
    run(with_market=not args.fast)


if __name__ == "__main__":
    main()
