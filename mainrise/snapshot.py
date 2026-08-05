"""同花顺实时行情快照（验证买点涨跌幅用）。"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
import requests

BASE = "https://fuyao.aicubes.cn"


def get_api_key() -> str:
    key = (os.environ.get("MAINRISE_API_KEY", "")
           or os.environ.get("FUYAO_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("缺少 API key：请设置环境变量 MAINRISE_API_KEY")
    return key


def ths_code(code: str) -> str:
    c = code.split(".")[0]
    # 沪市基金/ETF：51x / 56x / 58x（含 588/589 科创类 ETF）
    if c.startswith(("51", "56", "58")):
        return f"{c}.SH"
    # 深市基金/ETF/LOF：15x（如 159xxx）
    if c.startswith("15"):
        return f"{c}.SZ"
    if c.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return f"{c}.SH"
    if c.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return f"{c}.SZ"
    return f"{c}.BJ"


def is_etf(code: str) -> bool:
    """沪市(51/56/58)与深市(15)场内基金/ETF 代码段。"""
    c = code.split(".")[0]
    return c.startswith(("51", "56", "58", "15"))


def fetch_snapshot(codes: list[str], key: str | None = None) -> pd.DataFrame:
    """行情快照（当日实时）。股票走 a-share 端点，ETF/LOF 走 fund 端点。"""
    key = key or get_api_key()
    frames = []
    for code in codes:
        fund = is_etf(code)
        path = "/api/fund/market/snapshot" if fund else "/api/a-share/prices/snapshot"
        params = {"thscode": ths_code(code)} if fund else {"thscodes": ths_code(code)}
        try:
            r = requests.get(f"{BASE}{path}",
                             params=params, headers={"X-api-key": key}, timeout=30)
            d = r.json()
            if d.get("code") != 0:
                frames.append(pd.DataFrame([{
                    "code": code, "close": np.nan, "price_change": np.nan,
                    "price_change_ratio_pct": np.nan, "open": np.nan,
                    "high": np.nan, "low": np.nan, "prev_close": np.nan,
                    "volume": np.nan, "turnover": np.nan,
                    "error": f"code={d.get('code')} {d.get('message', '')}"}]))
                continue
            items = (d.get("data") or {}).get("item") or []
            if not items:
                frames.append(pd.DataFrame([{
                    "code": code, "close": np.nan, "error": "无数据"}]))
                continue
            df = pd.DataFrame(items)
            df["code"] = df["ticker"].astype(str)
            df = df.rename(columns={
                "last_price": "close",
                "open_price": "open",
                "high_price": "high",
                "low_price": "low",
                "prev_price": "prev_close",
            })
            keep = [c for c in ["code", "close", "price_change",
                                "price_change_ratio_pct", "open", "high",
                                "low", "prev_close", "volume", "turnover"]
                    if c in df.columns]
            df = df[keep]
            df["error"] = ""
            frames.append(df)
        except Exception as e:  # noqa: BLE001
            frames.append(pd.DataFrame([{
                "code": code, "close": np.nan, "error": f"{type(e).__name__}: {e}"}]))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
