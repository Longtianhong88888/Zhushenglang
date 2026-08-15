"""市场周期状态卡：描述"当前主线在哪、走到哪个阶段"（不预测切换）。

设计原则（前序五项研究结论：风格检测/热主题动态化/大周期追溯/机构调研/研报
检索全部证明"方向无可靠领先信号"）：
- **只描述、不预测**：输出当前主线（相对全市场持续超额的行业主题）、所处阶段
  （启动/主升/高潮/退潮）、模型热主题吻合度与档位建议——全部是已发生的状态，
  不产生新的买卖信号，避免负优化；
- **无未来函数**：全部用截至今日的收盘数据计算。

数据源（全部已有缓存，不联网）：
- 主题行业指数：themeindex 缓存 output/state/ths_kline/*.csv（同花顺全A行业
  指数 2022-01 起，8 主题 → 粗行业指数归一化等权均值）
- 大盘基准：上证指数 output/state/cycle_ths/000001.SH.csv（cycle-check 缓存）
- 大盘状态：market_state.json（杀跌区/结构 diff，收盘口径）

阶段判定（主题指数 60 日涨幅 vs 自身历史分位 + 超额方向）：
  高潮  = 60日涨幅历史分位 >75% 或 (分位>50% 且 20日超额转负)
  主升  = 分位 50~75% 且 20日超额>0
  启动  = 分位 25~50% 且 60日超额>0
  退潮  = 分位 <25% 或 60日超额<0 且 20日超额<0
主线 = 120日超额>0 且 60日超额>0 且 60日超额排名前3（要求持续性）。

输出：
- output/state/cycle_state.json（盯盘页/门户读取）
- output/web/cycle.html（独立页，Quant Dark，可嵌入/导航）
- HTML 卡片段（供 monitor.render_live_html 追加到大盘状态卡下方）

用法:
    python3 -m mainrise.cycle_state             # 计算 + 写 JSON + 独立页 + 打印卡
    python3 -m mainrise.cycle_state --json-only # 只写 JSON
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import paths, themeindex
from mainrise import bigtrend

CN_TZ = timezone(timedelta(hours=8))

MODEL_THEMES = {"AI硬件", "半导体", "存储"}   # 大牛模型固定热主题（static）
STAGE_ORDER = ["启动", "主升", "高潮", "退潮"]
LEVELS = {
    "L0": ("正常", "模型热主题与主线吻合，满仓运行（现规则）", "#3FB950"),
    "L1": ("预警", "主线与模型主题部分脱节：建议评分门槛 ≥2→≥3（胜率 44%→51%、"
           "回撤 -34%→-32%，牺牲收益换质量）", "#D29922"),
    "L2": ("收缩", "主线与模型主题脱节：建议新开仓减半（simulate downshift='half'）",
           "#F85149"),
    "L3": ("暂停", "大盘杀跌区或主线全面退潮：停开新仓，等风格回归（现有杀跌区规则）",
           "#F85149"),
}


def _theme_series() -> pd.DataFrame:
    """8 主题 → 行业指数归一化等权均值（读 themeindex 缓存，不联网）。"""
    idxs = {}
    for theme, boards in themeindex.THEME_INDEXES.items():
        cols = []
        for code, _name in boards:
            p = paths.state_dir() / "ths_kline" / f"{code}.csv"
            if not p.exists():
                continue
            try:
                s = pd.read_csv(p).set_index("date")["close"]
                if len(s) > 60:
                    cols.append(s / float(s.iloc[0]))
            except Exception:  # noqa: BLE001
                pass
        if cols:
            idxs[theme] = pd.concat(cols, axis=1).mean(axis=1)
    if not idxs:
        return pd.DataFrame()
    return pd.DataFrame(idxs).sort_index()


def _benchmark() -> pd.Series:
    """上证指数 close（cycle-check 缓存；缺则腾讯 300 根）。"""
    p = paths.state_dir() / "cycle_ths" / "000001.SH.csv"
    if p.exists():
        try:
            s = pd.read_csv(p).set_index("date")["close"]
            if len(s) > 120:
                return s
        except Exception:  # noqa: BLE001
            pass
    try:
        import requests
        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "sh000001,day,,,320,qfq"}, timeout=10)
        d = r.json()
        node = (d.get("data") or {}).get("sh000001") or {}
        bars = node.get("qfqday") or node.get("day") or []
        s = pd.Series({b[0]: float(b[2]) for b in bars}).sort_index()
        if len(s) > 120:
            return s
    except Exception:  # noqa: BLE001
        pass
    return pd.Series(dtype=float)


def _stage_of(s: pd.Series) -> str:
    """主题指数阶段：中期方向（120日）优先，其次 60日涨幅历史分位 + 20日方向。"""
    c = s.dropna()
    if len(c) < 130:
        return "数据不足"
    r20 = c / c.shift(20) - 1
    r60 = c / c.shift(60) - 1
    r120 = c / c.shift(120) - 1
    cur60 = float(r60.iloc[-1])
    xs20 = float(r20.iloc[-1])
    mid = float(r120.iloc[-1])
    pct = float((r60.iloc[:-1] < cur60).mean()) if len(r60) > 1 else 0.5
    if mid < 0:                          # 中期下行（120日负）
        return "启动" if xs20 > 0 else "退潮"   # 中期跌+短期反弹=超跌反弹窗口
    if cur60 <= 0:                       # 中期涨但近 60 日转负 → 退潮
        return "退潮"
    if pct > 0.75 or (pct > 0.5 and xs20 < 0):
        return "高潮"
    if 0.5 < pct <= 0.75:
        return "主升"
    if 0.25 < pct <= 0.5:
        return "启动"
    return "退潮"


def _state_on(idx: pd.DataFrame, bench: pd.Series, mkt20: float | None,
              date_label: str | None = None) -> dict:
    """对给定（截至某日的）主题指数 + 上证基准计算周期状态（无未来函数）。

    idx/bench 由调用方截断到目标日；mkt20 = 该日大盘等权 20 日涨幅（杀跌区）。
    """
    out = {}
    if idx.empty:
        out["error"] = "主题指数缓存缺失（先跑 mainrise themeindex --refresh）"
        return out
    if len(bench) < 120:
        out["error"] = "上证基准不足（先跑 mainrise cycle-check --fetch-only）"
        return out
    b60 = float(bench.iloc[-1] / bench.iloc[-60] - 1) if len(bench) >= 60 else np.nan
    b120 = float(bench.iloc[-1] / bench.iloc[-120] - 1) if len(bench) >= 120 else np.nan
    rows = []
    for theme in idx.columns:
        s = idx[theme].dropna()
        if len(s) < 130:
            continue
        r20 = float(s.iloc[-1] / s.iloc[-20] - 1) if len(s) >= 20 else np.nan
        r60 = float(s.iloc[-1] / s.iloc[-60] - 1) if len(s) >= 60 else np.nan
        r120 = float(s.iloc[-1] / s.iloc[-120] - 1) if len(s) >= 120 else np.nan
        vol20 = float(s.pct_change().rolling(20).std().iloc[-1])
        rows.append({
            "theme": theme,
            "ret20": r20, "ret60": r60, "ret120": r120,
            "xs60": r60 - b60 if b60 == b60 else np.nan,
            "xs120": r120 - b120 if b120 == b120 else np.nan,
            "stage": _stage_of(s),
            "vol20": round(vol20, 4),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        out["error"] = "主题指数数据不足"
        return out
    df = df.sort_values("xs60", ascending=False)

    cand = df[(df["xs120"] > 0) & (df["xs60"] > 0)]
    mainline = cand.head(3)
    mainline_list = [{"theme": r["theme"], "xs60": round(r["xs60"] * 100, 1),
                      "xs120": round(r["xs120"] * 100, 1),
                      "stage": r["stage"]}
                     for _, r in mainline.iterrows()]

    hit = [t for t in mainline_list if t["theme"] in MODEL_THEMES]
    match = len(hit)

    if mkt20 is not None and mkt20 <= -5:
        level, advice, color = "L3", LEVELS["L3"][1], LEVELS["L3"][2]
    elif match >= 2 and all(t["stage"] != "退潮" for t in mainline_list):
        level, advice, color = "L0", LEVELS["L0"][1], LEVELS["L0"][2]
    elif match == 1:
        level, advice, color = "L1", LEVELS["L1"][1], LEVELS["L1"][2]
    else:
        level, advice, color = "L2", LEVELS["L2"][1], LEVELS["L2"][2]

    out.update({
        "date": date_label or datetime.now(CN_TZ).strftime("%Y-%m-%d"),
        "bench_ret60": round(b60 * 100, 1) if b60 == b60 else None,
        "mkt_ret20": mkt20,
        "mainline": mainline_list,
        "match": match,
        "model_themes": sorted(MODEL_THEMES),
        "level": level, "level_name": LEVELS[level][0],
        "advice": advice, "color": color,
        "themes": [{"theme": r["theme"], "ret20": round(r["ret20"] * 100, 1),
                    "ret60": round(r["ret60"] * 100, 1),
                    "ret120": round(r["ret120"] * 100, 1),
                    "xs60": round(r["xs60"] * 100, 1),
                    "xs120": round(r["xs120"] * 100, 1),
                    "stage": r["stage"]} for _, r in df.iterrows()],
        "retreats": int((df["stage"] == "退潮").sum()),
        "highs": int(df["stage"].isin(("主升", "高潮")).sum()),
    })
    return out


def compute() -> dict:
    """计算当前周期状态 → dict（含 HTML 卡）。"""
    idx = _theme_series()
    bench = _benchmark()
    mkt = None
    try:
        st = json.loads((paths.state_dir() / "market_state.json")
                        .read_text(encoding="utf-8"))
        mkt = st.get("mkt_ret20")
    except Exception:  # noqa: BLE001
        pass
    out = _state_on(idx, bench, mkt)
    out.setdefault("date", datetime.now(CN_TZ).strftime("%Y-%m-%d"))
    out["note"] = "描述当前主线与阶段，不预测切换（研究结论：方向无可靠领先信号）"
    return out


def render_card(state: dict) -> str:
    """HTML 卡（Quant Dark，供盯盘页/独立页嵌入）。"""
    if state.get("error"):
        return (f'<section class="card"><h2>市场周期状态</h2>'
                f'<div class="body"><div class="empty">{state["error"]}'
                f'</div></div></section>')
    color = state.get("color") or "#8B949E"
    ml = state.get("mainline") or []
    ml_s = "、".join(f'<b style="color:#E6EDF3">{t["theme"]}</b>'
                     f'<span style="color:#8B949E">({t["stage"]}'
                     f' {t["xs60"]:+.0f}%)</span>' for t in ml) or "无（超额为负）"
    rows = "".join(
        f'<tr><td style="color:#E6EDF3">{r["theme"]}</td>'
        f'<td>{r["ret60"]:+.0f}%</td>'
        f'<td>{r["ret120"]:+.0f}%</td>'
        f'<td>{r["xs60"]:+.0f}%</td>'
        f'<td style="color:{("#F85149" if r["stage"] in ("主升", "高潮") else "#8B949E")}">'
        f'{r["stage"]}</td></tr>' for r in state.get("themes", []))
    return (f'<section class="card"><h2>市场周期状态'
            f'<span style="font-size:11px;color:#8B949E;margin-left:8px">'
            f'{state.get("date", "")} · 描述当前主线，不预测切换</span></h2>'
            f'<div class="body">'
            f'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
            f'<span style="background:{color}22;border:1px solid {color};'
            f'color:{color};border-radius:999px;padding:2px 14px;'
            f'font-weight:600">{state.get("level", "")} '
            f'{state.get("level_name", "")}</span>'
            f'<span style="color:#8B949E">主线：</span>{ml_s}</div>'
            f'<div style="margin-top:8px;color:#E6EDF3">{state.get("advice", "")}'
            f'</div>'
            f'<table style="width:100%;margin-top:10px;border-collapse:collapse">'
            f'<tr style="color:#8B949E;font-size:11px"><th align="left">主题</th>'
            f'<th>60日</th><th>120日</th><th>超额60</th><th>阶段</th></tr>'
            f'{rows}</table>'
            f'<div class="note">口径：同花顺全A行业指数（2022-01 起）归一化等权'
            f'均值；超额=主题-上证；阶段=60日涨幅历史分位+超额方向；'
            f'吻合度=主线∩模型固定热主题（AI硬件/半导体/存储）。'
            f'研究结论：方向无可靠领先信号，本卡仅描述状态。</div>'
            f'</div></section>')


def render_page(state: dict) -> str:
    """独立页 output/web/cycle.html。"""
    card = render_card(state)
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>市场周期状态</title>
<style>
body{{background:#0D1117;color:#E6EDF3;font-family:-apple-system,
"PingFang SC","Microsoft YaHei",sans-serif;margin:0;padding:16px}}
h1{{color:#39D2C0;font-size:18px;margin:0 0 12px}}
.card{{background:#161B22;border:1px solid #30363D;border-radius:10px;
padding:16px;margin-bottom:14px}}
.body{{font-size:13px;line-height:1.7}}
.note{{margin-top:10px;color:#6E7681;font-size:11px;line-height:1.6}}
table{{font-size:12px}} th,td{{padding:3px 8px;border-bottom:1px solid #21262D}}
a{{color:#58A6FF;text-decoration:none}}
.empty{{color:#8B949E}}
</style></head><body>
<h1>市场周期状态</h1>
{card}
<div class="note" style="margin-top:20px">研究用途，不构成投资建议 ·
数据：同花顺全A行业指数 + 上证指数（缓存）· 每日更新：mainrise cycle-state</div>
</body></html>"""


def write_outputs() -> str:
    """计算并落盘 JSON + cycle.html；返回卡 HTML。"""
    st = compute()
    paths.ensure_dirs()
    (paths.state_dir() / "cycle_state.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    web = paths.web_dir()
    web.mkdir(parents=True, exist_ok=True)
    (web / "cycle.html").write_text(render_page(st), encoding="utf-8")
    return render_card(st)


def backtest(step: int = 3, start: str = "2022-06-01") -> str:
    """逐日回放周期状态判定（无未来函数），验证能否识别历史暴跌区间。

    每 step 个交易日采样一次：用截至该日的主题指数/上证/大盘20日涨幅计算
    level/mainline/stage。输出 CSV + Markdown 报告（重点：2026-07 暴跌回顾）。
    """
    from mainrise.data import load_all_panels
    from mainrise.entry_study import market_features
    from mainrise.report import load_chokepoint_codes
    from mainrise.signals import in_universe

    print("加载行情（全市场等权 20 日 + 卡点池对照）...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    mkt = market_features(full)
    mkt20 = dict(zip(mkt["date"], mkt["mkt_ret20"]))
    ck = {c for c in load_chokepoint_codes()
          if not c.startswith("301") and not c.startswith("688")}

    p = full.assign(pct=full["pct_chg"].clip(-21, 21))
    p["grp"] = np.where(p["code"].isin(ck), "tech", "other")
    g = (p.groupby(["date", "grp"])["pct"].mean().unstack().sort_index())
    tech_nav = (g["tech"] / 100 + 1).cumprod()
    idx_all = g.index

    idx = _theme_series()
    bench = _benchmark()
    print("逐日回放周期状态...")
    rows = []
    for d in idx.index[::step]:
        if d < start:
            continue
        idxd = idx[idx.index <= d]
        benchd = bench[bench.index <= d]
        if len(idxd) < 130 or len(benchd) < 120:
            continue
        st = _state_on(idxd, benchd, mkt20.get(d), d)
        if st.get("error"):
            continue
        # 卡点池 60 日涨幅（截至 d）
        ck60 = np.nan
        hist = tech_nav[tech_nav.index <= d]
        if len(hist) > 60:
            ck60 = float(hist.iloc[-1] / hist.iloc[-61] - 1)
        rows.append({
            "date": d,
            "level": st["level"], "level_name": st["level_name"],
            "match": st["match"],
            "mainline": "、".join(t["theme"] for t in st["mainline"]) or "-",
            "mkt_ret20": st["mkt_ret20"],
            "retreats": st["retreats"], "highs": st["highs"],
            "ck_ret60": ck60,
        })
    df = pd.DataFrame(rows)
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    csv_path = paths.report_dir() / f"周期状态回放_{dstr}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    L: list = []
    L.append(f"# 周期状态卡回测：能否识别 2026-07 暴跌区间（{dstr}）")
    L.append("")
    L.append("> 方法：用截至每日的主题指数/上证/全市场等权20日涨幅，逐日回放周期"
             "状态判定（无未来函数，每 3 交易日采样）；对照卡点池等权 60 日涨幅。")
    L.append("> 口径：L0 满仓 / L1 评分≥3 / L2 半仓 / L3 停开（杀跌区=大盘20日≤-5%"
             " 直接 L3）；主线=120&60日超额>0 且排名前3；退潮=120日负或60日转负。")
    L.append("")
    df["isL3"] = df["level"] == "L3"
    groups, cur = [], []
    for _, r in df.iterrows():
        if r["isL3"]:
            cur.append(r["date"])
        elif cur:
            groups.append((cur[0], cur[-1], len(cur)))
            cur = []
    if cur:
        groups.append((cur[0], cur[-1], len(cur)))
    L.append("## 一、全期 L3（停开）时段")
    L.append("")
    L.append("| 开始 | 结束 | 采样点数 |")
    L.append("| --- | --- | --- |")
    for s_, e_, n in groups:
        L.append(f"| {s_} | {e_} | {n} |")
    L.append("")
    recent = df[df["date"] >= "2026-04-01"]
    L.append("## 二、2026-04 起逐日明细（对照卡点池）")
    L.append("")
    L.append("| 日期 | 档位 | 主线 | 吻合 | 大盘20日 | 退潮主题 | 卡点池60日 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for _, r in recent.iterrows():
        ck60 = f"{r['ck_ret60']*100:+.0f}%" if r["ck_ret60"] == r["ck_ret60"] else "-"
        mkt_s = f"{r['mkt_ret20']:+.1f}%" if r["mkt_ret20"] is not None else "-"
        L.append(f"| {r['date']} | **{r['level']}** {r['level_name']} | "
                 f"{r['mainline']} | {r['match']} | {mkt_s} | "
                 f"{r['retreats']} | {ck60} |")
    L.append("")
    L.append("## 三、2026-06-25 ~ 08-10 暴跌窗口逐点")
    L.append("")
    jul = recent[(recent["date"] >= "2026-06-25") & (recent["date"] <= "2026-08-10")]
    for _, r in jul.iterrows():
        mkt_s = f"{r['mkt_ret20']:+.1f}%" if r["mkt_ret20"] is not None else "-"
        ck60 = f"{r['ck_ret60']*100:+.0f}%" if r["ck_ret60"] == r["ck_ret60"] else "-"
        L.append(f"- {r['date']}：{r['level']}（大盘20日 {mkt_s}，退潮 "
                 f"{r['retreats']}，卡点池60日 {ck60}，主线 {r['mainline']}）")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")
    md_path = paths.report_dir() / f"周期状态回测_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    print(f"完成：{md_path} / {csv_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="市场周期状态卡")
    ap.add_argument("--json-only", action="store_true", help="只写 JSON")
    ap.add_argument("--backtest", action="store_true",
                    help="逐日回放周期状态判定（验证暴跌识别）")
    args = ap.parse_args()
    if args.backtest:
        backtest()
        return
    st = compute()
    if args.json_only:
        (paths.state_dir() / "cycle_state.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(st, ensure_ascii=False))
        return
    card = write_outputs()
    print(card)
    print(f"\nJSON: {paths.state_dir() / 'cycle_state.json'}")
    print(f"页面: {paths.web_dir() / 'cycle.html'}")


if __name__ == "__main__":
    main()
