# -*- coding: utf-8 -*-
"""热主题放宽回测验证（研究脚本，2026-08-15）

对比：
- 固定热主题（AI硬件/半导体/存储，固化口径）：hot_set = 三主题股票
- 全行业放宽：hot_set = 全部 97 只卡点企业（热主题门槛形同虚设）

其余规则完全一致（固化版）：
- 90日T0≥3 + 评分≥2 + 1/3仓×3只
- 退出：MA20 + 洗盘8日未收复（wash_exit_days=8）
- 杀跌区停开 + 追高弱市禁入（ban_overbought_weak=True）

输出：output/reports/热主题放宽回测_<date>.md
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mainrise.portfolio_bt import (  # noqa: E402
    simulate, build_info, metrics, load_chokepoint_codes, in_universe,
    market_features, MIN_T0_90,
)
from mainrise.data import load_all_panels  # noqa: E402
import mainrise.bigtrend as bigtrend  # noqa: E402


def main() -> None:
    print("加载行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    ck = {c for c in load_chokepoint_codes()
          if not c.startswith("301") and not c.startswith("688")}
    panels = full[full["code"].isin(ck)].copy()
    print(f"范围：卡点企业 {len(ck)} 家，{len(panels):,} 行")

    mkt = market_features(full)
    del full
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))

    theme_map = bigtrend.load_theme()
    hot_static = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}
    hot_all = set(ck)          # 全行业放宽：所有卡点企业都算热
    print(f"固定热主题股票 {len(hot_static)} 只；全行业放宽 {len(hot_all)} 只")

    info_static = build_info(panels, hot_static, MIN_T0_90)
    info_all = build_info(panels, hot_all, MIN_T0_90)

    base_kw = dict(max_pos=3, min_t0=MIN_T0_90, mkt_ret20=mkt_ret20,
                   downshift="stop", exit_ma=20, rebuy="none",
                   score_min=2, hard_rule=True,
                   ban_overbought_weak=True, wash_exit_days=8)

    sims = {
        "固定热主题（固化）": simulate(info_static, hot_static, **base_kw),
        "全行业放宽": simulate(info_all, hot_all, **base_kw),
    }

    print(f"\n{'方案':<22}{'笔数':>6}{'胜率':>7}{'总收益':>9}{'年化':>8}"
          f"{'MDD':>8}{'PF':>7}{'≥60%':>7}")
    print("-" * 78)
    rows = []
    for label, sim in sims.items():
        m = metrics(sim, label)
        rows.append((label, m))
        print(f"{label:<22}{m[1]:>6}{m[2]:>7}{m[3]:>9}{m[4]:>8}{m[5]:>8}"
              f"{m[6]:>7}{m[7]:>7}")

    # 分年度对比
    print("\n=== 分年度收益对比 ===")
    print(f"{'年份':<8}{'固定热主题':>14}{'全行业放宽':>14}")
    nav_s = sims["固定热主题（固化）"]["nav"].copy()
    nav_a = sims["全行业放宽"]["nav"].copy()
    for d in ["2021-12-31", "2022-12-30", "2023-12-29", "2024-12-31",
              "2025-12-31", nav_s["date"].iloc[-1]]:
        s = nav_s[nav_s["date"] <= d]
        a = nav_a[nav_a["date"] <= d]
        if len(s) == 0 or len(a) == 0:
            continue
        sy = s["nav"].iloc[-1] / s["nav"].iloc[0] - 1
        ay = a["nav"].iloc[-1] / a["nav"].iloc[0] - 1
        print(f"{d[:4]:<8}{sy:>+13.0%}{ay:>+14.0%}")

    # 全行业放宽额外抓到的交易样例
    tr_s = sims["固定热主题（固化）"]["trades"]
    tr_a = sims["全行业放宽"]["trades"]
    extra_codes = set(tr_a["code"]) - set(tr_s["code"])
    print(f"\n=== 全行业放宽新增交易标的（{len(extra_codes)} 只）===")
    theme_map = bigtrend.load_theme()
    for c in sorted(extra_codes):
        sub = tr_a[tr_a["code"] == c]
        print(f"  {c} {theme_map.get(str(c), '?')}: {len(sub)} 笔, "
              f"平均 {sub['ret'].mean():+.1%}")

    out = ROOT / "output" / "reports" / f"热主题放宽回测_{dt.date.today():%Y-%m-%d}.md"
    L = [f"# 热主题放宽回测验证（{dt.date.today()}）\n",
         "- 基线：固定热主题（AI硬件/半导体/存储）+ 90日T0≥3 + 评分≥2",
         "- 对比：热主题放宽到全部 97 只卡点企业（热门槛形同虚设）",
         "- 其余规则一致：MA20 退出 + 洗盘8日未收复 + 杀跌区停开 + 追高弱市禁入\n",
         "| 方案 | 笔数 | 胜率 | 总收益 | 年化 | MDD | PF | 抓到≥60% |",
         "|---|---|---|---|---|---|---|---|",
         ]
    for label, m in rows:
        L.append(f"| {label} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]} | {m[6]} | {m[7]} |")
    L.append("")
    L.append("> 口径：2021-08~2026-08 全期；费用 0.2%/笔；1/3 仓×3 只。")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"\n已输出: {out}")


if __name__ == "__main__":
    main()
