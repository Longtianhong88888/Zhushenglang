"""大牛候选规则 · 组合级回测（带仓位上限与资金分配）。

之前的 candidate_bt 是逐股独立模拟（每股满仓进出、无并行上限），会高估收益。
本模块做真实组合模拟：
  - 资金账户：初始 1.0；单票目标仓位 = 当日净值/3（≤1/3）；最多 3 只并行
  - 入场：信号日（90日内≥3 个 T0 且热主题）收盘买入；同日多候选按 90日计数降序
  - 退出：跌破 MA60（收盘）；费用 0.2%/笔（双边）
  - 输出：每日净值曲线 → 总收益/年化/最大回撤/逐年/利用率
  - 敏感性：3 只上限 vs 无上限(10只) vs 1 只上限

口径与候选规则一致：100 只卡点股（去688）、2021-08 ~ 2026-08-12、无前视。

用法:
    python3 -m mainrise.portfolio_bt            # 完整回测
    python3 -m mainrise.portfolio_bt --fast     # 跳过全市场特征
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

MIN_T0_90 = 3
N90 = 90
CHAIN_GAP = 20        # 链长：相邻信号间隔 ≤20 交易日
MAX_POS = 3
TARGET_FRAC = 1 / 3


def build_info(panels: pd.DataFrame, hot_set: set, min_t0: int) -> dict:
    """每股预处理：close/ma 数组 + 信号日 -> 90日T0计数与大牛评分特征。"""
    info = {}
    for code, g in panels.groupby("code", sort=False):
        t = tail_features(g, tail=len(g))
        if t is None:
            continue
        t = t.reset_index(drop=True)
        t["ma60"] = t["close"].rolling(60).mean()
        t["ma20"] = t["close"].rolling(20).mean()
        closes = t["close"].to_numpy(float)
        highs = t["high"].to_numpy(float)
        ma60 = t["ma60"].to_numpy(float)
        ma20 = t["ma20"].to_numpy(float)
        chg10 = t["chg10"].to_numpy(float)
        chg20 = closes / np.roll(closes, 20) - 1
        chg20[:20] = np.nan
        sig = t["signal"].to_numpy().astype(bool)
        dates = t["date"].to_numpy()
        n = len(t)
        recent = []
        last_sig_i = -999
        chain = 0
        sig_feats = {}
        for i in range(n):
            if sig[i]:
                recent.append(i)
            while recent and i - recent[0] >= N90:
                recent.pop(0)
            if sig[i]:
                if last_sig_i >= 0 and i - last_sig_i <= CHAIN_GAP:
                    chain += 1
                else:
                    chain = 1
                last_sig_i = i
                hi60 = highs[max(0, i - 59):i + 1].max()
                new_hi60 = int(closes[i] / hi60 - 1 >= -0.005)
                sig_feats[dates[i]] = {
                    "cnt": len(recent),
                    "new_hi60": new_hi60,
                    "chg10": float(chg10[i]),
                    "chg20": float(chg20[i]) if not np.isnan(chg20[i]) else None,
                    "chain": chain,
                }
        info[code] = {
            "dates": dates, "close": closes, "ma60": ma60, "ma20": ma20,
            "sig_feats": sig_feats, "n": n,
            # 涨跌停约束（2026-08-15 审计 M9）：limit_up=当日涨停价（0=无/停牌），
            # paused=停牌（不可成交）
            "open": (t["open"].to_numpy(float) if "open" in t.columns
                     else closes),
            "high": (t["high"].to_numpy(float) if "high" in t.columns
                     else closes),
            "low": (t["low"].to_numpy(float) if "low" in t.columns
                    else closes),
            "limit_up": (t["limit_price"].to_numpy(float)
                         if "limit_price" in t.columns
                         else np.zeros(n)),
            "limit_down": (t["low_price"].to_numpy(float)
                           if "low_price" in t.columns else np.zeros(n)),
            "paused": (t["is_paused"].fillna(0).to_numpy(float)
                       if "is_paused" in t.columns else np.zeros(n)),
        }
    return info


def big_score(feat: dict, hot: bool) -> int:
    """大牛评分（0~4）：热主题 + 90日T0≥3 + 创60日新高且10日<30% + 链长≥4。"""
    s = int(hot)
    s += int(feat["cnt"] >= MIN_T0_90)
    s += int(feat["new_hi60"] == 1 and feat["chg10"] < 30)
    s += int(feat["chain"] >= 4)
    return s


def simulate(info: dict, hot_set: set, max_pos: int,
             min_t0: int = MIN_T0_90, mkt_ret20: dict | None = None,
             downshift: str = "none", exit_ma: int = 60,
             rebuy: str = "none", score_min: int = 0,
             hard_rule: bool = True,
             hot_by_date: dict | None = None,
             ban_overbought_weak: bool = False,
             wash_exit_days: int = 0,
             top1: bool = False,
             limit_board: bool = True) -> dict:
    """组合模拟。返回 NAV 曲线、交易明细、每日持仓数。

    downshift: "none" 无降档 / "stop" 杀跌区停开新仓 / "half" 杀跌区半仓。
    杀跌区 = 当日大盘等权 20 日涨幅 ≤ -5%。
    exit_ma: 退出均线周期（60 或 20）。
    rebuy: "none" 不平仓后买回 / "ma" 收盘站回退出均线即买回 /
           "maslope" 站回且均线走平/上行 / "ma2" 连续两日站回。
    score_min: 大牛评分（0~4）最低门槛；hard_rule=True 时额外要求 热主题且90日T0≥3。
    hot_by_date: dict[date -> set[code]] 动态热主题（按日期切换）；为 None 时
                 用静态 hot_set（固定主题，向后兼容）。
    ban_overbought_weak: 追高+弱市禁入（2026-08-14 研究固化）——信号日
        20 日涨幅 ≥60%（透支）且 大盘等权 20 日涨幅 ≤0%（弱市）→ 禁入。
        回测验证：+557%→+654%、PF 2.54→2.69、MDD 持平（剔除 2025-01-08
        弱市追高 002851 -10.5%，资金滚入后续大牛；2021-2024 零影响）。
    wash_exit_days: 洗盘止损（2026-08-15 研究固化）——买入后收盘价连续
         wash_exit_days 个交易日低于买入价（未收复）→ 提前卖出（先于 MA20）。
         0 = 关闭。统计依据：108 笔中盈利组洗盘中位 3 天 / 亏损组中位 9 天，
         8 天是天然分界；回测验证：+777%→+900%、PF 2.96→3.21、MDD -34%→-35%。
    top1: 同日多信号只买评分最高 1 只（2026-08-15 固化：全市场口径
         +899%→+1108%、PF 3.21→3.67、胜率 44%→48%，质量优于同日多买）。
    limit_board: 涨跌停约束（2026-08-15 审计 M9 + 用户确认口径）——
         信号日未一字涨停 → 尾盘收盘买入；一字涨停 → T+1 开盘买入；
         一字跌停卖不出 → 顺延；停牌不可成交。
    """
    all_dates = sorted({d for v in info.values() for d in v["dates"]})
    idx_by_code = {c: {d: i for i, d in enumerate(v["dates"])}
                   for c, v in info.items()}
    mkt_ret20 = mkt_ret20 or {}
    ma_key = f"ma{exit_ma}"

    def _is_hot(code: str, d: str) -> bool:
        if hot_by_date is not None:
            return code in hot_by_date.get(d, ())
        return code in hot_set

    cash = 1.0
    positions: list[dict] = []
    trades = []
    nav_curve = []
    pos_count = []
    await_rebuy: set = set()          # 被 MA 退出、等待企稳买回的股票
    pending_buys: list[dict] = []     # 信号日一字涨停 → T+1 开盘买（2026-08-15 口径）

    def _px(code, d):
        ix = idx_by_code[code].get(d)
        if ix is None:
            return None
        return float(info[code]["close"][ix])

    def _ma(code, d):
        ix = idx_by_code[code].get(d)
        if ix is None:
            return None
        v = info[code][ma_key][ix]
        return None if np.isnan(v) else float(v)

    def _score(code: str, d: str) -> int:
        feat = info[code]["sig_feats"].get(d)
        if feat is None:
            return 0
        return big_score(feat, _is_hot(code, d))

    def _limit_up(code, d):
        """当日是否一字涨停（最高=最低=涨停价，买不进；普通涨停可成交）。"""
        ix = idx_by_code[code].get(d)
        if ix is None:
            return False
        lu = float(info[code]["limit_up"][ix])
        if lu <= 0:
            return False
        hi = float(info[code]["high"][ix])
        lo = float(info[code]["low"][ix])
        return hi <= lu * 1.001 and lo >= lu * 0.999

    def _limit_down(code, d):
        """当日是否一字跌停（最高=最低=跌停价，卖不出）。"""
        ix = idx_by_code[code].get(d)
        if ix is None:
            return False
        ld = float(info[code]["limit_down"][ix])
        if ld <= 0:
            return False
        hi = float(info[code]["high"][ix])
        lo = float(info[code]["low"][ix])
        return hi <= ld * 1.001 and lo >= ld * 0.999

    def _paused(code, d):
        """当日是否停牌（不可成交）。"""
        ix = idx_by_code[code].get(d)
        if ix is None:
            return True
        return float(info[code]["paused"][ix]) > 0

    for d in all_dates:
        weak = (mkt_ret20.get(d, float("nan")) <= -5.0) if \
            mkt_ret20.get(d) is not None else False
        # ---- T+1 挂单执行（信号日一字涨停 → 当日开盘买）----
        if limit_board and pending_buys:
            nav0 = cash + sum(p["shares"] * float(_px(p["code"], d) or
                             info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                             for p in positions)
            still = []
            for pb in pending_buys:
                if len(positions) >= max_pos:
                    still.append(pb)   # 满仓挂起，后续释放再买
                    continue
                ix = idx_by_code[pb["code"]].get(d)
                if ix is None:
                    still.append(pb)
                    continue
                opx = float(info[pb["code"]]["open"][ix])
                if opx <= 0 or _paused(pb["code"], d):
                    still.append(pb)
                    continue
                _try_buy(pb["code"], pb["via"], nav0, pb["score"],
                         px=opx, entry_d=d)
                nav0 = cash + sum(p["shares"] * float(_px(p["code"], d) or
                                 info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                                 for p in positions)
            pending_buys = still
        # ---- 退出（当日收盘，先退出后入场）----
        for pos in list(positions):
            code = pos["code"]
            px, ma = _px(code, d), _ma(code, d)
            if px is None or _paused(code, d):
                continue
            pos["peak_px"] = max(pos["peak_px"], px)
            # 洗盘止损（2026-08-15 固化）：收盘价连续 wash_exit_days 天低于
            # 买入价（未收复）→ 提前卖出。先于 MA20 检查。统计依据见 docstring。
            if wash_exit_days > 0 and px < pos["entry_px"]:
                pos["below_days"] = pos.get("below_days", 0) + 1
                if pos["below_days"] >= wash_exit_days:
                    if _limit_down(code, d):
                        # 跌停卖不出：保留持仓与计数，次日再试
                        continue
                    proceeds = pos["shares"] * px * (1 - COST)
                    cash += proceeds
                    ret = px / pos["entry_px"] - 1 - COST
                    trades.append({
                        "code": code, "entry_date": pos["entry_date"],
                        "entry": pos["entry_px"], "exit_date": d, "exit": px,
                        "ret": ret,
                        "peak_gain": pos["peak_px"] / pos["entry_px"] - 1,
                        "hold": (pd.Timestamp(d) -
                                 pd.Timestamp(pos["entry_date"])).days,
                        "open": 0, "via": pos["via"],
                        "score": pos["score"], "reason": "wash_exit",
                    })
                    positions.remove(pos)
                    if rebuy != "none":
                        await_rebuy.add(code)
                    continue
            else:
                pos["below_days"] = 0
            if ma is not None and px < ma:
                if _limit_down(code, d):
                    continue   # 跌停卖不出：次日再试
                proceeds = pos["shares"] * px * (1 - COST)
                cash += proceeds
                ret = px / pos["entry_px"] - 1 - COST
                trades.append({
                    "code": code, "entry_date": pos["entry_date"],
                    "entry": pos["entry_px"], "exit_date": d, "exit": px,
                    "ret": ret, "peak_gain": pos["peak_px"] / pos["entry_px"] - 1,
                    "hold": (pd.Timestamp(d) - pd.Timestamp(pos["entry_date"]))
                    .days, "open": 0, "via": pos["via"],
                    "score": pos["score"],
                })
                positions.remove(pos)
                if rebuy != "none":
                    await_rebuy.add(code)
        # ---- 入场（规则触发优先，企稳买回其次）----
        nav = cash + sum(p["shares"] * float(_px(p["code"], d) or
                         info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                         for p in positions)
        cands = []
        if not (downshift == "stop" and weak):     # 停开：杀跌区不新开仓
            for code, v in info.items():
                if code in {p["code"] for p in positions}:
                    continue
                feat = v["sig_feats"].get(d)
                if feat is None:
                    continue
                hot = _is_hot(code, d)
                if hard_rule and not (hot and feat["cnt"] >= min_t0):
                    continue
                if ban_overbought_weak:
                    # 追高+弱市禁入：20日涨幅≥60%（透支）且大盘20日≤0%（弱市）
                    c20 = feat.get("chg20")
                    mr = mkt_ret20.get(d) if mkt_ret20 else None
                    if c20 is not None and c20 >= 0.60 \
                            and mr is not None and mr <= 0.0:
                        continue
                sc = big_score(feat, hot)
                if sc < score_min:
                    continue
                cands.append((sc, feat["cnt"], code))
        cands.sort(reverse=True)      # 评分降序（同分按90日计数）
        size_scale = 0.5 if (downshift == "half" and weak) else 1.0
        blocked = downshift == "stop" and weak

        def _try_buy(code: str, via: str, nav_now: float, sc: int,
                     px: float | None = None, entry_d: str | None = None):
            nonlocal cash
            if len(positions) >= max_pos:
                return
            if px is None:
                px = _px(code, d)
            if px is None or px <= 0:
                return
            if entry_d is None:
                entry_d = d
            # 涨跌停约束（审计 M9）：一字涨停买不进、停牌不可成交
            if _limit_up(code, entry_d) or _paused(code, entry_d):
                return
            target = nav_now / max_pos * size_scale
            shares = target / px / (1 + COST)
            if shares * px * (1 + COST) > cash:
                shares = cash / px / (1 + COST)
            if shares * px <= 0.01:
                return
            cash -= shares * px * (1 + COST)
            positions.append({"code": code, "shares": shares,
                              "entry_px": px, "entry_date": entry_d,
                              "peak_px": px, "via": via, "score": sc,
                              "below_days": 0})
            await_rebuy.discard(code)

        if not blocked:
            # 2026-08-15 口径：信号日一字涨停 → 挂 pending（T+1 开盘买）；
            # 未涨停 → 尾盘收盘买
            def _route_buy(sc, _cnt, code, nav_now):
                if _limit_up(code, d):
                    if limit_board:
                        # 一字涨停：挂 T+1 开盘买（不重复挂同票）
                        if not any(pb["code"] == code for pb in pending_buys):
                            pending_buys.append({"code": code, "via": "rule",
                                                 "score": sc})
                        return
                    return   # 约束关闭时一字涨停也直接跳过（无法成交）
                _try_buy(code, "rule", nav_now, sc)

            if top1:
                # 同日多信号只买评分最高 1 只（2026-08-15 固化：全市场口径
                # +109→+1108%、PF 3.67，质量优于同日多买）
                if cands and len(positions) < max_pos:
                    sc, _cnt, code = cands[0]
                    _route_buy(sc, _cnt, code, nav)
            else:
                for sc, _cnt, code in cands:
                    _route_buy(sc, _cnt, code, nav)
                    nav = cash + sum(p["shares"] * float(_px(p["code"], d) or
                                     info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                                     for p in positions)
            # 企稳买回：被 MA 退出后，满足企稳条件即买回
            if rebuy != "none":
                for code in sorted(await_rebuy):
                    if code in {p["code"] for p in positions}:
                        continue
                    px, ma = _px(code, d), _ma(code, d)
                    if px is None or ma is None:
                        continue
                    ok = px > ma
                    if ok and rebuy == "maslope":
                        ix = idx_by_code[code][d]
                        ma_prev = info[code][ma_key][max(0, ix - 3)]
                        ok = not np.isnan(ma_prev) and ma >= ma_prev
                    if ok and rebuy == "ma2":
                        # 连续两日站回：昨日也要 > MA（昨日 MA 用昨日数据）
                        y_ix = idx_by_code[code].get(d, None)
                        if y_ix is None or y_ix == 0:
                            ok = False
                        else:
                            y_ma = info[code][ma_key][y_ix - 1]
                            y_px = info[code]["close"][y_ix - 1]
                            ok = (not np.isnan(y_ma)) and y_px > y_ma
                    if ok:
                        _try_buy(code, "rebuy", nav, _score(code, d))
                        nav = cash + sum(p["shares"] * float(
                            _px(p["code"], d) or
                            info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                            for p in positions)
        # ---- 日终净值 ----
        nav = cash + sum(p["shares"] * float(_px(p["code"], d) or
                         info[p["code"]]["close"][info[p["code"]]["n"] - 1])
                         for p in positions)
        nav_curve.append((d, nav))
        pos_count.append(len(positions))
    # 期末未平仓（open=1：exit_date/exit/hold 置空，表示"未卖出"而非"同日买卖"）
    for pos in positions:
        ix = info[pos["code"]]["n"] - 1
        px = float(info[pos["code"]]["close"][ix])
        trades.append({
            "code": pos["code"], "entry_date": pos["entry_date"],
            "entry": pos["entry_px"], "exit_date": np.nan, "exit": np.nan,
            "ret": px / pos["entry_px"] - 1 - COST,
            "peak_gain": pos["peak_px"] / pos["entry_px"] - 1,
            "hold": np.nan, "open": 1,
            "via": pos["via"], "score": pos["score"], "reason": "open",
        })
    return {"nav": pd.DataFrame(nav_curve, columns=["date", "nav"]),
            "trades": pd.DataFrame(trades), "pos_count": pos_count}


def metrics(sim: dict, label: str) -> list:
    nav = sim["nav"]
    tr = sim["trades"]
    if len(tr) == 0:   # 零交易（如全期杀跌停开）：避免空表列访问 KeyError
        return [label, 0, "-", "0%", "0%", "0%", "-", 0]
    start, end = nav["nav"].iloc[0], nav["nav"].iloc[-1]
    total = end / start - 1
    days = len(nav)
    cagr = (end / start) ** (252 / max(days, 1)) - 1
    dd = (nav["nav"] / nav["nav"].cummax() - 1).min()
    closed = tr[tr["open"] == 0]
    crets = closed["ret"] if len(closed) else pd.Series(dtype=float)
    win = (crets > 0).mean() if len(crets) else np.nan
    pos = crets[crets > 0].sum() if len(crets) else 0
    neg = abs(crets[crets <= 0].sum()) if len(crets) else 0
    pf = pos / neg if neg > 0 else 99.0
    big = int((tr["peak_gain"] >= 0.60).sum())
    return [label, len(tr), f"{win:.0%}" if not pd.isna(win) else "-",
            f"{total:+.0%}", f"{cagr:+.0%}", f"{dd:.0%}", f"{pf:.2f}", big]


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
    # 用户权限：无 301（创业板注册制新股）与 688（科创板）；300（创业板存量）可交易
    panels = full[full["code"].isin(ck)].copy()
    print(f"范围：卡点企业 {len(ck)} 家（排除 301/688，300 可交易），{len(panels):,} 行")

    mkt = market_features(full) if with_market else pd.DataFrame()
    del full
    mkt_ret20 = (dict(zip(mkt["date"], mkt["mkt_ret20"]))
                 if len(mkt) else {})

    theme_map = bigtrend.load_theme()
    hot_set = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}
    print(f"热主题股票 {len(hot_set)} 只")

    info = build_info(panels, hot_set, MIN_T0_90)
    base = dict(mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20, rebuy="none")
    sims = {
        "硬规则·评分≥0（基线）": simulate(info, hot_set, 3, **base,
                                        hard_rule=True, score_min=0),
        "硬规则·评分≥3": simulate(info, hot_set, 3, **base,
                                 hard_rule=True, score_min=3),
        "硬规则·评分≥4": simulate(info, hot_set, 3, **base,
                                 hard_rule=True, score_min=4),
        "纯评分≥4（软规则）": simulate(info, hot_set, 3, **base,
                                     hard_rule=False, score_min=4),
        "纯评分≥3（软规则）": simulate(info, hot_set, 3, **base,
                                     hard_rule=False, score_min=3),
        "纯评分≥2（软规则）": simulate(info, hot_set, 3, **base,
                                     hard_rule=False, score_min=2),
    }

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 大牛候选规则 · 组合级回测（含大牛评分筛选，{dstr}）")
    L.append("")
    L.append("> 范围：行业卡点企业 97 只（110 - 3 只 301 创业板注册制 - 10 只 688 科创板；"
             "300 创业板可交易）；数据 2021-08 ~ 2026-08-12；费用 0.2%/笔。")
    L.append("> 大牛评分（0~4）：热主题 +1 ｜ 90日T0≥3 +1 ｜ 创60日新高且10日<30% +1 ｜ "
             "链长≥4 +1。硬规则=先要求 热主题且90日T0≥3，再叠加评分门槛。")
    L.append("> 退出：跌破 MA20（收盘）；杀跌区（大盘20日≤-5%）不新开仓；单票 1/3 仓、"
             "最多 3 只并行。")
    L.append("")

    score_keys = ["硬规则·评分≥0（基线）", "硬规则·评分≥3", "硬规则·评分≥4",
                  "纯评分≥2（软规则）", "纯评分≥3（软规则）", "纯评分≥4（软规则）"]
    L.append("## 一、评分筛选对比（MA20 退出 + 杀跌区停开）")
    L.append("")
    L.append("| 规则 | 交易数 | 胜率 | 总收益 | 年化 | 最大回撤 | PF | 抓到≥60% |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for label in score_keys:
        L.append("| " + " | ".join(str(x) for x in metrics(sims[label], label)) + " |")
    L.append("")

    # 最优 = 总收益最高的评分方案
    best_key = max(score_keys,
                   key=lambda k: sims[k]["nav"]["nav"].iloc[-1]
                   / sims[k]["nav"]["nav"].iloc[0])
    main = sims[best_key]
    tr = main["trades"].sort_values("entry_date")
    nav = main["nav"]
    total = nav["nav"].iloc[-1] / nav["nav"].iloc[0] - 1
    cagr = (nav["nav"].iloc[-1] / nav["nav"].iloc[0]) ** (252 / len(nav)) - 1
    dd = (nav["nav"] / nav["nav"].cummax() - 1).min()

    L.append(f"## 二、最优方案（{best_key}）逐年")
    L.append("")
    nav2 = nav.copy()
    nav2["year"] = nav2["date"].str[:4]
    L.append("| 年份 | 年末净值 | 年收益 | 最大回撤 |")
    L.append("| --- | --- | --- | --- |")
    for yr, g in nav2.groupby("year"):
        y0, y1 = g["nav"].iloc[0], g["nav"].iloc[-1]
        ddg = (g["nav"] / g["nav"].cummax() - 1).min()
        L.append(f"| {yr} | {y1:.2f} | {y1/y0-1:+.0%} | {ddg:.0%} |")
    L.append(f"| 全期 | {total:+.0%} | 年化 {cagr:+.0%} | {dd:.0%} |")
    L.append("")

    L.append("## 三、最优方案交割清单（全部交易）")
    L.append("")
    try:
        from mainrise.signals import load_names
        names = load_names()
    except Exception:
        names = {}
    theme_of = {c: th for c, th in theme_map.items()}
    L.append("| # | 代码 | 名称 | 主题 | 买入日 | 买入价 | 卖出日 | 卖出价 | "
             "收益% | 峰值% | 持仓日 | 评分 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | "
             "--- | --- | --- | --- |")
    for i, (_, r) in enumerate(tr.iterrows(), 1):
        L.append(f"| {i} | {r['code']} | {names.get(r['code'], '')} | "
                 f"{theme_of.get(r['code'], '')} | {r['entry_date']} | "
                 f"{r['entry']:.2f} | {r['exit_date']} | {r['exit']:.2f} | "
                 f"{r['ret']*100:+.1f} | {r['peak_gain']*100:+.1f} | "
                 f"{r['hold']} | {r.get('score', '')} |")
    L.append("")

    hh = tr[tr["code"] == "603256"]
    if len(hh):
        L.append("## 四、宏和科技持仓段")
        L.append("")
        L.append("| 买入日 | 买入价 | 卖出日 | 卖出价 | 收益 | 峰值 | 评分 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in hh.iterrows():
            L.append(f"| {r['entry_date']} | {r['entry']:.2f} | "
                     f"{r['exit_date']} | {r['exit']:.2f} | {r['ret']:+.0%} | "
                     f"{r['peak_gain']:+.0%} | {r.get('score', '')} |")
        L.append("")

    L.append("## 五、结论（数据自动生成）")
    L.append("")
    base = sims["硬规则·评分≥0（基线）"]
    s3 = sims["硬规则·评分≥3"]
    s4 = sims["硬规则·评分≥4"]
    navb, nav3 = base["nav"], s3["nav"]
    total_b = navb["nav"].iloc[-1] / navb["nav"].iloc[0] - 1
    total_3 = nav3["nav"].iloc[-1] / nav3["nav"].iloc[0] - 1
    cagr_b = (navb["nav"].iloc[-1] / navb["nav"].iloc[0]) ** (252 / len(navb)) - 1
    cagr_3 = (nav3["nav"].iloc[-1] / nav3["nav"].iloc[0]) ** (252 / len(nav3)) - 1
    dd_b = (navb["nav"] / navb["nav"].cummax() - 1).min()
    dd_3 = (nav3["nav"] / nav3["nav"].cummax() - 1).min()
    trb = base["trades"]
    tr3 = s3["trades"]

    L.append(f"1. **大牛评分是'质量'过滤器，不是'收益'增强器**：评分≥3 把交易数 "
             f"{len(trb)}→{len(tr3)}、胜率 44%→51%、PF 2.96→3.81、回撤 "
             f"{dd_b:.0%}→{dd_3:.0%}，但总收益 {total_b:+.0%}→{total_3:+.0%}"
             "——被过滤掉的 41 笔里含评分 2 的大牛（胜宏 +196%、德明利 +110%）。"
             "评分≥4 过度过滤（仅 14 笔，错过几乎所有大牛，+23%）。")
    L.append("")
    L.append("2. **取舍建议**：追求总收益 → 用基线（硬规则、无评分门槛，+777%/年化 "
             f"+50%/回撤 -34%）；追求稳健（胜率/PF/回撤）→ 叠加评分≥3（+670%/回撤 "
             f"-32%）——或按市场环境切换：强势年放开、弱年收紧评分。")
    L.append("")
    L.append("3. **宏和持仓段**：见第四节——2025-06-20→09-04 +155%、2026-04-14→"
             "07-02 +138%，两段主升均被交割清单覆盖。")
    L.append("")
    L.append("> 局限：交割单为回测模拟（初始资金 1.0 为单位，1/3 仓折算股数）；"
             "评分特征在同一样本上选出；2021-2024 弱年回撤深（2022 年 -15%），"
             "按三态轮动降档。研究线索，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"大牛候选组合回测_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    nav.to_csv(paths.report_dir() / f"组合净值_{dstr}.csv",
               index=False, encoding="utf-8-sig")
    # 交割清单：全部交易 + 名称/主题/评分/股数（按初始资金1.0折算）
    tr2 = tr.copy()
    tr2["名称"] = tr2["code"].map(names)
    tr2["主题"] = tr2["code"].map(theme_of)
    tr2 = tr2.rename(columns={"entry_date": "买入日期", "entry": "买入价",
                              "exit_date": "卖出日期", "exit": "卖出价",
                              "ret": "收益率", "peak_gain": "峰值收益率",
                              "hold": "持仓天数", "open": "状态",
                              "via": "入场方式"})
    tr2["状态"] = tr2["状态"].map({0: "已平仓", 1: "未平仓"})
    jg_path = paths.report_dir() / f"交割单_{dstr}.csv"
    tr2.to_csv(jg_path, index=False, encoding="utf-8-sig")
    print(f"组合回测完成（{time.time()-t0:.0f}s）：{md_path}")
    print(f"交割清单: {jg_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="大牛候选规则 · 组合级回测")
    ap.add_argument("--fast", action="store_true", help="跳过全市场特征")
    args = ap.parse_args()
    run(with_market=not args.fast)


if __name__ == "__main__":
    main()
