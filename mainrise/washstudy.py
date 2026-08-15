# -*- coding: utf-8 -*-
"""大牛模型回测 108 笔交易：买入后直接上涨 vs 回调洗盘 统计研究

口径：
- 直接上涨：买入后（含买入日次日）收盘价从未跌破买入价
- 回调洗盘：买入后至少一日收盘价 < 买入价（先回调后启动）
- 洗盘天数：买入日 → 收盘价最低点日 的间隔交易日数（买入日计第 0 天）
- 洗盘幅度：最低收盘 / 买入价 - 1
- 盈利/亏损：按整笔交易收益率 >0 / <=0 分组

输出：output/reports/洗盘统计_<date>.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPORT_DIR = ROOT / "output" / "reports"
DATA_DIR = ROOT / "data" / "zzshare_daily"


def load_daily(code: int, start: str, end: str) -> pd.DataFrame:
    """按日期区间拼接日线（zzshare_daily/日期.csv，按 code 过滤）。"""
    s = dt.date.fromisoformat(start)
    e = dt.date.fromisoformat(end)
    frames = []
    d = s
    while d <= e:
        p = DATA_DIR / f"{d:%Y%m%d}.csv"
        if p.exists():
            try:
                df = pd.read_csv(p)
                df = df[df["code"] == code]
                if not df.empty:
                    frames.append(df)
            except Exception:
                pass
        d += dt.timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out


def analyze_trade(row: pd.Series) -> dict:
    """对单笔交易分析买入后的路径。"""
    code = int(row["代码"])
    buy_date = str(row["买入日期"])
    sell_date = str(row["卖出日期"])
    buy_price = float(row["买入价"])

    # 取买入日 → 卖出日 的日线
    daily = load_daily(code, buy_date, sell_date)
    if daily.empty:
        return {"代码": row["代码"], "名称": row["名称"], "缺数据": True}
    daily = daily.sort_values("date").reset_index(drop=True)

    # 买入日之后的日子（买入日当天是信号日收盘买入，从次日开始考察）
    after = daily[daily["date"] > buy_date].copy()
    if after.empty:
        return {"代码": row["代码"], "名称": row["名称"], "缺数据": True}

    closes = after["close"].values
    # 最低收盘及相对买入价的回撤
    min_idx = int(closes.argmin())
    min_close = float(closes[min_idx])
    drawdown = min_close / buy_price - 1  # <=0 表示曾跌破买入价

    # 直接上涨: 从未跌破买入价；洗盘: 曾跌破
    washed = drawdown < 0

    # 洗盘天数：买入日(0) → 最低收盘日
    wash_days = min_idx + 1 if washed else 0

    # 洗盘后是否收复（在卖出前重新站上买入价）
    if washed:
        rebounded = bool((closes[min_idx:] > buy_price).any())
    else:
        rebounded = True

    # 峰值收益率来自交割单（持有期内最高收盘/买入价-1 或盘中？保持交割单口径）
    return {
        "代码": row["代码"],
        "名称": row["名称"],
        "主题": row["主题"],
        "买入日期": buy_date,
        "卖出日期": sell_date,
        "买入价": buy_price,
        "收益率": float(row["收益率"]),
        "持仓天数": int(row["持仓天数"]),
        "是否洗盘": washed,
        "洗盘天数": wash_days,
        "洗盘幅度%": round(drawdown * 100, 2),
        "洗盘后收复": rebounded,
        "最低收盘": min_close,
        "洗盘最低日": after.iloc[min_idx]["date"] if washed else "",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade-file", default=None, help="交割单 CSV，默认取最新 大牛模型交割单_*.csv")
    ap.add_argument("--out", default=None, help="输出 md 路径")
    args = ap.parse_args()

    if args.trade_file:
        trade_file = Path(args.trade_file)
    else:
        files = sorted(REPORT_DIR.glob("大牛模型交割单_*.csv"))
        if not files:
            raise SystemExit("未找到交割单 CSV")
        trade_file = files[-1]

    trades = pd.read_csv(trade_file)
    trades = trades[trades["状态"] == "已平仓"].copy()
    print(f"读取交割单: {trade_file.name}, 已平仓 {len(trades)} 笔")

    rows = []
    missing = 0
    for _, r in trades.iterrows():
        res = analyze_trade(r)
        if res.get("缺数据"):
            missing += 1
            continue
        rows.append(res)
    df = pd.DataFrame(rows)
    if missing:
        print(f"⚠ {missing} 笔缺日线数据被跳过")

    df["盈利"] = df["收益率"] > 0

    # ===== 统计 =====
    lines = []
    lines.append(f"# 买入后走势统计（{trade_file.stem}）\n")
    lines.append(f"- 数据区间: {df['买入日期'].min()} ~ {df['卖出日期'].max()}")
    lines.append(f"- 有效交易: {len(df)} 笔（盈利 {int((df['盈利']).sum())} / 亏损 {int((~df['盈利']).sum())}）\n")

    # 1) 直接上涨 vs 洗盘 总览
    total = len(df)
    direct = df[~df["是否洗盘"]]
    wash = df[df["是否洗盘"]]
    lines.append("## 一、总览：直接上涨 vs 回调洗盘\n")
    lines.append("| 类型 | 笔数 | 占比 | 盈利数 | 胜率 | 平均收益率 | 平均持仓天数 |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, sub in [("直接上涨", direct), ("回调洗盘", wash), ("合计", df)]:
        lines.append(
            f"| {name} | {len(sub)} | {len(sub)/total*100:.1f}% | "
            f"{int(sub['盈利'].sum())} | {sub['盈利'].mean()*100:.1f}% | "
            f"{sub['收益率'].mean()*100:+.1f}% | {sub['持仓天数'].mean():.1f} |"
        )
    lines.append("")

    # 2) 盈利/亏损 × 直接/洗盘 交叉
    lines.append("## 二、盈利 vs 亏损 × 走势类型\n")
    lines.append("| 分组 | 笔数 | 平均收益率 | 平均持仓天数 | 洗盘平均天数 | 洗盘平均幅度 |")
    lines.append("|---|---|---|---|---|---|")
    for label, sub in [
        ("盈利+直接上涨", df[(df["盈利"]) & (~df["是否洗盘"])]),
        ("盈利+回调洗盘", df[(df["盈利"]) & (df["是否洗盘"])]),
        ("亏损+直接上涨", df[(~df["盈利"]) & (~df["是否洗盘"])]),
        ("亏损+回调洗盘", df[(~df["盈利"]) & (df["是否洗盘"])]),
    ]:
        wash_sub = sub[sub["是否洗盘"]]
        lines.append(
            f"| {label} | {len(sub)} | {sub['收益率'].mean()*100:+.1f}% | "
            f"{sub['持仓天数'].mean():.1f} | "
            f"{wash_sub['洗盘天数'].mean() if len(wash_sub) else '-'} | "
            f"{wash_sub['洗盘幅度%'].mean() if len(wash_sub) else '-'} |"
        )
    lines.append("")

    # 3) 洗盘天数分布（盈利/亏损分开）
    wash_win = wash[wash["盈利"]]
    wash_loss = wash[~wash["盈利"]]
    lines.append("## 三、洗盘天数分布（仅洗盘交易）\n")
    for name, sub in [("盈利组", wash_win), ("亏损组", wash_loss), ("全部洗盘", wash)]:
        lines.append(f"### {name}（{len(sub)} 笔）")
        lines.append("")
        lines.append(f"- 洗盘天数: 均值 {sub['洗盘天数'].mean():.1f} 天｜中位数 {sub['洗盘天数'].median():.0f} 天｜范围 {sub['洗盘天数'].min()}~{sub['洗盘天数'].max()} 天")
        lines.append(f"- 洗盘幅度: 均值 {sub['洗盘幅度%'].mean():.2f}%｜中位数 {sub['洗盘幅度%'].median():.2f}%")
        # 分布直方图（文本）
        lines.append("")
        lines.append("天数分布（1-5/6-10/11-20/21-30/31+）:")
        bins = [(1, 5), (6, 10), (11, 20), (21, 30), (31, 999)]
        for lo, hi in bins:
            n = int(((sub["洗盘天数"] >= lo) & (sub["洗盘天数"] <= hi)).sum())
            bar = "█" * n
            hi_s = "30+" if hi == 999 else f"{hi}"
            lines.append(f"  {lo:>2}-{hi_s:<3}天: {n:>2} 笔 {bar}")
        lines.append("")

    # 4) 洗盘后是否收复
    lines.append("## 四、洗盘后收复情况（洗盘交易内）\n")
    lines.append("| 分组 | 收复 | 未收复(卖出前仍低于买入价) |")
    lines.append("|---|---|---|")
    for name, sub in [("盈利组", wash_win), ("亏损组", wash_loss)]:
        reb = int(sub["洗盘后收复"].sum())
        lines.append(f"| {name} | {reb} | {len(sub)-reb} |")
    lines.append("")

    # 5) 明细表
    lines.append("## 五、明细（按买入日期）\n")
    lines.append("| 代码 | 名称 | 主题 | 买入 | 卖出 | 买入价 | 收益率% | 持仓天 | 类型 | 洗盘天数 | 洗盘幅度% | 最低日 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in df.sort_values("买入日期").iterrows():
        typ = "洗盘" if r["是否洗盘"] else "直涨"
        lines.append(
            f"| {r['代码']} | {r['名称']} | {r['主题']} | {r['买入日期']} | {r['卖出日期']} | "
            f"{r['买入价']:.2f} | {r['收益率']*100:+.1f} | {r['持仓天数']} | {typ} | "
            f"{r['洗盘天数'] if r['是否洗盘'] else '-'} | {r['洗盘幅度%'] if r['是否洗盘'] else '-'} | {r['洗盘最低日']} |"
        )
    lines.append("")

    out_path = args.out or (REPORT_DIR / f"洗盘统计_{dt.date.today():%Y-%m-%d}.md")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已输出: {out_path}")

    # 控制台摘要
    print("\n===== 摘要 =====")
    print(f"直接上涨 {len(direct)} 笔（{len(direct)/total*100:.1f}%），回调洗盘 {len(wash)} 笔（{len(wash)/total*100:.1f}%）")
    for name, sub in [("盈利组", wash_win), ("亏损组", wash_loss)]:
        if len(sub):
            print(f"{name}洗盘: {len(sub)} 笔, 天数均值 {sub['洗盘天数'].mean():.1f} 中位 {sub['洗盘天数'].median():.0f}, 幅度均值 {sub['洗盘幅度%'].mean():.2f}%")


if __name__ == "__main__":
    main()
