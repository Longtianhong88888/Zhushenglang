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
    scan_two_stage,
    tail_features,
)

STOP = 0.96          # -4% 止损（2026-08-13 用户确认收紧）
PULLBACK = 0.92      # 高点回落 8% 止盈
TIME_STOP = 20       # 20 日时间止损（两级模型让赢家跑）
MAX_PARALLEL = 3     # 最多并行纸面持仓
PULLBACK_MIN_SCORE = 75.0  # 回踩自动建仓的最低综合分
MAX_10D_GAIN = 150.0  # 10 日涨幅 >= 此值（%）不进买点提示（2026-08 研究：强趋势股胜率更高，放宽）


# 板块退潮保护：观察池同一行业 >=3 只且均价跌破 MA10 时暂停该行业买点
INDUSTRY_KEYWORDS = {
    "有色/黄金": ["金", "银", "铜", "钨", "钴", "锂", "稀土", "锡", "镍", "锌",
                  "铝", "矿", "有色", "贵金属", "铅"],
    "半导体": ["半导体", "芯片", "存储", "光刻", "封测", "晶圆", "PCB", "覆铜板",
               "电子"],
    "医药": ["医药", "药", "生物", "医疗", "CRO", "CXO", "中药"],
    "AI/算力": ["AI", "算力", "服务器", "光模块", "光通信", "智算", "数据中心",
                "边缘"],
    "新能源": ["锂电", "电池", "光伏", "风电", "储能", "新能源"],
    "消费": ["白酒", "食品", "饮料", "调味", "乳业", "消费"],
    "化工材料": ["化工", "材料", "玻纤", "钛白", "化纤"],
    "机械设备": ["机械", "装备", "设备", "机床"],
    "油气": ["油气", "石油", "天然气", "油田", "煤炭"],
    "电力": ["电力", "水电", "电网"],
}


def broad_industry(track: object) -> str | None:
    """赛道描述 -> 大行业（关键词优先级匹配）。"""
    s = str(track or "")
    for ind, kws in INDUSTRY_KEYWORDS.items():
        if any(k in s for k in kws):
            return ind
    return None


def weak_industries(watch: pd.DataFrame, by_code: dict, date: str) -> set[str]:
    """板块退潮检测：同行业观察池标的 >=3 且平均收盘 < 平均 MA10。"""
    grp: dict[str, list[tuple[float, float]]] = {}
    for _, r in watch.iterrows():
        ind = broad_industry(r.get("track"))
        if not ind:
            continue
        g = by_code.get(r["code"])
        if g is None or len(g) < 35:
            continue
        t = tail_features(g)
        if t is None or t.iloc[-1]["date"] != date:
            continue
        today = t.iloc[-1]
        if pd.notna(today["close"]) and pd.notna(today["ma10"]):
            grp.setdefault(ind, []).append((today["close"], today["ma10"]))
    weak: set[str] = set()
    for ind, pairs in grp.items():
        if len(pairs) >= 3:
            avg_c = float(np.mean([c for c, _ in pairs]))
            avg_m = float(np.mean([m for _, m in pairs]))
            if avg_c < avg_m:
                weak.add(ind)
    return weak


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
    """纸面持仓：两级模型自动建仓（B3 打底仓 / 二波加仓）、更新峰值、平仓。"""
    pos = pos.copy()
    watch_map = dict(zip(watch.get("code", []), watch.get("name", [])))
    weak = weak_industries(watch, panels_by_code, date)   # 板块退潮保护

    open_cnt = int(((pos["status"] == "active") | (pos["status"] == "pending")).sum())
    for _, r in watch.iterrows():
        if pd.isna(r["composite"]) or r["composite"] < PULLBACK_MIN_SCORE:
            continue
        if broad_industry(r.get("track")) in weak:
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
        if label != "B3打底仓":
            continue
        has_open = ((pos["code"] == code) &
                    (pos["status"].isin(["active", "pending"]))).any()
        if has_open or open_cnt >= MAX_PARALLEL:
            continue
        pos = pd.concat([pos, pd.DataFrame([{
            "code": code, "name": watch_map.get(code, names.get(code, "")),
            "signal_date": today["date"], "confirm_date": today["date"],
            "buy_kind": 3, "buy_date": np.nan, "buy_price": np.nan,
            "peak": np.nan, "peak_date": np.nan, "status": "pending",
            "close_date": np.nan, "close_price": np.nan,
            "reason": "B3打底仓"}] )],
            ignore_index=True)
        open_cnt += 1

    for _, r in watch.iterrows():
        if pd.isna(r["composite"]) or r["composite"] < PULLBACK_MIN_SCORE:
            continue
        if broad_industry(r.get("track")) in weak:
            continue
        code = r["code"]
        g = panels_by_code.get(code)
        if g is None or len(g) < 35:
            continue
        t = tail_features(g)
        if t is None or len(t) < 2 or t.iloc[-1]["date"] != date:
            continue
        today = t.iloc[-1]
        if pd.notna(today.get("chg10", np.nan)) and today["chg10"] >= max_10d:
            continue
        label, _ = row_status(today, False)
        if label != "二波加仓":
            continue
        open_pos = pos[(pos["code"] == code) &
                       (pos["status"].isin(["active", "pending"]))]
        if len(open_pos):
            # 已有底仓 → 加仓：规则"明日开盘加仓"（M3 修复：不再按信号日收盘
            # 当天加权）——标记 pending，次日流转用开盘价 底仓2/3+加仓1/3 加权。
            # 仅当底仓已成交（buy_price 非空）才可加权；否则保持 B3 待建仓不动。
            old = open_pos.iloc[0]
            if pd.notna(old.get("buy_price")):
                pos.loc[open_pos.index[0], "confirm_date"] = today["date"]
                pos.loc[open_pos.index[0], "status"] = "pending"
                pos.loc[open_pos.index[0], "reason"] = "B3底仓+二波待加仓"
            else:
                pos.loc[open_pos.index[0], "reason"] = "B3底仓+二波加仓"
            continue
        if open_cnt >= MAX_PARALLEL:
            continue
        pos = pd.concat([pos, pd.DataFrame([{
            "code": code, "name": watch_map.get(code, names.get(code, "")),
            "signal_date": today["date"], "confirm_date": today["date"],
            "buy_kind": 4, "buy_date": np.nan, "buy_price": np.nan,
            "peak": np.nan, "peak_date": np.nan, "status": "pending",
            "close_date": np.nan, "close_price": np.nan,
            "reason": "二波加仓"}])], ignore_index=True)
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
                    if pd.notna(row["buy_price"]) and row["reason"] == "B3底仓+二波待加仓":
                        # 已有底仓的加仓：次日开盘价 底仓2/3 + 加仓1/3 加权
                        old_px = float(row["buy_price"])
                        pos.loc[i, "buy_price"] = round((old_px * 2 + b["open"]) / 3, 3)
                        pos.loc[i, "peak"] = max(
                            float(row["peak"]) if pd.notna(row["peak"]) else 0,
                            b["high"])
                        pos.loc[i, "reason"] = "B3底仓+二波加仓"
                    else:
                        # 新建仓（B3 底仓 / 独立二波）：次日开盘价建仓
                        pos.loc[i, "buy_price"] = b["open"]
                        pos.loc[i, "peak"] = b["high"]
                        pos.loc[i, "peak_date"] = b["date"]
                    pos.loc[i, "buy_date"] = b["date"]
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
            reason = f"止损-4%（低点{last['low']:.2f}≤{buy_p*STOP:.2f}）"
        elif last["close"] <= peak * PULLBACK:
            reason = f"高点回落8%（峰值{peak:.2f}→收盘{last['close']:.2f}）"
        elif last["close"] < last["ma10"]:
            reason = f"跌破MA10（{last['ma10']:.2f}）"
        elif days_held >= TIME_STOP:
            reason = f"{TIME_STOP}日时间止损（已{days_held}日）"
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
    # 大盘状态（三态轮动）：等权20日涨幅 + 市场宽度 + 成交额水位
    mkt_state = None
    try:
        from mainrise import market_state
        mkt_state = market_state.compute_daily(date=date, panels=panels)
        print(f"大盘状态: {mkt_state['label']} → {mkt_state['advice']}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 大盘状态计算失败（不影响报告）: {e}")
    if not no_scan:
        print("扫描全市场新信号（范围：行业卡点企业）...")

    by_code = {c: g.reset_index(drop=True)
               for c, g in panels.groupby("code", sort=False)}
    names = load_names()
    watch = load_watchlist()
    print(f"观察池: {len(watch)} 只")
    if not no_scan:
        from mainrise.report import load_chokepoint_codes
        chokepoint = load_chokepoint_codes()
        found = scan_two_stage(panels[panels["code"].isin(chokepoint)],
                               date, names)
    else:
        found = pd.DataFrame()

    state_rows = []
    for _, r in watch.iterrows():
        nm = names.get(str(r["code"]), "")
        if not nm and r["name"] and str(r["name"]) != "待补":
            nm = str(r["name"])
        g = by_code.get(r["code"])
        if g is None or len(g) < 35:
            state_rows.append({"code": r["code"],
                               "name": nm,
                               "composite": r["composite"], "close": np.nan,
                               "chg": np.nan, "status": "无数据", "hint": "",
                               "ma10": np.nan, "ma20": np.nan,
                               "vr": np.nan, "chg10": np.nan})
            continue
        t = tail_features(g)
        if t is None or t.iloc[-1]["date"] != date:
            state_rows.append({"code": r["code"],
                               "name": nm,
                               "composite": r["composite"], "close": np.nan,
                               "chg": np.nan, "status": "停牌/无数据", "hint": "",
                               "ma10": np.nan, "ma20": np.nan,
                               "vr": np.nan, "chg10": np.nan})
            continue
        today = t.iloc[-1]
        yest = t.iloc[-2] if len(t) >= 2 else None
        prev_b3 = bool(yest is not None and yest["b3"])
        label, hint = row_status(today, prev_b3, max_10d)
        state_rows.append({
            "code": r["code"], "name": nm,
            "composite": r["composite"], "close": today["close"],
            "chg": today["chg"], "status": label, "hint": hint,
            "ma10": today["ma10"], "ma20": today["ma20"],
            "vr": today["vol_ratio"], "chg10": today["chg10"]})
    # 仅观察池为空时，才把全市场新信号并入状态表兜底（避免买点区空白）；
    # 观察池非空时新信号只在第四节"全市场新信号"列示，不占用买点提示区
    if len(watch) == 0:
        for _, r in found.iterrows():
            label = "二波加仓" if r["kind"] == "二波" else "B3打底仓"
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

    has_scores = len(watch) > 0      # 观察池为空时保留新信号兜底（无分放行）
    buy_points = status_df[
        status_df["status"].isin(["B3打底仓", "二波加仓"]) &
        (status_df["chg10"].isna() | (status_df["chg10"] < max_10d)) &
        ((not has_scores) | pd.notna(status_df["composite"]))]  # 无综合分不进买点
    weak = weak_industries(watch, by_code, date)
    if weak:
        track_by_code = dict(zip(watch.get("code", []), watch.get("track", [])))
        buy_points = buy_points[~buy_points["code"].map(
            lambda c: broad_industry(track_by_code.get(c))).isin(weak)]
    buy_points = buy_points.sort_values("composite", ascending=False,
                                        na_position="last")
    active = pos[pos["status"] == "active"]
    pending = pos[pos["status"] == "pending"]

    # 启动加仓模型：r7mA/r9A 信号（卡点范围，报告日收盘判定 → 次日开盘打底仓）
    launch_cands = []
    try:
        from mainrise.launch import _pre_features, _signal_mask
        from mainrise.report import load_chokepoint_codes
        ck = load_chokepoint_codes()
        zt_map = (panels[panels["close"] >= panels["limit_price"] - 1e-6]
                  .groupby("date").size())
        for code, g in by_code.items():
            if code not in ck or len(g) < 30 or g.iloc[-1]["date"] != date:
                continue
            g2 = g.reset_index(drop=True).copy()
            g2 = g2.merge(zt_map.rename("mkt_zt"), on="date", how="left")
            g2 = _pre_features(g2)
            m = _signal_mask(g2)
            for tag in ("r9A", "r7mA"):
                if bool(m[tag].iloc[-1]):
                    row = g2.iloc[-1]
                    launch_cands.append({
                        "code": code, "name": names.get(code, "") or code,
                        "rule": tag, "chg": float(row["pct_chg"]),
                        "mkt_zt": int(zt_map.get(date, 0))})
                    break
    except Exception:  # noqa: BLE001
        launch_cands = []

    # 两级模型：B3（粘合爆量突破）打底仓 / 二波（回调后再启动）加仓
    b3_cands, w2_cands = [], []
    try:
        for code, g in by_code.items():
            if code not in ck or g.iloc[-1]["date"] != date:
                continue
            t = tail_features(g.reset_index(drop=True))
            if t is None or len(t) == 0:
                continue
            last = t.iloc[-1]
            if last["date"] != date:
                continue
            nm = names.get(code, "") or code
            if bool(last.get("wave2", False)):
                w2_cands.append({
                    "code": code, "name": nm, "level": "二波",
                    "chg": float(last["chg"]), "vr": float(last["vol_ratio"]),
                    "spread": float(last["spread"]),
                    "action": "明日开盘加仓（1/3，总仓≤1/3）"})
            elif bool(last.get("b3", False)):
                b3_cands.append({
                    "code": code, "name": nm, "level": "B3",
                    "chg": float(last["chg"]), "vr": float(last["vol_ratio"]),
                    "spread": float(last["spread"]),
                    "action": "明日开盘打底仓（计划仓位 2/3）"})
    except Exception:  # noqa: BLE001
        b3_cands, w2_cands = [], []
    # 状态 CSV：盯盘页读取 → 盘中提醒"明日动作"
    try:
        ts_rows = [{"code": r["code"], "name": r["name"], "level": r["level"],
                    "date": date, "action": r["action"]}
                   for r in b3_cands + w2_cands]
        pd.DataFrame(ts_rows, columns=["code", "name", "level", "date", "action"]
                     ).to_csv(paths.state_dir() / "mainrise_twostage.csv",
                              index=False, encoding="utf-8-sig")
    except Exception:  # noqa: BLE001
        pass

    lines = [f"# 主升浪信号跟踪（{date}）",
             f"> 跟踪池 {len(status_df)} 只（综合评分=40%财务+30%信号+30%产业地位）",
             "> 规则：两级模型——B3（均线粘合≤3%+爆量阳线+站上三均线+低位）=打底仓；二波（B3后深回调2-12%+再次粘合≤2%+缩量→放量再启动）=最优买点加仓",
             "> 过滤：10日涨幅≥150%不进；板块退潮（同行业≥3只且均价破MA10）暂停该板块买点；无综合分不进买点",
             "> 止损-4% / 跌破MA10；止盈高点回落8%；20日时间止损；单票≤1/3仓，最多3只并行",
             "> 免责：研究线索，不构成投资建议",
             ""]
    if mkt_state:
        r20 = mkt_state.get("mkt_ret20")
        brd = mkt_state.get("breadth")
        awl = mkt_state.get("amount_wl")
        vwl = mkt_state.get("vol_wl")
        lines.append("## 大盘状态（三态轮动）")
        lines.append("")
        lines.append(f"- **{mkt_state['label']}** → {mkt_state['advice']}")
        if None not in (r20, brd, awl, vwl):
            lines.append(f"- 等权20日涨幅 {r20:+.1f}% ｜ 市场宽度 "
                         f"{brd*100:.0f}% ｜ 成交额水位 {awl:.2f} ｜ "
                         f"成交量水位 {vwl:.2f}")
        else:
            lines.append("- 大盘状态数据部分缺失（等权20日/宽度/量能）")
        tech20 = mkt_state.get("tech20")
        other20 = mkt_state.get("other20")
        diff = mkt_state.get("diff")
        if None not in (tech20, other20, diff):
            lines.append(f"- 结构：科技20日 {tech20:+.1f}% ｜ 非科技20日 "
                         f"{other20:+.1f}% ｜ 强弱差 {diff:+.1f}pp → "
                         f"{mkt_state.get('structure', '—')}")
        lines.append("- 口径：等权20日/宽度/量能为收盘口径（17:30 流水线更新）；"
                     "盘中以上证指数20日涨幅实时为准。")
        lines.append("")
    lines.append("## 一、今日买点提示")
    lines.append("")
    if buy_points.empty:
        lines.append("今日无触发买点的标的。")
    else:
        if mkt_state:
            lines.append(f"> 市场状态：**{mkt_state['label']}** · "
                         f"结构**{mkt_state.get('structure', '—')}**"
                         + (" → B3/二波买点轻仓/降档" if mkt_state.get("state")
                            == "主升区" else ""))
        lines.append("| 代码 | 名称 | 综合分 | 状态 | 市场 | 提示 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for _, r in buy_points.iterrows():
            warn = " ⚠低分慎入" if (
                pd.notna(r["composite"]) and r["composite"] < 65) else ""
            st = str(r["status"])
            tn = market_state.signal_note(mkt_state) if mkt_state else ""
            lines.append(f"| {r['code']} | {r['name']} | "
                         f"{'-' if pd.isna(r['composite']) else r['composite']} | "
                         f"{st}{warn} | {tn or '-'} | {r['hint']} |")
    lines.append("")
    lines.append("")
    lines.append("## 二、启动加仓模型（明日打底仓候选）")
    lines.append("")
    if not launch_cands:
        lines.append("今日无 r7mA/r9A 启动信号"
                     "（回撤≥15% + 涨≥5% + 前5日<0 + 市场门限）。")
    else:
        if mkt_state and mkt_state.get("state") == "主升区":
            lines.append(f"> ⚠ 当前为**主升区**（{mkt_state['label']}），"
                         "按三态轮动规则启动加仓模型应停开，以下信号仅供观察，"
                         "不建议开仓。")
        elif mkt_state and (mkt_state.get("amount_wl") or 1.0) < 1.0:
            lines.append("> ⚠ 当前成交额水位不足 1.0（缩量反弹），"
                         "以下信号需等放量确认再考虑开仓。")
        lines.append(f"信号日 {date} 收盘判定，共 {len(launch_cands)} 只；"
                     "**明日开盘打底仓（计划仓位 2/3）**，T+1 收盘站上 MA10 次日加仓；"
                     "止损=加权均价-7%、止盈=高点回落10%（收盘）、时间止损10日、"
                     "全市场涨停<50 退潮清仓。盘中信号以盯盘页为准。")
        lines.append("")
        lines.append("| 代码 | 名称 | 信号 | 当日涨幅% | 市场涨停 | 动作 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r in sorted(launch_cands, key=lambda x: (x["rule"] != "r9A", x["code"])):
            action = (market_state.launch_action(mkt_state, r["rule"])
                      if mkt_state else f"启动{r['rule']}：明日开盘打底仓")
            lines.append(f"| {r['code']} | {r['name']} | {r['rule']} | "
                         f"{r['chg']:+.1f} | {r['mkt_zt']} | {action} |")
    lines.append("")
    lines.append("## 三、持仓管理")
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
                f"峰值{r['peak']:.2f}，-4%止损/回落8%止盈"
            lines.append(f"| {r['code']} | {r['name']} | {r['buy_date']} | "
                         f"{r['buy_price']:.2f} | "
                         f"{'--' if pd.isna(px) else f'{px:.2f}'} | "
                         f"{'--' if pd.isna(pnl) else f'{pnl:.1f}%'} | "
                         f"{r['peak']:.2f} | {r['status']} | {note} |")
    lines.append("")
    lines.append("## 四、观察池状态（按综合分排序）")
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
        lines.append("## 五、B3 / 二波 新信号（当日，卡点名单）")
        lines.append("")
        if found.empty:
            lines.append("当日无 B3/二波 新信号。")
        else:
            lines.append(f"当日共 {len(found)} 只（仅列量比前 20，其余见 CSV 留档）：")
            lines.append("")
            lines.append("| 代码 | 名称 | 类型 | 涨幅% | 量比 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            watch_codes = set(watch["code"])
            for _, r in found.sort_values("vr", ascending=False).head(20).iterrows():
                kind = "二波加仓" if r["kind"] == "二波" else "B3打底仓"
                note = "已入观察池" if r["code"] in watch_codes else "待财务评估"
                if pd.notna(r.get("chg10", np.nan)) and r["chg10"] >= max_10d:
                    note += f"，10日+{r['chg10']:.0f}%涨幅过大"
                lines.append(f"| {r['code']} | {r['name']} | {kind} | "
                             f"{r['chg']:.1f} | {r['vr']:.2f} | {note} |")

    lines.append("")
    lines.append("## 六、两级模型：B3 打底仓 / 二波加仓（2026-08-14 新规则）")
    lines.append("")
    lines.append("- **第一级 B3**（均线粘合爆量突破）：粘合≤3% + 阳线 + 量比≥2 + "
                 "涨幅≥1% + 站上三均线 + 距60日低点<30% → **明日开盘打底仓**"
                 "（计划仓位 2/3）")
    lines.append("- **第二级 二波**（最优买点）：B3 后 3~30 日内深回调 2~12% + "
                 "均线再次粘合≤2% + 缩量 → 放量阳线再启动 → **明日开盘加仓**"
                 "（1/3，总仓≤1/3）")
    lines.append("- 风控：止损 -4%（盘中低点）；止盈高点回落 8%；20 日时间止损让赢家跑")
    lines.append("")
    if b3_cands:
        lines.append("### B3 打底仓提示")
        lines.append("")
        lines.append("| 代码 | 名称 | 涨幅% | 量比 | 均线偏离% | 动作 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r in sorted(b3_cands, key=lambda x: x["vr"], reverse=True):
            lines.append(f"| {r['code']} | {r['name']} | {r['chg']:+.1f} | "
                         f"{r['vr']:.2f} | {r['spread']*100:.1f} | {r['action']} |")
        lines.append("")
    if w2_cands:
        lines.append("### 二波加仓信号（最优买点）")
        lines.append("")
        lines.append("| 代码 | 名称 | 涨幅% | 量比 | 动作 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in sorted(w2_cands, key=lambda x: x["vr"], reverse=True):
            lines.append(f"| {r['code']} | {r['name']} | {r['chg']:+.1f} | "
                         f"{r['vr']:.2f} | {r['action']} |")
        lines.append("")
    if not b3_cands and not w2_cands:
        lines.append("今日无 B3/二波信号。")
        lines.append("")

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
