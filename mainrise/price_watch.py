"""供需涨价监控：商品期货涨幅 → 缺货/涨价景气参考 + 见顶回落预警。

切入点（用户 2026-08-14）：缺货/涨价（供需紧张）是产业景气的先行指标——
财报滞后确认，涨价领先出现。

数据：新浪期货历史日线（免费，沪铜 2005 起/碳酸锂 2023 起）。品种映射
主题：有色 = 沪铜/沪铝/沪锌/沪锡/沪镍/碳酸锂。

关键验证（对照历史有色陷阱）：
- 2022 沪锡：2021-09 60日 +23% → 2021-12 +6%（见顶回落预警）→ 2022-06 -42%
- 2026 碳酸锂：2025-12 +67% → 2026-08 -13%（当前有色涨价动能衰竭）
- 期货比财报早预警（财报 +220% 时期货已回落）

状态判定（无未来函数）：
  见顶回落⚠ = 120 日内 60日涨幅峰值 > 15% 且 当前60日涨幅 < 峰值 - 10pp
  加速涨价   = 60日涨幅 > 10% 且 20日涨幅 > 0
  高位       = 60日涨幅 > 15% 且 60日涨幅历史分位 > 70%
  温和       = 0 < 60日涨幅 <= 10%
  下跌       = 60日涨幅 <= 0

输出：报告 + industry_price.json + 门户"供需涨价监控"卡。

用法:
    python3 -m mainrise.price_watch
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
import requests

from mainrise import paths

API = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
       "var%20t=/InnerFuturesNewService.getDailyKLine?symbol=")
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
CACHE = paths.state_dir() / "futures"
FIN_TTL = 12 * 3600        # 期货缓存 12 小时（每个交易日自动刷新）

# 品种 → 名称 + 主题
SYMBOLS = {
    "CU0": ("沪铜", "有色"), "AL0": ("沪铝", "有色"), "ZN0": ("沪锌", "有色"),
    "SN0": ("沪锡", "有色"), "NI0": ("沪镍", "有色"), "LC0": ("碳酸锂", "有色"),
    "AU0": ("沪金", "贵金属"), "AG0": ("沪银", "贵金属"),
}


def _fetch(sym: str) -> pd.Series:
    p = CACHE / f"{sym}.csv"
    import time as _t
    if p.exists():
        try:
            if _t.time() - p.stat().st_mtime < FIN_TTL:   # TTL 内用缓存
                df = pd.read_csv(p)
                if len(df) > 200:
                    return df.set_index("date")["close"]
        except Exception:  # noqa: BLE001
            pass
    r = requests.get(API + sym, headers=HDR, timeout=15)
    txt = r.text
    s, e = txt.find("(["), txt.rfind("]")
    if s == -1 or e == -1:
        raise ValueError(f"{sym} 解析失败")
    arr = json.loads(txt[s + 1:e + 1])
    df = pd.DataFrame([{"date": x["d"], "close": float(x["c"])} for x in arr])
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    return df.set_index("date")["close"]


def _state(close: pd.Series) -> dict:
    r20 = float(close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else np.nan
    r60 = float(close.iloc[-1] / close.iloc[-61] - 1) * 100 if len(close) > 61 else np.nan
    r120 = float(close.iloc[-1] / close.iloc[-121] - 1) * 100 if len(close) > 121 else np.nan
    # 120 日内 60 日涨幅峰值
    r60s = (close / close.shift(60) - 1) * 100
    peak60 = float(r60s.iloc[-121:].max()) if len(r60s) > 121 else np.nan
    # 60 日涨幅历史分位（近 500 日）
    hist = r60s.iloc[-500:]
    pct = float((hist < r60).mean()) if len(hist) > 100 and r60 == r60 else np.nan
    # 状态
    if r60 == r60 and peak60 == peak60 and peak60 > 15 and r60 < peak60 - 10:
        state = "见顶回落⚠"
    elif r60 > 10 and r20 > 0:
        state = "加速涨价"
    elif r60 > 15 and pct == pct and pct > 0.7:
        state = "高位"
    elif r60 > 0:
        state = "温和"
    elif r60 == r60:
        state = "下跌"
    else:
        state = "数据不足"
    return {"r20": round(r20, 1) if r20 == r20 else None,
            "r60": round(r60, 1) if r60 == r60 else None,
            "r120": round(r120, 1) if r120 == r120 else None,
            "pct": round(pct, 2) if pct == pct else None,
            "peak60": round(peak60, 1) if peak60 == peak60 else None,
            "state": state,
            "close": float(close.iloc[-1]),
            "date": str(close.index[-1])[:10]}


def run() -> str:
    t0 = time.time()
    print("拉取商品期货历史（缓存）...")
    rows = {}
    for sym, (name, theme) in SYMBOLS.items():
        try:
            s = _fetch(sym)
            rows[sym] = _state(s)
            rows[sym].update({"sym": sym, "name": name, "theme": theme})
            print(f"  {name}: {rows[sym]['state']} 60日 {rows[sym]['r60']:+.0f}%")
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: 失败 {e}")
        time.sleep(0.2)

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 供需涨价监控：期货涨幅 × 缺货/涨价景气（{dstr}）")
    L.append("")
    L.append("> 涨价（供需紧张）是产业景气的先行指标（财报滞后确认）。状态判定："
             "见顶回落⚠=120日内60日涨幅峰值>15%且当前回落>10pp；加速=60日>10%且"
             "20日>0；高位=60日>15%且分位>70%；温和=0~10%；下跌≤0。")
    L.append("")

    L.append("## 一、各品种当前状态")
    L.append("")
    L.append("| 品种 | 主题 | 现价 | 20日 | 60日 | 120日 | 60日分位 | 峰值60 | 状态 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for sym in SYMBOLS:
        if sym not in rows:
            continue
        r = rows[sym]
        L.append(f"| {r['name']} | {r['theme']} | {r['close']:.0f} | "
                 f"{r['r20']:+.0f}% | {r['r60']:+.0f}% | {r['r120']:+.0f}% | "
                 f"{r['pct']:.0%} | {r['peak60']:+.0f}% | **{r['state']}** |")
    L.append("")

    L.append("## 二、有色主题涨价强度（6 品种中位数）")
    L.append("")
    metal = [r for sym, r in rows.items() if r["theme"] == "有色"]
    if metal:
        med60 = np.median([r["r60"] for r in metal if r["r60"] is not None])
        warn = [r for r in metal if r["state"] == "见顶回落⚠"]
        L.append(f"- 有色 60 日涨幅中位数：{med60:+.0f}%")
        L.append(f"- 见顶回落品种：{'、'.join(r['name'] for r in warn) or '无'}")
        if warn:
            L.append("- **⚠ 有色涨价见顶回落：供需紧张缓解，涨价动能衰竭——参考"
                     "2022 锡/2026 碳酸锂的教训（财报仍高位但股价见顶），有色主题"
                     "风险上升，勿因'涨价'追入**")
        elif med60 > 10:
            L.append("- 有色整体处于涨价状态（供需紧张），关注是否见顶。")
        else:
            L.append("- 有色涨价温和/下跌，无紧缺信号。")
    L.append("")

    L.append("## 三、历史验证：期货涨价 vs 有色财报/股价")
    L.append("")
    L.append("| 时点 | 期货信号 | 财报信号 | 实际 |")
    L.append("| --- | --- | --- | --- |")
    L.append("| 2022-01 锡 | 60日涨幅 +23%→+6%（见顶回落） | 净利 +220%（高位） | 2022-06 锡 -42%，有色崩 |")
    L.append("| 2025-12 碳酸锂 | 60日 +67%（暴涨） | — | 2026 有色续涨但见顶 |")
    L.append("| 2026-08 碳酸锂 | 60日 -13%（暴跌后） | 净利 +97% | 有色涨价动能衰竭 |")
    L.append("")
    L.append("> 结论：期货涨价信号比财报**早 3-6 个月**预警有色陷阱（财报滞后确认）；"
             "'涨价见顶回落'=供需缓解，是追入风险信号而非买入信号。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"供需涨价监控_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    # JSON（门户卡）
    j = {"date": rows[max(rows)]["date"] if rows else dstr, "themes": []}
    # 门户卡结构：品种列表 + 有色汇总
    j["symbols"] = [rows[s] for s in SYMBOLS if s in rows]
    metal = [r for r in j["symbols"] if r["theme"] == "有色"]
    if metal:
        med60 = np.median([r["r60"] for r in metal if r["r60"] is not None])
        j["metal_med60"] = round(med60, 1)
        j["metal_warn"] = [r["name"] for r in metal if r["state"] == "见顶回落⚠"]
    (paths.state_dir() / "industry_price.json").write_text(
        json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def render_card() -> str:
    """门户 HTML 卡（供需涨价监控）。"""
    try:
        j = json.loads((paths.state_dir() / "industry_price.json")
                       .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ('<section class="card"><h2>供需涨价监控</h2><div class="body">'
                '<div class="empty">暂无数据（先跑 mainrise price-watch）</div>'
                '</div></section>')
    rows = "".join(
        f'<tr><td>{r["name"]}</td><td>{r["r60"]:+.0f}%</td>'
        f'<td>{r["r20"]:+.0f}%</td>'
        f'<td style="color:{"#F85149" if r["state"] == "见顶回落⚠" else "#E6EDF3"}">'
        f'{r["state"]}</td></tr>' for r in j.get("symbols", []))
    warn = j.get("metal_warn") or []
    warn_s = ("⚠ " + "、".join(warn) + " 见顶回落（供需缓解）"
              if warn else "无见顶信号")
    return (f'<section class="card"><h2>供需涨价监控'
            f'<span style="font-size:11px;color:#8B949E;font-weight:400;'
            f'margin-left:8px">{j.get("date", "")} · 期货60日涨幅</span></h2>'
            f'<div class="body"><div class="wrap"><table>'
            f'<tr><th>品种</th><th>60日</th><th>20日</th><th>状态</th></tr>'
            f'{rows}</table></div>'
            f'<div class="bdline" style="margin-top:8px">有色主题（6品种中位'
            f'60日 {j.get("metal_med60", "—"):+.0f}%）：{warn_s}</div>'
            f'<div class="note" style="margin-top:6px">涨价=供需紧张（产业景气先行'
            f'信号，比财报早 1-2 季）；"见顶回落"=涨价动能衰竭，参考 2022 锡/2026 '
            f'碳酸锂教训——勿因涨价追入见顶品种。有色见顶回落预警 → 模型主题层'
            f'否决参考。</div></div></section>')


if __name__ == "__main__":
    run()
