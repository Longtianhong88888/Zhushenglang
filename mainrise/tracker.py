"""每日跟踪：买点提示 / 纸面持仓账本 / 观察池状态 / 全市场新信号。"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.signals import (
    in_universe,
    load_names,
    row_status,
    scan_market,
    tail_features,
)

STOP = 0.95          # -5% 止损
PULLBACK = 0.92      # 高点回落 8% 止盈
TIME_STOP = 5        # 5 日时间止损
MAX_PARALLEL = 3     # 最多并行纸面持仓
PULLBACK_MIN_SCORE = 75.0  # 回踩自动建仓的最低综合分
MAX_10D_GAIN = 80.0  # 10 日涨幅 >= 此值（%）不进买点提示


def watch_path() -> "Path":
    from pathlib import Path
    return Path(paths.state_dir()) / "mainrise_watchlist.csv"


def pos_path() -> "Path":
    from pathlib import Path
    return Path(paths.state_dir()) / "mainrise_positions.csv"


def load_watchlist() -> pd.DataFrame:
    p = watch_path()
    if not p.exists():
        # 空池兜底：只有全市场扫描，无评分
        return pd.DataFrame(columns=["code", "name", "track", "fin_score",
                                     "signals", "pos", "composite"])
    df = pd.read_csv(p, dtype={"code": str})
    for col in ["code", "name", "track", "fin_score", "signals", "pos", "composite"]:
        if col not in df.columns:
            df[col] = np.nan
    df["composite"] = pd.to_numeric(df.get("composite"), errors="coerce")
    df["name"] = df.get("name", pd.Series("", index=df.index)).fillna("")
    return df


def save_watchlist(df: pd.DataFrame) -> None:
    paths.ensure_dirs()
    df.to_csv(watch_path(), index=False, encoding="utf-8-sig")


def load_positions() -> pd.DataFrame:
    cols = ["code", "name", "signal_date", "confirm_date", "buy_kind",
            "buy_date", "buy_price", "peak", "peak_date", "status",
            "close_date", "close_price", "reason"]
    text_cols = {"name", "signal_date", "confirm_date", "buy_date",
                 "peak_date", "close_date", "reason", "status"}
    p = pos_path()
    if p.exists():
        df = pd.read_csv(p, dtype={"code": str})
        for c in cols:
            if c not in df.columns:
                df[c] = np.nan
    else:
        df = pd.DataFrame(columns=cols)
    # 文本/日期列统一为 object，避免 float64 列赋字符串触发 FutureWarning
    for c in text_cols:
        if c in df.columns:
            df[c] = df[c].astype("object")
    return df[cols]


def save_positions(df: pd.DataFrame) -> None:
    paths.ensure_dirs()
    df.to_csv(pos_path(), index=False, encoding="utf-8-sig")


def update_positions(pos: pd.DataFrame, panels_by_code: dict, watch: pd.DataFrame,
                     date: str, names: dict, max_10d: float = MAX_10D_GAIN) -> pd.DataFrame:
    """纸面持仓：回踩/T1 自动建仓、更新峰值、平仓。"""
    pos = pos.copy()
    watch_map = dict(zip(watch.get("code", []), watch.get("name", [])))

    open_cnt = int(((pos["status"] == "active") | (pos["status"] == "pending")).sum())
    for _, r in watch.iterrows():
        if pd.isna(r["composite"]) or r["composite"] < PULLBACK_MIN_SCORE:
            continue
        code = r["code"]
        g = panels_by_code.get(code)
        if g is None or len(g) < 35:
            continue
        t = tail_features(g)
        if t is None:
            continue
        today = t.iloc[-1]
        if today["date"] != date:
            continue
        if pd.notna(today.get("chg10", np.nan)) and today["chg10"] >= max_10d:
            continue
        label, _ = row_status(today, False)
        if label != "回踩低吸":
            continue
        has_open = ((pos["code"] == code) &
                    (pos["status"].isin(["active", "pending"]))).any()
        if has_open or open_cnt >= MAX_PARALLEL:
            continue
        pos = pd.concat([pos, pd.DataFrame([{
            "code": code, "name": watch_map.get(code, names.get(code, "")),
            "signal_date": np.nan, "confirm_date": np.nan, "buy_kind": 2,
            "buy_date": date, "buy_price": today["close"],
            "peak": today["high"], "peak_date": date, "status": "active",
            "close_date": np.nan, "close_price": np.nan, "reason": "回踩低吸"}])],
            ignore_index=True)
        open_cnt += 1

    for _, r in watch.iterrows():
        if pd.isna(r["composite"]) or r["composite"] < PULLBACK_MIN_SCORE:
            continue
        code = r["code"]
        g = panels_by_code.get(code)
        if g is None or len(g) < 35:
            continue
        t = tail_features(g)
        if t is None or len(t) < 2 or t.iloc[-1]["date"] != date:
            continue
        yest, today = t.iloc[-2], t.iloc[-1]
        if pd.notna(today.get("chg10", np.nan)) and today["chg10"] >= max_10d:
            continue
        is_confirm = bool(yest["signal"]) and today["close"] > today["ma5"] and \
            today["low"] >= yest["close"] * 0.97
        if not is_confirm:
            continue
        has_open = ((pos["code"] == code) &
                    (pos["status"].isin(["active", "pending"]))).any()
        if has_open or open_cnt >= MAX_PARALLEL:
            continue
        pos = pd.concat([pos, pd.DataFrame([{
            "code": code, "name": watch_map.get(code, names.get(code, "")),
            "signal_date": yest["date"], "confirm_date": today["date"],
            "buy_kind": 1, "buy_date": np.nan, "buy_price": np.nan,
            "peak": np.nan, "peak_date": np.nan, "status": "pending",
            "close_date": np.nan, "close_price": np.nan,
            "reason": "T1确认"}])], ignore_index=True)
        open_cnt += 1

    for i in pos.index:
        row = pos.loc[i]
        code = row["code"]
        g = panels_by_code.get(code)
        if g is None:
            continue
        g = g.reset_index(drop=True)
        t = tail_features(g)
        if t is None:
            continue
        if row["status"] == "pending":
            if pd.isna(row["buy_date"]):
                after = g[g["date"] > row["confirm_date"]]
                if len(after):
                    b = after.iloc[0]
                    pos.loc[i, "buy_date"] = b["date"]
                    pos.loc[i, "buy_price"] = b["open"]
                    pos.loc[i, "peak"] = b["high"]
                    pos.loc[i, "peak_date"] = b["date"]
                    pos.loc[i, "status"] = "active"
                    row = pos.loc[i]
                else:
                    continue
        if row["status"] != "active":
            continue
        buy_date = row["buy_date"]
        seg = g[g["date"] >= buy_date]
        if len(seg) == 0:
            continue
        hi_max = seg["high"].max()
        if hi_max >= float(row["peak"]):
            pos.loc[i, "peak"] = hi_max
            pos.loc[i, "peak_date"] = seg.loc[seg["high"].idxmax(), "date"]
        last = t.iloc[-1]
        if last["date"] <= buy_date:
            continue
        buy_p = float(row["buy_price"])
        peak = float(pos.loc[i, "peak"])
        days_held = int((g["date"] > buy_date).sum())
        reason = None
        if last["low"] <= buy_p * STOP:
            reason = f"止损-5%（低点{last['low']:.2f}≤{buy_p*STOP:.2f}）"
        elif last["close"] <= peak * PULLBACK:
            reason = f"高点回落8%（峰值{peak:.2f}→收盘{last['close']:.2f}）"
        elif last["close"] < last["ma10"]:
            reason = f"跌破MA10（{last['ma10']:.2f}）"
        elif days_held >= TIME_STOP:
            reason = f"5日时间止损（已{days_held}日）"
        if reason:
            pos.loc[i, "status"] = "closed"
            pos.loc[i, "close_date"] = last["date"]
            pos.loc[i, "close_price"] = last["close"]
            pos.loc[i, "reason"] = reason
    return pos


def run(date: str | None = None, no_scan: bool = False,
        max_10d: float = MAX_10D_GAIN) -> dict:
    """执行一次跟踪并写报告，返回摘要 dict。"""
    print("加载行情数据...")
    panels = load_all_panels()
    if panels.empty:
        raise SystemExit("本地无行情数据，请先运行: mainrise init")
    panels = panels[panels["code"].map(in_universe)]
    panels = panels[~panels["is_st"].fillna(0).astype(int).astype(bool)]
    panels = panels[~panels["is_paused"].fillna(0).astype(int).astype(bool)]
    panels = panels.sort_values(["code", "date"])

    date = date or panels["date"].max()
    panels = panels[panels["date"] <= date]
    if panels.empty:
        raise SystemExit(f"{date} 无数据")
    print(f"行情数据截止: {date}（{len(panels):,} 行）")
    if not no_scan:
        print("扫描全市场新信号...")

    by_code = {c: g.reset_index(drop=True)
               for c, g in panels.groupby("code", sort=False)}
    names = load_names()
    watch = load_watchlist()
    print(f"观察池: {len(watch)} 只")
    found = scan_market(panels, date, names) if not no_scan else pd.DataFrame()

    state_rows = []
    for _, r in watch.iterrows():
        g = by_code.get(r["code"])
        if g is None or len(g) < 35:
            state_rows.append({"code": r["code"],
                               "name": r["name"] or names.get(r["code"], ""),
                               "composite": r["composite"], "close": np.nan,
                               "chg": np.nan, "status": "无数据", "hint": "",
                               "ma10": np.nan, "ma20": np.nan,
                               "vr": np.nan, "chg10": np.nan})
            continue
        t = tail_features(g)
        if t is None or t.iloc[-1]["date"] != date:
            state_rows.append({"code": r["code"],
                               "name": r["name"] or names.get(r["code"], ""),
                               "composite": r["composite"], "close": np.nan,
                               "chg": np.nan, "status": "停牌/无数据", "hint": "",
                               "ma10": np.nan, "ma20": np.nan,
                               "vr": np.nan, "chg10": np.nan})
            continue
        today = t.iloc[-1]
        yest = t.iloc[-2] if len(t) >= 2 else None
        prev_sig = bool(yest is not None and yest["signal"])
        label, hint = row_status(today, prev_sig, max_10d)
        state_rows.append({
            "code": r["code"], "name": r["name"] or names.get(r["code"], ""),
            "composite": r["composite"], "close": today["close"],
            "chg": today["chg"], "status": label, "hint": hint,
            "ma10": today["ma10"], "ma20": today["ma20"],
            "vr": today["vol_ratio"], "chg10": today["chg10"]})
    # 仅观察池为空时，才把全市场新信号并入状态表兜底（避免买点区空白）；
    # 观察池非空时新信号只在第四节"全市场新信号"列示，不占用买点提示区
    if len(watch) == 0:
        for _, r in found.iterrows():
            label = "T0新信号" if r["kind"] == "T0" else "T1确认买点"
            state_rows.append({
                "code": r["code"], "name": r["name"] or names.get(r["code"], ""),
                "composite": np.nan, "close": np.nan, "chg": r["chg"],
                "status": label,
                "hint": "新信号，待财务评估后入池（先评估再考虑买点）",
                "ma10": np.nan, "ma20": np.nan, "vr": r["vr"], "chg10": r["chg10"]})
    status_df = pd.DataFrame(state_rows)
    for col in ["code", "name", "composite", "close", "chg", "status", "hint",
                "ma10", "ma20", "vr", "chg10"]:
        if col not in status_df.columns:
            status_df[col] = np.nan
    status_df = status_df.sort_values("composite", ascending=False,
                                      na_position="last")

    paths.ensure_dirs()
    pos = load_positions()
    pos = update_positions(pos, by_code, watch, date, names, max_10d)
    save_positions(pos)

    # 持仓现价/盈亏（供 Excel 展示）
    pos_view = pos.copy()
    for i in pos_view.index:
        if pos_view.loc[i, "status"] not in ("active", "pending"):
            continue
        g = by_code.get(pos_view.loc[i, "code"])
        last = tail_features(g).iloc[-1] if g is not None and len(g) >= 35 else None
        px = last["close"] if last is not None else np.nan
        bp = pos_view.loc[i, "buy_price"]
        pos_view.loc[i, "last_close"] = px
        pos_view.loc[i, "pnl"] = ((px / float(bp) - 1) * 100
                                  if pd.notna(px) and pd.notna(bp) and bp else np.nan)

    buy_points = status_df[
        status_df["status"].isin(["T0新信号", "T1确认买点", "回踩低吸"]) &
        (status_df["chg10"].isna() | (status_df["chg10"] < max_10d))]
    buy_points = buy_points.sort_values("composite", ascending=False,
                                        na_position="last")
    active = pos[pos["status"] == "active"]
    pending = pos[pos["status"] == "pending"]

    lines = [f"# 主升浪信号跟踪（{date}）",
             f"> 跟踪池 {len(status_df)} 只（综合评分=40%财务+30%信号+30%产业地位）",
             "> 规则：信号=多头排列+创20日新高+放量上攻；买点1=次日确认后开盘买；买点2=回踩MA10缩量企稳低吸",
             "> 止损-5% / 跌破MA10；止盈高点回落8%；5日时间止损；单票≤1/3仓，最多3只并行",
             "> 免责：研究线索，不构成投资建议",
             ""]
    lines.append("## 一、今日买点提示")
    lines.append("")
    if buy_points.empty:
        lines.append("今日无触发买点的标的。")
    else:
        lines.append("| 代码 | 名称 | 综合分 | 状态 | 提示 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for _, r in buy_points.iterrows():
            warn = " ⚠低分慎入" if (
                pd.notna(r["composite"]) and r["composite"] < 65) else ""
            lines.append(f"| {r['code']} | {r['name']} | "
                         f"{'-' if pd.isna(r['composite']) else r['composite']} | "
                         f"{r['status']}{warn} | {r['hint']} |")
    lines.append("")
    lines.append("## 二、持仓管理")
    lines.append("")
    if active.empty and pending.empty:
        lines.append("当前无纸面持仓。")
    else:
        lines.append("| 代码 | 名称 | 买入日 | 买入价 | 现价 | 盈亏% | 峰值 | 状态 | 提示 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for _, r in pd.concat([active, pending]).iterrows():
            g = by_code.get(r["code"])
            last = tail_features(g).iloc[-1] if g is not None else None
            px = last["close"] if last is not None else np.nan
            pnl = (px / float(r["buy_price"]) - 1) * 100 if (
                pd.notna(px) and pd.notna(r["buy_price"]) and r["buy_price"]) else np.nan
            note = "明日开盘买入" if r["status"] == "pending" else \
                f"峰值{r['peak']:.2f}，-5%止损/回落8%止盈"
            lines.append(f"| {r['code']} | {r['name']} | {r['buy_date']} | "
                         f"{r['buy_price']:.2f} | "
                         f"{'--' if pd.isna(px) else f'{px:.2f}'} | "
                         f"{'--' if pd.isna(pnl) else f'{pnl:.1f}%'} | "
                         f"{r['peak']:.2f} | {r['status']} | {note} |")
    lines.append("")
    lines.append("## 三、观察池状态（按综合分排序）")
    lines.append("")
    lines.append("| 排名 | 代码 | 名称 | 综合分 | 收盘 | 涨跌% | 状态 | 提示 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, (_, r) in enumerate(status_df.iterrows(), 1):
        comp = "-" if pd.isna(r["composite"]) else f"{r['composite']:.1f}"
        close_s = "-" if pd.isna(r["close"]) else f"{r['close']:.2f}"
        chg_s = "-" if pd.isna(r["chg"]) else f"{r['chg']:.2f}"
        lines.append(f"| {i} | {r['code']} | {r['name']} | {comp} | {close_s} | "
                     f"{chg_s} | {r['status']} | {r['hint']} |")
    lines.append("")
    if not no_scan:
        lines.append("## 四、全市场新信号（当日）")
        lines.append("")
        if found.empty:
            lines.append("当日无新主升浪信号。")
        else:
            lines.append(f"当日共 {len(found)} 只（仅列量比前 20，其余见 CSV 留档）：")
            lines.append("")
            lines.append("| 代码 | 名称 | 类型 | 涨幅% | 量比 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            watch_codes = set(watch["code"])
            for _, r in found.sort_values("vr", ascending=False).head(20).iterrows():
                kind = "T0信号日" if r["kind"] == "T0" else "T1确认日"
                note = "已入观察池" if r["code"] in watch_codes else "待财务评估"
                if pd.notna(r.get("chg10", np.nan)) and r["chg10"] >= max_10d:
                    note += f"，10日+{r['chg10']:.0f}%涨幅过大"
                lines.append(f"| {r['code']} | {r['name']} | {kind} | "
                             f"{r['chg']:.1f} | {r['vr']:.2f} | {note} |")

    md_path = paths.report_dir() / f"主升浪跟踪_{date}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    status_df.to_csv(paths.report_dir() / f"主升浪跟踪_{date}.csv",
                     index=False, encoding="utf-8-sig")
    from mainrise.excel_report import write_tracking_excel
    xlsx_path = paths.report_dir() / f"主升浪跟踪_{date}.xlsx"
    write_tracking_excel(xlsx_path, date, buy_points, pos_view, status_df, found)
    return {
        "report": str(md_path),
        "excel": str(xlsx_path),
        "active": len(active),
        "pending": len(pending),
        "closed": int((pos["status"] == "closed").sum()),
        "buy_points": buy_points,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="每日跟踪")
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--max-10d-gain", type=float, default=MAX_10D_GAIN)
    args = ap.parse_args()
    out = run(args.date, args.no_scan, args.max_10d_gain)
    print(f"跟踪报告: {out['report']}")
    print(f"持仓: {out['active']} 活跃 / {out['pending']} 待买入 / {out['closed']} 已平仓")
    for _, r in out["buy_points"].iterrows():
        print(f"  {r['code']} {r['name']} [{r['status']}] {r['hint']}")
