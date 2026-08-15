"""当日复盘：连板梯队 / 行业分组 / 短线情绪周期 / 情绪趋势折线图 / 龙虎榜资金。

数据源：
- 当日涨停池：东方财富 getTopicZTPool（连板数/封单/行业板块，仅支持当日）
- 历史涨停池（情绪趋势近 12 日）：本地 zzshare 全市场日线计算（limit_price 判定）
- 龙虎榜：东方财富 RPT_DAILYBILLBOARD_DETAILSNEW（买卖净额/上榜原因）
连板梯队按连板数分组、按行业分组；情绪周期基于近 12 个交易日
涨停家数/连板高度/首板晋级率启发式判定（研究参考）。

说明（数据边界，不造假）：
- 腾讯无涨停池/龙虎榜公开接口；东财涨停池不提供涨停原因关键词（以行业板块代替）。
- 腾讯无"全市场主力/大户/散户资金流向"分钟级端点，资金流向仍用东方财富。

用法:
    python3 -m mainrise.cli review    # 生成 output/web/review.html + review.json
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from mainrise import paths
from mainrise.monitor import TZ_CN
from mainrise.web_dashboard import (CSS, _chg_span, _code_link, _esc,
                                    _fmt, _page)

EM_ZT = "https://push2ex.eastmoney.com/getTopicZTPool"
EM_ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"
EM_BILLBOARD = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_BASE = "https://push2.eastmoney.com"
EM_HOSTS = ("https://push2.eastmoney.com", "https://1.push2.eastmoney.com",
            "https://push2his.eastmoney.com")
TX_RANK = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
CONCEPT_JUNK = ("昨日", "涨停", "首板", "连板", "新股", "次新", "破净", "破发",
                "ST", "退市", "低价", "高送转", "转债", "融资", "融券",
                "深股通", "沪股通", "MSCI", "标普", "富时", "摘帽", "预亏",
                "微盘", "高股息", "中字头", "茅指数", "宁组合", "业绩预")
WATCH_THEMES = {
    "AI硬件": ["AI硬件", "AI服务器", "算力", "CPO", "液冷", "光模块", "光通信"],
    "半导体设备与材料": ["半导体", "半导体设备", "半导体材料", "光刻机", "光刻胶",
                         "先进封装", "Chiplet", "第三代半导体"],
    "存储": ["存储", "HBM", "DRAM", "NAND", "闪存"],
    "商业航天": ["商业航天", "卫星", "航天", "低空经济"],
    "机器人": ["机器人", "减速器", "伺服", "具身智能"],
    "创新药": ["创新药", "医药", "CRO", "CXO", "减肥药", "生物医药"],
    "食品饮料": ["食品饮料", "白酒", "乳业", "调味", "饮料"],
    "有色": ["有色金属", "黄金", "稀土", "铜", "钴", "镍", "稀有金属",
             "小金属", "锂"],
}


def _match_board(rows: list[dict], kws: list[str]) -> dict | None:
    """按关键词优先级匹配板块：取第一个命中的关键词里总资金最大者。"""
    for kw in kws:
        hits = [r for r in rows if kw in r["name"]]
        if hits:
            return max(hits, key=lambda r: r["total"])
    return None
TREND_DAYS = 12          # 情绪趋势回看交易日数
_LOCAL_STREAK: dict[str, int] = {}   # 本地连板累计（code -> 连续涨停天数）
_LOCAL_NAMES: dict[str, str] | None = None


# ---------------------------------------------------------------- 数据获取

def fetch_limit_up_pool(date_str: str, key: str | None = None) -> list[dict]:
    """涨停池：当日走东财实时（连板/封单/行业），历史/失败走本地日线计算。"""
    today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
    if date_str >= today:
        try:
            pool = _em_zt_pool(date_str)
            if pool:
                return pool
        except Exception:  # noqa: BLE001
            pass
    return local_limit_pool(date_str)


def _em_zt_pool(date_str: str) -> list[dict]:
    """东财当日涨停池（仅支持当日；含连板数/封单/行业板块）。"""
    r = requests.get(EM_ZT, params={
        "ut": EM_ZT_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 500,
        "sort": "fbt:asc", "date": date_str.replace("-", "")}, timeout=10)
    d = r.json()
    pool = (d.get("data") or {}).get("pool") or []
    out = []
    for it in pool:
        fbt = int(it.get("fbt") or 0)
        cnt = int(it.get("lbc") or 1)
        out.append({
            "ticker": str(it.get("c")), "code": str(it.get("c")),
            "name": it.get("n"), "continue_day_cnt": cnt,
            "continue_day_text": f"{cnt}连板" if cnt > 1 else "首板",
            "limit_up_reason": it.get("hybk") or "",
            "industry": it.get("hybk") or "",
            "limit_up_time": (f"{fbt // 10000:02d}:{(fbt % 10000) // 100:02d}"
                              if fbt else ""),
            "seal_money": it.get("fund"),
            "price_change_ratio_pct": it.get("zdp"),
        })
    return out


def _local_names() -> dict[str, str]:
    global _LOCAL_NAMES
    if _LOCAL_NAMES is None:
        from mainrise.signals import load_names
        _LOCAL_NAMES = load_names()
    return _LOCAL_NAMES


def local_limit_pool(date_str: str) -> list[dict]:
    """本地日线计算当日涨停池（close >= limit_price 判定；连板数逐日累计）。"""
    p = paths.data_dir() / "zzshare_daily" / f"{date_str.replace('-', '')}.csv"
    if not p.exists():
        return []
    try:
        df = pd.read_csv(p, dtype={"code": str})
    except Exception:  # noqa: BLE001
        return []
    names = _local_names()
    items = []
    for _, r in df.iterrows():
        code = r["code"]
        at_limit = (r.get("is_paused") == 0 and r.get("close") is not None
                    and r.get("limit_price")
                    and r["close"] >= r["limit_price"] * 0.998)
        if at_limit:
            cnt = _LOCAL_STREAK.get(code, 0) + 1
            _LOCAL_STREAK[code] = cnt
            items.append({
                "ticker": code, "code": code,
                "name": names.get(code, code), "continue_day_cnt": cnt,
                "continue_day_text": f"{cnt}连板" if cnt > 1 else "首板",
                "limit_up_reason": "", "industry": "",
                "limit_up_time": "", "seal_money": None,
                "price_change_ratio_pct": r.get("pct_chg"),
            })
        else:
            _LOCAL_STREAK[code] = 0
    return items


def fetch_dragon_tiger(date_str: str | None, key: str | None = None) -> dict:
    """东财龙虎榜（当日上榜股：买入/卖出/净额/上榜原因，分页取全）。"""
    items: list[dict] = []
    page = 1
    while page <= 5:
        params = {
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
            "pageNumber": page, "pageSize": 500}
        if date_str:
            params["filter"] = f"(TRADE_DATE='{date_str}')"
        r = requests.get(EM_BILLBOARD, params=params, timeout=15)
        j = r.json()
        rows = (j.get("result") or {}).get("data") or []
        if not rows:
            break
        for x in rows:
            code = str(x.get("SECURITY_CODE") or "")[:6]
            items.append({
                "name": x.get("SECURITY_NAME_ABBR"), "ticker": code,
                "code": code, "net_value": x.get("BILLBOARD_NET_AMT"),
                "buy": x.get("BILLBOARD_BUY_AMT"),
                "sell": x.get("BILLBOARD_SELL_AMT"),
                "limit_reason": x.get("EXPLANATION"), "concept_list": [],
            })
        if len(rows) < 500:
            break
        page += 1
    trade_date = ""
    if items:
        trade_date = date_str or ""
    return {"trade_date": trade_date, "count": len(items),
            "stock_items": items}


# ---------------------------------------------------------------- 资金流（东方财富）

def _eastmoney(path: str, params: dict) -> dict:
    """东财公开接口：rc==0 视为成功，失败抛错由调用方兜底。"""
    last = None
    for base in EM_HOSTS:
        for attempt in range(2):
            try:
                r = requests.get(base + path, params=params, timeout=10)
                d = r.json()
                if d.get("rc") == 0:
                    return d.get("data") or {}
                last = RuntimeError(f"{base} rc={d.get('rc')}")
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1 + attempt)
    raise last if last else RuntimeError("eastmoney 接口失败")


def _tx_sector_flow(top: int = 15) -> list[dict]:
    """腾讯行业板块资金流（31 个大行业，成交额+主力净流入，单位亿）。"""
    r = requests.get(TX_RANK, params={
        "board_type": "hy", "sort_type": "price", "direct": "down",
        "offset": 0, "count": max(top, 90)}, timeout=8)
    d = r.json()
    items = (d.get("data") or {}).get("rank_list") or []
    out = []
    for it in items:
        total = float(it.get("turnover") or 0) / 1e4     # 万 -> 亿
        main = float(it.get("zljlr") or 0) / 1e4
        out.append({"name": it.get("name"), "code": it.get("code"),
                    "chg": float(it.get("zdf") or 0), "total": total,
                    "main": main, "super": None, "big": None, "mid": None,
                    "small": None,
                    "main_pct": (main / total * 100) if total else None})
    out.sort(key=lambda x: -x["total"])
    return out[:top]


def _tx_concept_flow(top: int = 200) -> list[dict]:
    """腾讯概念板块（gn，200+）：过滤昨日涨停/新股等非题材项，返回全部真实题材。"""
    r = requests.get(TX_RANK, params={
        "board_type": "gn", "sort_type": "price", "direct": "down",
        "offset": 0, "count": top}, timeout=8)
    d = r.json()
    items = (d.get("data") or {}).get("rank_list") or []
    out = []
    for it in items:
        name = it.get("name") or ""
        if any(j in name for j in CONCEPT_JUNK):
            continue
        total = float(it.get("turnover") or 0) / 1e4
        main = float(it.get("zljlr") or 0) / 1e4
        out.append({"name": name, "code": it.get("code"),
                    "chg": float(it.get("zdf") or 0), "total": total,
                    "main": main,
                    "main_pct": (main / total * 100) if total else None})
    return out


def build_concept_board() -> dict:
    """热门概念板块：Top10（按总资金）+ 特别关注匹配（概念优先、行业兜底）。"""
    try:
        rows = _tx_concept_flow()
    except Exception:  # noqa: BLE001
        rows = []
    try:
        sector = _tx_sector_flow(90)
    except Exception:  # noqa: BLE001
        sector = []
    top = sorted(rows, key=lambda x: -x["total"])[:10]
    watch = []
    for theme, kws in WATCH_THEMES.items():
        hit = _match_board(rows, kws)
        src = "概念"
        if hit is None:
            hit = _match_board(sector, kws)
            src = "行业"
        watch.append({"theme": theme, "board": hit["name"] if hit else None,
                      "source": src if hit else None,
                      "total": hit["total"] if hit else None,
                      "main": hit["main"] if hit else None,
                      "chg": hit["chg"] if hit else None})
    return {"top": top, "watch": watch}


def _safe_concept_board() -> dict:
    try:
        return build_concept_board()
    except Exception:  # noqa: BLE001
        return {"top": [], "watch": []}


def fetch_market_flow() -> dict | None:
    """腾讯全市场资金净值：聚合全部行业板块（总成交额+主力净流入，亿）。"""
    try:
        rows = _tx_sector_flow(90)
        if not rows:
            return None
        return {"source": "腾讯", "boards": len(rows),
                "total": sum(r["total"] for r in rows),
                "main": sum(r["main"] for r in rows)}
    except Exception:  # noqa: BLE001
        return None


def _em_market_minutes() -> list[dict]:
    """东财沪深全市场分钟级累计（主力/超大/大/中/小单）。"""
    try:
        series = []
        for secid in ("1.000001", "0.399001"):
            d = _eastmoney("/api/qt/stock/fflow/kline/get", {
                "lmt": 0, "klt": 1, "secid": secid,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56"})
            series.append([k.split(",") for k in (d.get("klines") or [])])
        by_min: dict[str, list[float]] = {}
        for kl in series:
            for parts in kl:
                if len(parts) != 6:
                    continue
                t = parts[0][11:]  # HH:MM
                vals = [float(v) for v in parts[1:]]
                m = by_min.setdefault(t, [0.0] * 5)
                for i in range(5):
                    m[i] += vals[i]
        minutes = [{"time": t, "main": m[0], "small": m[1], "mid": m[2],
                    "big": m[3], "super": m[4]}
                   for t, m in sorted(by_min.items())]
        return minutes
    except Exception:  # noqa: BLE001
        return []


def fetch_fund_flow() -> dict | None:
    """全市场资金流向：腾讯聚合净值优先，东财分钟序列兜底。"""
    market = None
    try:
        market = fetch_market_flow()
    except Exception:  # noqa: BLE001
        pass
    minutes = []
    try:
        minutes = _em_market_minutes()
    except Exception:  # noqa: BLE001
        minutes = []
    if market is None and not minutes:
        return None
    return {"market": market, "minutes": minutes,
            "latest": minutes[-1] if minutes else None}


def fetch_sector_flow(top: int = 15) -> list[dict]:
    """行业板块资金流：腾讯优先（稳定），失败回退东财。"""
    try:
        rows = _tx_sector_flow(top)
        if rows:
            return rows
    except Exception:  # noqa: BLE001
        pass
    return _em_sector_flow(top)


def _em_sector_flow(top: int = 15) -> list[dict]:
    """东财行业板块资金流（大分类，按总资金=成交额 TopN，实时）。"""
    d = _eastmoney("/api/qt/clist/get", {
        "pn": 1, "pz": top, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f6", "fs": "m:90+t:2+f:!50",
        "fields": "f12,f14,f3,f6,f62,f184,f66,f72,f78"})
    out = []
    for it in (d.get("diff") or []):
        total = (it.get("f6") or 0) / 1e8
        main = (it.get("f62") or 0) / 1e8
        super_ = (it.get("f66") or 0) / 1e8
        big = (it.get("f72") or 0) / 1e8
        small = (it.get("f78") or 0) / 1e8
        mid = -(main + small)   # 主力=超大+大单，四类合计≈0
        out.append({"name": it.get("f14"), "code": it.get("f12"),
                    "chg": it.get("f3"), "total": total, "main": main,
                    "super": super_, "big": big, "mid": mid,
                    "small": small, "main_pct": it.get("f184")})
    return out


def recent_trading_days(n: int = TREND_DAYS, end: str | None = None) -> list[str]:
    """从本地交易日历取最近 n 个交易日（<= end）。"""
    p = paths.trade_dates_path()
    end = end or datetime.now(TZ_CN).strftime("%Y-%m-%d")
    days = []
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("trade_date") and s <= end:
                    days.append(s.split(",")[0])
    if not days:  # 兜底：近 n 个工作日
        d = datetime.now(TZ_CN).date()
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d -= timedelta(days=1)
    return days[-n:]


# ---------------------------------------------------------------- 分析

def tokenize_reason(reason: str) -> list[str]:
    return [t.strip() for t in re.split(r"[+、,，/；;|]", reason or "")
            if t.strip()]


def build_review(date_str: str | None = None, key: str | None = None) -> dict:
    """取数并计算复盘数据。"""
    today = date_str or datetime.now(TZ_CN).strftime("%Y-%m-%d")
    days = recent_trading_days(TREND_DAYS, end=today)
    _LOCAL_STREAK.clear()
    pools: dict[str, list[dict]] = {}
    for d in days:
        pools[d] = fetch_limit_up_pool(d, key=key)
    pool = pools.get(today, [])

    # 按连板数分组（从高到低）
    by_count: list[dict] = []
    for cnt in sorted({x.get("continue_day_cnt", 1) for x in pool},
                      reverse=True):
        stocks = [x for x in pool if x.get("continue_day_cnt", 1) == cnt]
        by_count.append({"board": f"{cnt}连板" if cnt > 1 else "首板",
                         "cnt": cnt, "stocks": stocks})

    # 按题材分组（涨停原因关键词，取前 12）
    theme_map: dict[str, list[dict]] = {}
    for x in pool:
        for tok in tokenize_reason(x.get("limit_up_reason", "")):
            theme_map.setdefault(tok, []).append(x)
    by_theme = sorted(
        ({"theme": t, "count": len(v),
          "max_board": max(s.get("continue_day_cnt", 1) for s in v),
          "stocks": v} for t, v in theme_map.items()),
        key=lambda r: (r["count"], r["max_board"]), reverse=True)[:12]

    # 情绪指标（近 12 日）
    zt_counts = [len(pools[d]) for d in days]
    height = max((x.get("continue_day_cnt", 1) for x in pool), default=1)
    two_plus = sum(1 for x in pool if x.get("continue_day_cnt", 1) >= 2)
    prev_first = {x["ticker"] for x in pools.get(days[-2], [])
                  if x.get("continue_day_cnt", 1) == 1}
    promoted = sum(1 for x in pool
                   if x.get("continue_day_cnt", 1) >= 2
                   and x.get("ticker") in prev_first)
    promo_rate = (promoted / len(prev_first) if prev_first else None)
    avg10 = (sum(zt_counts) / len(zt_counts) if zt_counts else 0)
    metrics = {
        "today": today,
        "zt_count": len(pool),
        "zt_avg10": round(avg10, 1),
        "height": height,
        "two_plus": two_plus,
        "first_count": sum(1 for x in pool if x.get("continue_day_cnt", 1) == 1),
        "promoted": promoted,
        "prev_first": len(prev_first),
        "promo_rate": round(promo_rate * 100, 1) if promo_rate is not None else None,
        "trend": [{"date": d, "zt": len(pools[d]),
                   "height": max((x.get("continue_day_cnt", 1) for x in pools[d]),
                                 default=1)} for d in days],
    }
    cycle, reason = classify_sentiment(metrics)
    metrics["cycle"] = cycle
    metrics["cycle_reason"] = reason

    # 龙虎榜资金（买入/卖出/净额，口径=当日上榜股；东财无机构/游资分类）
    dt = fetch_dragon_tiger(today, key=key)
    dt_stocks = dt.get("stock_items") or []
    dt_view = [{
        "name": x.get("name"), "code": x.get("ticker"),
        "net": x.get("net_value"), "buy": x.get("buy"),
        "sell": x.get("sell"), "reason": x.get("limit_reason"),
    } for x in dt_stocks]
    return {
        "today": today,
        "pool_size": len(pool),
        "by_count": by_count,
        "by_theme": by_theme,
        "metrics": metrics,
        "fund_flow": fetch_fund_flow(),
        "concept_flow": _safe_sector_flow(),
        "concept_board": _safe_concept_board(),
        "dragon": {"trade_date": dt.get("trade_date"),
                   "count": dt.get("count"),
                   "buy_total": sum(x.get("buy") or 0 for x in dt_stocks),
                   "sell_total": sum(x.get("sell") or 0 for x in dt_stocks),
                   "net_total": sum(x.get("net_value") or 0 for x in dt_stocks),
                   "stocks": dt_view},
    }


def _safe_sector_flow() -> list[dict]:
    """行业板块资金流容错：东财不可用时返回空列表，不阻塞复盘页生成。"""
    try:
        return fetch_sector_flow(15)
    except Exception:  # noqa: BLE001
        return []


def classify_sentiment(m: dict) -> tuple[str, str]:
    """启发式情绪周期：冰点/启动/发酵/高潮/退潮/震荡。"""
    zt, height, two_plus = m["zt_count"], m["height"], m["two_plus"]
    promo = m["promo_rate"]
    avg10 = m["zt_avg10"]
    prev_avg = m["trend"][-2]["zt"] if len(m["trend"]) > 1 else avg10
    rising = zt > prev_avg * 1.15 and prev_avg > 0
    falling = zt < prev_avg * 0.85 and prev_avg > 0
    if height >= 7 or zt >= 90:
        return "高潮", (f"涨停{zt}家、最高{height}板，情绪亢奋，注意分歧风险")
    if height >= 5 or zt >= 70 or (promo and promo >= 35 and zt >= 40):
        return "发酵", (f"涨停{zt}家、最高{height}板、首板晋级{promo or 0}%，"
                       f"赚钱效应扩散")
    if zt <= 25 and height <= 2:
        return "冰点", (f"涨停仅{zt}家、最高{height}板，短线冷清，等待修复")
    if falling and height <= 3 and (promo is None or promo < 20):
        return "退潮", (f"涨停{zt}家较前日{prev_avg}家回落，高度降至{height}板，"
                       f"亏钱效应阶段")
    if rising and promo is not None and promo >= 25:
        return "启动", (f"涨停{zt}家回升（前日{prev_avg}家）、首板晋级{promo}%，"
                       f"新周期试错期")
    return "震荡", (f"涨停{zt}家（10日均{avg10}家）、最高{height}板，"
                   f"多空拉锯，结构性行情")


# ---------------------------------------------------------------- 渲染

def _svg_trend(trend: list[dict]) -> str:
    """涨停家数 + 最高连板 双线折线图（SVG，无外部依赖）。"""
    w, h = 820, 260
    pad_l, pad_r, pad_t, pad_b = 46, 16, 18, 34
    zts = [t["zt"] for t in trend]
    hs = [t["height"] for t in trend]
    max_z = max(zts + [1]) * 1.15
    max_h = max(hs + [1])
    n = len(trend)
    def x(i): return pad_l + (w - pad_l - pad_r) * i / max(1, n - 1)
    def yz(v): return pad_t + (h - pad_t - pad_b) * (1 - v / max_z)
    def yh(v): return pad_t + (h - pad_t - pad_b) * (1 - v / max_h)
    grid = "".join(
        f'<line x1="{pad_l}" y1="{y}" x2="{w - pad_r}" y2="{y}" '
        f'stroke="#21262D" stroke-width="1"/>'
        for y in range(pad_t, h - pad_b + 1, 40))
    def line(ys, color, width=2.2):
        pts = " ".join(f"{x(i)},{ys[i]}" for i in range(len(ys)))
        return (f'<polyline points="{pts}" fill="none" stroke="{color}" '
                f'stroke-width="{width}" stroke-linejoin="round"/>')
    dots = "".join(
        f'<circle cx="{x(i)}" cy="{yz(zts[i])}" r="3" fill="#F85149"/>'
        f'<circle cx="{x(i)}" cy="{yh(hs[i])}" r="3" fill="#39D2C0"/>'
        for i in range(n))
    labels = "".join(
        f'<text x="{x(i)}" y="{h - 10}" fill="#8B949E" font-size="10" '
        f'text-anchor="middle">{t["date"][5:]}</text>'
        for i, t in enumerate(trend) if i % max(1, n // 8) == 0)
    zt_last = zts[-1]
    h_last = hs[-1]
    legend = (f'<text x="{w - 170}" y="{pad_t + 12}" fill="#F85149" font-size="11">'
              f'● 涨停家数 {zt_last}</text>'
              f'<text x="{w - 90}" y="{pad_t + 12}" fill="#39D2C0" font-size="11">'
              f'● 最高板 {h_last}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:820px">'
            f"{grid}{line([yz(v) for v in zts], '#F85149')}"
            f"{line([yh(v) for v in hs], '#39D2C0')}{dots}{labels}{legend}</svg>")


def _svg_flow(minutes: list[dict]) -> str:
    """资金流向 5 线折线图（主力/超大/大/中/小单，当日累计，单位亿）。"""
    w, h = 820, 280
    pad_l, pad_r, pad_t, pad_b = 52, 16, 20, 30
    keys = [("main", "#F85149"), ("super", "#BC8CFF"), ("big", "#D29922"),
            ("mid", "#58A6FF"), ("small", "#3FB950")]
    ys = {k: [m[k] / 1e8 for m in minutes] for k, _ in keys}
    allv = [v for series in ys.values() for v in series] or [0]
    lo, hi = min(allv), max(allv)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    n = len(minutes)
    def x(i): return pad_l + (w - pad_l - pad_r) * i / max(1, n - 1)
    def y(v): return pad_t + (h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))
    grid = "".join(
        f'<line x1="{pad_l}" y1="{yy}" x2="{w - pad_r}" y2="{yy}" '
        f'stroke="#21262D" stroke-width="1"/>'
        for i in range(5)
        for yy in [pad_t + (h - pad_t - pad_b) * i / 4])
    lines = "".join(
        f'<polyline points="{" ".join(f"{x(i)},{y(v)}" for i, v in enumerate(series))}" '
        f'fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
        for series, (_, color) in zip(ys.values(), keys))
    dots = "".join(
        f'<circle cx="{x(n - 1)}" cy="{y(series[-1])}" r="3" fill="{color}"/>'
        for series, (_, color) in zip(ys.values(), keys))
    labels = "".join(
        f'<text x="{x(i)}" y="{h - 10}" fill="#8B949E" font-size="10" '
        f'text-anchor="middle">{minutes[i]["time"]}</text>'
        for i in range(n) if i % max(1, n // 10) == 0)
    names = {"main": "主力", "super": "超大单", "big": "大单",
             "mid": "中单", "small": "小单"}
    legend = "".join(
        f'<text x="{w - 200 + (j % 2) * 105}" y="{pad_t + 14 + (j // 2) * 16}" '
        f'fill="{color}" font-size="10">{names[k]} {ys[k][-1]:+.1f}亿</text>'
        for j, (k, color) in enumerate(keys))
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:820px">'
            f"{grid}{lines}{dots}{labels}{legend}</svg>")


def _fmt_yi(v) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:+.2f}亿"
    if a >= 1e4:
        return f"{v / 1e4:+.0f}万"
    return f"{v:+.0f}"


def render_review_html(r: dict) -> str:
    m = r["metrics"]
    cycle_colors = {"冰点": "#58A6FF", "启动": "#3FB950", "发酵": "#D29922",
                    "高潮": "#F85149", "退潮": "#8B949E", "震荡": "#39D2C0"}
    cc = cycle_colors.get(m["cycle"], "#8B949E")

    def kpi(label, value, color):
        return (f'<div class="kpi" style="border-bottom-color:{color}">'
                f'<div class="label">{label}</div>'
                f'<div class="value" style="color:{color}">{value}</div></div>')

    # 连板梯队（按连板数）
    sections = ""
    if not r["by_count"]:
        sections += '<div class="empty">当日暂无涨停股。</div>'
    for g in r["by_count"]:
        trs = "".join(
            f"<tr><td>{_code_link(s.get('ticker'))}</td><td>{_esc(s.get('name'))}</td>"
            f"<td>{_esc(s.get('limit_up_time'))}</td>"
            f"<td>{_fmt_yi(s.get('seal_money'))}</td>"
            f"<td style='white-space:normal'>{_esc(s.get('limit_up_reason'))}</td></tr>"
            for s in g["stocks"])
        color = "#F85149" if g["cnt"] >= 4 else (
            "#D29922" if g["cnt"] >= 2 else "#8B949E")
        sections += (f"<h2 style='color:{color}'>{g['board']}（{len(g['stocks'])}只）</h2>"
                     '<div class="tbl-wrap"><table>'
                     "<tr><th>代码</th><th>名称</th><th>涨停时间</th><th>封单</th>"
                     "<th>涨停原因/题材</th></tr>" + trs + "</table></div>")

    # 题材分组
    theme_trs = "".join(
        f"<tr><td>{_esc(t['theme'])}</td><td>{t['count']}</td><td>{t['max_board']}板</td>"
        f"<td style='white-space:normal'>"
        + "、".join(f"{_esc(s.get('name'))}({s.get('continue_day_cnt', 1)}板)"
                    for s in t["stocks"][:6])
        + ("…" if len(t["stocks"]) > 6 else "") + "</td></tr>"
        for t in r["by_theme"])

    # 龙虎榜
    dt = r["dragon"]
    dt_trs = "".join(
        f"<tr><td>{_esc(s['name'])}</td><td>{_code_link(s['code'])}</td>"
        f"<td>{_fmt_yi(s['net'])}</td><td>{_fmt_yi(s['buy'])}</td>"
        f"<td>{_fmt_yi(s['sell'])}</td>"
        f"<td style='white-space:normal'>{_esc(s['reason'] or '')}</td></tr>"
        for s in dt["stocks"][:15])

    promo = f"{m['promo_rate']}%" if m["promo_rate"] is not None else "-"
    stale_note = ("<br>⚠ 资金流/板块为缓存数据（东财接口暂不可用），"
                  "恢复后重跑 `mainrise review` 自动更新。"
                  if r.get("flow_stale") else "")
    # 资金流卡
    ff = r.get("fund_flow")
    stale_warn = ('<div class="note" style="color:#F85149;border:1px solid #F85149;'
                  'border-radius:6px;margin-bottom:8px">⚠ 资金流向为<b>缓存数据</b>'
                  '（数据源暂不可用，最后成功更新至 '
                  f'{_esc(ff["latest"]["time"]) if ff and ff.get("latest") else "-"}'
                  '）；恢复后点「🔄 刷新」即可更新。</div>'
                  if r.get("flow_stale") else "")
    if ff and (ff.get("market") or ff.get("latest")):
        market = ff.get("market")
        kpis = ""
        if market:
            kpis += kpi("全市场总成交额", f"{market['total']:,.0f}亿", "#39D2C0")
            kpis += kpi("全市场主力净流入",
                        f"{market['main']:+.2f}亿",
                        "#F85149" if market["main"] > 0 else (
                            "#3FB950" if market["main"] < 0 else "#8B949E"))
        lt = ff.get("latest")
        if lt:
            kpis += kpi("主力净流入(东财分钟)",
                        f"{lt['main'] / 1e8:+.2f}亿",
                        "#F85149" if lt["main"] > 0 else (
                            "#3FB950" if lt["main"] < 0 else "#8B949E"))
        minutes = ff.get("minutes") or []
        chart = (f'<div style="padding:10px 0">{_svg_flow(minutes)}</div>'
                 if minutes else "")
        src = "腾讯" if market else "东方财富"
        boards = market["boards"] if market else "-"
        flow_card = (f'<section class="card"><h2>资金流向（沪深全市场 · {src}优先）</h2>'
                     f'<div class="body">{stale_warn}{kpis}{chart}'
                     f'<div class="note">总成交额/主力净流入聚合自腾讯行业板块'
                     f'（{boards} 个，覆盖全市场）；分钟明细来自东方财富'
                     f'（腾讯暂无分钟级资金流），口径：超大单+大单=主力。'
                     f'盘中为动态数据。</div>'
                     f'</div></section>')
    else:
        flow_card = ('<section class="card"><h2>资金流向（沪深全市场）</h2>'
                     '<div class="body"><div class="empty">资金流数据暂不可用'
                     '（腾讯/东财均未返回）。</div></div></section>')
    # 行业板块资金流卡
    cf = r.get("concept_flow") or []
    if cf:
        cf_trs = "".join(
            f"<tr><td>{_esc(c['name'])}</td><td>{_chg_span(c['chg'])}</td>"
            f"<td>{_fmt_yi(c['total'] * 1e8)}</td><td>{_fmt_yi(c['main'] * 1e8)}</td>"
            f"<td>{c['main_pct'] if c['main_pct'] is not None else '-'}%</td></tr>"
            for c in cf)
        concept_card = (f'<section class="card"><h2>行业板块资金流（大分类）'
                        f'（总资金 Top{len(cf)} · 实时）</h2>'
                        '<div class="body"><div class="tbl-wrap"><table>'
                        "<tr><th>板块</th><th>涨跌%</th><th>总资金(成交额)</th>"
                        "<th>资金净额(主力)</th><th>主力净占比</th></tr>"
                        f"{cf_trs}</table></div>"
                        "<div class='note'>腾讯行业板块（31 个大行业分类），按总资金="
                        "成交额排序；数据源：腾讯优先、东财兜底，均失败才显示缓存。"
                        "盘中实时、收盘后为最终值。</div></div></section>")
    else:
        concept_card = ('<section class="card"><h2>行业板块资金流</h2>'
                        '<div class="body"><div class="empty">行业板块资金流'
                        '暂不可用。</div></div></section>')
    # 热门概念板块卡（腾讯 · 特别关注 + Top10）
    cb = r.get("concept_board") or {}
    cb_watch = cb.get("watch") or []
    cb_top = cb.get("top") or []
    if cb_watch or cb_top:
        watch_rows = "".join(
            f"<tr><td style='color:#39D2C0;font-weight:bold'>★ {_esc(w['theme'])}</td>"
            f"<td>{_esc(w['board'] or '暂无匹配')}"
            f"{'（行业）' if w.get('source') == '行业' else ''}</td>"
            f"<td>{_fmt_yi((w['total'] or 0) * 1e8)}</td>"
            f"<td>{_fmt_yi((w['main'] or 0) * 1e8)}</td>"
            f"<td>{_chg_span(w['chg'])}</td></tr>" for w in cb_watch)
        top_rows = "".join(
            f"<tr><td>{_esc(c['name'])}</td><td>{_chg_span(c['chg'])}</td>"
            f"<td>{_fmt_yi(c['total'] * 1e8)}</td><td>{_fmt_yi(c['main'] * 1e8)}</td>"
            f"<td>{c['main_pct'] if c['main_pct'] is not None else '-'}%</td></tr>"
            for c in cb_top)
        concept_board_card = (
            f'<section class="card"><h2>热门概念板块（腾讯 · 特别关注）</h2>'
            f'<div class="body">'
            f'<div style="color:#39D2C0;font-weight:bold;padding:6px 0">★ 特别关注</div>'
            '<div class="tbl-wrap"><table>'
            "<tr><th>主题</th><th>对应板块</th><th>总资金(成交额)</th>"
            f"<th>资金净额</th><th>涨跌%</th></tr>{watch_rows}</table></div>"
            f'<div style="color:#39D2C0;font-weight:bold;padding:10px 0 6px">'
            f'概念板块 Top{len(cb_top)}（按总资金）</div>'
            '<div class="tbl-wrap"><table>'
            "<tr><th>板块</th><th>涨跌%</th><th>总资金(成交额)</th>"
            f"<th>资金净额</th><th>主力净占比</th></tr>{top_rows}</table></div>"
            "<div class='note'>数据源：腾讯概念板块（已过滤昨日涨停/新股等非题材项）；"
            "特别关注按关键词匹配对应板块（取总资金最大者）。</div>"
            "</div></section>")
    else:
        concept_board_card = ('<section class="card"><h2>热门概念板块（腾讯）</h2>'
                              '<div class="body"><div class="empty">概念板块数据暂不可用。'
                              '</div></div></section>')
    body = f"""
<header>
  <h1>当日复盘 · {m['today']}</h1>
  <div class="sub"><a href="index.html" style="color:#8B949E">首页</a>
   ｜ <a href="dashboard.html" style="color:#8B949E">KPI仪表盘</a>
   ｜ <a href="live.html" style="color:#8B949E">实时盯盘</a>
   ｜ 数据源：东财涨停池/龙虎榜 + 本地日线（{m['today']}）</div>
</header>
<div class="kpis">
  {kpi("短线情绪周期", m["cycle"], cc)}
  {kpi("涨停家数", m["zt_count"], "#F85149")}
  {kpi("最高连板", f"{m['height']}板", "#D29922")}
  {kpi("连板家数", m["two_plus"], "#D29922")}
  {kpi("首板晋级率", promo, "#58A6FF")}
  {kpi("涨停10日均值", m["zt_avg10"], "#8B949E")}
</div>
<div class="card"><div class="body note" style="padding-top:10px">
  {_esc(m["cycle_reason"])}（启发式判定，仅作研究参考）</div></div>

<section class="card">
  <h2>短线情绪趋势（近{len(m["trend"])}个交易日）</h2>
  <div class="body">{_svg_trend(m["trend"])}</div>
</section>

{flow_card}

{concept_card}

{concept_board_card}

<section class="card">
  <h2>连板梯队（按连板数）</h2>
  <div class="body">{sections}</div>
</section>

<section class="card">
  <h2>连板梯队（按行业/题材，Top{len(r["by_theme"])}）</h2>
  <div class="body"><div class="tbl-wrap"><table>
    <tr><th>题材</th><th>涨停数</th><th>最高板</th><th>代表个股</th></tr>
    {theme_trs}</table></div></div>
</section>

<section class="card">
  <h2>龙虎榜资金（{_esc(dt["trade_date"])}，上榜 {dt["count"]} 只）</h2>
  <div class="body">
    <div class="kpis">
      {kpi("上榜净额合计", _fmt_yi(dt["net_total"]), "#F85149")}
      {kpi("总买入", _fmt_yi(dt["buy_total"]), "#F85149")}
      {kpi("总卖出", _fmt_yi(dt["sell_total"]), "#3FB950")}
    </div>
    <div class="tbl-wrap"><table>
      <tr><th>名称</th><th>代码</th><th>净额</th><th>买入</th><th>卖出</th><th>上榜原因</th></tr>
      {dt_trs}</table></div>
    <div class="note">口径说明：龙虎榜仅覆盖当日上榜股（涨停/异动），
    买卖/净额合计仅代表上榜标的，不等同全市场资金流向；机构/游资分类
    东财接口不提供，故以买卖净额呈现。</div>
  </div>
</section>

<div class="note">数据说明：连板/行业分组：当日来自东方财富涨停池
（{m["today"]}，盘中动态、收盘后最终；东财不含涨停原因关键词，以行业板块代替），
历史 12 日来自本地全市场日线计算（收盘涨停判定，含连板累计）；
龙虎榜来自东方财富（买卖净额口径）；情绪周期为启发式判定。
资金流向与行业板块来自东方财富公开接口（口径：超大单/大单/中单/小单，
主力=超大单+大单；中单≈大户、小单≈散户）。
所有内容仅供研究学习，不构成投资建议。</div>{stale_note}
"""
    return _page(f"当日复盘 · {m['today']}", body, refresh="review")


# ---------------------------------------------------------------- 对外入口

def update_review(output_dir: Path | None = None, date_str: str | None = None,
                  key: str | None = None) -> Path:
    out = Path(output_dir) if output_dir else paths.web_dir()
    out.mkdir(parents=True, exist_ok=True)
    r = build_review(date_str, key=key)
    # 东财数据缓存：取数失败时沿用上次成功数据，页面不降级为空
    cache_path = out / ".flow_cache.json"
    r["flow_stale"] = False
    ff = r.get("fund_flow") or {}
    if not ff.get("market") or not r.get("concept_flow"):
        if cache_path.exists():
            try:
                c = json.loads(cache_path.read_text(encoding="utf-8"))
                if not ff.get("market") and c.get("fund_flow", {}).get("market"):
                    r["fund_flow"] = c["fund_flow"]
                    r["flow_stale"] = True
                if not r.get("concept_flow") and c.get("concept_flow"):
                    r["concept_flow"] = c["concept_flow"]
                    r["flow_stale"] = True
            except Exception:  # noqa: BLE001
                pass
    if (r.get("fund_flow") or {}).get("market") and r.get("concept_flow"):
        try:
            cache_path.write_text(json.dumps(
                {"fund_flow": r["fund_flow"], "concept_flow": r["concept_flow"]},
                ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    (out / "review.json").write_text(
        json.dumps(r, ensure_ascii=False), encoding="utf-8")
    (out / "review.html").write_text(render_review_html(r), encoding="utf-8")
    return out / "review.html"


if __name__ == "__main__":
    print(update_review())
