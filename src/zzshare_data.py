"""zzshare 全市场日线抓取（按交易日，含官方涨停价/换手率/ST/停牌标记）。

每交易日一次调用即可获得全市场 5500+ 只股票的完整行情，
其中 high_limit/low_limit 为交易所口径涨停/跌停价，
天然解决除权除息、北交所取整等自算涨停价的坑。
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
from tqdm import tqdm
from zzshare.client import DataApi

from config import ZZSHARE_DIR

_RENAME = {
    "ts_code": "code",
    "trade_date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "high_limit": "limit_price",
    "low_limit": "low_price",
    "turnover_rate": "turnover",   # 换手率 %
    "turnover": "amount",          # 成交额 元
    "quote_rate": "pct_chg",       # 涨跌幅 %
    "prev_close": "prev_close",
    "is_st": "is_st",
    "is_paused": "is_paused",
    "volume": "volume",
}

_KEEP = ["date", "code", "open", "high", "low", "close", "limit_price",
         "low_price", "turnover", "amount", "pct_chg", "prev_close",
         "is_st", "is_paused", "volume"]

_api = None


def get_api() -> DataApi:
    global _api
    if _api is None:
        _api = DataApi()
    return _api


def _normalize(df: pd.DataFrame, date: str) -> pd.DataFrame:
    df = df.rename(columns=_RENAME)
    df["code"] = df["code"].astype(str).str.split(".").str[0].str.strip()
    df["date"] = date
    for c in _KEEP:
        if c not in df.columns:
            df[c] = pd.NA
    return df[_KEEP]


def _cache_path(date: str) -> Path:
    return ZZSHARE_DIR / f"{date.replace('-', '')}.csv"


def fetch_day_panel(date: str, force: bool = False, tries: int = 6) -> pd.DataFrame:
    """抓取指定交易日全市场日线（缓存为 csv）。date 格式 YYYY-MM-DD。"""
    path = _cache_path(date)
    if path.exists() and not force:
        try:
            return pd.read_csv(path, dtype={"code": str})
        except Exception:  # noqa: BLE001
            pass
    api = get_api()
    ymd = date.replace("-", "")
    raw = None
    for i in range(tries):
        try:
            raw = api.daily(trade_date=ymd, limit=10000, fields="all")
            break
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {date} 重试 {i + 1}: {type(e).__name__}")
            time.sleep(min(2.0 * (i + 1), 12))
    if raw is None or len(raw) == 0:
        if path.exists():
            path.unlink()
        return pd.DataFrame()
    df = _normalize(raw, date)
    df.to_csv(path, index=False)
    return df


def fetch_all_panels(
    start: str,
    end: str | None = None,
    force: bool = False,
    sleep: float = 0.2,
) -> tuple[list[str], list[str]]:
    """按交易日增量抓取全市场日线，返回 (成功日期, 空日期)。"""
    from src.data_fetcher import trading_days_between
    dates = trading_days_between(start, end)
    ok, empty = [], []
    for d in tqdm(dates, desc="抓取 zzshare 全市场日线"):
        df = fetch_day_panel(d, force=force)
        (ok if not df.empty else empty).append(d)
        if sleep:
            time.sleep(sleep)
    print(f"完成：{len(ok)} 个交易日有数据，{len(empty)} 个为空/滞后")
    return ok, empty


def load_all_panels() -> pd.DataFrame:
    """合并全部已缓存的全市场日线。"""
    parts = []
    for p in sorted(ZZSHARE_DIR.glob("[0-9]*.csv")):
        try:
            parts.append(pd.read_csv(p, dtype={"code": str}))
        except Exception:  # noqa: BLE001
            continue
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)
