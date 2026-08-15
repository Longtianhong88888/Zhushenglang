"""大牛模型（固化版）：全市场 + 固定热主题 + 同日Top1 + MA20退出/洗盘8天止损 + 杀跌区停开 + 追高弱市禁入。

最终选定模型（2026-08-15 用户确认）：
  1. 追踪范围：全市场（排除 ST/停牌，约 4700 只；2026-08-15 由卡点企业 97 只放宽）
     —— 热主题仍限固定三主题（AI硬件/半导体/存储）
  2. 入场：信号日（T0）收盘买入，需同时满足——
     - 硬规则：热主题（AI硬件/半导体/存储）且 90 日内累计 T0 信号 ≥3
     - 大牛评分 ≥2（热主题+1 ｜ 90日T0≥3 +1 ｜ 创60日新高且10日<30% +1 ｜
       链长≥4 +1；硬规则下评分天然 ≥2）
  3. 仓位：单票 1/3 仓，最多 3 只并行；同日多信号只买评分最高 1 只（Top1，
     2026-08-15 固化：+899%→+1108%、PF 3.21→3.67）
  4. 退出：收盘跌破 MA20 卖出；或买入后收盘价连续 8 个交易日未收复买入价（洗盘止损，
     2026-08-15 研究固化）提前卖出；不平仓后买回（等规则再次触发再入场）
  5. 弱市降档：大盘等权 20 日涨幅 ≤-5%（杀跌区）不新开仓
  6. 追高+弱市禁入（2026-08-14 研究固化）：信号日 20 日涨幅 ≥60%（透支）且
     大盘等权 20 日涨幅 ≤0%（弱市）→ 禁入（回测 +557%→+654%、PF 2.54→2.69、
     MDD 持平；剔除 2025-01-08 弱市追高 002851 -10.5%，2021-2024 零影响）
  7. 费用 0.2%/笔（双边）

回测结果（2021-08 ~ 2026-08-12）：总收益 +777%、年化 +50%、最大回撤 -34%、
PF 2.96、胜率 44%、108 笔全平仓。

输出：
  - output/reports/大牛模型回测_<date>.md     完整报告（含交割清单）
  - output/reports/大牛模型交割单_<date>.csv  交割清单（可导入核对）
  - output/reports/大牛模型净值_<date>.csv    每日净值曲线

用法:
    python3 -m mainrise.bigbull            # 固定模型回测（约 15 秒）
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.signals import in_universe
from mainrise.report import load_chokepoint_codes
from mainrise.entry_study import market_features
from mainrise import bigtrend
from mainrise import portfolio_bt as pb

SCORE_MIN = 2        # 大牛评分门槛（硬规则下等价于 ≥2）
EXIT_MA = 20         # 退出均线
DOWNSHIFT = "stop"   # 杀跌区停开新仓
# 追高+弱市禁入（2026-08-14 研究固化）：信号日 20日涨幅≥60%（透支）且
# 大盘等权20日≤0%（弱市）→ 禁入。回测 +557%→+654%、PF 2.54→2.69、MDD 持平。
BAN_OVERBOUGHT_WEAK = True
# 交易范围（2026-08-15 固化）：全市场（排除 ST/停牌/301/688，热主题仍限固定
# 三主题）。回测：97 只卡点 → 全市场 4262 只（+899%→+998%、PF 3.21→3.45、
# 胜率 44%→46%；301 不可交易股混入曾虚增到 +1108%，已排除修正）。
MARKET_SCOPE = "all"      # "all" 全市场 / "chokepoint" 卡点企业 97 只
TOP1_DAILY = True         # 同日多信号只买评分最高 1 只（2026-08-15 固化）
# 洗盘止损（2026-08-15 研究固化）：买入后收盘价连续 8 个交易日低于买入价
# （未收复）→ 提前卖出（先于 MA20）。回测：+777%→+900%、PF 2.96→3.21、
# MDD -34%→-35%。统计依据：盈利组洗盘中位 3 天 / 亏损组中位 9 天，8 天为分界。
WASH_EXIT_DAYS = 8
MAX_POS = 3
PORTAL_NAME = "index.html"   # 门户：只展示大牛模型有效信息
PORTAL_REBASE = "2026-08-01"  # 门户净值曲线/交割记录重置起点（净值 1.0 起，聚焦后续收益）

# ---- 热主题模式 ----
# "dynamic"：跟随市场动态——用 全A行业指数（同花顺行业指数，成分=全市场）的
#            月线级别强度排名判定热主题：主题映射行业指数 lookback 日（默认
#            60 日≈一季度，月线级别）涨幅排名，取前 HOT_RANK_TOP（≥10）为热，
#            逐日切换、无前视，见 themeindex.py（rank_top 模式）。
#            "static"：固定 AI硬件/半导体/存储。
# 回测结论（2026-08-14）：早期日线级方案为负优化（趋势滞后+空仓踏空）；月线级
# 排名 Top10 为本轮用户指定方向，回测对比后再定默认。
# 同花顺行业指数历史自 2022-01 起，dynamic 覆盖前（2021-08~2021-12）回退静态。
HOT_MODE = "static"
HOT_RANK_TOP = 10          # 月线级排名取前 N 为热主题（用户要求 ≥10）
HOT_LOOKBACK_DAYS = 60     # 排名用涨幅回看天数（月线级别）
HOT_PRE_FALLBACK = "static"   # 行业指数覆盖前的回退：static=固定三主题
HOT_MA_SHORT = 20
HOT_MA_LONG = 60
HOT_SLOPE = False
HOT_MIN_DAYS = 0


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _nc(name: str, code: str) -> str:
    """名称/代码合并单元格：上名称下代码，代码直达看盘页 /stock/<code>。"""
    return (f'<div style="font-weight:600;color:#E6EDF3">{_esc(name)}</div>'
            f'<div style="font-size:11px;color:#8B949E">'
            f'<a href="/stock/{_esc(code)}" style="color:#58A6FF;'
            f'text-decoration:none">{_esc(code)}</a></div>')


# ---------------- 大牛模型候选（轻量文件，供盯盘页实时读取） ----------------

def write_cands_json(cands: list, last_date: str, state_dir=None,
                     mkt_ret20: float | None = None,
                     holdings: list | None = None,
                     hot_themes: list | None = None) -> str:
    """候选落盘 output/state/bigbull_cands.json（盯盘页 monitor 读取，避免全量重算）。

    mkt_ret20: 最新大盘等权 20 日涨幅，供 push --close（17:30 收盘确认）直接读取。
    holdings: 大牛模型当前持仓（交割单未平仓 + 最新 MA20/收盘），供盯盘页/14:50
              推送盘中按 MA20 监控卖出。
    hot_themes: 最新一日动态热主题（HOT_MODE=dynamic 时），门户/推送展示。
    """
    import json
    p = (Path(state_dir) if state_dir else paths.state_dir()) / "bigbull_cands.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated": last_date, "cands": cands}
    if mkt_ret20 is not None or hot_themes is not None:
        mkt = {}
        if mkt_ret20 is not None:
            m = float(mkt_ret20)
            if m == m:                          # 排除 NaN
                mkt["mkt_ret20"] = round(m, 4)
        if hot_themes:
            mkt["hot_themes"] = list(hot_themes)
        if mkt:
            data["mkt"] = mkt
    if holdings:
        data["holdings"] = holdings
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def model_candidates(info: dict, hot_set: set, min_t0: int = pb.MIN_T0_90,
                     score_min: int = SCORE_MIN, cutoff_days: int = 45,
                     last_date: str | None = None,
                     hot_by_date: dict | None = None,
                     mkt_ret20: dict | None = None,
                     ban_overbought_weak: bool = True) -> list:
    """大牛模型近期候选：评分≥2 硬规则信号（热主题 且 90日T0≥min_t0 且 评分≥score_min）。

    热主题按信号日判定：hot_by_date 提供时用当日动态热主题，否则用静态 hot_set。
    ban_overbought_weak（2026-08-15 固化，与 simulate 同口径）：信号日 20 日涨幅
    ≥60% 且 大盘等权 20 日 ≤0% → 不列为候选（盯盘/推送与回测一致不追高）。
    按信号日倒序返回 [{code, date, cnt, score, px, ma20}]，供门户与盯盘页共用。
    """
    from datetime import timedelta
    if last_date is None:
        last_date = max(d for v in info.values() for d in v["sig_feats"])
    cutoff = (pd.Timestamp(last_date) - timedelta(days=cutoff_days)) \
        .strftime("%Y-%m-%d")
    mkt_ret20 = mkt_ret20 or {}
    cands = []
    for code, v in info.items():
        for d, feat in v["sig_feats"].items():
            if d < cutoff:
                continue
            hot = (code in hot_by_date.get(d, ()) if hot_by_date is not None
                   else code in hot_set)
            if not (hot and feat["cnt"] >= min_t0):
                continue
            if ban_overbought_weak:
                # 追高+弱市禁入（与 simulate 同口径）：20日涨幅≥60% 且 大盘20日≤0
                c20 = feat.get("chg20")
                mr = mkt_ret20.get(d)
                if c20 is not None and c20 >= 0.60 \
                        and mr is not None and mr <= 0.0:
                    continue
            sc = pb.big_score(feat, hot)
            if sc < score_min:
                continue
            px = float(v["close"][-1])
            ma = v["ma20"][-1]
            cands.append({"code": code, "date": d, "cnt": feat["cnt"],
                          "score": sc, "px": px,
                          "ma20": float(ma) if not np.isnan(ma) else np.nan})
    cands.sort(key=lambda x: x["date"], reverse=True)
    # 去重：同一代码只保留最近一次信号（列表已按日期倒序，首个即最新）
    seen: set = set()
    out = []
    for c in cands:
        if c["code"] in seen:
            continue
        seen.add(c["code"])
        out.append(c)
    return out


def write_portal(tr: pd.DataFrame, nav: pd.DataFrame, info: dict,
                 hot_set: set, mkt_ret20: dict, names: dict, theme_map: dict,
                 last_date: str, out_dir: str | None = None,
                 hot_by_date: dict | None = None,
                 hot_themes: list | None = None) -> str:
    """生成模型门户 output/web/index.html（只含大牛模型有效信息）。

    收益曲线与交割记录为 {PORTAL_REBASE} 起重置口径（净值 1.0 起）；
    完整历史回测口径保留在页脚与 大牛模型回测_*.md。
    hot_by_date/hot_themes：动态热主题（None 时用静态 hot_set）。
    """
    # ---- 完整历史口径（页脚参照）----
    total_full = nav["nav"].iloc[-1] / nav["nav"].iloc[0] - 1
    cagr_full = (nav["nav"].iloc[-1] / nav["nav"].iloc[0]) ** (252 / len(nav)) - 1
    dd_full = (nav["nav"] / nav["nav"].cummax() - 1).min()
    closed_full = tr[tr["open"] == 0]
    crets_full = closed_full["ret"] if len(closed_full) else pd.Series(dtype=float)
    win_full = (crets_full > 0).mean() if len(crets_full) else np.nan
    pos_s = crets_full[crets_full > 0].sum() if len(crets_full) else 0
    neg_s = abs(crets_full[crets_full <= 0].sum()) if len(crets_full) else 0
    pf_full = pos_s / neg_s if neg_s > 0 else 99.0

    # ---- 重置口径：净值 1.0 起，交割记录取窗口内 ----
    nw = nav[nav["date"].astype(str) >= PORTAL_REBASE].copy()
    if len(nw) < 2:
        nw = nav.tail(400).copy()
    nw["nav"] = nw["nav"] / float(nw["nav"].iloc[0])
    total = float(nw["nav"].iloc[-1]) - 1
    cagr = float(nw["nav"].iloc[-1]) ** (252 / len(nw)) - 1
    dd = (nw["nav"] / nw["nav"].cummax() - 1).min()
    tw = tr[tr["entry_date"].astype(str) >= PORTAL_REBASE]
    closed = tw[tw["open"] == 0]
    crets = closed["ret"] if len(closed) else pd.Series(dtype=float)
    win = (crets > 0).mean() if len(crets) else np.nan
    win_txt = "—" if win != win else f"{win:.0%}"   # 窗口内无平仓 → 显示 —
    pos_s = crets[crets > 0].sum() if len(crets) else 0
    neg_s = abs(crets[crets <= 0].sum()) if len(crets) else 0
    pf = pos_s / neg_s if neg_s > 0 else 99.0
    mr = mkt_ret20.get(last_date)
    weak = mr is not None and mr <= -5.0
    mkt_txt = (f"{mr:+.1f}%" if mr is not None else "—")
    mkt_state = "⚠ 杀跌区 → 停开新仓" if weak else "正常 → 可开仓（按规则）"

    # 近期候选（最近 45 个自然日，评分≥2 硬规则信号，按代码去重取最近一次）
    cands = model_candidates(info, hot_set, last_date=last_date,
                             hot_by_date=hot_by_date, mkt_ret20=mkt_ret20,
                             ban_overbought_weak=BAN_OVERBOUGHT_WEAK)

    # ---- 买卖点提醒（今日口径）----
    today_buys = [c for c in cands if c["date"] == last_date]
    # 持仓不过滤窗口（2026-08-15 审计 M10）：2026-08-01 前入场但当前
    # 仍未平仓的持仓也必须显示，不能因 entry_date 早于重置点被漏掉
    holds = tr[tr["open"] == 1] if len(tr) else tr.iloc[0:0]
    today_sells = tw[(tw["open"] == 0) &
                     (tw["exit_date"].astype(str) == last_date)]

    # 点火信号卡（读 ignite5.json：即将满足买入条件的个股 5 分钟点火盘中信号）
    ignite_card_html = ""
    try:
        import json as _jig
        _ig = paths.state_dir() / "ignite5.json"
        if _ig.exists():
            _id = _jig.loads(_ig.read_text(encoding="utf-8"))
            # 只显示与最新交易日匹配的信号（避免非交易日残留的旧信号误报）
            _ig_date = str(_id.get("date", ""))
            _sigs = _id.get("signals") or []
            if _ig_date != last_date:
                _sigs = []
            if _sigs:
                _rows = "".join(
                    f'<tr><td style="color:#F85149">🔥 {_esc(names.get(str(s.get("code")), ""))}'
                    f'</td><td>{_esc(str(s.get("code")))}</td>'
                    f'<td>{_esc(str((s.get("ignite") or {}).get("time", "")))}</td>'
                    f'<td>{(s.get("ignite") or {}).get("px", "")}</td>'
                    f'<td>{(s.get("ignite") or {}).get("vol_mult", "")}×</td>'
                    f'<td>{(s.get("ignite") or {}).get("chg", "")}%</td></tr>'
                    for s in _sigs)
                ignite_card_html = (
                    f'<div class="bdline"><b style="color:#F85149">🔥 盘中点火信号'
                    f'（{_esc(_id.get("date", ""))} · 5分钟资金进攻 · 即将满足买入条件）</b></div>'
                    f'<div class="wrap"><table>'
                    f'<tr><th>名称</th><th>代码</th><th>点火时点</th><th>点火价</th>'
                    f'<th>量比</th><th>涨幅</th></tr>{_rows}</table></div>')
    except Exception:  # noqa: BLE001
        ignite_card_html = ""

    def _latest(code: str, key: str):
        v = info.get(code)
        arr = v.get(key) if v else None
        if arr is None or len(arr) == 0:
            return None
        x = float(arr[-1])
        return None if x != x else x

    # 净值 SVG（重置口径，线性）
    seg = nw
    ys = seg["nav"].to_numpy()
    ymin, ymax = float(ys.min()), float(ys.max())
    span = (ymax - ymin) or 1.0
    W, H, PAD = 900, 220, 8
    pts = []
    for i, y in enumerate(ys):
        x = PAD + i * (W - 2 * PAD) / max(len(ys) - 1, 1)
        yy = H - PAD - (y - ymin) / span * (H - 2 * PAD)
        pts.append(f"{x:.1f},{yy:.1f}")
    svg = (f'<svg viewBox="0 0 {W} {H}" class="navchart">'
           f'<polyline points="{" ".join(pts)}" fill="none" '
           f'stroke="#FF0000" stroke-width="2"/>'
           f'<text x="{PAD}" y="{H-PAD+14}" fill="#8B949E" font-size="11">'
           f'{seg["date"].iloc[0]}（净值 {ys[0]:.2f}）</text>'
           f'<text x="{W-PAD-60}" y="{H-PAD+14}" fill="#8B949E" font-size="11">'
           f'{seg["date"].iloc[-1]}（净值 {ys[-1]:.2f}）</text></svg>')

    rows_cand = ""
    for c in cands[:20]:
        if c["score"] >= 3:
            g = "background:#161B22"
        elif c["date"] == last_date:               # 今日新信号高亮（琥珀）
            g = "background:#3D2A15"
        else:
            g = ""
        ma_txt = f"{c['px']/c['ma20']-1:+.1%}" if c["ma20"] == c["ma20"] else "—"
        today = (" <b style='color:#E3B341'>【今日新信号】</b>" if
                 c["date"] == last_date else "")
        rows_cand += (f'<tr style="{g}"><td>{_nc(names.get(c["code"], ""), c["code"])}</td>'
                      f'<td>{_esc(theme_map.get(c["code"], ""))}</td>'
                      f'<td>{c["date"]}{today}</td><td>{c["cnt"]}</td>'
                      f'<td><b>{c["score"]}</b></td>'
                      f'<td>{c["px"]:.2f}</td><td>{ma_txt}</td></tr>')

    bb_rows = ""
    for c in today_buys:
        bb_rows += (f'<tr><td>{_nc(names.get(c["code"], ""), c["code"])}</td>'
                    f'<td>{_esc(theme_map.get(c["code"], ""))}</td>'
                    f'<td><b>{c["score"]}</b></td><td>{c["cnt"]}</td>'
                    f'<td>信号日收盘买入（涨停则T+1开盘买）（1/3 仓，评分降序最多 3 只）</td></tr>')
    sell_rows = ""
    for _, r in today_sells.iterrows():
        color = "#F85149" if r["ret"] > 0 else "#3FB950"   # 国内红涨绿跌
        sell_rows += (f'<tr><td>{_nc(names.get(r["code"], ""), r["code"])}</td>'
                      f'<td>{_esc(theme_map.get(r["code"], ""))}</td>'
                      f'<td>{r["entry_date"]}</td><td>{r["exit"]:.2f}</td>'
                      f'<td style="color:{color}">{r["ret"]:+.1%}</td>'
                      f'<td>收盘跌破 MA20 卖出</td></tr>')
    hold_rows = ""
    for _, r in holds.iterrows():
        px, ma = _latest(r["code"], "close"), _latest(r["code"], "ma20")
        px_t = "—" if px is None else f"{px:.2f}"
        if px is not None and ma is not None and px < ma:
            st = '<span class="warn">⚠ 破MA20 → 收盘确认卖出</span>'
        elif px is not None and ma is not None:
            st = '<span class="ok">守MA20</span>'
        else:
            st = "—"
        ma_txt = "—" if (px is None or ma is None) else f"{px/ma-1:+.1%}"
        hold_rows += (f'<tr><td>{_nc(names.get(r["code"], ""), r["code"])}</td>'
                      f'<td>{_esc(theme_map.get(r["code"], ""))}</td>'
                      f'<td>{r["entry_date"]}</td><td>{r["entry"]:.2f}</td>'
                      f'<td>{px_t}</td><td>{ma_txt}</td><td>{st}</td></tr>')

    rows_tr = ""
    # 未平仓（open=1）置顶显示"持仓中"；已平仓按卖出日倒序
    trs_open = tw[tw["open"] == 1] if len(tw) else tw.iloc[0:0]
    trs_closed = (tw[tw["open"] == 0]
                  .sort_values("exit_date", ascending=False)
                  if len(tw) else tw.iloc[0:0])
    trs = pd.concat([trs_open, trs_closed]).head(20)
    for _, r in trs.iterrows():
        color = "#F85149" if r["ret"] > 0 else "#3FB950"   # 国内红涨绿跌
        if r["open"] == 1:
            exit_txt, exit_px = "持仓中", "—"
        else:
            exit_txt, exit_px = r["exit_date"], f"{r['exit']:.2f}"
        rows_tr += (f'<tr><td>{_esc(r["code"])}</td>'
                    f'<td>{_esc(names.get(r["code"], ""))}</td>'
                    f'<td>{r["entry_date"]}</td><td>{r["entry"]:.2f}</td>'
                    f'<td>{exit_txt}</td><td>{exit_px}</td>'
                    f'<td style="color:{color}">{r["ret"]:+.1%}</td>'
                    f'<td>{r["peak_gain"]:+.0%}</td>'
                    f'<td>{r.get("score", "")}</td></tr>')

    # 周期状态卡（读 cycle_state.json，门户注入；无文件/异常则空）
    cycle_card_html = ""
    try:
        import json as _json
        from mainrise import cycle_state as _cs
        _p = paths.state_dir() / "cycle_state.json"
        if _p.exists():
            _st = _json.loads(_p.read_text(encoding="utf-8"))
            if not _st.get("error"):
                _color = _st.get("color") or "#8B949E"
                _ml = "、".join(f'{t["theme"]}({t["stage"]} {t["xs60"]:+.0f}%)'
                                for t in (_st.get("mainline") or [])) or "无（超额为负）"
                _rows = "".join(
                    f'<tr><td style="color:#E6EDF3">{r["theme"]}</td>'
                    f'<td>{r["ret60"]:+.0f}%</td><td>{r["ret120"]:+.0f}%</td>'
                    f'<td>{r["xs60"]:+.0f}%</td><td>{r["stage"]}</td></tr>'
                    for r in _st.get("themes", []))
                cycle_card_html = (
                    f'<div class="card"><h2>市场周期状态'
                    f'<span style="font-size:11px;color:#8B949E;font-weight:400;'
                    f'margin-left:8px">{_st.get("date", "")} · 描述当前主线，'
                    f'不预测切换</span></h2>'
                    f'<div><span class="{"warn" if _st.get("level", "L0") in ("L2", "L3") else "ok"}">'
                    f'{_st.get("level", "")} {_st.get("level_name", "")}</span>'
                    f' ｜ 主线：{_ml} ｜ 模型吻合 '
                    f'{_st.get("match", 0)}/{len(_st.get("model_themes", []) or [])}</div>'
                    f'<div class="bdline">{_st.get("advice", "")}</div>'
                    f'<div class="wrap"><table>'
                    f'<tr><th>主题</th><th>60日</th><th>120日</th>'
                    f'<th>超额60</th><th>阶段</th></tr>{_rows}</table></div>'
                    f'<div class="note" style="margin-top:6px">口径：同花顺全A行业指数'
                    f'（2022-01 起）归一化等权均值；超额=主题-上证；阶段=120日方向'
                    f'+60日涨幅历史分位；档位=主线∩固定热主题吻合度（L0 满仓/L1 评分'
                    f'≥3/L2 半仓/L3 停开，杀跌区直接 L3）。研究结论：方向无可靠领先'
                    f'信号，本卡仅描述状态。</div></div>')
    except Exception:  # noqa: BLE001
        cycle_card_html = ""


    # 供需涨价（期货见顶回落）→ 有色主题标签：读 industry_price.json 判断
    price_warn_themes = set()
    price_warn_detail = ""
    try:
        import json as _json3
        _p3 = paths.state_dir() / "industry_price.json"
        if _p3.exists():
            _pj = _json3.loads(_p3.read_text(encoding="utf-8"))
            _pwarn = _pj.get("metal_warn") or []
            if _pwarn:
                price_warn_themes = {"有色"}
                price_warn_detail = "（" + "、".join(_pwarn) + " 期货见顶回落，供需缓解）"
    except Exception:  # noqa: BLE001
        pass
    # 产业链涨价事件标签（读 price_events.json）→ 各主题涨价状态
    price_event_labels = {}
    price_event_card_html = ""
    try:
        import json as _json4
        _p4 = paths.state_dir() / "price_events.json"
        if _p4.exists():
            _pe = _json4.loads(_p4.read_text(encoding="utf-8"))
            for th, t in (_pe.get("themes") or {}).items():
                if t.get("label") and t["label"] != "平静":
                    price_event_labels[th] = t["label"]
            # 产业链涨价追踪卡：分产品事件明细 + 主题标签
            _pe_rows = ""
            _pe_color = {"量价齐升": "#3FB950", "持续涨价": "#39D2C0",
                         "价格见顶": "#F85149", "启动": "#D29922",
                         "平静": "#8B949E"}
            for th, t in sorted((_pe.get("themes") or {}).items(),
                                key=lambda x: -x[1].get("n90", 0)):
                if t.get("n90", 0) == 0:
                    continue
                _c = _pe_color.get(t.get("label", ""), "#E6EDF3")
                _pe_rows += (
                    f'<tr><td style="color:{_c}">'
                    f'{"⭐" if th in ("AI硬件", "半导体", "存储") else ""}{th}</td>'
                    f'<td>{t.get("n90", 0)}</td><td>{t.get("n30", 0)}</td>'
                    f'<td style="color:{_c}">{t.get("label", "")}</td>'
                    f'<td style="color:#8B949E;font-size:11px">{t.get("detail", "")}</td></tr>')
            if _pe_rows:
                price_event_card_html = (
                    f'<div class="card"><h2>产业链涨价追踪'
                    f'<span style="font-size:11px;color:#8B949E;font-weight:400;'
                    f'margin-left:8px">{_pe.get("date", "")} · 东财/腾讯资讯'
                    f'产品名+涨价 近90天新闻</span></h2>'
                    f'<div class="wrap"><table>'
                    f'<tr><th>主题</th><th>90天</th><th>30天</th><th>标签</th>'
                    f'<th>产品事件（名=条数）</th>'
                    f'</tr>{_pe_rows}</table></div>'
                    f'<div class="note" style="margin-top:6px">口径：按产品名+涨价'
                    f'搜索（光纤/覆铜板/MLCC/HBM/DDR4 等卡点产品），标题含产品词+'
                    f'涨价意图词计数；标签=量价齐升（持续涨价+主题指数20日>0）/'
                    f'持续涨价/价格见顶（30天无新）/启动；⭐=模型固定热主题。</div>'
                    f'</div>')
    except Exception:  # noqa: BLE001
        pass
    # 产业景气度卡（读 industry_trend.json，门户注入；无文件/异常则空）
    industry_card_html = ""
    try:
        import json as _json2
        _p2 = paths.state_dir() / "industry_trend.json"
        if _p2.exists():
            _it = _json2.loads(_p2.read_text(encoding="utf-8"))
            if _it.get("themes"):
                def _itxt(r):
                    return ("—" if r.get("value") is None
                            else f"{r['value']:+.0f}%{r.get('tag', '')}")
                def _ilab(r):
                    lab = r.get("label", "")
                    if r["theme"] in price_warn_themes:
                        lab = f'<span style="color:#F85149">⚠{lab}·见顶回落</span>'
                    pe = price_event_labels.get(r["theme"])
                    if pe:
                        pe_color = {"量价齐升": "#3FB950", "持续涨价": "#39D2C0",
                                    "价格见顶": "#F85149", "启动": "#D29922"}
                        c = pe_color.get(pe, "#E6EDF3")
                        lab = f"{lab}<span style='color:{c}'>{'｜' if lab else ''}{pe}</span>"
                    return lab
                _irows = "".join(
                    f'<tr><td style="color:{"#F85149" if r["theme"] in ("AI硬件", "半导体", "存储") else "#E6EDF3"}">'
                    f'{"⭐" if r["theme"] in ("AI硬件", "半导体", "存储") else ""}{r["theme"]}</td>'
                    f'<td>{_itxt(r)}</td><td>{r.get("trend", "")}</td>'
                    f'<td>{_ilab(r)}</td></tr>'
                    for r in _it["themes"])
                industry_card_html = (
                    f'<div class="card"><h2>产业景气度'
                    f'<span style="font-size:11px;color:#8B949E;font-weight:400;'
                    f'margin-left:8px">{_it.get("date", "")} · 单季净利同比，'
                    f'*=业绩预告</span></h2>'
                    f'<div class="wrap"><table>'
                    f'<tr><th>主题</th><th>最新增速</th><th>环比</th><th>状态</th>'
                    f'</tr>{_irows}</table></div>'
                    f'<div class="note" style="margin-top:6px">口径：主题龙头归母净利'
                    f'单季同比中位数（累计差分）；* = 业绩预告（比财报早 1-3 个月，'
                    f'无财报期填补）；⭐ = 模型固定热主题。产业信号做"确认/否决"'
                    f'（高位确认持有、连续下滑否决换入），价格信号做交易。</div></div>')
    except Exception:  # noqa: BLE001
        industry_card_html = ""

    css = """
:root{--bg:#0D1117;--sf:#161B22;--teal:#39D2C0;--blue:#58A6FF;--grn:#3FB950;
--red:#F85149;--amber:#D29922;--txt:#E6EDF3;--sub:#8B949E;--ft:#6E7681;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 Consolas,Menlo,monospace;
padding:16px;max-width:1080px;margin:0 auto}
h1{color:var(--teal);font-size:22px;margin-bottom:4px}
.nav{margin:8px 0 12px;display:flex;flex-wrap:wrap;gap:8px}
.nav a{background:var(--sf);border:1px solid #21262D;border-radius:6px;
color:var(--blue);font-size:12.5px;padding:5px 12px;text-decoration:none}
.nav a:hover{border-color:var(--blue)}
.sub{color:var(--sub);font-size:12px;margin-bottom:16px}
.card{background:var(--sf);border:1px solid #21262D;border-radius:8px;
padding:14px 16px;margin-bottom:14px}
h2{color:var(--blue);font-size:15px;margin-bottom:10px;border-bottom:1px solid #21262D;
padding-bottom:6px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;
margin-bottom:14px}
.kpi{background:var(--sf);border:1px solid #21262D;border-radius:8px;padding:10px}
.kpi .v{font-size:22px;font-weight:bold;color:#FFF}
.kpi .l{color:var(--sub);font-size:11px;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--sub);text-align:left;padding:6px 8px;border-bottom:1px solid var(--blue);
font-weight:bold}
td{padding:6px 8px;border-bottom:1px solid #21262D}
tr:nth-child(even) td{background:rgba(22,27,34,.5)}
.wrap{overflow-x:auto}
.warn{color:var(--red);font-weight:bold}
.ok{color:var(--grn)}
.navchart{width:100%;height:auto;background:var(--bg);border:1px solid #21262D;
border-radius:6px}
.bdline{margin:12px 0 6px;font-size:13px}
.foot{color:var(--ft);font-size:11px;margin-top:18px;padding-top:10px;
border-top:1px solid #21262D}
ul{margin:6px 0 0 18px}li{margin:3px 0}
@media(max-width:640px){.kpis{grid-template-columns:repeat(3,1fr)}}
"""
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>大牛模型 · 主升浪跟踪</title><style>{css}</style></head><body>
<h1>大牛模型 · 主升浪跟踪</h1>
<div class="nav">
<a href="reports.html">📄 每日报告</a>
<a href="live.html">📈 实时盯盘</a>
<a href="review.html">📊 当日复盘</a>
<a href="dashboard.html">📋 KPI 仪表盘</a>
<a href="about.html">❓ 使用说明</a>
</div>
<div class="sub">全市场 + 固定热主题 + 同日Top1 ｜ 硬规则·评分≥2 + MA20退出/洗盘8天止损 + 杀跌区停开 + 追高弱市禁入 ｜ 数据截止 {last_date} ｜
完整回测 2021-08 ~ {last_date}（本页曲线与交割为 {PORTAL_REBASE} 起重置口径）｜
研究线索，不构成投资建议</div>

<div class="card"><h2>买卖点提醒（{last_date}）</h2>
{ignite_card_html}
<div class="bdline"><b style="color:#3FB950">🟢 今日买点</b>
（信号日收盘买入（涨停则T+1开盘买） · 1/3 仓 · 评分降序最多 3 只{(' · <span class="warn">⚠ 杀跌区 → 今日停开新仓</span>' if weak else '')}）</div>
<div class="wrap"><table>
<tr><th>名称/代码</th><th>主题</th><th>评分</th><th>90日T0</th><th>提示</th></tr>
{bb_rows or '<tr><td colspan="5">今日无硬规则信号（无买点）。</td></tr>'}
</table></div>
<div class="bdline"><b style="color:#F85149">🔴 今日卖出（收盘跌破 MA20 / 洗盘8天未收复）</b></div>
<div class="wrap"><table>
<tr><th>名称/代码</th><th>主题</th><th>买入日</th><th>卖出价</th><th>收益</th><th>原因</th></tr>
{sell_rows or '<tr><td colspan="6">今日无持仓触发卖出。</td></tr>'}
</table></div>
<div class="bdline"><b style="color:#D29922">📊 当前持仓（{len(holds)} 只）</b></div>
<div class="wrap"><table>
<tr><th>名称/代码</th><th>主题</th><th>买入日</th><th>成本</th><th>现价</th><th>距MA20</th><th>状态</th></tr>
{hold_rows or '<tr><td colspan="7">当前空仓。</td></tr>'}
</table></div></div>

<div class="card"><h2>市场状态</h2>
<div><span class="{('warn' if weak else 'ok')}">{mkt_state}</span>
（大盘等权 20 日涨幅 {mkt_txt}；≤-5% 为杀跌区 → 不新开仓）</div>
{('' if hot_themes is None else f'<div class="bdline">🔥 当前热主题'
   f'（{"动态 · 同花顺全A行业指数均线趋势" if HOT_MODE == "dynamic" else "固定 · AI硬件/半导体/存储"}）：'
   f'<b style="color:#E3B341">{" / ".join(hot_themes)}</b></div>')}</div>

{cycle_card_html}
{industry_card_html}
{price_event_card_html}

<div class="kpis">
<div class="kpi"><div class="v">{total:+.0%}</div><div class="l">总收益（{PORTAL_REBASE} 起）</div></div>
<div class="kpi"><div class="v">{cagr:+.0%}</div><div class="l">年化</div></div>
<div class="kpi"><div class="v">{dd:.0%}</div><div class="l">最大回撤</div></div>
<div class="kpi"><div class="v">{pf:.2f}</div><div class="l">盈亏比 PF</div></div>
<div class="kpi"><div class="v">{win_txt}</div><div class="l">胜率</div></div>
<div class="kpi"><div class="v">{len(tw)}</div><div class="l">交易笔数（窗口）</div></div>
</div>

<div class="card"><h2>近期候选信号（最近 45 日，评分≥2 硬规则，按代码去重）</h2>
<div class="wrap"><table>
<tr><th>名称/代码</th><th>主题</th><th>信号日</th><th>90日T0</th>
<th>评分</th><th>最新收盘</th><th>距MA20</th></tr>
{rows_cand or '<tr><td colspan="7">近 45 日无符合模型硬规则的信号（保持空仓/持有纪律）</td></tr>'}
</table></div></div>

<div class="card"><h2>模型净值曲线（{PORTAL_REBASE} 起 · 净值 1.0 重置 · {len(seg)} 个交易日）</h2>{svg}</div>

<div class="card"><h2>最近交割记录（{PORTAL_REBASE} 起 · 倒序 · 20 笔）</h2>
<div class="wrap"><table>
<tr><th>代码</th><th>名称</th><th>买入日</th><th>买入价</th><th>卖出日</th>
<th>卖出价</th><th>收益</th><th>峰值</th><th>评分</th></tr>
{rows_tr or '<tr><td colspan="9">{PORTAL_REBASE} 起暂无平仓记录。</td></tr>'}
</table></div></div>

<div class="card"><h2>模型规则（固化版）</h2>
<ul>
<li><b>追踪范围</b>：全市场（排除 ST/停牌，约 4700 只）；热主题仍限固定三主题</li>
<li><b>入场</b>：信号日收盘买入（涨停则T+1开盘买）；硬规则={'动态热主题（同花顺全A行业指数均线趋势，2022-01 起，此前回退固定三主题）' if HOT_MODE == 'dynamic' else '热主题（AI硬件/半导体/存储）'} 且 90 日内 T0≥3；
大牛评分≥2（热主题/90日T0≥3/创60日新高且10日<30%/链长≥4，各+1）</li>
<li><b>仓位</b>：单票 1/3 仓，最多 3 只并行</li>
<li><b>退出</b>：收盘跌破 MA20 卖出；或买入后连续 8 个交易日未收复买入价（洗盘止损）；不手动买回，等规则再次触发再入场</li>
<li><b>弱市降档</b>：大盘 20 日 ≤-5%（杀跌区）不新开仓</li>
<li><b>追高弱市禁入</b>：信号日 20 日涨幅 ≥60% 且 大盘 20 日 ≤0% → 不追（2026-08-14 研究固化）</li>
<li><b>费用</b>：0.2%/笔（双边）</li>
</ul></div>

<div class="foot">本页 KPI/曲线/交割为 <b>{PORTAL_REBASE} 起重置口径</b>（净值 1.0 起）。
完整历史回测 2021-08 ~ {last_date}：总收益 {total_full:+.0%}、年化 {cagr_full:+.0%}、
最大回撤 {dd_full:.0%}、PF {pf_full:.2f}、胜率 {win_full:.0%}、{len(tr)} 笔
（全平仓，详见 大牛模型回测_*.md）。2021-2024 弱年回撤深，按三态轮动降档。
研究线索，不构成投资建议。</div>
</body></html>"""
    web_dir = Path(out_dir) if out_dir else paths.web_dir()
    web_dir.mkdir(parents=True, exist_ok=True)
    out = web_dir / PORTAL_NAME
    out.write_text(html, encoding="utf-8")
    return str(out)


def trades_to_csv_frame(tr: pd.DataFrame, names: dict, theme_of: dict) -> pd.DataFrame:
    """交割单 → 导出 DataFrame（中文表头：代码/名称/主题 + 买卖字段）。"""
    tr2 = tr.copy()
    tr2["名称"] = tr2["code"].map(names)
    tr2["主题"] = tr2["code"].map(theme_of)
    tr2 = tr2.rename(columns={"code": "代码",
                              "entry_date": "买入日期", "entry": "买入价",
                              "exit_date": "卖出日期", "exit": "卖出价",
                              "ret": "收益率", "peak_gain": "峰值收益率",
                              "hold": "持仓天数", "open": "状态",
                              "via": "入场方式"})
    tr2["状态"] = tr2["状态"].map({0: "已平仓", 1: "未平仓"})
    return tr2


def run() -> str:
    t0 = time.time()
    print("加载行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    # 用户权限：301（创业板注册制新股）/688（科创板）不可交易，两种口径都排除
    full = full[~full["code"].astype(str).str.startswith(("301", "688"))]
    if MARKET_SCOPE == "all":
        panels = full.copy()          # 全市场（2026-08-15 固化，排除 301/688）
        print(f"范围：全市场（排除 301/688）{panels['code'].nunique()} 只，"
              f"{len(panels):,} 行")
    else:
        ck = {c for c in load_chokepoint_codes()
              if not c.startswith("301") and not c.startswith("688")}
        panels = full[full["code"].isin(ck)].copy()
        print(f"范围：卡点企业 {len(ck)} 家（排除 301/688，300 可交易），"
              f"{len(panels):,} 行")

    mkt = market_features(full)
    mkt_ret20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))

    try:
        from mainrise.signals import load_names
        names = load_names()
    except Exception:  # noqa: BLE001
        names = {}
    theme_map = bigtrend.load_theme()
    hot_set = {c for c, th in theme_map.items() if th in bigtrend.HOT_THEMES}

    # 热主题模式：dynamic = 全A行业指数（同花顺）月线级排名 Top-N 判定（无前视）；
    # static = 固定三主题。行业指数覆盖前（2022 前）与拉取失败 → 回退静态热主题。
    hot_by_date = None
    if HOT_MODE == "dynamic":
        from mainrise import themeindex as ti
        hs, miss = ti.hot_themes_series(refresh=False, ma_short=HOT_MA_SHORT,
                                        ma_long=HOT_MA_LONG, slope=HOT_SLOPE,
                                        min_days=HOT_MIN_DAYS,
                                        rank_top=HOT_RANK_TOP,
                                        lookback_days=HOT_LOOKBACK_DAYS)
        if hs:
            hot_by_date = ti.hot_codes_by_date(hs, theme_map)
            if HOT_PRE_FALLBACK == "static":
                first = min(hs)
                for d0 in panels.loc[panels["date"] < first, "date"].unique():
                    hot_by_date.setdefault(str(d0), set()).update(hot_set)
            today_hot = hs.get(str(panels["date"].max())) or []
            print(f"热主题(同花顺全A行业指数): 共 {len(hs)} 个交易日"
                  f"（覆盖 {first} 起，此前回退静态）; 今日 "
                  f"{panels['date'].max()} → {today_hot or '—'}；"
                  f"缺失板块 {miss or '无'}")
        else:
            print(f"⚠ 行业指数不可用（缺失 {miss or '全部'}），回退静态热主题")
    if hot_by_date is None:
        today_hot = [t for t in bigtrend.HOT_THEMES if t in bigtrend.THEMES]
    del full
    info = pb.build_info(panels, hot_set, pb.MIN_T0_90)

    sim = pb.simulate(info, hot_set, MAX_POS, mkt_ret20=mkt_ret20,
                      downshift=DOWNSHIFT, exit_ma=EXIT_MA, rebuy="none",
                      score_min=SCORE_MIN, hard_rule=True,
                      hot_by_date=hot_by_date,
                      ban_overbought_weak=BAN_OVERBOUGHT_WEAK,
                      wash_exit_days=WASH_EXIT_DAYS,
                      top1=TOP1_DAILY)
    tr = sim["trades"].sort_values("entry_date")
    nav = sim["nav"]
    total = nav["nav"].iloc[-1] / nav["nav"].iloc[0] - 1
    cagr = (nav["nav"].iloc[-1] / nav["nav"].iloc[0]) ** (252 / len(nav)) - 1
    dd = (nav["nav"] / nav["nav"].cummax() - 1).min()
    closed = tr[tr["open"] == 0]
    crets = closed["ret"] if len(closed) else pd.Series(dtype=float)
    win = (crets > 0).mean() if len(crets) else np.nan
    pos_sum = crets[crets > 0].sum() if len(crets) else 0
    neg_sum = abs(crets[crets <= 0].sum()) if len(crets) else 0
    pf = pos_sum / neg_sum if neg_sum > 0 else 99.0
    big_n = int((tr["peak_gain"] >= 0.60).sum())

    theme_of = {c: th for c, th in theme_map.items()}

    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    hot_txt = ("动态热主题（同花顺全A行业指数均线趋势，2022-01 起，此前回退固定三主题）"
               if HOT_MODE == "dynamic"
               else "热主题（AI硬件/半导体/存储，固定）")
    L: list = []
    L.append(f"# 大牛模型回测（固化版，{dstr}）")
    L.append("")
    L.append(f"> **模型规格**（{hot_txt}，2026-08-14 用户确认）：")
    L.append("> 1. 范围：全市场（排除 ST/停牌，约 4700 只）——2026-08-15 由卡点企业 97 只放宽，热主题仍限固定三主题")
    L.append(f"> 2. 入场：信号日收盘买入（涨停则T+1开盘买）；硬规则={hot_txt} 且 90日T0≥3；"
             "大牛评分≥2（热主题/90日T0≥3/创60日新高且10日<30%/链长≥4，各+1 分）")
    L.append("> 3. 仓位：单票 1/3 仓，最多 3 只并行；同日多信号只买评分最高 1 只（Top1，2026-08-15 固化）")
    L.append("> 4. 退出：收盘跌破 MA20 卖出；或买入后收盘价连续 8 个交易日未收复买入价（洗盘止损）；")
    L.append(">    不手动买回，等规则再次触发再入场")
    L.append("> 5. 弱市降档：大盘等权 20 日涨幅 ≤-5%（杀跌区）不新开仓")
    L.append("> 6. 费用 0.2%/笔；数据 2021-08 ~ 2026-08-12（zzshare 日线）")
    L.append("")
    L.append("## 一、模型表现")
    L.append("")
    L.append(f"| 总收益 | 年化 | 最大回撤 | PF | 胜率 | 交易数 | 抓到≥60% |")
    L.append(f"| --- | --- | --- | --- | --- | --- | --- |")
    L.append(f"| {total:+.0%} | {cagr:+.0%} | {dd:.0%} | {pf:.2f} | "
             f"{win if pd.isna(win) else f'{win:.0%}'} | {len(tr)} | {big_n} |")
    L.append("")
    nav2 = nav.copy()
    nav2["year"] = nav2["date"].str[:4]
    L.append("| 年份 | 年末净值 | 年收益 | 最大回撤 |")
    L.append("| --- | --- | --- | --- |")
    for yr, g in nav2.groupby("year"):
        y0, y1 = g["nav"].iloc[0], g["nav"].iloc[-1]
        ddg = (g["nav"] / g["nav"].cummax() - 1).min()
        L.append(f"| {yr} | {y1:.2f} | {y1/y0-1:+.0%} | {ddg:.0%} |")
    L.append("")

    L.append("## 二、交割清单（全部交易）")
    L.append("")
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
        L.append("## 三、宏和科技持仓段")
        L.append("")
        L.append("| 买入日 | 买入价 | 卖出日 | 卖出价 | 收益 | 峰值 | 评分 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in hh.iterrows():
            L.append(f"| {r['entry_date']} | {r['entry']:.2f} | "
                     f"{r['exit_date']} | {r['exit']:.2f} | {r['ret']:+.0%} | "
                     f"{r['peak_gain']:+.0%} | {r.get('score', '')} |")
        L.append("")

    L.append("## 四、模型说明与纪律")
    L.append("")
    L.append("1. **这是回测口径**：交割单为模拟（初始资金 1.0 折算股数），"
             "实盘按 1/3 仓 × 当前净值下单，最多 3 只并行。")
    L.append("2. **弱市降档**：杀跌区（大盘 20 日 ≤-5%）不新开仓；已有持仓按 MA20 纪律退出。")
    L.append("3. **不做企稳买回**：卖出后等规则（评分≥2 的硬规则信号）再次触发才入场"
             "（研究结论：企稳买回为负优化）。")
    L.append("4. **2021-2024 弱年**：回撤深（2022 年 -15%），按三态轮动降档。")
    L.append("5. **免责**：研究线索，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"大牛模型回测_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    nav.to_csv(paths.report_dir() / f"大牛模型净值_{dstr}.csv",
               index=False, encoding="utf-8-sig")
    tr2 = trades_to_csv_frame(tr, names, theme_of)
    jg_path = paths.report_dir() / f"大牛模型交割单_{dstr}.csv"
    tr2.to_csv(jg_path, index=False, encoding="utf-8-sig")
    # 候选落盘（盯盘页实时读取 + 17:30 收盘确认推送，避免 monitor/push 全量重算）
    try:
        last_date = panels["date"].max()
        cands = model_candidates(info, hot_set, last_date=last_date,
                                 hot_by_date=hot_by_date, mkt_ret20=mkt_ret20,
                                 ban_overbought_weak=BAN_OVERBOUGHT_WEAK)
        # 大牛模型当前持仓（交割单未平仓 + 最新 MA20/收盘，供盘中按 MA20 监控卖出）
        holdings = []
        for _, r in tr[tr["open"] == 1].iterrows():
            code = str(r["code"])
            v = info.get(code)
            ma20 = (float(v["ma20"][-1]) if v is not None
                    and np.isfinite(v["ma20"][-1]) else None)
            px = float(v["close"][-1]) if v is not None else None
            holdings.append({"code": code, "name": names.get(code, ""),
                             "theme": theme_of.get(code, ""),
                             "entry_date": str(r["entry_date"]),
                             "entry": float(r["entry"]),
                             "px": px, "ma20": ma20,
                             "ret": float(r["ret"]),
                             "score": int(r.get("score") or 0)})
        cj = write_cands_json(cands, last_date,
                              mkt_ret20=mkt_ret20.get(last_date),
                              holdings=holdings,
                              hot_themes=today_hot)
        print(f"候选JSON: {cj}（{len(cands)} 条候选, {len(holdings)} 持仓, "
              f"热主题 {today_hot}）")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 候选落盘失败（不影响回测）: {e}")
        last_date = panels["date"].max()
    # 门户：只展示大牛模型有效信息（替换 output/web/index.html）
    try:
        portal = write_portal(tr, nav, info, hot_set, mkt_ret20, names,
                              theme_map, last_date,
                              hot_by_date=hot_by_date,
                              hot_themes=today_hot)
        print(f"门户: {portal}")
        # 每日报告列表页 + 详情页（bigbull 重做门户后旧 reports.html 不再
        # 生成；此处复用 web_dashboard 渲染，保证门户"每日报告"入口可用）
        try:
            from mainrise import web_dashboard as _wd
            _rpt = paths.report_dir()
            _view = paths.web_dir() / "reports_view"
            _view.mkdir(exist_ok=True)
            for _p in (sorted(_rpt.glob("主升浪跟踪_*.md"))
                       + sorted(_rpt.glob("主升浪信号综合评估_*.md"))
                       + sorted(_rpt.glob("信号评估_*.md"))):
                _title = _p.stem.replace("_", " ").replace("主升浪跟踪", "每日跟踪")
                (_view / f"{_p.stem}.html").write_text(
                    _wd._render_report_page(_title, _p.read_text(encoding="utf-8")),
                    encoding="utf-8")
            (paths.web_dir() / "reports.html").write_text(
                _wd._render_reports_page(_rpt), encoding="utf-8")
            print(f"每日报告列表: {paths.web_dir() / 'reports.html'}")
        except Exception as e:  # noqa: BLE001  报告渲染失败不影响门户
            print(f"⚠ 每日报告渲染失败（不影响门户）: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 门户生成失败（不影响回测）: {e}")
    print(f"大牛模型回测完成（{time.time()-t0:.0f}s）：{md_path}")
    print(f"交割清单: {jg_path}")
    return str(md_path)


def main() -> None:
    run()


if __name__ == "__main__":
    main()
