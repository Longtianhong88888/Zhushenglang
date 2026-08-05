"""zzshare 数据层：交易日历 / 股票名册 / 全市场日线 的初始化、增量更新与加载。"""
from __future__ import annotations

import time
import sys
import zipfile
from pathlib import Path

import pandas as pd
from tqdm import tqdm
from zzshare.client import DataApi

from mainrise import paths

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


def init_calendar() -> int:
    """从 zzshare 拉取全量交易日历（2021 起）写入 trade_dates.csv。"""
    days = get_api().trade_days(day_start="20210101", day_end="20301231")
    days = [str(d) for d in days]
    if not days:
        raise RuntimeError("zzshare 交易日历返回为空")
    pd.DataFrame({"trade_date": sorted(days)}).to_csv(
        paths.trade_dates_path(), index=False)
    return len(days)


def init_stock_list() -> int:
    """从 zzshare 拉取股票名册（code+name）写入 stock_list.csv。"""
    sb = get_api().stock_basic()
    df = pd.DataFrame(sb)
    if df.empty or "symbol" not in df.columns:
        raise RuntimeError("zzshare 股票名册返回为空")
    df = df[["symbol", "name"]].rename(columns={"symbol": "code"})
    df["code"] = df["code"].astype(str)
    df["name"] = df["name"].astype(str).str.strip()
    df.to_csv(paths.stock_list_path(), index=False, encoding="utf-8-sig")
    return len(df)


def trade_dates() -> list[str]:
    """交易日历（升序）。本地无缓存时从 zzshare 拉取。"""
    p = paths.trade_dates_path()
    if p.exists():
        return sorted(pd.read_csv(p, dtype={"trade_date": str})["trade_date"].tolist())
    init_calendar()
    return trade_dates()


def latest_trading_day(ref: str | None = None) -> str:
    from datetime import datetime
    today = ref or datetime.now().strftime("%Y-%m-%d")
    prev = [d for d in trade_dates() if d <= today]
    if not prev:
        raise ValueError(f"交易日历中无 {today} 之前的日期")
    return prev[-1]


def trading_days_between(start: str, end: str | None = None) -> list[str]:
    dates = trade_dates()
    if end is None:
        end = latest_trading_day()
    return [d for d in dates if start <= d <= end]


def _normalize(df: pd.DataFrame, date: str) -> pd.DataFrame:
    df = df.rename(columns=_RENAME)
    df["code"] = df["code"].astype(str).str.split(".").str[0].str.strip()
    df["date"] = date
    for c in _KEEP:
        if c not in df.columns:
            df[c] = pd.NA
    return df[_KEEP]


def _cache_path(date: str) -> Path:
    return paths.zzshare_dir() / f"{date.replace('-', '')}.csv"


def fetch_day_panel(date: str, force: bool = False, tries: int = 6) -> pd.DataFrame:
    """抓取指定交易日全市场日线（缓存为 csv）。"""
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
    df = _normalize(pd.DataFrame(raw), date)
    df.to_csv(path, index=False)
    return df


def fetch_all_panels(start: str, end: str | None = None, force: bool = False,
                     sleep: float = 0.2) -> tuple[list[str], list[str]]:
    """按交易日增量抓取全市场日线，返回 (成功日期, 空日期)。"""
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
    for p in sorted(paths.zzshare_dir().glob("[0-9]*.csv")):
        try:
            parts.append(pd.read_csv(p, dtype={"code": str}))
        except Exception:  # noqa: BLE001
            continue
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def cached_days() -> int:
    """本地行情缓存天数。"""
    return len(list(paths.zzshare_dir().glob("[0-9]*.csv")))


def bundled_zip_path() -> Path | None:
    """内置行情数据包位置（PyInstaller 打包资源 / 开发模式 build/）。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "mainrise_data" / "bundled_data.zip")
    candidates.append(Path(sys.executable).parent / "mainrise_data" / "bundled_data.zip")
    candidates.append(Path(sys.executable).parent.parent / "mainrise_data" / "bundled_data.zip")
    candidates.append(Path(sys.executable).parent.parent / "Resources" / "mainrise_data" / "bundled_data.zip")
    candidates.append(Path(__file__).resolve().parent.parent / "build" / "bundled_data.zip")
    for p in candidates:
        if p.exists():
            return p
    return None


def ensure_bundled_data(progress: callable | None = None) -> bool:
    """数据目录无行情缓存时，从内置数据包解压。返回是否执行了解压。"""
    if cached_days() > 0:
        return False
    zp = bundled_zip_path()
    if zp is None:
        return False
    print(f"发现内置行情数据包（{zp.stat().st_size / 1e6:.0f} MB），开始解压...")
    paths.ensure_dirs()
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        total = len(names)
        for i, n in enumerate(names, 1):
            # 按前缀映射到对应目录（兼容项目模式 output/ 与独立模式）
            if n.startswith("data/"):
                z.extract(n, paths.data_dir().parent)
            elif n.startswith("state/"):
                z.extract(n, paths.state_dir().parent)
            elif n.startswith("reports/"):
                z.extract(n, paths.report_dir().parent)
            else:
                z.extract(n, paths.home())
            if progress is not None and i % 100 == 0:
                progress(i, total)
            elif i % 500 == 0:
                print(f"  解压进度 {i}/{total}")
    print(f"内置数据解压完成：{cached_days()} 天行情")
    return True
