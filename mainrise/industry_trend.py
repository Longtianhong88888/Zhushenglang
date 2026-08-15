"""产业趋势研究 v2：单季净利同比 + 业绩预告填补（缩短信息空白期）。

问题（用户 2026-08-14）：原研究用"最新可用财报的累计净利同比"，两次财报之间
跨度太长（Q1→5-15 / 中报→8-31 / Q3→10-31 / 年报→次年04-30，中间 3-5 个月
无新信息），且累计同比平滑掉最近季度变化。

改进：
1. **单季口径**：归母净利润绝对值（PARENT_NETPROFIT）同财年差分 → 单季净利
   → 单季同比（比累计更敏感，如中际旭创 2026Q1 单季 +262% vs 累计 +158%）
2. **业绩预告填补**：东财 RPT_PUBLIC_OP_NEWPREDICT（NOTICE_DATE + INCREASE_JZ
   幅度中值），预告比财报早 1-3 个月（年报预告 1 月底 / 中报预告 7 月）——
   在"无财报期"用最新预告的增减幅度作为景气度，标注"预告"。

用法:
    python3 -m mainrise.industry_trend
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import requests

from mainrise import paths

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
CACHE = paths.state_dir() / "ind_fin"
FIN_TTL = 7 * 86400          # 财报/预告缓存 7 天（季度披露 + 月度预告，周频刷新足够）

THEME_PICKS = {
    "AI硬件": ["300308", "300502", "300394", "002463"],
    "半导体": ["603501", "002371", "600584", "603986"],
    "存储": ["603986", "000021", "300475"],
    "有色": ["601899", "603993", "600111", "000960"],
    "创新药": ["603259", "600276", "002821", "300347"],
    "机器人": ["300124", "603728", "002050"],
    "商业航天": ["600118", "600879", "300762"],
    "自动驾驶": ["002920", "603596", "002906"],
}


def _available(report: str) -> str:
    """报告期 → 保守可用日（无前视）。"""
    y, m = int(report[:4]), int(report[5:7])
    if m == 3:
        return f"{y}-05-15"
    if m == 6:
        return f"{y}-08-31"
    if m == 9:
        return f"{y}-10-31"
    return f"{y + 1}-04-30"


def _fetch_fin(code: str) -> pd.DataFrame:
    """拉全部历史季度财报（含归母净利润绝对值，缓存）。"""
    p = CACHE / f"{code}.csv"
    import time as _t
    if p.exists():
        try:
            if _t.time() - p.stat().st_mtime < FIN_TTL:
                df = pd.read_csv(p)
                if len(df) and "PARENT_NETPROFIT" in df.columns:
                    return df
        except Exception:  # noqa: BLE001
            pass
    r = requests.get(API,
                     params={"reportName": "RPT_LICO_FN_CPD",
                             "columns": "SECURITY_CODE,REPORTDATE,SJLTZ,"
                                        "PARENT_NETPROFIT",
                             "filter": f'(SECURITY_CODE="{code}")',
                             "pageSize": 100, "pageNumber": 1,
                             "sortColumns": "REPORTDATE", "sortTypes": "-1"},
                     headers=HDR, timeout=20)
    d = r.json()
    rows = ((d.get("result") or {}).get("data")) or []
    df = pd.DataFrame([{k: row.get(k) for k in
                        ("SECURITY_CODE", "REPORTDATE", "SJLTZ",
                         "PARENT_NETPROFIT")} for row in rows])
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    return df


def _fetch_predict(code: str) -> pd.DataFrame:
    """拉全部历史业绩预告（缓存）。"""
    p = CACHE / f"{code}_pred.csv"
    import time as _t
    if p.exists():
        try:
            if _t.time() - p.stat().st_mtime < FIN_TTL:
                df = pd.read_csv(p)
                if len(df):
                    return df
        except Exception:  # noqa: BLE001
            pass
    r = requests.get(API,
                     params={"reportName": "RPT_PUBLIC_OP_NEWPREDICT",
                             "columns": "SECURITY_CODE,REPORT_DATE,NOTICE_DATE,"
                                        "PREDICT_TYPE,INCREASE_JZ,"
                                        "PREDICT_RATIO_LOWER,PREDICT_RATIO_UPPER,"
                                        "PREDICT_FINANCE",
                             "filter": f'(SECURITY_CODE="{code}")'
                                       '(PREDICT_FINANCE="归属于上市公司股东的净利润")',
                             "pageSize": 60, "pageNumber": 1,
                             "sortColumns": "NOTICE_DATE", "sortTypes": "-1"},
                     headers=HDR, timeout=20)
    d = r.json()
    rows = ((d.get("result") or {}).get("data")) or []
    df = pd.DataFrame([{k: row.get(k) for k in
                        ("SECURITY_CODE", "REPORT_DATE", "NOTICE_DATE",
                         "PREDICT_TYPE", "INCREASE_JZ", "PREDICT_RATIO_LOWER",
                         "PREDICT_RATIO_UPPER")} for row in rows])
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    return df


def _quarterly(df: pd.DataFrame) -> pd.DataFrame:
    """累计 → 单季净利 + 单季同比（无未来函数：只用历史报告期）。"""
    d = df.dropna(subset=["PARENT_NETPROFIT"]).copy()
    if not len(d):
        return d
    d["REPORTDATE"] = d["REPORTDATE"].astype(str).str[:10]
    d = d.sort_values("REPORTDATE")
    d["NP"] = d["PARENT_NETPROFIT"].astype(float)
    d["y"] = d["REPORTDATE"].str[:4].astype(int)
    d["prev_np"] = d.groupby("y")["NP"].shift(1)
    d["QNP"] = d["NP"] - d["prev_np"].fillna(0)
    d["mon"] = d["REPORTDATE"].str[5:7]
    qyoy = []
    for _, row in d.iterrows():
        last = d[(d["mon"] == row["mon"]) & (d["REPORTDATE"] < row["REPORTDATE"])]
        v = None
        if len(last):
            lq = float(last.iloc[-1]["QNP"])
            if lq > 0 and row["QNP"] > 0:
                v = row["QNP"] / lq - 1
        qyoy.append(v)
    d["QYOY"] = qyoy
    return d


def _predict_amp(df: pd.DataFrame) -> dict:
    """预告 DataFrame → {REPORT_DATE: 幅度中值}（取每条最新值）。"""
    out = {}
    if not len(df):
        return out
    d = df.copy()
    d["NOTICE_DATE"] = d["NOTICE_DATE"].astype(str).str[:10]
    d["REPORT_DATE"] = d["REPORT_DATE"].astype(str).str[:10]
    d["amp"] = pd.to_numeric(d["INCREASE_JZ"], errors="coerce")
    mask = d["amp"].isna()
    d.loc[mask, "amp"] = (
        (pd.to_numeric(d.loc[mask, "PREDICT_RATIO_LOWER"], errors="coerce")
         + pd.to_numeric(d.loc[mask, "PREDICT_RATIO_UPPER"], errors="coerce")) / 2)
    d = d.dropna(subset=["amp"])
    for _, row in d.sort_values("NOTICE_DATE").iterrows():
        out[row["REPORT_DATE"]] = float(row["amp"])
    return out


def compute_card(fin: dict, pred: dict) -> dict:
    """当前各主题景气度：最新单季同比/预告幅度 + 环比方向（门户卡数据）。"""
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    prev_month = (pd.Timestamp.now() - pd.DateOffset(days=35)).strftime("%Y-%m-%d")
    themes = []
    for theme, codes in THEME_PICKS.items():
        def latest(d: str):
            vals, tag, rep = [], "", None
            for c in codes:
                if c not in fin or not len(fin[c]):
                    continue
                avail = fin[c][fin[c]["REPORTDATE"].map(_available) <= d]
                if not len(avail):
                    continue
                r0 = avail["REPORTDATE"].iloc[-1]
                v = avail["QYOY"].iloc[-1]
                if v == v and v is not None:
                    vals.append(float(v) * 100)
                    rep = r0
            for c in codes:
                if c not in pred or not pred[c]:
                    continue
                preds = [(rd, amp) for rd, amp in pred[c].items()
                         if rd and rd <= d]
                if not preds:
                    continue
                rd = max(r_ for r_, _ in preds)
                if _available(rd) > d and (rep is None or rd > rep):
                    tag = "*"
                    amp = [a for r_, a in preds if r_ == rd][0]
                    vals = [float(amp)]
                    rep = rd
            if vals:
                return np.median(vals), tag
            return None, ""
        cur = latest(today)
        prev = latest(prev_month)
        val, tag = cur if cur else (None, "")
        pv = prev[0] if prev and prev[0] is not None else None
        trend = ""
        if val is not None and pv is not None:
            trend = "↑" if val > pv + 3 else ("↓" if val < pv - 3 else "→")
        if val is None:
            label = "无数据"
        elif val >= 50:
            label = "高景气"
        elif val >= 0:
            label = "温和"
        else:
            label = "负增长"
        themes.append({"theme": theme, "value": round(val, 0) if val is not None else None,
                       "tag": tag, "trend": trend, "label": label})
    return {"date": today, "note": "主题龙头单季净利同比中位数（* = 业绩预告）",
            "themes": themes}


def write_card(fin: dict, pred: dict) -> str:
    """计算当前景气度 → 写 industry_trend.json + 返回门户卡 HTML。"""
    st = compute_card(fin, pred)
    paths.ensure_dirs()
    (paths.state_dir() / "industry_trend.json").write_text(
        __import__("json").dumps(st, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return render_card(st)


def _val_txt(r: dict) -> str:
    if r["value"] is None:
        return "—"
    return f"{r['value']:+.0f}%{r['tag']}"


def render_card(st: dict) -> str:
    """门户 HTML 卡（Quant Dark，供 bigbull 门户注入）。"""
    if not st.get("themes"):
        return ('<section class="card"><h2>产业景气度</h2><div class="body">'
                '<div class="empty">暂无产业景气度数据（先跑 mainrise industry-trend）'
                '</div></div></section>')
    rows = "".join(
        f'<tr><td style="color:{"#F85149" if r["theme"] in ("AI硬件", "半导体", "存储") else "#E6EDF3"}">'
        f'{"⭐" if r["theme"] in ("AI硬件", "半导体", "存储") else ""}{r["theme"]}</td>'
        f'<td>{_val_txt(r)}</td>'
        f'<td>{r["trend"]}</td><td>{r["label"]}</td></tr>'
        for r in st["themes"])
    return (f'<section class="card"><h2>产业景气度'
            f'<span style="font-size:11px;color:#8B949E;font-weight:400;'
            f'margin-left:8px">{st.get("date", "")} · 单季净利同比，'
            f'*=业绩预告</span></h2><div class="body">'
            f'<div class="wrap"><table>'
            f'<tr><th>主题</th><th>最新增速</th><th>环比</th><th>状态</th></tr>'
            f'{rows}</table></div>'
            f'<div class="note" style="margin-top:6px">口径：主题龙头归母净利单季'
            f'同比中位数（累计差分）；* = 业绩预告（比财报早 1-3 个月，无财报期'
            f'填补）；⭐ = 模型固定热主题。环比=与 1 个月前相比。产业信号做'
            f'"确认/否决"（高位确认持有、连续下滑否决换入），价格信号做交易。</div>'
            f'</div></section>')


def run() -> str:
    t0 = time.time()
    print("拉取季度财报 + 业绩预告（缓存）...")
    all_codes = sorted({c for codes in THEME_PICKS.values() for c in codes})
    fin, pred = {}, {}
    for code in all_codes:
        fin[code] = _quarterly(_fetch_fin(code))
        pred[code] = _predict_amp(_fetch_predict(code))
        print(f"  {code}: 财报{len(fin[code])}期 预告{len(pred[code])}条")
        time.sleep(0.15)

    # 月度采样日（每月 15 日）
    samples = []
    y, m = 2021, 5
    while (y, m) <= (2026, 8):
        samples.append(f"{y}-{m:02d}-15")
        m += 1
        if m == 13:
            y, m = y + 1, 1

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 产业趋势研究 v2：单季净利同比 + 业绩预告（{dstr}）")
    L.append("")
    L.append("> 单季口径=累计归母净利同财年差分后同比（比累计更敏感）；业绩预告"
             "（RPT_PUBLIC_OP_NEWPREDICT，归母净利口径，INCREASE_JZ 中值）比财报"
             "早 1-3 个月发布，在无财报期填补景气度（标注*）。无前视：财报可用日"
             "=报告期+披露延迟（Q1→05-15/中报→08-31/Q3→10-31/年报→次年04-30）；"
             "预告可用日=公告日。")
    L.append("")

    L.append("## 一、各主题单季净利同比中位数时间线（% ，* = 业绩预告填补）")
    L.append("")
    L.append("| 采样日 | " + " | ".join(THEME_PICKS.keys()) + " |")
    L.append("| --- |" + " --- |" * len(THEME_PICKS))
    theme_qyoy = {}
    for theme, codes in THEME_PICKS.items():
        s = {}
        for d in samples:
            vals = []
            for c in codes:
                if c not in fin or not len(fin[c]):
                    continue
                avail = fin[c][fin[c]["REPORTDATE"].map(_available) <= d]
                if not len(avail):
                    continue
                rep = avail["REPORTDATE"].iloc[-1]
                v = avail["QYOY"].iloc[-1]
                if v == v and v is not None:
                    vals.append(("财", float(v) * 100, rep))
            # 预告填补：预告公告日 > 最新财报可用日
            for c in codes:
                if c not in pred or not pred[c]:
                    continue
                preds = [(rd, amp) for rd, amp in pred[c].items()]
                avail_p = [(rd, amp) for rd, amp in preds if rd and rd <= d]
                if not avail_p:
                    continue
                # 该预告对应的财报可用日（预告填补只在该财报未披露时）
                rd = max(rd for rd, _ in avail_p)
                if _available(rd) > d:     # 对应财报还没披露 → 预告生效
                    amp = [a for r_, a in avail_p if r_ == rd][0]
                    vals.append(("预", float(amp), rd))
            if vals:
                # 预告比最新财报新（对应更晚报告期）→ 用预告填补（标注*）
                fin_items = [(v, rd) for t, v, rd in vals if t == "财"]
                pred_items = [(v, rd) for t, v, rd in vals if t == "预"]
                if pred_items and (not fin_items or max(rd for _, rd in pred_items)
                                   > max(rd for _, rd in fin_items)):
                    s[d] = (np.median([v for v, _ in pred_items]), "*")
                elif fin_items:
                    s[d] = (np.median([v for v, _ in fin_items]), "")
        theme_qyoy[theme] = s
    for d in samples:
        row = f"| {d} |"
        for theme in THEME_PICKS:
            v = theme_qyoy[theme].get(d)
            if v is None:
                row += " — |"
            else:
                val, tag = v
                row += f" {val:+.0f}{tag} |"
        L.append(row)
    L.append("")

    L.append("## 二、关键时点对照（单季+预告 vs 原累计口径）")
    L.append("")
    checks = [
        ("2022-01（有色陷阱）", "2022-01-15", ["有色"]),
        ("2025-01（科技延续）", "2025-01-15", ["AI硬件", "半导体", "存储"]),
        ("2026-01（有色再现）", "2026-01-15", ["有色"]),
        ("2026-08（当前）", "2026-08-15", ["AI硬件", "半导体", "存储", "有色"]),
    ]
    L.append("| 时点 | 主题 | 单季+预告（新口径） | 原累计口径 |")
    L.append("| --- | --- | --- | --- |")
    for label, d, themes in checks:
        for th in themes:
            v = theme_qyoy[th].get(d)
            s_new = f"{v[0]:+.0f}%{v[1]}" if v else "—"
            # 原累计口径：最新可用财报累计同比
            cums = []
            for c in THEME_PICKS[th]:
                if c in fin and len(fin[c]):
                    avail = fin[c][fin[c]["REPORTDATE"].map(_available) <= d]
                    if len(avail):
                        vv = avail["SJLTZ"].iloc[-1]
                        if vv == vv and vv is not None:
                            cums.append(float(vv))
            s_old = f"{np.median(cums):+.0f}%" if cums else "—"
            L.append(f"| {label} | {th} | {s_new} | {s_old} |")
    L.append("")

    L.append("## 三、结论")
    L.append("")
    L.append("- 单季口径+预告是否比累计口径更早反映拐点（如 2022 有色单季增速在"
             " 2021 下半年已回落、2026 有色预告/单季是否已现疲态）；")
    L.append("- 预告填补是否让'无财报期'（1-4 月、7-8 月）的景气度有信号可看。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"产业趋势研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    # 门户卡（industry_trend.json + 卡 HTML）
    card = write_card(fin, pred)
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    print(f"景气度卡: {paths.state_dir() / 'industry_trend.json'}")
    return str(md_path)


if __name__ == "__main__":
    run()
