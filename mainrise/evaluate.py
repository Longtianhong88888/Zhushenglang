"""信号日财务评估（同花顺指标，信号日已披露报告期，避免前视）。"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import pandas as pd
import requests

from mainrise import paths
from mainrise.snapshot import get_api_key, ths_code

BASE = "https://fuyao.aicubes.cn"
RECENT_DAYS = 40      # 评估最近 40 个交易日
MIN_SIGNALS = 1       # 最少信号次数


def api(path: str, params: dict) -> dict:
    r = requests.get(BASE + path, params=params,
                     headers={"X-api-key": get_api_key()}, timeout=30)
    return r.json()


def available_report(signal_date: str) -> str:
    """信号日可用的最新已披露报告期（披露截止：年报/一季报4-30、中报8-31、三季报10-31）。"""
    y, m, _ = map(int, signal_date.split("-"))
    if m >= 11:
        return f"{y}-3"        # 11月起可用当年三季报
    if m >= 9:
        return f"{y}-2"        # 9月起可用当年中报
    if m >= 5:
        return f"{y}-1"        # 5月起可用当年一季报
    return f"{y - 1}-4"        # 1-4月：前一年年报


def fetch_indicators(code: str, report: str) -> dict | None:
    try:
        d = api("/api/a-share/financials/indicators",
                {"thscode": ths_code(code), "report": report})
        if d.get("code") != 0:
            return None
        data = d.get("data") or {}
        out = {"report": data.get("report")}
        for ab in data.get("abilities") or []:
            inds = {i["index_id"]: i["value"] for i in ab["indicators"]}
            out[ab["ability"]] = inds
        return out
    except Exception:  # noqa: BLE001
        return None


def g(d: dict, key: str) -> float:
    v = d.get(key)
    try:
        return float(v) if v is not None else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def quality_score(f: dict) -> tuple[float, str]:
    if not f:
        return 0, "C"
    growth = f.get("growth") or {}
    prof = f.get("profitability") or {}
    solv = f.get("solvency") or {}
    rev_yoy = g(growth, "calculate_operating_income_yoy_growth_ratio")
    np_yoy = g(growth, "calculate_parent_holder_net_profit_yoy_growth_ratio")
    gm = g(prof, "sale_gross_margin")
    roe = g(prof, "index_weighted_avg_roe")
    debt = g(solv, "assets_debt_ratio")
    score = 0
    score += 25 if rev_yoy >= 20 else (15 if rev_yoy >= 0 else 5)
    score += 25 if np_yoy >= 30 else (15 if np_yoy >= 0 else 5)
    score += 20 if gm >= 30 else (12 if gm >= 15 else 5)
    score += 20 if roe >= 12 else (12 if roe >= 6 else 5)
    score += 10 if debt <= 60 else (5 if debt <= 75 else 0)
    grade = "A" if score >= 80 else ("B" if score >= 55 else "C")
    return score, grade


def classify(signals: int, grade: str) -> str:
    if grade == "A" and signals >= 1:
        return "重点线索"
    if grade in ("A", "B"):
        return "观察线索"
    return "暂不适合"


TRACK_MAP = {
    "603986": "存储芯片（NOR Flash/利基DRAM）",
    "603256": "玻纤电子布（覆铜板上游）",
    "688256": "AI芯片",
    "600519": "白酒龙头",
    "002371": "半导体设备",
    "688981": "晶圆代工",
    "300750": "动力电池",
    "002594": "新能源车",
}


def run() -> str:
    trades_path = paths.report_dir() / "mainrise_trades.csv"
    if not trades_path.exists():
        raise SystemExit(f"缺少 {trades_path}，请先运行: mainrise backtest")
    trades = pd.read_csv(trades_path, dtype={"code": str})
    recent_dates = sorted(trades["buy_date"].unique())[-RECENT_DAYS:]
    sub = trades[trades["buy_date"].isin(recent_dates)]
    cnt = sub.groupby("code").size().to_dict()
    latest_signal = sub.groupby("code")["S_date"].max().to_dict()
    codes = [c for c, n in cnt.items() if n >= MIN_SIGNALS]
    print(f"最近{RECENT_DAYS}个交易日信号标的: {len(codes)} 只")

    rows = []
    for i, code in enumerate(codes):
        report = available_report(latest_signal[code])
        f = fetch_indicators(code, report)
        if f is None:
            f = fetch_indicators(code, f"{int(report[:4]) - 1}-4")
        score, grade = quality_score(f)
        growth = (f.get("growth") or {}) if f else {}
        prof = (f.get("profitability") or {}) if f else {}
        solv = (f.get("solvency") or {}) if f else {}
        rows.append({
            "code": code,
            "signals": cnt[code],
            "营收同比%": round(g(growth, "calculate_operating_income_yoy_growth_ratio"), 1),
            "净利同比%": round(g(growth, "calculate_parent_holder_net_profit_yoy_growth_ratio"), 1),
            "毛利率%": round(g(prof, "sale_gross_margin"), 1),
            "ROE%": round(g(prof, "index_weighted_avg_roe"), 1),
            "负债率%": round(g(solv, "assets_debt_ratio"), 1),
            "质量分": score, "评级": grade,
            "归类": classify(cnt[code], grade),
            "赛道": TRACK_MAP.get(code, "待核验"),
            "报告期": f.get("report", "-") if f else "-",
        })
        if (i + 1) % 20 == 0:
            print(f"  已评估 {i + 1}/{len(codes)}")
        time.sleep(0.1)

    df = pd.DataFrame(rows).sort_values(["归类", "质量分"], ascending=[True, False])
    date = datetime.now().strftime("%Y-%m-%d")
    path = paths.report_dir() / f"信号评估_{date}.md"
    lines = [f"# 主升浪信号标的评估（{date}）",
             f"> 范围：最近 {RECENT_DAYS} 个交易日主升浪信号标的 {len(df)} 只",
             "> 方法：同花顺财务指标（**信号日已披露报告期**，避免前视偏差）质量评分 + 信号频次归类",
             "> 免责：仅研究线索，不构成投资建议；赛道标注『待核验』需人工确认",
             "",
             "## 一、重点线索（A级财务 + 有信号）", ""]
    for cat in ["重点线索", "观察线索", "暂不适合"]:
        lines.append(f"### {cat}")
        lines.append("| 代码 | 信号次数 | 报告期 | 营收同比% | 净利同比% | 毛利率% | ROE% | 负债率% | 质量分 | 赛道 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for _, r in df[df["归类"] == cat].iterrows():
            lines.append(f"| {r['code']} | {r['signals']} | {r['报告期']} | {r['营收同比%']} | {r['净利同比%']} | "
                         f"{r['毛利率%']} | {r['ROE%']} | {r['负债率%']} | {r['质量分']} | {r['赛道']} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"评估报告: {path}")
    return str(path)


def main() -> None:
    try:
        path = run()
    except SystemExit as e:
        print(e)
        sys.exit(1)
