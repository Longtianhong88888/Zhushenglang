"""实时行情快照（腾讯 qt.gtimg.cn，无需 API Key，股票/ETF 统一接口）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import requests

TX_QUOTE = "https://qt.gtimg.cn/q="
BATCH = 50          # 单请求批量上限（实测 60 条稳定）


def get_api_key() -> str:
    """腾讯接口无需 key；保留该函数仅为兼容旧调用，返回空串。"""
    return ""


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


def tx_code(code: str) -> str:
    """腾讯行情代码：sh/sz/bj + 6 位数字。"""
    num, market = ths_code(code).split(".")
    return market.lower() + num


def _parse_quote(line: str, code: str) -> dict:
    """解析腾讯报价串（~ 分隔），缺失字段置 NaN。"""
    f = line.split("~")

    def num(i: int) -> float:
        try:
            v = f[i].strip()
            return float(v) if v not in ("", "-") else np.nan
        except (IndexError, TypeError, ValueError):
            return np.nan

    close = num(3)
    prev_close = num(4)
    return {
        "code": code,
        "close": close,
        "price_change": close - prev_close,
        "price_change_ratio_pct": num(32),
        "open": num(5),
        "high": num(33),
        "low": num(34),
        "prev_close": prev_close,
        "volume": num(6),       # 手
        "turnover": num(37),    # 万元
        "error": "",
    }


def fetch_snapshot(codes: list[str], key: str | None = None) -> pd.DataFrame:
    """行情快照（当日实时）：腾讯单接口批量，股票/ETF 通用，无 key。"""
    rows = []
    for i in range(0, len(codes), BATCH):
        batch = codes[i:i + BATCH]
        rev = {tx_code(c): c for c in batch}
        try:
            r = requests.get(TX_QUOTE + ",".join(rev), timeout=8)
            r.encoding = "gbk"
            text = r.text
        except Exception as e:  # noqa: BLE001
            text = ""
            err = f"{type(e).__name__}: {e}"
            for code in batch:
                rows.append({"code": code, "close": np.nan, "error": err})
            continue
        parsed = set()
        for line in text.strip().split(";"):
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            tx = k.strip()[2:]        # v_sh600519 -> sh600519
            code = rev.get(tx)
            if code:
                rows.append(_parse_quote(v.strip().strip('"'), code))
                parsed.add(code)
        for code in batch:
            if code not in parsed:
                rows.append({"code": code, "close": np.nan, "error": "无数据"})
    if not rows:
        return pd.DataFrame(columns=["code", "close", "price_change",
                                     "price_change_ratio_pct", "open",
                                     "high", "low", "prev_close", "volume",
                                     "turnover", "error"])
    return pd.DataFrame(rows)
