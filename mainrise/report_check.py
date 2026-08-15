"""市场研报信号研究：券商研报能否筛出"可靠方向"（证据驱动验证）。

数据源：东财 reportapi.eastmoney.com/report/list（qType=0 个股研报，免费，
历史可回拉）。字段含：stockCode/stockName/title/orgSName/emRatingName（评级）/
ratingChange（评级变化）/indvIsNew（是否新覆盖）/publishDate/industryName。

背景（前序研究）：行情类风格指标（等权20日/强弱差/宽度/量能/涨停/滚动胜率）
全部无稳健预测力（style_check，7 指标相关≤0.15）；热主题动态化四方案全负优化。
本报告检验研报这类"情报源"是否例外——假设：券商研报是卖方信息，热度大概率是
"拥挤度"而非"领先信号"，但**增量事件**（新覆盖/评级上调/密度变化率）可能领先。

验证设计（无前视）：
- 全量拉取 2021-01 ~ 2026-08 个股研报（按月分页缓存）
- 每日指标：卡点池研报密度（3日滚动）、评级上调数、新覆盖数、
  相对密度（卡点池/全市场研报密度比）
- 被解释变量：卡点池等权未来 20/60 日超额 vs 全市场（tech_xs fwd）
- 五档分组 + 相关：单调且高低档差显著 = 有效研报信号

用法:
    python3 -m mainrise.report_check              # 拉取（增量）+ 验证 + 报告
    python3 -m mainrise.report_check --fetch-only # 只拉数据缓存
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.report import load_chokepoint_codes
from mainrise.signals import in_universe

API = "https://reportapi.eastmoney.com/report/list"
HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
PAGE_SIZE = 100
UA = ["Mozilla/5.0", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"]

CACHE = paths.state_dir() / "research_reports"


def _fetch_month(ym: str, key: str = "") -> list:
    """拉单月全量个股研报（分页）。ym = 'YYYY-MM'。"""
    begin = f"{ym}-01"
    if ym.endswith("12"):
        end = f"{int(ym[:4]) + 1}-01-01"
    else:
        end = f"{ym}-{int(ym[5:7]) + 1:02d}-01"
    items, page = [], 1
    while True:
        r = requests.get(API,
                         params={"industryCode": "*", "pageSize": PAGE_SIZE,
                                 "industry": "*", "rating": "*",
                                 "ratingChange": "*",
                                 "beginTime": begin, "endTime": end,
                                 "pageNo": page, "qType": 0},
                         headers={"User-Agent": UA[page % 2]}, timeout=20)
        d = r.json()
        data = d.get("data") or []
        items.extend(data)
        if not data or page >= (d.get("TotalPage") or 1):
            break
        page += 1
        time.sleep(0.2)
    return items


def _norm(it: dict) -> dict:
    """研报条目 → 精简字段（code/date/评级/变化/新覆盖/行业/券商）。"""
    code = str(it.get("stockCode") or "").zfill(6)
    if not code or code == "000000":
        return {}
    return {
        "code": code,
        "date": str(it.get("publishDate") or "")[:10],
        "rating": str(it.get("emRatingName") or ""),
        "rating_change": str(it.get("ratingChange") or ""),
        "is_new": int(bool(it.get("indvIsNew"))),
        "industry": str(it.get("industryName") or ""),
        "org": str(it.get("orgSName") or ""),
        "title": str(it.get("title") or ""),
    }


def fetch_all(refresh: bool = False) -> pd.DataFrame:
    """按月拉取 2021-01 ~ 上月，增量缓存 CSV；返回合并表。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    months = []
    y, m = 2021, 1
    now = pd.Timestamp.now()
    while (y, m) <= (now.year, now.month):
        months.append(f"{y}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    frames = []
    for ym in months:
        p = CACHE / f"{ym}.csv"
        if not refresh and p.exists():
            df = pd.read_csv(p, dtype={"code": str})
            if len(df):
                frames.append(df)
                continue
        try:
            items = _fetch_month(ym)
        except Exception as e:  # noqa: BLE001
            print(f"  {ym} 拉取失败: {e}")
            continue
        rows = [_norm(it) for it in items]
        rows = [r for r in rows if r]
        df = pd.DataFrame(rows)
        df.to_csv(p, index=False, encoding="utf-8-sig")
        print(f"  {ym}: {len(df)} 条")
        frames.append(df)
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["date"] = out["date"].astype(str)
    return out


def run(refresh: bool = False) -> str:
    t0 = time.time()
    print("拉取研报（增量缓存）...")
    rep = fetch_all(refresh)
    print(f"研报总数 {len(rep)} 条，{rep['date'].min() if len(rep) else '-'} ~ "
          f"{rep['date'].max() if len(rep) else '-'}")
    if rep.empty:
        raise SystemExit("无研报数据")

    ck = {c for c in load_chokepoint_codes()
          if not c.startswith("301") and not c.startswith("688")}

    # 每日指标：全量研报数 / 卡点池研报数 / 上调数 / 新覆盖数
    rep["date"] = pd.to_datetime(rep["date"]).dt.strftime("%Y-%m-%d")
    rep["is_ck"] = rep["code"].isin(ck)
    daily = rep.groupby("date").agg(
        n_all=("code", "count"),
        n_ck=("is_ck", "sum"),
        up_ck=("is_ck", lambda s: int((rep.loc[s.index, "rating_change"]
                                       .str.contains("上调")).sum())),
        new_ck=("is_ck", lambda s: int((rep.loc[s.index, "is_new"] == 1).sum())),
    )
    daily["n_all"] = daily["n_all"].astype(int)
    daily["n_ck"] = daily["n_ck"].astype(int)
    daily["up_ck"] = daily["up_ck"].astype(int)
    daily["new_ck"] = daily["new_ck"].astype(int)
    daily = daily.sort_index()
    daily["den3"] = daily["n_ck"].rolling(3, min_periods=1).mean()
    daily["den3_all"] = daily["n_all"].rolling(3, min_periods=1).mean()
    daily["rel_den"] = daily["den3"] / daily["den3_all"] * 1000   # 每千份中卡点池份数

    # 卡点池 vs 全市场 20/60 日超额（zzshare 全市场面板）
    print("加载行情计算相对超额...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    p = full.assign(pct=full["pct_chg"].clip(-21, 21))
    p["grp"] = np.where(p["code"].isin(ck), "tech", "other")
    g = (p.groupby(["date", "grp"])["pct"].mean().unstack().sort_index())
    tech = g["tech"] / 100 + 1
    other = g["other"] / 100 + 1
    tech_nav = tech.cumprod()
    other_nav = other.cumprod()
    xs20 = tech_nav / tech_nav.shift(20) / (other_nav / other_nav.shift(20)) - 1
    xs60 = tech_nav / tech_nav.shift(60) / (other_nav / other_nav.shift(60)) - 1

    df = daily.join(pd.DataFrame({"xs20": xs20, "xs60": xs60}), how="inner")
    # fwd：未来 20/60 日超额
    df["fwd_xs20"] = df["xs20"].shift(-20)
    df["fwd_xs60"] = df["xs60"].shift(-60)
    df = df.dropna(subset=["fwd_xs60"])
    print(f"有效样本 {len(df)} 天")

    cols = ["den3", "rel_den", "up_ck", "new_ck"]
    labels = {
        "den3": "卡点池研报密度（3日滚动）",
        "rel_den": "相对研报热度（每千份中卡点池份数）",
        "up_ck": "卡点池当日评级上调数",
        "new_ck": "卡点池当日新覆盖数",
    }
    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 市场研报信号研究：券商研报能否筛出可靠方向（{dstr}）")
    L.append("")
    L.append(f"> 数据：东财 reportapi 个股研报全量 {len(rep)} 条 "
             f"（{rep['date'].min()} ~ {rep['date'].max()}）；卡点池 {len(ck)} 只。")
    L.append("> 被解释变量：卡点池等权 vs 全市场等权 未来 60 日相对超额"
             "（fwd_xs60，无前视）。研报信号分五档，单调且高低档差显著 = 有效。")
    L.append("")

    for col in cols:
        sub = df.dropna(subset=[col])
        if len(sub) < 100:
            L.append(f"## {labels[col]}（样本不足）")
            L.append("")
            continue
        q = pd.qcut(sub[col], 5, labels=False, duplicates="drop")
        corr20 = float(np.corrcoef(sub[col], sub["fwd_xs20"])[0, 1])
        corr60 = float(np.corrcoef(sub[col], sub["fwd_xs60"])[0, 1])
        L.append(f"## {labels[col]}（相关 fwd20 {corr20:+.2f} / fwd60 {corr60:+.2f}）")
        L.append("")
        L.append("| 档位 | 天数 | 未来20日超额 | 未来60日超额 |")
        L.append("| --- | --- | --- | --- |")
        for qq in range(5):
            v20 = sub["fwd_xs20"][q == qq]
            v60 = sub["fwd_xs60"][q == qq]
            s20 = f"{v20.mean():+.2%}" if len(v20) else "-"
            s60 = f"{v60.mean():+.2%}" if len(v60) else "-"
            L.append(f"| Q{qq} | {int((q == qq).sum())} | {s20} | {s60} |")
        L.append("")
        lo = sub["fwd_xs60"][q == 0]
        hi = sub["fwd_xs60"][q == q.max()]
        if len(lo) >= 10 and len(hi) >= 10:
            diff60 = hi.mean() - lo.mean()
            L.append(f"- Q0 fwd60 {lo.mean():+.2%} vs Q4 {hi.mean():+.2%}，"
                     f"差 {diff60:+.2%} → " +
                     ("**有效研报信号**" if diff60 > 0.02 and corr60 > 0.15
                      else "区分度不足（研报热度≈拥挤度，非领先信号）"))
        else:
            L.append("- 高低档样本不足")
        L.append("")

    # 逐年对照
    df2 = df.copy()
    df2["year"] = df2.index.str[:4]
    L.append("## 逐年：卡点池研报热度 vs 60日超额中位数")
    L.append("")
    L.append("| 年份 | 研报密度(3日) | 相对热度 | 评级上调/月 | 新覆盖/月 | fwd60超额 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for yr, g2 in df2.groupby("year"):
        if not len(g2):
            continue
        L.append(f"| {yr} | {g2['den3'].median():.1f} | "
                 f"{g2['rel_den'].median():.0f} | "
                 f"{g2['up_ck'].sum():.0f} | {g2['new_ck'].sum():.0f} | "
                 f"{g2['fwd_xs60'].median():+.1%} |")
    L.append("")

    L.append("## 结论")
    L.append("")
    L.append("- 若研报密度/相对热度/上调/新覆盖 的 fwd60 五档无单调且高低档差 <2pp，"
             "则**研报热度是拥挤度而非领先信号**（卖方滞后于股价），不能作为方向"
             "触发器——与热主题动态化、风格检测结论一致；")
    L.append("- 若某个增量事件（如新覆盖/评级上调）有效，则作为**候选池增强器**"
             "（产业逻辑验证）而非买卖触发器。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"市场研报信号研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    df.reset_index().to_csv(paths.report_dir() / f"研报信号明细_{dstr}.csv",
                            index=False, encoding="utf-8-sig")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="市场研报信号研究")
    ap.add_argument("--refresh", action="store_true", help="强制重拉研报缓存")
    ap.add_argument("--fetch-only", action="store_true", help="只拉研报缓存")
    args = ap.parse_args()
    if args.fetch_only:
        fetch_all(refresh=True)
        return
    run(refresh=args.refresh)


if __name__ == "__main__":
    main()
