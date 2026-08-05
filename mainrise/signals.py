"""主升浪信号指标：均线/量比/创新高/回踩状态判定。"""
from __future__ import annotations

import pandas as pd

MIN_CHG = 5.0
MIN_VR = 1.5


def in_universe(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002",
                            "003", "300", "301"))


def load_names() -> dict:
    from mainrise import paths
    try:
        df = pd.read_csv(paths.stock_list_path(), dtype={"code": str})
        return dict(zip(df["code"], df["name"]))
    except Exception:
        return {}


def tail_features(g: pd.DataFrame, tail: int = 60) -> pd.DataFrame | None:
    """对单只股票最近 N 个交易日计算指标（返回快照帧）。"""
    if len(g) < 35:
        return None
    t = g.tail(tail).reset_index(drop=True)
    c = t["close"].to_numpy(float)
    h = t["high"].to_numpy(float)
    l = t["low"].to_numpy(float)
    v = t["volume"].to_numpy(float)
    cs = pd.Series(c)
    m5 = cs.rolling(5).mean().to_numpy()
    m10 = cs.rolling(10).mean().to_numpy()
    m20 = cs.rolling(20).mean().to_numpy()
    v5 = pd.Series(v).rolling(5).mean().shift(1).to_numpy()
    prev_c = cs.shift(1).to_numpy()
    hi20 = pd.Series(h).rolling(20).max().shift(1).to_numpy()
    chg = (c / prev_c - 1) * 100
    chg10 = (c / cs.shift(10).to_numpy() - 1) * 100
    vr = v / v5
    bull = (m5 > m10) & (m10 > m20)
    new_high = c > hi20
    gem = str(t["code"].iloc[0]).startswith(("300", "301", "688"))
    limit_up = c >= prev_c * (1.195 if gem else 1.095)
    surge = (chg >= MIN_CHG) & (vr >= MIN_VR)
    signal = bull & new_high & (surge | limit_up) & (m20 > 0) & (hi20 > 0)
    out = t.copy()
    out["ma5"] = m5
    out["ma10"] = m10
    out["ma20"] = m20
    out["vol_ratio"] = vr
    out["chg"] = chg
    out["chg10"] = chg10
    out["bull"] = bull
    out["new_high"] = new_high
    out["signal"] = signal
    return out


def row_status(row: pd.Series, prev_signal: bool,
               max_10d: float = 80.0) -> tuple[str, str]:
    """返回 (标签, 提示)。row 为当日行，prev_signal 为昨日是否信号日。"""
    close = row["close"]
    low = row["low"]
    m5, m10, m20 = row["ma5"], row["ma10"], row["ma20"]
    extended = pd.notna(row.get("chg10", float("nan"))) and row["chg10"] >= max_10d
    if row["signal"]:
        hint = f"明日确认：收>{m5:.2f} 且 低点≥{close*0.97:.2f} → 后日开盘买入"
        if extended:
            hint += f"（⚠10日已+{row['chg10']:.0f}%，涨幅过大勿追）"
        return ("T0新信号", hint)
    if prev_signal:
        hint = f"买点1：明日开盘买入（信号日收盘 {row['prev_close']:.2f}，止损-5%）"
        if extended:
            hint += f"（⚠10日已+{row['chg10']:.0f}%，涨幅过大勿追）"
        return ("T1确认买点", hint)
    if row["bull"] and low >= m20 * 0.99 and row["vol_ratio"] <= 1.0 and close < m5:
        hint = f"买点2：MA10({m10:.2f})附近缩量企稳，可低吸"
        if extended:
            hint += f"（⚠10日已+{row['chg10']:.0f}%，涨幅过大勿追）"
        return ("回踩低吸", hint)
    if row["bull"] and close >= m10:
        return ("多头持有", f"MA10({m10:.2f})上方，回踩低吸/持有")
    if row["bull"] and close >= m20 * 0.99:
        return ("多头回踩", f"回踩 MA10({m10:.2f})→MA20({m20:.2f})区间，观察企稳")
    if close < m20:
        return ("破位", "跌破 MA20，趋势破坏，暂不参与")
    return ("空头", "均线未多头，观望")


def scan_market(panels: pd.DataFrame, date: str, names: dict) -> pd.DataFrame:
    """全市场扫描最近 2 日的 T0/T1 主升浪信号。"""
    from tqdm import tqdm
    rows = []
    for code, g in tqdm(panels.groupby("code", sort=False), desc="市场扫描",
                        leave=False):
        if not in_universe(code) or len(g) < 35:
            continue
        t = tail_features(g)
        if t is None:
            continue
        last2 = t.tail(2)
        if last2.empty:
            continue
        today = last2.iloc[-1]
        yest = last2.iloc[-2] if len(last2) >= 2 else None
        if today["date"] != date:
            continue
        if today["signal"]:
            rows.append({"code": code, "name": names.get(code, ""),
                         "date": date, "kind": "T0", "chg": today["chg"],
                         "vr": today["vol_ratio"], "chg10": today["chg10"]})
        elif yest is not None and yest["signal"]:
            rows.append({"code": code, "name": names.get(code, ""),
                         "date": date, "kind": "T1", "chg": yest["chg"],
                         "vr": yest["vol_ratio"], "chg10": today["chg10"]})
    return pd.DataFrame(rows)
