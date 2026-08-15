"""盘中实时盯盘（网页版，无推送）。

交易日盘中（北京时间 9:30-11:30 / 13:00-15:00）每 3 秒轮询腾讯实时行情，
自动监控 持仓 + 观察池，按模型纪律触发盘中提醒：
  - 卖出信号：持仓止损-4%（低点触发）/ 高点回落8% / 跌破 MA10/MA20
  - 买入信号：两级模型（B3 打底仓 / 二波加仓）+ 启动加仓模型
盘中提醒仅针对买入/卖出信号（不含涨跌幅/MA 触碰噪音）。
每只票同类提醒 10 分钟限频；接口失败自动退避。

输出 output/web/live.html（页面内每 3 秒轮询更新 + 分时缩略图）+ live.json，
沿用 Nginx 同一访问口令。

用法:
    python3 -m mainrise.cli monitor             # 常驻：盘中轮询、盘后休眠
    python3 -m mainrise.cli monitor --once      # 立即跑一轮后退出（测试）
    python3 -m mainrise.cli monitor --interval 10
"""
from __future__ import annotations

import json
import glob
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise import market_state as ms_mod
from mainrise.snapshot import fetch_snapshot
from mainrise.web_dashboard import CSS, _chg_span, _esc, _fmt, _refresh_ui

TZ_CN = timezone(timedelta(hours=8))   # A 股统一北京时间，不受服务器时区影响
STOP = 0.96            # -4% 止损（2026-08-13 用户确认收紧：两天连续破位割肉）
PULLBACK = 0.92        # 高点回落 8%
COOLDOWN_SEC = 600     # 每只票同类提醒限频 10 分钟
DEFAULT_INTERVAL = 3   # 盘中轮询间隔（秒）：行情秒级更新
LIVE_POLL_MS = 3000    # 页面内 JS 轮询间隔（毫秒）
LIVE_FALLBACK_REFRESH = 30   # 页面整页刷新兜底（秒）
ALERT_HISTORY = 50     # 提醒记录保留条数
PENDING_STATUSES = {"B3打底仓", "二波加仓", "B3待二波"}
TIDE_ZT = 50            # 市场退潮保护：全市场涨停家数 < 50 → 启动模型持仓次日清仓
ZT_REFRESH_SEC = 60     # 东财涨停池缓存（秒）
_DAILY_FEATURES: dict[str, dict] = {}
_DAILY_FEATURES_DATE = ""
_ZT_CACHE = {"ts": 0.0, "count": None}


# ---------------------------------------------------------------- 时间判断

def beijing_now() -> datetime:
    return datetime.now(TZ_CN)


def market_status(now: datetime | None = None, trade_dates: set | None = None) -> str:
    now = now or beijing_now()
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return "周末休市"
    if trade_dates is not None and now.strftime("%Y-%m-%d") not in trade_dates:
        return "节假日休市"
    if hm < 570:
        return "盘前"
    if hm < 690:
        return "上午盘中"
    if hm < 780:
        return "午间休市"
    if hm < 900:
        return "下午盘中"
    return "盘后"


def is_trading_time(now: datetime | None = None, trade_dates: set | None = None) -> bool:
    return market_status(now, trade_dates) in ("上午盘中", "下午盘中")


def load_trade_dates(path: Path | None = None) -> set[str]:
    p = path or paths.trade_dates_path()
    if not p.exists():
        return set()
    out = set()
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("trade_date"):
                out.add(line.split(",")[0])
    return out


# ---------------------------------------------------------------- 数据读取

def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"code": str})


# ---------------- 大牛模型候选/持仓（轻量：读取 bigbull 每日落盘的 JSON） ----------------
_BB_FILE_CACHE: dict = {"ts": 0.0, "data": {}, "err": ""}
BB_FILE_TTL = 60.0          # 文件读取缓存（秒）


def _bigbull_data(state_dir: Path | None = None) -> dict:
    """读取 bigbull 落盘 output/state/bigbull_cands.json（含候选与持仓）。

    仅做文件读取（含 60s 缓存），不做全量行情计算——候选/持仓为日频特征，
    盘中只刷新实时报价（由 3 秒轮询负责）。
    """
    if time.time() - _BB_FILE_CACHE["ts"] <= BB_FILE_TTL and \
            _BB_FILE_CACHE["ts"] > 0:
        return _BB_FILE_CACHE["data"]
    p = Path(state_dir) if state_dir else paths.state_dir()
    f = p / "bigbull_cands.json"
    data: dict = {}
    try:
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        _BB_FILE_CACHE.update({"ts": time.time(),
                               "data": _BB_FILE_CACHE["data"], "err": str(e)})
        return _BB_FILE_CACHE["data"]
    _BB_FILE_CACHE.update({"ts": time.time(), "data": data, "err": ""})
    return data


def _bigbull_cands(state_dir: Path | None = None) -> list:
    """大牛模型候选（近45日硬规则信号，评分≥2）。"""
    return _bigbull_data(state_dir).get("cands") or []


def _bigbull_holdings(state_dir: Path | None = None) -> list:
    """大牛模型当前持仓（bigbull 交割单未平仓 + 最新 MA20/收盘）。"""
    return _bigbull_data(state_dir).get("holdings") or []


def load_monitor_rows(state_dir: Path | None = None,
                      reports_dir: Path | None = None) -> dict:
    """汇总待监控标的：持仓（active/pending）+ 观察池 + 最新日线 MA 参考。"""
    states = Path(state_dir) if state_dir else paths.state_dir()
    reports = Path(reports_dir) if reports_dir else paths.report_dir()
    from mainrise.signals import load_names
    names = load_names()

    watch = _read_csv(states / "mainrise_watchlist.csv")
    if not watch.empty:
        watch = watch[["code", "name"]].copy()
    pos = _read_csv(states / "mainrise_positions.csv")
    rows: dict[str, dict] = {}
    if not pos.empty:
        for _, r in pos.iterrows():
            if r.get("status") not in ("active", "pending"):
                continue
            code = str(r["code"])
            buy = _num(r.get("buy_price"))
            peak = _num(r.get("peak"))
            rows[code] = {
                "code": code,
                "name": str(r.get("name") or "").strip() or code,
                "group": "持仓",
                "buy_price": buy,
                "peak": peak,
            }
    if not watch.empty:
        for _, r in watch.iterrows():
            code = str(r["code"])
            name = str(r.get("name") or "").strip() or code
            if code in rows:
                rows[code]["name"] = name
            else:
                rows[code] = {"code": code, "name": name, "group": "观察",
                              "buy_price": None, "peak": None}
    # 中文名回退到股票名册，避免显示"待补"
    for code, info in rows.items():
        if not info["name"] or info["name"] == "待补":
            info["name"] = names.get(code, "") or code

    # 最新日线 MA10/MA20（回踩支撑参考）
    ma_map: dict[str, tuple[float | None, float | None]] = {}
    report_date = ""
    cands = sorted(reports.glob("主升浪跟踪_*.csv"))
    if cands:
        report_date = cands[-1].stem.replace("主升浪跟踪_", "")
        df = _read_csv(cands[-1])
        if not df.empty and {"code", "ma10", "ma20"}.issubset(df.columns):
            for _, r in df.iterrows():
                code = str(r["code"])
                ma_map[code] = (_num(r.get("ma10")), _num(r.get("ma20")))
                if code in rows:
                    rows[code]["status"] = str(r.get("status") or "").strip()
    for code, info in rows.items():
        info.setdefault("status", "")
    # 启动加仓模型扫描范围：72 只行业卡点企业（不在持仓/观察池内的也扫）
    from mainrise.report import load_chokepoint_codes, load_industry_info
    scan_codes = load_chokepoint_codes() - set(rows)
    ck_names = load_industry_info()
    scan_names = {c: ck_names[c]["name"] for c in scan_codes}
    # 两级模型信号（B3 打底仓 / 二波加仓，收盘判定，来自每日报告）
    ts = _read_csv(states / "mainrise_twostage.csv")
    if not ts.empty:
        for _, r in ts.iterrows():
            code = str(r["code"])
            level = str(r.get("level") or "").strip()
            if level not in ("B3", "二波"):
                continue
            sig_date = str(r.get("date") or "").strip()
            if sig_date and report_date and sig_date != report_date:
                continue
            info = rows.setdefault(code, {})
            info.update({
                "code": code,
                "name": str(r.get("name") or "").strip() or names.get(code, "") or code,
                "group": "两级",
                "status": level,
                "buy_price": None, "peak": None,
                "twostage_action": str(r.get("action") or "").strip() or (
                    "明日开盘打底仓（计划仓位 2/3）" if level == "B3"
                    else "明日开盘加仓（1/3，总仓≤1/3）"),
            })
    # 大牛模型候选（评分≥2 硬规则，近 45 日信号，实时轮询展示）
    bb_cands = _bigbull_cands(states)
    for c in bb_cands:
        code = c["code"]
        info = rows.setdefault(code, {})
        info.update({
            "code": code,
            "name": str(info.get("name") or names.get(code, "") or code),
            "group": "大牛模型", "status": "大牛模型",
            "buy_price": None, "peak": None,
            "bb_score": c["score"], "bb_date": c["date"],
        })
    # 大牛模型当前持仓（bigbull 交割单未平仓）：盘中按 MA20 监控卖出（14:50 推送口径）
    for h in _bigbull_holdings(states):
        code = h["code"]
        info = rows.setdefault(code, {})
        info.update({
            "code": code,
            "name": str(info.get("name") or names.get(code, "") or code),
            "group": "大牛模型", "status": "大牛模型持仓",
            "buy_price": None, "peak": None,
            "bb_score": h.get("score"), "bb_date": h.get("entry_date"),
            "bb_hold": True, "bb_entry": h.get("entry"),
        })
    daily = load_daily_features(set(rows) | scan_codes,
                                datetime.now(TZ_CN).strftime("%Y-%m-%d"))
    return {"rows": rows, "ma_map": ma_map, "daily": daily,
            "report_date": report_date, "scan_codes": scan_codes,
            "scan_names": scan_names, "bb": bb_cands}


def _num(v) -> float | None:
    try:
        return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 盘中买卖信号

def _trading_minutes(now: datetime) -> int:
    """截至 now 的当日交易分钟数（9:30-11:30 / 13:00-15:00）。"""
    hm = now.hour * 60 + now.minute
    if hm < 570:
        return 0
    if hm < 690:
        return hm - 570
    if hm < 780:
        return 120
    if hm < 900:
        return 120 + (hm - 780)
    return 240


def _volume_ratio(volume: float | None, avg5_vol: float | None,
                  elapsed: int) -> float | None:
    """实时量比 = 成交量(手)×100 ÷ (5日均量/240 × 已交易分钟)。"""
    if not volume or not avg5_vol or elapsed <= 0:
        return None
    return (volume * 100) / (avg5_vol / 240 * elapsed)


def _realtime_zt_count(now: datetime | None = None) -> int | None:
    """盘中全市场涨停家数（东财涨停池，60 秒缓存；失败/非交易时段返回 None）。"""
    global _ZT_CACHE
    now = now or beijing_now()
    if not is_trading_time(now):
        return None
    if now.timestamp() - _ZT_CACHE["ts"] < ZT_REFRESH_SEC:
        return _ZT_CACHE["count"]
    try:
        from mainrise import review
        pool = review._em_zt_pool(now.strftime("%Y-%m-%d"))
        _ZT_CACHE = {"ts": now.timestamp(), "count": len(pool)}
    except Exception:  # noqa: BLE001  接口失败不判定市场门限
        _ZT_CACHE = {"ts": now.timestamp(), "count": None}
    return _ZT_CACHE["count"]


def _launch_signal(r: dict, d: dict | None, mkt_zt: int | None) -> str:
    """启动加仓模型盘中信号（无未来函数，当日数据判定）。
    r9A：回撤≥15% + 涨≥5% + 量比≥1.3 + 前5日<0 + 涨停≥130；
    r7mA：回撤≥15% + 涨≥5% + 量比1~2 + 前5日<0 + 涨停≥90。"""
    if not d or mkt_zt is None:
        return ""
    px = r.get("price")
    chg = r.get("chg")
    vr = r.get("vr")
    hi20 = d.get("hi20")
    close5 = d.get("close5")
    if not px or chg is None or vr is None or not hi20 or not close5:
        return ""
    if px / hi20 - 1 > -0.15:          # 20 日回撤 ≥15%
        return ""
    if chg < 5:                        # 当日涨幅 ≥5%
        return ""
    if px / close5 - 1 >= 0:           # 前5日涨幅 <0（干净首阳）
        return ""
    if mkt_zt >= 130 and vr >= 1.3:
        return "r9A"
    if mkt_zt >= 90 and 1.0 <= vr <= 2.0:
        return "r7mA"
    return ""


def load_daily_features(codes: set[str], date: str) -> dict[str, dict]:
    """date 之前最近 ~25 个交易日的日线特征（MA/20日高/5日均量，用于盘中信号判定）。
    按日期缓存，每天只读一次 zzshare 文件。"""
    global _DAILY_FEATURES, _DAILY_FEATURES_DATE
    if _DAILY_FEATURES_DATE == date and _DAILY_FEATURES:
        return _DAILY_FEATURES
    files = sorted(glob.glob(str(paths.data_dir() / "zzshare_daily" / "*.csv")))
    want = [f for f in files if Path(f).stem < date.replace("-", "")][-25:]
    parts = []
    for f in want:
        try:
            parts.append(pd.read_csv(
                f, dtype={"code": str},
                usecols=["date", "code", "close", "high", "low", "volume"]))
        except Exception:  # noqa: BLE001
            continue
    out: dict[str, dict] = {}
    if parts:
        df = pd.concat(parts, ignore_index=True)
        df = df[df["code"].isin(codes)]
        for code, g in df.groupby("code"):
            g = g.sort_values("date")
            if len(g) < 25:
                continue
            closes = g["close"].to_numpy(float)
            highs = g["high"].to_numpy(float)
            lows = g["low"].to_numpy(float)
            vols = g["volume"].to_numpy(float)
            out[code] = {
                "prev_close": float(closes[-1]),
                "close5": float(closes[-6]),
                "close10": float(closes[-11]),
                "lo60": float(lows[-60:].min()),
                "ma5": float(closes[-5:].mean()),
                "ma10": float(closes[-10:].mean()),
                "ma20": float(closes[-20:].mean()),
                "hi20": float(highs[-20:].max()),
                "avg5_vol": float(vols[-5:].mean()),   # 股
                "limit_pct": (0.20 if str(code).startswith(("300", "301", "688"))
                              else 0.10),
            }
    _DAILY_FEATURES, _DAILY_FEATURES_DATE = out, date
    return out


def _buy_sell_signals(r: dict, d: dict | None, now: datetime,
                      report_today: bool = False,
                      mkt_state: dict | None = None) -> tuple[str, str, float | None]:
    """买入/卖出信号（盘中动态）。r 含 price/chg/volume/buy_price/peak/group/status。"""
    px = r.get("price")
    if px is None:
        return "", "", None
    ma10 = r.get("ma10") or (d or {}).get("ma10")
    ma20 = r.get("ma20") or (d or {}).get("ma20")
    held = r.get("group") == "持仓"
    avg5 = (d or {}).get("avg5_vol")
    elapsed = _trading_minutes(now)
    vr = _volume_ratio(r.get("volume"), avg5, elapsed)

    # ---- 卖出信号：持仓纪律优先，其次跌破 MA10/MA20 ----
    sell = ""
    buy_px, peak = r.get("buy_price"), r.get("peak")
    if buy_px and px <= buy_px * 0.96:
        sell = f"⚠止损-4%（买入{buy_px:.2f}）"
    elif peak and peak > 0 and px <= peak * 0.92:
        sell = f"⚠高点回落8%（峰值{peak:.2f}）"
    elif ma20 is not None and px < ma20:
        sell = ("破位" if not held else f"跌破MA20（{ma20:.2f}）")
    elif held and ma10 is not None and px < ma10:
        sell = f"跌破MA10（{ma10:.2f}）"
    else:
        sell = "—"

    # ---- 买入信号：持仓=持有中；两级模型（B3/二波）动作 + 市场前缀 ----
    buy = ""
    status = r.get("status") or ""
    if held:
        buy = "持有中"
    elif status in ("B3打底仓", "二波加仓"):
        buy = r.get("buy") or (
            "明日开盘打底仓（计划仓位 2/3）" if status == "B3打底仓"
            else "明日开盘加仓（1/3，总仓≤1/3）")
        note = ms_mod.signal_note(mkt_state)   # 主升区轻仓 / 科技强优先 / 非科技强降档
        if note:
            buy = note + "｜" + buy
    return buy, sell, vr


# ---------------------------------------------------------------- 提醒规则

def evaluate_alert(row: dict, now: datetime, cooldown: dict) -> str | None:
    """盘中提醒仅针对买入/卖出信号（止损/高点回落/破位/两级模型），不含涨跌幅噪音。"""
    price = row.get("price")
    low = row.get("low")
    buy = row.get("buy_price")
    peak = row.get("peak")
    sell = row.get("sell") or ""
    buy_sig = row.get("buy") or ""
    code = str(row.get("code") or "")
    ts = now.timestamp()

    def fire(key: str, msg: str) -> str | None:
        last = cooldown.get(key)
        if last is not None and ts - last < COOLDOWN_SEC:
            return None
        cooldown[key] = ts
        return msg

    if row.get("status") == "B3":   # 两级模型第一级：打底仓
        return fire("ts_b3_" + str(row.get("code")),
                    f"📈 B3 信号（{row.get('name', '')}）：明日开盘打底仓（2/3 仓）")
    if row.get("status") == "二波":  # 两级模型第二级：最优买点加仓
        return fire("ts_w2_" + str(row.get("code")),
                    f"📈 二波信号（{row.get('name', '')}）：明日开盘加仓（1/3 仓）")
    if row.get("launch"):  # 启动加仓模型买入信号优先（主升区/量能不足只观察，不提醒打底仓）
        action = row.get("launch_action") or ""
        if any(k in action for k in ("仅观察", "量能不足", "轻仓")):
            return None
        return fire("launch_" + row["launch"],
                    f"📈 启动信号（{row['launch']}）：明日开盘打底仓"
                    f"{f'（现价{price:.2f}）' if price else ''}")
    if buy and low is not None and low <= buy * STOP:
        return fire(f"stop_{code}",
                    f"⚠ 止损-4%：低点{low:.2f} ≤ 买入{buy:.2f}×0.96")
    if buy and peak and price is not None and peak > 0 and price <= peak * PULLBACK:
        return fire(f"pullback_{code}",
                    f"⚠ 高点回落8%：峰值{peak:.2f} → 现价{price:.2f}")
    if "跌破MA20" in sell:
        return fire(f"break20_{code}", f"🔻 {sell}，持仓卖出/待买放弃")
    if "跌破MA10" in sell:
        return fire(f"break10_{code}", f"🔻 {sell}，持仓卖出/待买放弃")
    if "破位" in sell and row.get("group") != "持仓":
        return fire(f"break20_{code}", f"🔻 {sell}，放弃买入")
    return None


# ---------------------------------------------------------------- 状态构建

def build_state(quotes: pd.DataFrame, rows: dict, ma_map: dict,
                state: dict, now: datetime,
                daily: dict | None = None,
                report_date: str = "", mkt_zt: int | None = None,
                scan_codes: set | None = None,
                scan_names: dict | None = None,
                mkt_state: dict | None = None) -> dict:
    """quotes -> live 状态；新提醒追加进 state['alerts']（含限频）。"""
    cooldown = state.setdefault("cooldown", {})
    q = quotes.set_index("code")
    daily = daily or {}
    scan_codes = scan_codes or set()
    scan_names = scan_names or {}
    TWO_STAGE = ("B3", "二波")     # 两级模型：B3 打底仓 / 二波加仓（收盘判定）
    stocks = []
    for code, info in rows.items():
        r = {}
        if code in q.index:
            s = q.loc[code]
            r = {
                "price": _num(s.get("close")),
                "chg": _num(s.get("price_change_ratio_pct")),
                "high": _num(s.get("high")),
                "low": _num(s.get("low")),
                "prev_close": _num(s.get("prev_close")),
                "volume": _num(s.get("volume")),
                "error": str(s.get("error") or "").strip(),
            }
        d = daily.get(code)
        ma10 = (d["ma10"] if d else None) or ma_map.get(code, (None, None))[0]
        ma20 = (d["ma20"] if d else None) or ma_map.get(code, (None, None))[1]
        status = info.get("status") or ""
        if status in TWO_STAGE:
            buy, sell, vr = _buy_sell_signals(
                {**r, "buy": info.get("twostage_action") or "",
                 "buy_price": None, "peak": None, "group": info["group"],
                 "status": status, "ma10": ma10, "ma20": ma20},
                d, now, report_date == now.strftime("%Y-%m-%d"), mkt_state)
            sell = "—"                            # 两级信号收盘判定，卖出只看持仓
        else:
            buy, sell, vr = _buy_sell_signals(
                {**r, "buy_price": info["buy_price"], "peak": info["peak"],
                 "group": info["group"], "status": status,
                 "ma10": ma10, "ma20": ma20},
                d, now, report_date == now.strftime("%Y-%m-%d"), mkt_state)
        if info.get("group") == "大牛模型":
            if info.get("bb_hold"):
                # 大牛模型持仓（交割单未平仓）：持有中；盘中跌破 MA20 → 收盘确认卖出
                buy = "持有中"
                px = r.get("price")
                if px is not None and ma20 is not None and px < ma20:
                    sell = (f"⚠ 跌破MA20（现价 {px:.2f} < MA20 "
                            f"{ma20:.2f}）→ 收盘确认卖出")
                else:
                    sell = ("—" if ma20 is None
                            else f"守MA20（{ma20:.2f}）")
                bb_approx = False
            else:
                # 大牛候选：盘中 T0 近似信号提醒
                sc = info.get("bb_score")
                sell = "—"
                chg = r.get("chg")
                px_ok = ma20 is None or (r.get("price") or 0) > ma20
                bb_approx = bool(px_ok and chg is not None and vr is not None and
                                 ((chg >= 5.0 and vr >= 1.5) or chg >= 9.5))
                if bb_approx:
                    buy = (f"⚠ 今日或成T0信号（{chg:+.1f}% 量比{vr:.1f}）："
                           "14:50 尾盘确认买入")
                else:
                    buy = f"✓ 大牛候选 评分{sc}" if sc else "✓ 大牛候选"
        launch = _launch_signal({**r, "vr": vr}, d, mkt_zt)
        if launch:
            buy = ms_mod.launch_action(mkt_state, launch)
        row = {
            "code": code,
            "name": info["name"],
            "group": info["group"],
            "buy_price": info["buy_price"],
            "peak": info["peak"],
            "ma10": ma10,
            "ma20": ma20,
            "status": status,
            "sell": sell,
            "buy": buy,
            "vr": vr,
            "launch": launch,
            "launch_action": (ms_mod.launch_action(mkt_state, "")
                              if launch else ""),
            "in_card": (info["group"] == "持仓"
                        or status in PENDING_STATUSES or status in TWO_STAGE
                        or bool(launch)),
            "bb_score": info.get("bb_score"),
            "bb_date": info.get("bb_date"),
            "bb_hold": bool(info.get("bb_hold")),
            "bb_entry": info.get("bb_entry"),
            "bb_approx": bb_approx if info.get("group") == "大牛模型" else False,
            "updated": now.strftime("%H:%M:%S"),
            **r,
        }
        # 盘中提醒只针对信号卡内的标的（持仓 + 待触发买点），
        # 避免"破位"等已排除票也弹出放弃买入提醒
        alert = evaluate_alert(row, now, cooldown) if row["in_card"] else ""
        if not alert and row.get("bb_hold") and "跌破MA20" in str(row.get("sell") or ""):
            # 大牛模型持仓：盘中跌破 MA20 → 收盘确认卖出
            key = f"bbh_{code}"
            last = cooldown.get(key)
            if last is None or now.timestamp() - last >= COOLDOWN_SEC:
                cooldown[key] = now.timestamp()
                alert = (f"📉 大牛模型持仓：{info['name']} "
                         f"{row.get('sell')}")
        if not alert and row.get("bb_approx"):      # 大牛模型：盘中 T0 近似信号提醒
            key = f"bb_{code}"
            last = cooldown.get(key)
            if last is None or now.timestamp() - last >= COOLDOWN_SEC:
                cooldown[key] = now.timestamp()
                alert = (f"📈 大牛模型：{info['name']} {buy}")
        if alert:
            item = {"time": now.strftime("%H:%M:%S"), "code": code,
                    "name": info["name"], "message": alert}
            state["alerts"].append(item)
            state["alerts"] = state["alerts"][-ALERT_HISTORY:]
            row["alert"] = alert
        else:
            row["alert"] = ""
        stocks.append(row)

    # 卡点名单扫描：只有触发启动信号的标的新增进列表（不污染主表）
    for code in sorted(scan_codes):
        if code in rows:
            continue
        r = {}
        if code in q.index:
            s = q.loc[code]
            r = {
                "price": _num(s.get("close")),
                "chg": _num(s.get("price_change_ratio_pct")),
                "high": _num(s.get("high")),
                "low": _num(s.get("low")),
                "prev_close": _num(s.get("prev_close")),
                "volume": _num(s.get("volume")),
                "error": str(s.get("error") or "").strip(),
            }
        d = daily.get(code)
        if not r.get("price") or not d:
            continue
        vr = _volume_ratio(r.get("volume"), d.get("avg5_vol"),
                           _trading_minutes(now))
        launch = _launch_signal({**r, "vr": vr}, d, mkt_zt)
        if not launch:
            continue
        row = {
            "code": code, "name": scan_names.get(code, code), "group": "启动",
            "buy_price": None, "peak": None, "ma10": None, "ma20": None,
            "status": "", "sell": "—",
            "buy": ms_mod.launch_action(mkt_state, launch),
            "launch_action": ms_mod.launch_action(mkt_state, ""),
            "vr": vr, "launch": launch,
            "in_card": True, "updated": now.strftime("%H:%M:%S"), **r,
        }
        alert = evaluate_alert(row, now, cooldown)
        if alert:
            item = {"time": now.strftime("%H:%M:%S"), "code": code,
                    "name": row["name"], "message": alert}
            state["alerts"].append(item)
            state["alerts"] = state["alerts"][-ALERT_HISTORY:]
            row["alert"] = alert
        else:
            row["alert"] = ""
        stocks.append(row)

    # 市场退潮保护：全市场涨停 <50 时提醒启动模型持仓次日开盘清仓（全局限频）
    if mkt_zt is not None and mkt_zt < TIDE_ZT:
        tide_key = "tide"
        last = cooldown.get(tide_key)
        if last is None or now.timestamp() - last >= COOLDOWN_SEC:
            cooldown[tide_key] = now.timestamp()
            state["alerts"].append({
                "time": now.strftime("%H:%M:%S"), "code": "市场",
                "name": "全市场",
                "message": (f"🔻 市场退潮：涨停仅 {mkt_zt} 家（<{TIDE_ZT}），"
                            f"启动模型持仓次日开盘清仓")})
            state["alerts"] = state["alerts"][-ALERT_HISTORY:]
        for s in stocks:
            if s["group"] == "持仓" and s.get("sell") in ("", "—"):
                s["sell"] = f"退潮保护：涨停{mkt_zt}<{TIDE_ZT}"

    stocks.sort(key=lambda x: (x["group"] != "持仓", x["code"]))
    state["mkt_zt"] = mkt_zt
    return {"stocks": stocks, "alerts": list(state["alerts"]),
            "mkt_zt": mkt_zt}


# ---------------------------------------------------------------- 渲染

def _spark_svg(minutes: list | None, color: str) -> str:
    """分时缩略图（SVG 迷你折线；data-min 供前端 JS 续画）。"""
    if not minutes:
        return ""
    pts = [[t, p] for t, p in minutes if p is not None]
    if len(pts) < 2:
        return ""
    step = max(1, len(pts) // 90)
    pts = pts[::step]
    if pts[-1] != minutes[-1] and minutes[-1][1] is not None:
        pts.append(list(minutes[-1]))
    prices = [p for _, p in pts]
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1.0
    w, h = 92, 30
    coords = []
    for i, (_, p) in enumerate(pts):
        x = 2 + (w - 4) * i / max(1, len(pts) - 1)
        y = 2 + (h - 6) * (1 - (p - lo) / span)
        coords.append(f"{x:.1f},{y:.1f}")
    data_min = json.dumps(pts, ensure_ascii=False)
    return (f'<svg class="spark" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" data-min=\'{data_min}\' '
            f'style="display:block;width:100%;max-width:92px">'
            f'<polyline points="{" ".join(coords)}" fill="none" '
            f'stroke="{color}" stroke-width="1.4"/></svg>')


def _sell_span(v) -> str:
    if not v or v == "—":
        return '<span style="color:#6E7681">—</span>'
    return f'<span style="color:#F85149">{_esc(v)}</span>'


def _ma20_span(s: dict) -> str:
    """现价相对 MA20 的偏离（红涨绿跌）。"""
    px, ma = s.get("price"), s.get("ma20")
    if px is None or ma is None or not ma:
        return "—"
    v = px / ma - 1
    c = "#F85149" if v >= 0 else "#3FB950"
    return f'<span style="color:{c}">{v:+.1%}</span>'


def _buy_span(v) -> str:
    if not v:
        return '<span style="color:#8B949E">—</span>'
    if str(v).startswith("✓"):
        color = "#3FB950"
    elif str(v).startswith("破位"):
        color = "#F85149"
    else:
        color = "#8B949E"
    return f'<span style="color:{color}">{_esc(v)}</span>'


def render_live_html(state: dict, now: datetime,
                     status: str, watch_size: int,
                     concepts: list | None = None,
                     minutes: dict | None = None,
                     market_state: dict | None = None) -> str:
    up = sum(1 for s in state["stocks"] if (s.get("chg") or 0) > 0)
    down = sum(1 for s in state["stocks"] if (s.get("chg") or 0) < 0)
    alerted = sum(1 for s in state["stocks"] if s.get("alert"))
    n = len(state["stocks"])
    mkt_zt = state.get("mkt_zt")
    minutes = minutes or {}

    def kpi(label, value, color):
        return (f'<div class="kpi" style="border-bottom-color:{color}">'
                f'<div class="label">{label}</div>'
                f'<div class="value" style="color:{color}">{value}</div></div>')

    trs = []
    for s in state["stocks"]:
        code_link = (f'<a href="/stock/{_esc(s["code"])}" '
                     f'style="color:#58A6FF;text-decoration:none">{_esc(s["code"])}</a>')
        spark = _spark_svg(
            minutes.get(s["code"]),
            "#F85149" if (s.get("price") or 0) >= (s.get("prev_close") or 0)
            else "#3FB950")
        trs.append(
            f'<tr data-code="{_esc(s["code"])}">'
            f'<td><div style="font-weight:600;color:#E6EDF3">{_esc(s["name"])}</div>'
            f'<div style="font-size:11px;color:#8B949E">{code_link}</div></td>'
            f"<td class='c-price'>{_fmt(s.get('price'))}</td>"
            f"<td>{spark}</td>"
            f"<td class='c-chg'>{_chg_span(s.get('chg'))}</td>"
            f"<td class='c-vr'>{_fmt(s.get('vr'))}</td>"
            f"<td>{_esc(s['group'])}</td>"
            f"<td class='c-buy'>{_buy_span(s.get('buy'))}</td>"
            f"<td class='c-sell'>{_sell_span(s.get('sell'))}</td></tr>")
    table = ('<div class="tbl-wrap"><table>'
             '<thead><tr><th onclick="sortT(0)">代码/名称</th>'
             '<th onclick="sortT(1)">现价</th><th>分时</th>'
             '<th onclick="sortT(3)">涨跌%</th><th onclick="sortT(4)">量比</th>'
             '<th onclick="sortT(5)">分组</th>'
             "<th>买入信号</th><th>卖出信号</th></tr></thead>"
             f'<tbody id="mtb">' + "".join(trs) + "</tbody></table></div>")
    # 今日买入卖出信号卡（持仓 + 待触发买点，位于标的行情卡上方，盘中动态）
    card_stocks = [s for s in state["stocks"] if s.get("in_card")]
    if card_stocks:
        card_trs = "".join(
            f'<tr data-code="{_esc(s["code"])}">'
            f"<td>{_esc(s['group'])}</td>"
            f'<td><a href="/stock/{_esc(s["code"])}" '
            f'style="color:#E6EDF3;text-decoration:none;font-weight:600">'
            f'{_esc(s["name"])}</a>'
            f'<div style="font-size:11px;color:#8B949E">'
            f'<a href="/stock/{_esc(s["code"])}" '
            f'style="color:#58A6FF;text-decoration:none">{_esc(s["code"])}</a>'
            f'</div></td>'
            f"<td class='c-price'>{_fmt(s.get('price'))}</td>"
            f"<td class='c-chg'>{_chg_span(s.get('chg'))}</td>"
            f"<td class='c-vr'>{_fmt(s.get('vr'))}</td>"
            f"<td class='c-buy'>{_buy_span(s.get('buy'))}</td>"
            f"<td class='c-sell'>{_sell_span(s.get('sell'))}</td></tr>"
            for s in card_stocks)
        n_hold = sum(1 for s in card_stocks if s["group"] == "持仓")
        signal_card = (f'<section class="card"><h2>今日买入卖出信号'
                       f'（持仓 {n_hold} + 待买 {len(card_stocks) - n_hold}'
                       f' · 盘中动态）</h2>'
                       '<div class="body"><div class="tbl-wrap"><table>'
                       '<thead><tr><th>组</th><th>名称/代码</th>'
                       '<th>现价</th><th>涨跌%</th><th>实时量比</th>'
                       '<th>买入信号</th><th>卖出信号</th></tr></thead>'
                       f'<tbody id="sigtb">' + card_trs + "</tbody></table></div>"
                       '<div class="note">持仓：跌破 MA10/MA20/止损-4%/高点回落8%'
                       '盘中随时可见；待买（B3 打底仓 / 二波加仓）：'
                       '收盘判定，明日开盘执行动作。'
                       'B3/二波状态来自最新每日报告，收盘以报告为准。</div>'
                       "</div></section>")
    else:
        signal_card = ('<section class="card"><h2>今日买入卖出信号'
                       '（持仓 + 待买 · 盘中动态）</h2>'
                       '<div class="body"><div class="empty">暂无持仓与'
                       '待触发买点的标的。</div></div></section>')
    sort_ui = ('<div style="padding:8px 0;display:flex;gap:8px;align-items:center">'
               '<button onclick="sortT(3)" style="background:#21262D;color:#E6EDF3;'
               'border:1px solid #30363D;border-radius:6px;padding:4px 12px;cursor:pointer">'
               '按涨跌幅</button>'
               '<button onclick="sortT(1)" style="background:#21262D;color:#E6EDF3;'
               'border:1px solid #30363D;border-radius:6px;padding:4px 12px;cursor:pointer">'
               '按现价</button>'
               '<span style="color:#8B949E;font-size:11px">点击表头也可排序；'
               '点击代码进入分时/K线页；量比=实时量/5日均量分钟化</span></div>')
    sort_js = """<script>
var SC=-1,SA=true;
function cmp(a,b,i){var va=a.cells[i].innerText.replace(/[%,+]/g,''),vb=b.cells[i].innerText.replace(/[%,+]/g,'');
  var na=parseFloat(va),nb=parseFloat(vb);
  return (isNaN(na)||isNaN(nb))?va.localeCompare(vb,'zh'):na-nb;}
function applySort(){var tb=document.getElementById('mtb');if(!tb)return;
  var rows=Array.prototype.slice.call(tb.rows);
  rows.sort(function(a,b){return SA?cmp(a,b,SC):cmp(b,a,SC)});
  rows.forEach(function(r){tb.appendChild(r)});}
function sortT(i){if(SC===i){SA=!SA}else{SC=i;SA=true}applySort();
  location.hash='sort='+i+(SA?'a':'d');}
(function(){var m=(location.hash.match(/sort=(\\d+)([ad])/)||[]);
  if(m.length){SC=+m[1];SA=m[2]==='a';applySort();}})();
</script>"""

    poll_js = f"""<script>
var POLL_MS={LIVE_POLL_MS};
var SPARK={{}};
function fmtN(v){{return v==null||isNaN(v)?'-':(+v).toFixed(2);}}
function chgSpan(v){{if(v==null||isNaN(v))return '-';
  var c=v>=0?'#F85149':'#3FB950';
  return '<span style="color:'+c+'">'+(v>=0?'+':'')+v.toFixed(2)+'%</span>';}}
function esc(s){{var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}}
function initSpark(){{document.querySelectorAll('svg.spark').forEach(function(svg){{
  var tr=svg.closest('tr');if(!tr)return;var code=tr.getAttribute('data-code');
  try{{SPARK[code]=JSON.parse(svg.getAttribute('data-min')||'[]');}}catch(e){{SPARK[code]=[];}}}});}}
function sparkPts(code){{var a=SPARK[code]||[];if(a.length<2)return '';
  var ps=a.map(function(x){{return x[1];}}).filter(function(v){{return v!=null;}});
  if(ps.length<2)return '';
  var lo=Math.min.apply(null,ps),hi=Math.max.apply(null,ps),span=(hi-lo)||1;
  var w=92,h=30,o=[];
  for(var i=0;i<a.length;i++){{var x=2+(w-4)*i/(a.length-1),y=2+(h-6)*(1-(a[i][1]-lo)/span);
    o.push(x.toFixed(1)+','+y.toFixed(1));}}
  return o.join(' ');}}
function redrawSpark(code){{var tr=document.querySelector('#mtb tr[data-code="'+code+'"]');
  if(!tr)return;var svg=tr.querySelector('svg.spark');if(!svg)return;
  var p=svg.querySelector('polyline');if(!p)return;p.setAttribute('points',sparkPts(code));}}
function upRow(s){{var tr=document.querySelector('#mtb tr[data-code="'+s.code+'"]');if(!tr)return;
  var q=tr.querySelectorAll('td');
  if(q[1])q[1].textContent=fmtN(s.price);
  if(q[3])q[3].innerHTML=chgSpan(s.chg);
  if(q[4])q[4].textContent=s.vr==null?'-':(+s.vr).toFixed(2);
  if(q[6])q[6].innerHTML=buySpan(s.buy);
  if(q[7])q[7].innerHTML=sellSpan(s.sell);
  if(s.price!=null){{var arr=SPARK[s.code]||(SPARK[s.code]=[]);
    var d=new Date(),ts=('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2)+':'+('0'+d.getSeconds()).slice(-2);
    var last=arr[arr.length-1];
    if(last&&last[0]===ts){{last[1]=s.price;}}else{{arr.push([ts,s.price]);if(arr.length>720)arr.shift();}}
    redrawSpark(s.code);}}}}
function buySpan(v){{if(!v)return '<span style="color:#8B949E">—</span>';
  var c=v.indexOf('✓')===0?'#3FB950':(v.indexOf('破位')===0?'#F85149':'#8B949E');
  return '<span style="color:'+c+'">'+esc(v)+'</span>';}}
function sellSpan(v){{if(!v||v==='—')return '<span style="color:#6E7681">—</span>';
  return '<span style="color:#F85149">'+esc(v)+'</span>';}}
function sigRowHtml(s){{return '<tr data-code="'+s.code+'"><td>'+esc(s.group)+'</td>'
  +'<td><a href="/stock/'+s.code+'" style="color:#E6EDF3;text-decoration:none;font-weight:600">'+esc(s.name)+'</a>'
  +'<div style="font-size:11px;color:#8B949E"><a href="/stock/'+s.code+'" style="color:#58A6FF;text-decoration:none">'+s.code+'</a></div></td>'
  +'<td class="c-price">'+fmtN(s.price)+'</td>'
  +'<td class="c-chg">'+chgSpan(s.chg)+'</td><td class="c-vr">'+(s.vr==null?'-':(+s.vr).toFixed(2))+'</td>'
  +'<td class="c-buy">'+buySpan(s.buy)+'</td><td class="c-sell">'+sellSpan(s.sell)+'</td></tr>';}}
function upSig(s){{var tb=document.getElementById('sigtb');if(!tb)return;
  var tr=document.querySelector('#sigtb tr[data-code="'+s.code+'"]');
  if(!tr){{if(!s.in_card)return;
    var d=document.createElement('tbody');d.innerHTML=sigRowHtml(s);tb.appendChild(d.firstChild);return;}}
  if(!s.in_card){{tr.parentNode.removeChild(tr);return;}}
  var q=tr.querySelectorAll('td');
  if(q[2])q[2].textContent=fmtN(s.price);
  if(q[3])q[3].innerHTML=chgSpan(s.chg);
  if(q[4])q[4].textContent=s.vr==null?'-':(+s.vr).toFixed(2);
  if(q[5])q[5].innerHTML=buySpan(s.buy);
  if(q[6])q[6].innerHTML=sellSpan(s.sell);}}
function setKpi(i,v){{var els=document.querySelectorAll('.kpis .kpi .value');if(els[i])els[i].textContent=v;}}
function poll(){{fetch('/live.json?t='+Date.now()).then(function(r){{return r.json();}}).then(function(d){{
  var u=document.getElementById('upd');if(u&&d.updated_at)u.textContent=d.updated_at;
  var st=document.getElementById('mkt');if(st&&d.market_status)st.textContent=d.market_status;
  var up=0,down=0,al=0;
  (d.stocks||[]).forEach(function(s){{if(s.chg>0)up++;if(s.chg<0)down++;if(s.alert)al++;upRow(s);upSig(s);}});
  setKpi(1,up);setKpi(2,down);setKpi(3,al);
  applySort();
  setTimeout(poll,POLL_MS);
}}).catch(function(){{setTimeout(poll,POLL_MS);}});}}
initSpark();
poll();
</script>"""

    alerts = "".join(
        f"<div style='padding:4px 0;color:#E6EDF3'><span style='color:#8B949E'>{_esc(a['time'])}</span> "
        f"<b>{_esc(a['name'])}</b> {_esc(a['message'])}</div>"
        for a in reversed(state["alerts"]) if isinstance(a, dict))
    if not alerts:
        alerts = '<div class="empty">暂无盘中提醒。</div>'

    # 启动加仓模型卡（盘中信号 + 市场退潮保护）
    launch_stocks = [s for s in state["stocks"] if s.get("launch")]
    tide = mkt_zt is not None and mkt_zt < TIDE_ZT
    tide_html = ""
    if tide:
        tide_html = (f'<div style="padding:8px 12px;margin-bottom:8px;'
                     f'background:#3D1516;border:1px solid #F85149;'
                     f'border-radius:6px;color:#FF7B72">⚠ 市场退潮：全市场涨停 '
                     f'{mkt_zt} 家 &lt; {TIDE_ZT}，启动模型持仓次日开盘清仓</div>')
    ms = market_state
    ms_banner = ""
    if ms and ms.get("state") == "主升区":
        ms_banner = (f'<div style="padding:8px 12px;margin-bottom:8px;'
                     f'background:#3D1516;border:1px solid #F85149;'
                     f'border-radius:6px;color:#FF7B72">⚠ 主升区'
                     f'（{ms.get("label")}）：启动加仓模型停开，以下信号仅观察；'
                     f'B3/二波买点轻仓/降档</div>')
    elif ms and (ms.get("amount_wl") or 1.0) < 1.0:
        ms_banner = (f'<div style="padding:8px 12px;margin-bottom:8px;'
                     f'background:#3D2A15;border:1px solid #D29922;'
                     f'border-radius:6px;color:#E3B341">⚠ 量能不足'
                     f'（水位 {ms.get("amount_wl")} &lt; 1.0）：启动加仓信号'
                     f'等放量确认，不急于打底仓</div>')
    l_trs = "".join(
        f'<tr><td><a href="/stock/{_esc(s["code"])}" '
        f'style="color:#58A6FF;text-decoration:none">{_esc(s["code"])}</a></td>'
        f"<td>{_esc(s['name'])}</td><td class='c-price'>{_fmt(s.get('price'))}</td>"
        f"<td class='c-chg'>{_chg_span(s.get('chg'))}</td>"
        f"<td class='c-vr'>{_fmt(s.get('vr'))}</td>"
        f"<td style='color:#39D2C0'>{_esc(s['launch'])}</td>"
        f"<td style='color:#3FB950'>{_esc(s.get('launch_action') or '明日开盘打底仓')}</td></tr>"
        for s in launch_stocks)
    launch_card = (f'<section class="card"><h2>启动加仓模型（盘中）'
                   f'｜ 市场涨停 {mkt_zt if mkt_zt is not None else "—"} 家'
                   f'（r9A≥130 / r7mA≥90，退潮&lt;{TIDE_ZT}）</h2>'
                   f'<div class="body">{tide_html}'
                   f'{ms_banner}'
                   + (f'<div class="tbl-wrap"><table><thead><tr>'
                      f"<th>代码</th><th>名称</th><th>现价</th><th>涨跌%</th>"
                      f"<th>量比</th><th>信号</th><th>动作</th></tr></thead>"
                      f"<tbody>{l_trs}</tbody></table></div>"
                      if launch_stocks else
                      '<div class="empty">当前无 r7mA/r9A 启动信号；'
                      '满足条件（回撤≥15% + 涨≥5% + 前5日<0 + 市场门限）'
                      '即时提醒。</div>')
                   + "</div></section>")

    concepts = concepts or []
    if concepts:
        cf_trs = "".join(
            f"<tr><td>{_esc(c['name'])}</td><td>{_chg_span(c['chg'])}</td>"
            f"<td>{_fmt(c.get('total'))}亿</td>"
            f"<td>{_fmt(c.get('main'))}亿</td></tr>"
            for c in concepts)
        concept_card = (f'<section class="card"><h2>行业板块资金流（大分类）'
                        f'（总资金 Top{len(concepts)} · 10分钟刷新）</h2>'
                        '<div class="body"><div class="tbl-wrap"><table>'
                        "<tr><th>板块</th><th>涨跌%</th><th>总资金(亿)</th>"
                        "<th>资金净额(亿)</th></tr>"
                        f"{cf_trs}</table></div></div></section>")
    else:
        concept_card = ""
    market_card = ms_mod.render_card(market_state)
    # 市场周期状态卡（描述当前主线与阶段，不预测切换；每日 cycle-state 生成）
    try:
        from mainrise import cycle_state as cs_mod
        cs_state = json.loads(
            (paths.state_dir() / "cycle_state.json").read_text(encoding="utf-8"))
        cycle_card = cs_mod.render_card(cs_state)
    except Exception:  # noqa: BLE001  无周期状态文件则省略
        cycle_card = ""
    # 大牛模型候选卡（评分≥2 硬规则 · 实时）
    bb_stocks = [s for s in state["stocks"] if s.get("group") == "大牛模型"]
    mret = (market_state or {}).get("mkt_ret20")
    bb_weak = mret is not None and mret <= -5.0
    if mret is None:
        bb_mkt = "大盘 20 日 —"
    else:
        bb_mkt = (f'大盘 20 日 {mret:+.1f}% → '
                  f'<b style="color:{"#F85149" if bb_weak else "#3FB950"}">'
                  f'{"杀跌区 · 停开新仓" if bb_weak else "正常 · 可开仓"}</b>')
    if bb_stocks:
        def _bb_row(s: dict) -> str:
            approx = bool(s.get("bb_approx"))
            hold = bool(s.get("bb_hold"))
            broken = hold and "跌破MA20" in str(s.get("sell") or "")
            if broken:
                bg = ' style="background:#3D2A15"'
                state_td = ('<td style="color:#F85149"><b>⚠ 持仓破MA20'
                            '<br><span style="font-size:11px;color:#8B949E">'
                            '收盘确认卖出</span></b></td>')
            elif hold:
                bg = ' style="background:#161B22"'
                state_td = ('<td style="color:#D29922"><b>持仓</b>'
                            '<br><span style="font-size:11px;color:#8B949E">'
                            '守MA20</span></td>')
            elif approx:
                bg = ' style="background:#3D2A15"'
                state_td = (f'<td style="color:#E3B341"><b>⚠ 今日或成T0'
                            f'<br><span style="font-size:11px;color:#8B949E">'
                            f'14:50尾盘确认</span></b></td>')
            else:
                bg = ""
                state_td = '<td style="color:#8B949E">候选</td>'
            return (f'<tr data-code="{_esc(s["code"])}"{bg}>'
                    f'<td><div style="font-weight:600;color:#E6EDF3">'
                    f'{_esc(s["name"])}</div>'
                    f'<div style="font-size:11px;color:#8B949E">'
                    f'<a href="/stock/{_esc(s["code"])}" '
                    f'style="color:#58A6FF;text-decoration:none">'
                    f'{_esc(s["code"])}</a></div></td>'
                    f"<td class='c-price'>{_fmt(s.get('price'))}</td>"
                    f"<td class='c-chg'>{_chg_span(s.get('chg'))}</td>"
                    f"<td class='c-vr'>{_fmt(s.get('vr'))}</td>"
                    f"<td>{_ma20_span(s)}</td>"
                    f"<td style='color:#D29922'><b>评分{s.get('bb_score') or '-'}"
                    f"</b></td>"
                    f"<td style='color:#8B949E'>{_esc(s.get('bb_date') or '')}</td>"
                    f"{state_td}</tr>")
        bb_trs = "".join(_bb_row(s) for s in bb_stocks)
        bb_hold_n = sum(1 for s in bb_stocks if s.get("bb_hold"))
        bb_card = (f'<section class="card"><h2>大牛模型候选'
                   f'（评分≥2 硬规则 · 实时'
                   + (f' · 持仓 {bb_hold_n} 只' if bb_hold_n else '')
                   + f'）｜ {bb_mkt}</h2>'
                   '<div class="body"><div class="tbl-wrap"><table>'
                   '<thead><tr><th>名称/代码</th><th>现价</th><th>涨跌%</th>'
                   '<th>实时量比</th><th>距MA20</th><th>评分</th><th>信号日/买入日</th>'
                   '<th>状态</th></tr></thead><tbody id="bbtb">' + bb_trs
                   + '</tbody></table></div>'
                   '<div class="note">大牛候选 = 热主题 且 90日内T0≥3 且 评分≥2'
                   '（近45日信号）；⚠ 今日或成T0信号 = 盘中涨幅≥5% 且 量比≥1.5'
                   '（或涨停）且 站上MA20 → 14:50 尾盘按模型确认买入（1/3仓，'
                   '杀跌区除外）；持仓 = 大牛模型交割单未平仓，⚠ 破MA20 = 盘中'
                   '跌破 MA20 → 17:30 收盘确认卖出（模型退出纪律）。'
                   '</div></div></section>')
    else:
        bb_card = (f'<section class="card"><h2>大牛模型候选'
                   f'（评分≥2 硬规则）｜ {bb_mkt}</h2>'
                   '<div class="body"><div class="empty">近 45 日无符合模型'
                   '硬规则的信号（保持空仓/持有纪律）。</div></div></section>')
    body = f"""
<div class="wrap">
  <header>
    <h1>主升浪实时盯盘</h1>
    <div class="sub">市场状态：<span id="mkt" style="color:#39D2C0">{status}</span>
     ｜ 更新：<span class="date" id="upd">{now.strftime('%Y-%m-%d %H:%M:%S')}</span>
     ｜ 监控 {n} 只（观察池 {watch_size} + 持仓 + 大牛候选）｜ 每 3 秒实时更新
     ｜ <a href="index.html" style="color:#8B949E">大牛模型首页</a></div>
  </header>
  <div class="kpis">
    {kpi("监控标的", n, "#58A6FF")}
    {kpi("上涨", up, "#F85149")}
    {kpi("下跌", down, "#3FB950")}
    {kpi("盘中提醒", alerted, "#D29922" if alerted else "#8B949E")}
  </div>
  <section class="card">
    <h2>盘中提醒（最近 {ALERT_HISTORY} 条，每只票同类 10 分钟限频）</h2>
    <div class="body">{alerts}</div>
  </section>
  {market_card}
  {cycle_card}
  {bb_card}
  {launch_card}
  {signal_card}
  <section class="card">
    <h2>标的实时行情</h2>
    <div class="body">{sort_ui}{table}</div>
  </section>
  {concept_card}
  <div class="note">盘中提醒仅针对买入/卖出信号：卖出=止损-4%/高点回落8%/
  跌破 MA10/MA20/退潮保护；买入=两级模型（B3 打底仓/二波加仓）、启动加仓模型
  （r7mA/r9A 信号日提醒明日打底仓）。大牛模型候选（评分≥2 硬规则）仅供观察，
  收盘判定以每日跟踪报告与 `mainrise bigbull` 交割单为准。
  数据源：腾讯实时行情（3 秒轮询）；分时缩略图=当日分时（腾讯）+ 盘中快照累积。</div>
  <footer>免责声明：仅用于研究学习，不构成投资建议。股市有风险，决策需谨慎。</footer>
</div>
{sort_js}
{poll_js}
"""
    return (f"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"theme-color\" content=\"#0D1117\">"
            f"<meta http-equiv=\"refresh\" content=\"{LIVE_FALLBACK_REFRESH}\">"
            f"<title>主升浪实时盯盘</title>"
            f"<style>{CSS}</style></head><body>{body}"
            f"{_refresh_ui('live')}</body></html>")


# ---------------------------------------------------------------- 主循环

def run_one_cycle(out_dir: Path, state: dict, now: datetime | None = None,
                  status_hint: str = "") -> dict:
    """抓取一次实时行情并刷新 live.json / live.html。"""
    now = now or beijing_now()
    data = load_monitor_rows()
    rows, ma_map = data["rows"], data["ma_map"]
    scan_codes = data.get("scan_codes") or set()
    mkt_state = ms_mod.load_state()
    ms_mod.live_state(mkt_state)          # 盘中刷新上证指数20日涨幅（5分钟缓存）
    if not rows and not scan_codes:
        live = {"updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "market_status": status_hint or market_status(now),
                "stocks": [], "alerts": list(state.get("alerts", []))}
        (out_dir / "live.json").write_text(
            json.dumps(live, ensure_ascii=False), encoding="utf-8")
        (out_dir / "live.html").write_text(
            render_live_html({"stocks": [], "alerts": live["alerts"]}, now,
                             live["market_status"], 0, market_state=mkt_state),
            encoding="utf-8")
        return live

    codes = sorted(set(rows) | scan_codes)
    quotes = fetch_snapshot(codes)
    mkt_zt = _realtime_zt_count(now)
    live = build_state(quotes, rows, ma_map, state, now,
                       daily=data.get("daily") or {},
                       report_date=data.get("report_date", ""),
                       mkt_zt=mkt_zt, scan_codes=scan_codes,
                       scan_names=data.get("scan_names") or {},
                       mkt_state=mkt_state)
    # 分时缩略图序列：每 3 秒快照累积一个点（腾讯分时整日回填由预热线程负责）
    minutes: dict[str, list[list]] = {}
    try:
        from mainrise import stockpage
        for s in live["stocks"]:
            stockpage.append_minute_point(s["code"], s.get("price"), now)
        minutes = {s["code"]: stockpage.minute_series(s["code"])
                   for s in live["stocks"]}
    except Exception:  # noqa: BLE001
        minutes = {}
    # 行业板块资金流：10 分钟缓存一次，后台线程抓取，不阻塞行情轮询
    concepts = state.get("concepts") or []
    concepts_error = state.get("concepts_error", "")
    if now.timestamp() - state.get("concept_ts", 0) >= 600 \
            and not state.get("concept_fetching"):
        state["concept_fetching"] = True
        threading.Thread(target=_fetch_concepts_async,
                         args=(state, now), daemon=True).start()
    live["concepts"] = concepts
    live["concepts_error"] = concepts_error
    live["market_state"] = mkt_state
    live["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    live["market_status"] = status_hint or market_status(now)
    watch_size = sum(1 for r in rows.values() if r["group"] == "观察")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "live.json").write_text(
        json.dumps(live, ensure_ascii=False), encoding="utf-8")
    (out_dir / "live.html").write_text(
        render_live_html(live, now, live["market_status"], watch_size,
                         concepts, minutes, market_state=mkt_state),
        encoding="utf-8")
    print(f"[{now:%H:%M:%S}] 更新 {len(live['stocks'])} 只，"
          f"提醒 {len(live['alerts'])} 条 → {out_dir / 'live.html'}")
    return live


def _fetch_concepts_async(state: dict, now) -> None:
    """后台更新行业板块资金流（东财失败不阻塞主循环）。"""
    try:
        from mainrise.review import fetch_sector_flow
        state["concepts"] = fetch_sector_flow(10)
        state["concept_ts"] = now.timestamp()
        state["concepts_error"] = ""
    except Exception as e:  # noqa: BLE001
        state["concepts_error"] = f"{type(e).__name__}: {e}"
        try:
            (paths.web_dir() / "concepts_error.log").write_text(
                f"{now:%H:%M:%S} {state['concepts_error']}\n",
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    finally:
        state["concept_fetching"] = False


def _warm_loop(trade_dates: set) -> None:
    """K线/分时预热线程（盘中运行）：
    - 每 60 秒续刷最近访问标的（≤15 只）的日K+分时（腾讯优先，实测稳定无限流）；
    - 每 300 秒全量续刷所有监控代码的日K（约 41 只）+ 腾讯分时整日回填并落盘。
    接口失败静默跳过、保留缓存旧数据。"""
    last_full = 0.0
    while True:
        now = beijing_now()
        if is_trading_time(now, trade_dates):
            try:
                from mainrise import stockpage
                hot = stockpage.recent_viewed()
                ok = 0
                for c in hot:
                    try:
                        stockpage.warm_kline(c)
                        stockpage.warm_trends(c)
                        ok += 1
                    except Exception:  # noqa: BLE001
                        pass
                if ok:
                    print(f"热标的分时/K线续刷: {ok} 只", flush=True)
                if now.timestamp() - last_full >= 300:
                    last_full = now.timestamp()
                    codes = sorted(load_monitor_rows()["rows"])
                    n = 0
                    for c in codes:
                        try:
                            stockpage.warm_kline(c)
                            n += 1
                        except Exception:  # noqa: BLE001
                            pass
                    print(f"K线全量刷新: {n}/{len(codes)} 只", flush=True)
                    try:
                        ok = stockpage.backfill_minutes(codes)
                        stockpage.save_minutes()
                        print(f"分时整日回填: {ok}/{len(codes)} 只", flush=True)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        time.sleep(60)


def _initial_backfill() -> None:
    """启动时一次性回填全监控标的的当日分时（缩略图立即可用）。"""
    try:
        from mainrise import stockpage
        time.sleep(1)
        codes = sorted(load_monitor_rows()["rows"])
        if codes:
            ok = stockpage.backfill_minutes(codes)
            stockpage.save_minutes()
            print(f"分时缩略图初始回填: {ok}/{len(codes)} 只", flush=True)
    except Exception:  # noqa: BLE001
        pass


def monitor(interval: int = DEFAULT_INTERVAL, once: bool = False,
            out_dir: Path | None = None) -> None:
    out = Path(out_dir) if out_dir else paths.web_dir()
    trade_dates = load_trade_dates()
    if not trade_dates:
        # 交易日历缺失：一律判"节假日休市"会静默全天不轮询（M22 修复）
        print("⚠ 交易日历缺失（trade_dates.csv 不存在或为空）：盯盘将整天不轮询！"
              "请检查数据目录 " + str(paths.trade_dates_path()))
    else:
        last_td = max(trade_dates)
        stale_days = (beijing_now().date()
                      - datetime.strptime(last_td, "%Y-%m-%d").date()).days
        if stale_days > 5:
            print(f"⚠ 交易日历过期：最新交易日 {last_td} 距今 {stale_days} 天，"
                  f"数据可能长期未更新，请检查 update 流水线")
    state: dict = {"alerts": []}
    try:
        from mainrise.stockpage import start_server as start_stock_server
        start_stock_server()
        print("标的详情服务已启动: 127.0.0.1:8765（/stock/ 点击标的进入）")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 标的详情服务启动失败: {e}")
    try:
        from mainrise import stockpage
        stockpage.load_minutes()
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_initial_backfill, daemon=True).start()
    try:
        threading.Thread(target=_warm_loop, args=(trade_dates,),
                         daemon=True).start()
        print("K线预热线程已启动（热标的 60s / 全量 5 分钟）")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ K线预热线程启动失败: {e}")
    print(f"盘中盯盘启动：输出 {out}/live.html，轮询 {interval}s，"
          f"交易日 {len(trade_dates)} 天")
    first = True
    try:
        while True:
            now = beijing_now()
            status = market_status(now, trade_dates)
            # 启动时强制渲染一次（周期卡/候选卡立即生效，不依赖交易时段）
            if once or first or is_trading_time(now, trade_dates):
                try:
                    run_one_cycle(out, state, now, status)
                except Exception as e:  # noqa: BLE001
                    # 单轮异常不杀死盯盘服务：记日志后继续下一轮
                    # （冷却/提醒状态保留，避免重复提醒；曾因东财兜底
                    # 返回 '-' 字符串致 _chg_span TypeError 整体崩溃）
                    print(f"[{now:%H:%M:%S}] ⚠ 本轮盯盘异常: "
                          f"{type(e).__name__}: {e}")
                    try:
                        # 2026-08-15 审计 M5：必须 dict（渲染取 a['time']），
                        # 曾误 append 字符串导致渲染 TypeError → 盯盘页冻结
                        state["alerts"].append({
                            "time": now.strftime("%H:%M:%S"),
                            "name": "系统",
                            "message": f"⚠ 盯盘异常：{type(e).__name__}，已自动继续",
                        })
                    except Exception:  # noqa: BLE001
                        pass
                first = False
                if once:
                    break
                time.sleep(interval)
            else:
                print(f"[{now:%H:%M:%S}] {status}，休眠中（每分钟检查一次）")
                time.sleep(60)
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    monitor(once=True)
