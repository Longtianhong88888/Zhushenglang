"""每周绩效小结：读大牛模型交割单/净值/候选，汇总本周信号与绩效，微信推送。

数据源（全部只读文件，不做全量重算）：
  - output/reports/大牛模型交割单_*.csv  本周买入/卖出/持仓
  - output/reports/大牛模型净值_*.csv    本周净值变化
  - output/state/bigbull_cands.json      本周候选/信号、大盘状态

"本周" = 最新净值曲线的最后 5 个交易日（数据日期为准，不是自然周，
避免节假日错位）。发送走企业微信（优先，免费不限量），Server酱兜底。

用法:
    mainrise weekly              # 生成并推送周报
    mainrise weekly --dry-run    # 只打印不发送
"""
from __future__ import annotations

import argparse
import glob

import pandas as pd

from mainrise import paths
from mainrise import push


def load_latest_trades(report_dir=None) -> tuple[str, pd.DataFrame]:
    """最新交割单 → (数据日期, DataFrame)；无则 (日期空串, 空表)。"""
    d = report_dir or paths.report_dir()
    fs = sorted(glob.glob(str(d / "大牛模型交割单_*.csv")), reverse=True)
    if not fs:
        return "", pd.DataFrame()
    df = push.load_trades_csv(__import__("pathlib").Path(fs[0]))
    return fs[0].rsplit("_", 1)[1].rsplit(".", 1)[0], df


def load_latest_nav(report_dir=None) -> pd.DataFrame:
    """最新净值曲线 → DataFrame（date/nav）；无则空表。"""
    d = report_dir or paths.report_dir()
    fs = sorted(glob.glob(str(d / "大牛模型净值_*.csv")), reverse=True)
    if not fs:
        return pd.DataFrame()
    return pd.read_csv(fs[0])


def build_weekly(trades: pd.DataFrame, nav: pd.DataFrame,
                 cands: list) -> tuple[str, str]:
    """从交割单/净值/候选构建 (标题, markdown 正文)。"""
    if len(nav):
        week = list(nav["date"].astype(str).tail(5))
    else:
        week = []
    wd = set(week)

    buys = trades[trades["买入日期"].astype(str).isin(wd)] if len(trades) else \
        trades.iloc[0:0]
    sells = trades[(trades["卖出日期"].astype(str).isin(wd)) &
                   (trades["状态"] == "已平仓")] if len(trades) else trades.iloc[0:0]
    holds = trades[trades["状态"] == "未平仓"] if len(trades) else trades.iloc[0:0]
    sigs = [c for c in cands if str(c.get("date")) in wd]

    title = (f"周报｜本周买入{len(buys)} 卖出{len(sells)} "
             f"持仓{len(holds)} 信号{len(sigs)}")

    L = [f"### 大牛模型 · 每周绩效小结（{week[0] if week else '—'} ~ "
         f"{week[-1] if week else '—'}）", ""]

    if len(nav) and len(nav) >= 2:
        n0, n1 = float(nav["nav"].iloc[-6]), float(nav["nav"].iloc[-1])
        chg = n1 / n0 - 1
        L.append(f"- 本周净值 {n1:.3f}（{chg:+.1%}，上周五收盘 {n0:.3f}）")
    if len(sells):
        tot = sells["收益率"].sum()
        L.append(f"- 本周平仓 {len(sells)} 笔，合计收益 {tot:+.1%}"
                 f"（胜率 {(sells['收益率'] > 0).mean():.0%}）")
    mkt = "—"
    L.append("")

    L.append("**🟢 本周确认买入（信号日收盘，1/3 仓）**")
    L.append("")
    if len(buys):
        L.append("| 代码 | 名称 | 主题 | 买入日 | 买入价 | 评分 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for _, r in buys.iterrows():
            L.append(f"| {r['代码']} | {r['名称']} | {r['主题']} | "
                     f"{r['买入日期']} | {r['买入价']:.2f} | "
                     f"{r.get('score') or '-'} |")
    else:
        L.append("本周无确认买入。")
    L.append("")

    L.append("**🔴 本周确认卖出（收盘跌破 MA20）**")
    L.append("")
    if len(sells):
        L.append("| 代码 | 名称 | 卖出日 | 卖出价 | 收益 |")
        L.append("| --- | --- | --- | --- | --- |")
        for _, r in sells.iterrows():
            color = "#F85149" if r["收益率"] > 0 else "#3FB950"
            L.append(f"| {r['代码']} | {r['名称']} | {r['卖出日期']} | "
                     f"{r['卖出价']:.2f} | <font color=\"{color}\">"
                     f"{r['收益率']:+.1%}</font> |")
    else:
        L.append("本周无平仓。")
    L.append("")

    L.append(f"**📊 当前持仓（{len(holds)} 只）**")
    L.append("")
    if len(holds):
        for _, r in holds.iterrows():
            L.append(f"- {r['名称']}（{r['代码']}）：{r['买入日期']} 买入 "
                     f"{r['买入价']:.2f}，浮动 {r['收益率']:+.1%}")
    else:
        L.append("当前空仓。")
    L.append("")

    L.append(f"**📡 本周模型信号（{len(sigs)} 条）**")
    L.append("")
    if sigs:
        L.append("、".join(f"{c['code']}({c['date']},评分{c['score']})"
                           for c in sigs))
    else:
        L.append("本周无硬规则信号。")
    L.append("")

    L.append("> 收盘口径以 `mainrise bigbull` 交割单为准。研究线索，不构成投资建议。")
    return title, "\n".join(L)


def run(dry_run: bool = False) -> str:
    """生成并推送周报（企业微信优先，Server酱兜底）。"""
    nav = load_latest_nav()
    if len(nav) == 0:
        print("未找到净值曲线 CSV，跳过周报")
        return "skip"
    _, trades = load_latest_trades()
    data = push.load_cands()
    cands = (data or {}).get("cands") or []
    title, desp = build_weekly(trades, nav, cands)
    if dry_run:
        print(f"[dry-run] {title}\n\n{desp}")
        return "ok"
    return push.send_alert(title, desp)


def main() -> None:
    ap = argparse.ArgumentParser(description="每周绩效小结推送（企业微信/Server酱）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不发送")
    args = ap.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
