"""A股涨停相关数据抓取与本地缓存。

数据源：akshare 东方财富接口。所有数据按日期缓存为 csv，
重复运行只增量拉取缺失日期。
"""
from __future__ import annotations

from datetime import datetime
import time
from pathlib import Path

import akshare as ak
import pandas as pd

from config import DATA_DIR, FETCH_RETRIES, FETCH_SLEEP, POOL_DIR

POOL_TYPES = {
    "zt": "涨停股池",
    "zt_prev": "昨日涨停股池",
    "zb": "炸板股池",
    "dt": "跌停股池",
}

_RENAME = {
    "序号": "seq",
    "代码": "code",
    "名称": "name",
    "涨跌幅": "pct_chg",
    "最新价": "price",
    "涨停价": "limit_price",
    "成交额": "amount",
    "流通市值": "float_mcap",
    "总市值": "total_mcap",
    "换手率": "turnover",
    "封板资金": "seal_amount",
    "首次封板时间": "first_seal_time",
    "最后封板时间": "last_seal_time",
    "炸板次数": "break_times",
    "涨停统计": "limit_stats",
    "连板数": "boards",
    "所属行业": "industry",
    "涨速": "speed",
    "振幅": "amplitude",
    "昨日封板时间": "yest_seal_time",
    "昨日连板数": "yest_boards",
    "昨日首次封板时间": "yest_first_seal_time",
    "昨日炸板次数": "yest_break_times",
}


def _fetch_map():
    return {
        "zt": ak.stock_zt_pool_em,
        "zt_prev": ak.stock_zt_pool_previous_em,
        "zb": ak.stock_zt_pool_zbgc_em,
        "dt": ak.stock_zt_pool_dtgc_em,
    }


def normalize_pool(df: pd.DataFrame, date: str) -> pd.DataFrame:
    """统一股池列名并补充日期列。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    for col in df.columns:
        if isinstance(col, str):
            df.columns = [c.strip() for c in df.columns]
            break
    df = df.rename(columns=_RENAME)
    if "code" in df.columns:
        df["code"] = df["code"].astype(str).str.strip()
    if "name" in df.columns:
        df["name"] = df["name"].astype(str).str.strip()
    df["date"] = date
    return df


def _cache_path(pool_type: str, date: str) -> Path:
    return POOL_DIR / f"{pool_type}_{date.replace('-', '')}.csv"


def _call_with_retry(func, date: str) -> pd.DataFrame:
    last_err = None
    for i in range(FETCH_RETRIES):
        try:
            df = func(date=date.replace("-", ""))
            return df if df is not None else pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (i + 1))
    print(f"[warn] 抓取失败 {func.__name__} {date}: {last_err}")
    return pd.DataFrame()


def fetch_pool(pool_type: str, date: str, force: bool = False) -> pd.DataFrame:
    """抓取指定日期股池，优先读缓存。date 格式 YYYY-MM-DD。"""
    path = _cache_path(pool_type, date)
    if path.exists() and not force:
        try:
            return pd.read_csv(path, dtype={"code": str})
        except Exception:  # noqa: BLE001
            pass
    func = _fetch_map()[pool_type]
    raw = _call_with_retry(func, date)
    df = normalize_pool(raw, date)
    if not df.empty:
        df.to_csv(path, index=False)
    elif path.exists():
        path.unlink()  # 清掉旧的空缓存
    time.sleep(FETCH_SLEEP)
    return df


_TRADE_CAL = None


def trade_dates() -> list[str]:
    """全部交易日（升序），带本地缓存。"""
    global _TRADE_CAL
    cal_path = DATA_DIR / "trade_dates.csv"
    if cal_path.exists():
        df = pd.read_csv(cal_path, dtype={"trade_date": str})
        _TRADE_CAL = sorted(df["trade_date"].tolist())
        return _TRADE_CAL
    raw = ak.tool_trade_date_hist_sina()
    dates = sorted(raw["trade_date"].astype(str).tolist())
    pd.DataFrame({"trade_date": dates}).to_csv(cal_path, index=False)
    _TRADE_CAL = dates
    return dates


def trading_days_between(start: str, end: str | None = None) -> list[str]:
    """返回 [start, end] 区间内交易日。end=None 表示到今天为止的最近交易日。"""
    dates = trade_dates()
    if end is None:
        end = latest_trading_day()
    start = min(start, end)
    out = [d for d in dates if d >= start]
    out = [d for d in out if d <= end]
    return out


def latest_trading_day(ref: str | None = None) -> str:
    """今天（或指定日）之前最近的一个交易日。"""
    today = ref or datetime.now().strftime("%Y-%m-%d")
    prev = [d for d in trade_dates() if d <= today]
    if not prev:
        raise ValueError(f"交易日历中无 {today} 之前的日期")
    return prev[-1]


def next_trading_day(d: str) -> str | None:
    dates = trade_dates()
    try:
        i = dates.index(d)
    except ValueError:
        return None
    return dates[i + 1] if i + 1 < len(dates) else None


def available_pool_start(pool_type: str = "zt") -> str:
    """二分探测：找出该股池最早有数据的交易日（假设可用区间连续）。"""
    dates = trade_dates()
    hi = latest_trading_day()
    if hi not in dates:
        hi = dates[-1]
    if fetch_pool(pool_type, dates[0]).empty:
        lo_i = 0
    else:
        return dates[0]
    hi_i = dates.index(hi)
    if fetch_pool(pool_type, hi).empty:
        print(f"[warn] {pool_type} 池在最新交易日也无数据")
        return hi
    l, r = lo_i, hi_i
    while l < r:
        mid = (l + r) // 2
        if fetch_pool(pool_type, dates[mid]).empty:
            l = mid + 1
        else:
            r = mid
    return dates[l]
