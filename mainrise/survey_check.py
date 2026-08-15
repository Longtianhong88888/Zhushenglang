"""机构调研信号研究：机构调研（聪明钱关注度）能否筛出可靠方向。

数据源：东财 datacenter-web RPT_ORG_SURVEYNEW（机构调研公告，官方披露）。
每条 = 一家公司 × 一个接待机构，含 ORG_TYPE（机构类型：基金管理公司/券商/
保险/其它等）、RECEIVE_OBJECT（机构名）、NOTICE_DATE（公告日）、
RECEIVE_START_DATE（调研日）。

信号假设：机构调研是"行动前奏"（调研→建仓传导），比卖方研报（行动后解释）
领先。验证：卡点池调研密度/机构数/基金类调研数/相对关注度 → 卡点池未来
60 日相对超额（vs 全市场），五档检验，单调且高低档差显著 = 有效。

注：微信公众号（如 yanxunshe）无公开抓取渠道（微信无 API、搜狗微信搜索强
反爬），机构情报改用官方披露的调研/增减持/龙虎榜数据，可靠且可验证。

用法:
    python3 -m mainrise.survey_check              # 拉取（增量）+ 验证 + 报告
    python3 -m mainrise.survey_check --fetch-only # 只拉数据缓存
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.report import load_chokepoint_codes
from mainrise.signals import in_universe

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
PAGE_SIZE = 500

CACHE = paths.state_dir() / "org_survey"

# 机构类型 → 权重类（基金/外资=机构钱；券商=研究；其它=未知）
FUND_TYPES = ("基金管理公司", "QFII", "社保基金", "保险")


def _fetch_month(ym: str) -> list:
    """拉单月全量机构调研（按月 filter 分页；闭区间日期，避免东财 9501）。"""
    y, m = int(ym[:4]), int(ym[5:7])
    import calendar
    last = calendar.monthrange(y, m)[1]
    begin, end = f"{ym}-01", f"{ym}-{last:02d}"
    items, page = [], 1
    while True:
        r = requests.get(API,
                         params={"reportName": "RPT_ORG_SURVEYNEW",
                                 "columns": "ALL", "pageSize": PAGE_SIZE,
                                 "pageNumber": page,
                                 "filter": f"(NOTICE_DATE>='{begin}')"
                                           f"(NOTICE_DATE<='{end}')",
                                 "sortColumns": "NOTICE_DATE",
                                 "sortTypes": "-1"},
                         headers=HDR, timeout=25)
        d = r.json()
        data = ((d.get("result") or {}).get("data")) or []
        items.extend(data)
        res = d.get("result") or {}
        if not data or page >= (res.get("pages") or 1):
            break
        page += 1
        time.sleep(0.15)
    return items


def _norm(it: dict) -> dict:
    return {
        "code": str(it.get("SECURITY_CODE") or "").zfill(6),
        "name": str(it.get("SECURITY_NAME_ABBR") or ""),
        "notice": str(it.get("NOTICE_DATE") or "")[:10],
        "surv_date": str(it.get("RECEIVE_START_DATE") or "")[:10],
        "org_type": str(it.get("ORG_TYPE") or ""),
        "org_name": str(it.get("RECEIVE_OBJECT") or ""),
        "investigators": str(it.get("INVESTIGATORS") or ""),
    }


def fetch_all(refresh: bool = False) -> pd.DataFrame:
    """按月拉取 2021-01 ~ 上月，增量缓存 CSV。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    months = []
    y, m = 2025, 9          # 东财 RPT_ORG_SURVEYNEW 实测仅保留 2025-09 起
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
        df = pd.DataFrame(rows)
        df.to_csv(p, index=False, encoding="utf-8-sig")
        print(f"  {ym}: {len(df)} 条")
        frames.append(df)
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def run(refresh: bool = False) -> str:
    t0 = time.time()
    print("拉取机构调研（增量缓存）...")
    sv = fetch_all(refresh)
    if sv.empty:
        raise SystemExit("无调研数据")
    print(f"调研记录 {len(sv)} 条（{sv['notice'].min()} ~ {sv['notice'].max()}）")

    ck = {c for c in load_chokepoint_codes()
          if not c.startswith("301") and not c.startswith("688")}
    sv["is_ck"] = sv["code"].isin(ck)

    # 每日指标（按公告日）
    d = sv.groupby(["notice", "is_ck"]).size().unstack(fill_value=0)
    d = d.reindex(columns=[False, True], fill_value=0)
    d.columns = ["n_all", "n_ck"]
    # 基金类调研（卡点池）
    fund = sv[(sv["is_ck"]) & (sv["org_type"].isin(FUND_TYPES))]
    fund_cnt = fund.groupby("notice").size()
    # 调研公司数（卡点池去重）
    surv_firm = sv[sv["is_ck"]].drop_duplicates(["notice", "code"])
    firm_cnt = surv_firm.groupby("notice").size()

    daily = d.copy()
    daily["fund_ck"] = fund_cnt
    daily["firm_ck"] = firm_cnt
    daily["fund_ck"] = daily["fund_ck"].fillna(0).astype(int)
    daily["firm_ck"] = daily["firm_ck"].fillna(0).astype(int)
    daily["n_ck"] = daily["n_ck"].astype(int)
    daily["n_all"] = daily["n_all"].astype(int)
    daily = daily.sort_index()
    # 平滑：3 日滚动
    daily["sv3"] = daily["n_ck"].rolling(3, min_periods=1).mean()
    daily["sv3_all"] = daily["n_all"].rolling(3, min_periods=1).mean()
    daily["rel"] = daily["sv3"] / daily["sv3_all"] * 10000   # 每万条中卡点池条数
    daily["fund3"] = daily["fund_ck"].rolling(3, min_periods=1).mean()
    daily["firm3"] = daily["firm_ck"].rolling(3, min_periods=1).mean()

    # 卡点池 vs 全市场相对超额（60 日）
    print("加载行情计算相对超额...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    p = full.assign(pct=full["pct_chg"].clip(-21, 21))
    p["grp"] = np.where(p["code"].isin(ck), "tech", "other")
    g = (p.groupby(["date", "grp"])["pct"].mean().unstack().sort_index())
    tech_nav = (g["tech"] / 100 + 1).cumprod()
    other_nav = (g["other"] / 100 + 1).cumprod()
    xs20 = (tech_nav / tech_nav.shift(20)
            / (other_nav / other_nav.shift(20)) - 1)
    xs60 = (tech_nav / tech_nav.shift(60)
            / (other_nav / other_nav.shift(60)) - 1)

    df = daily.join(pd.DataFrame({"xs20": xs20, "xs60": xs60}), how="inner")
    df["fwd_xs20"] = df["xs20"].shift(-20)
    df["fwd_xs60"] = df["xs60"].shift(-60)
    df = df.dropna(subset=["fwd_xs60"])
    print(f"有效样本 {len(df)} 天")

    cols = ["sv3", "rel", "firm3", "fund3"]
    labels = {
        "sv3": "卡点池调研条数（3日滚动，机构数）",
        "rel": "相对调研热度（每万条中卡点池条数）",
        "firm3": "卡点池被调研公司数（3日滚动）",
        "fund3": "卡点池基金类调研（3日滚动）",
    }
    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 机构调研信号研究：机构调研能否筛出可靠方向（{dstr}）")
    L.append("")
    L.append(f"> 数据：东财 RPT_ORG_SURVEYNEW 机构调研全量 {len(sv)} 条 "
             f"（{sv['notice'].min()} ~ {sv['notice'].max()}）；卡点池 {len(ck)} 只。")
    L.append("> 口径：每条=公司×接待机构；被解释变量=卡点池 vs 全市场等权"
             " 未来 60 日相对超额（fwd_xs60，无前视）。五档检验：单调 + 高低档"
             " 差>2pp + 相关>0.15 才算有效。")
    L.append("")

    for col in cols:
        sub = df.dropna(subset=[col])
        if len(sub) < 100:
            L.append(f"## {labels[col]}（样本不足）")
            L.append("")
            continue
        q = pd.qcut(sub[col], 5, labels=False, duplicates="drop")
        c20 = float(np.corrcoef(sub[col], sub["fwd_xs20"])[0, 1])
        c60 = float(np.corrcoef(sub[col], sub["fwd_xs60"])[0, 1])
        L.append(f"## {labels[col]}（相关 fwd20 {c20:+.2f} / fwd60 {c60:+.2f}）")
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
            diff = hi.mean() - lo.mean()
            L.append(f"- Q0 fwd60 {lo.mean():+.2%} vs Q4 {hi.mean():+.2%}，"
                     f"差 {diff:+.2%} → " +
                     ("**有效调研信号**" if diff > 0.02 and c60 > 0.15
                      else "区分度不足（调研热度≈拥挤度）"))
        else:
            L.append("- 高低档样本不足")
        L.append("")

    L.append("## 结论")
    L.append("")
    L.append("- 若调研密度/相对热度/公司数/基金类调研 均无单调预测力，则机构调研"
             " 同样是'关注度'而非'领先信号'——机构调研后建仓存在传导，但可能被"
             " 调研本身的公开性（公告日即已反映）抵消；")
    L.append("- 若有效：作为候选池增强器（调研事件加分），不直接触发买卖。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"机构调研信号研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    df.reset_index().to_csv(paths.report_dir() / f"调研信号明细_{dstr}.csv",
                            index=False, encoding="utf-8-sig")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="机构调研信号研究")
    ap.add_argument("--refresh", action="store_true", help="强制重拉缓存")
    ap.add_argument("--fetch-only", action="store_true", help="只拉缓存")
    args = ap.parse_args()
    if args.fetch_only:
        fetch_all(refresh=True)
        return
    run(refresh=args.refresh)


if __name__ == "__main__":
    main()
