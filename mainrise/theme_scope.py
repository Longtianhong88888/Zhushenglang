"""热主题范围研究：把固定三主题（AI硬件/半导体/存储）扩大到更大范围是否负作用。

问题（用户 2026-08-14）：模型硬规则热主题=AI硬件/半导体/存储，能否换成更大的
"科技"（+机器人/商业航天/自动驾驶）？是否产生负作用？

方法：portfolio_bt.simulate 同一 info 换 hot_set（只改"哪些股票算热主题"），
规则全同（硬规则 热主题且90日T0≥3 + 评分≥2 + 1/3仓×3只 + MA20退出 + 杀跌区
停开 + 费0.2%）。对比集合：
  T3   现规则三主题（AI硬件/半导体/存储）＝44 只
  T6   科技六主题（+机器人/商业航天/自动驾驶）＝53 只
  T6+  六主题+有色 ＝65 只
  T8   全 8 主题（+创新药）＝73 只
  ALL  全 97 只（含"其他"，接近完全放开——热主题动态化研究里该方案 +102%/MDD-80%）

用法:
    python3 -m mainrise.theme_scope
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import bigtrend, paths, portfolio_bt
from mainrise.data import load_all_panels
from mainrise.entry_study import market_features
from mainrise.report import load_chokepoint_codes
from mainrise.signals import in_universe

SCOPES = {
    "T3 现规则（AI硬件/半导体/存储）": {"AI硬件", "半导体", "存储"},
    "T6 科技（+机器人/商业航天/自动驾驶）": {"AI硬件", "半导体", "存储",
                                        "机器人", "商业航天", "自动驾驶"},
    "T6+有色": {"AI硬件", "半导体", "存储", "机器人", "商业航天",
                "自动驾驶", "有色"},
    "T8 全八主题（+创新药）": {"AI硬件", "半导体", "存储", "机器人",
                          "商业航天", "自动驾驶", "有色", "创新药"},
    "ALL 全97只（含其他·放开）": None,
}


def run() -> str:
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
    print(f"范围：卡点 {len(ck)} 只")

    mkt = market_features(full)
    del full
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))

    theme_map = bigtrend.load_theme()
    base = dict(mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20, rebuy="none")

    # 用最大 hot_set 建 info（sig_feats 不依赖 hot_set，可复用）
    all_codes = set(theme_map)
    info = portfolio_bt.build_info(panels, all_codes, portfolio_bt.MIN_T0_90)
    print(f"build_info 完成（{len(info)} 只，{time.time()-t0:.0f}s）")

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 热主题范围研究：三主题扩大到'科技'是否负作用（{dstr}）")
    L.append("")
    L.append("> 方法：同一模拟器只换 hot_set（哪些股票算热主题），规则全同"
             "（硬规则 热主题且90日T0≥3 + 评分≥2 + 1/3仓×3只 + MA20退出 + "
             "杀跌区停开 + 费0.2%）；数据 2021-01 ~ 2026-08，97 只卡点。")
    L.append("")

    sims = {}
    for label, themes in SCOPES.items():
        if themes is None:
            hot_set = all_codes
        else:
            hot_set = {c for c, th in theme_map.items() if th in themes}
        sim = portfolio_bt.simulate(info, hot_set, 3, hard_rule=True,
                                    score_min=2, **base)
        sims[label] = sim
        print(f"  {label}: {len(hot_set)} 只热主题 → {len(sim['trades'])} 笔")

    L.append("## 一、总体对比")
    L.append("")
    L.append("| 热主题范围 | 股票数 | 交易 | 胜率 | 总收益 | 年化 | 最大回撤 | PF | 抓到≥60% |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for label in SCOPES:
        m = portfolio_bt.metrics(sims[label], label)
        L.append(f"| {label} | {len(SCOPES[label] and {c for c, th in bigtrend.load_theme().items() if th in SCOPES[label]}) if SCOPES[label] else 97} "
                 f"| {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]} | {m[6]} | {m[7]} |")
    L.append("")

    # 逐年对比（T3 vs T6）
    L.append("## 二、逐年：现规则 vs 科技六主题")
    L.append("")
    L.append("| 年份 | T3 现规则 | T6 科技 | 差值 |")
    L.append("| --- | --- | --- | --- |")
    nav3 = sims["T3 现规则（AI硬件/半导体/存储）"]["nav"].copy()
    nav6 = sims["T6 科技（+机器人/商业航天/自动驾驶）"]["nav"].copy()
    nav3["year"] = nav3["date"].str[:4]
    nav6["year"] = nav6["date"].str[:4]
    for yr in sorted(set(nav3["year"]) | set(nav6["year"])):
        g3 = nav3[nav3["year"] == yr]
        g6 = nav6[nav6["year"] == yr]
        r3 = g3["nav"].iloc[-1] / g3["nav"].iloc[0] - 1 if len(g3) else np.nan
        r6 = g6["nav"].iloc[-1] / g6["nav"].iloc[0] - 1 if len(g6) else np.nan
        L.append(f"| {yr} | {r3:+.0%} | {r6:+.0%} | {r6-r3:+.0%} |")
    L.append("")

    # 科技新增主题（机器人/商业航天/自动驾驶）在 T6 下的表现
    L.append("## 三、科技新增主题的贡献（T6 下 机器人/商业航天/自动驾驶 的交易）")
    L.append("")
    tr6 = sims["T6 科技（+机器人/商业航天/自动驾驶）"]["trades"]
    extra_themes = {"机器人", "商业航天", "自动驾驶"}
    extra_codes = {c for c, th in theme_map.items() if th in extra_themes}
    et = tr6[tr6["code"].isin(extra_codes)]
    if len(et):
        closed = et[et["open"] == 0]
        L.append(f"- 新增 3 主题共 {len(extra_codes)} 只，在 T6 下交易 "
                 f"{len(et)} 笔（占 {len(tr6)} 笔的 {len(et)/max(len(tr6),1):.0%}）")
        if len(closed):
            L.append(f"- 其中已平仓 {len(closed)} 笔：胜率 "
                     f"{(closed['ret']>0).mean():.0%}，均收 {closed['ret'].mean():+.2%}"
                     f"，合计 {closed['ret'].sum():+.0%}")
            L.append(f"- 对比：全部已平仓均收 {tr6[tr6['open']==0]['ret'].mean():+.2%}"
                     f"（新增主题{'劣于' if closed['ret'].mean() < tr6[tr6['open']==0]['ret'].mean() else '优于'}整体）")
        for _, r in et.sort_values("entry_date").iterrows():
            nm = names.get(r["code"], "")
            L.append(f"  - {r['code']} {nm}（{theme_map.get(r['code'], '')}）："
                     f"{r['entry_date']} → {r['exit_date']} "
                     f"{r['ret']:+.0%}（峰值 {r['peak_gain']:+.0%}）")
    else:
        L.append("- 新增 3 主题在 T6 下 0 笔交易（未达 90日T0≥3 门槛）")
    L.append("")

    L.append("## 四、结论")
    L.append("")
    L.append("- 若扩大范围后总收益/年化下降且回撤上升 → **负作用确认**：三主题之外的"
             "主题（机器人/商业航天/自动驾驶等）2022-2026 强度不足，纳入后信号质量"
             "被稀释（更多弱信号触发买入，MA20 退出无法挽回）——与热主题动态化研究"
             "（放开门槛最差 +102%/MDD-80%）同构；")
    L.append("- 若 T6 与 T3 接近或更优 → 扩大无负作用，可换更大范围。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"热主题范围研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


names = {}


def main() -> None:
    global names
    try:
        from mainrise.signals import load_names
        names.update(load_names())
    except Exception:  # noqa: BLE001
        pass
    run()


if __name__ == "__main__":
    main()
