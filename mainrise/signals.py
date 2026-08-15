"""主升浪信号指标：均线/量比/创新高/回踩状态判定。"""
from __future__ import annotations

import pandas as pd
import numpy as np

MIN_CHG = 5.0
MIN_VR = 1.5
MIN_VR_LIMIT = 1.0   # 涨停也要求量比 >= 1.0（缩量涨停/一字板不计信号）

# 两级模型（2026-08-14 用户确认）：
#   第一级 B3：均线粘合爆量突破 → 打底仓提示
#   第二级 二波：B3 后回调、均线再次粘合、再放量启动 → 加仓信号（最优买点）
B3_SPREAD = 0.03          # 均线最大偏离 ≤ 3%（粘合）
B3_MIN_VR = 2.0           # 爆量：量比 ≥ 2.0
B3_MIN_CHG = 1.0          # 涨幅 ≥ 1%（温和阳线即可，如秦安 8/6 +1.93%）
B3_LOW_POS = 0.30         # 距 60 日低点 < 30%（低位区）
W2_SPREAD = 0.02          # 再次粘合 ≤ 2%
W2_MIN_VR = 1.5           # 二波触发量比 ≥ 1.5
W2_MIN_CHG = 2.0          # 二波触发涨幅 ≥ 2%
W2_WIN_LO, W2_WIN_HI = 3, 30   # B3 后 3~30 个交易日内
W2_DEPTH_LO, W2_DEPTH_HI = 0.02, 0.12  # 回调深度 2%~12%


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
    # 内部至少取 90 行：lo60 需 60 日前史 + 二波窗口 30 日，
    # 避免 tail=60 时 lo60/二波因窗口不足恒为 NaN（曾导致 B3 全部漏判）
    need = max(int(tail), 90)
    t = g.tail(need).reset_index(drop=True)
    c = t["close"].to_numpy(float)
    h = t["high"].to_numpy(float)
    l = t["low"].to_numpy(float)
    o = t["open"].to_numpy(float)
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
    # （研究用，已不用于运行模型）多头 + 创新高 + 大阳线/涨停
    limit_up = (c >= prev_c * (1.195 if gem else 1.095)) & (vr >= MIN_VR_LIMIT)
    surge = (chg >= MIN_CHG) & (vr >= MIN_VR)
    signal = bull & new_high & (surge | limit_up) & (m20 > 0) & (hi20 > 0)

    # ── 两级模型：B3（粘合爆量突破） / 二波（加仓信号） ──
    spread = (np.maximum(np.maximum(m5, m10), m20)
              - np.minimum(np.minimum(m5, m10), m20)) / m20
    lo60 = pd.Series(l).rolling(60).min().shift(1).to_numpy()
    cross_all = (c > m5) & (c > m10) & (c > m20)
    yang = c > o
    b3 = ((spread <= B3_SPREAD) & yang & (chg >= B3_MIN_CHG)
          & (vr >= B3_MIN_VR) & cross_all
          & (lo60 > 0) & (c / lo60 - 1 < B3_LOW_POS))
    # 二波：B3 后 3~30 日内 → 回调 2~12% + 再次粘合≤2% + 缩量 → 触发日放量阳线站上三均线
    wave2 = np.zeros(len(t), dtype=bool)
    b3_idx = np.where(b3)[0]
    for i in range(len(t)):
        b = None
        for bi in reversed(b3_idx):      # 从最近的 B3 往前找
            d = i - bi
            if d > W2_WIN_HI:
                break
            if d >= W2_WIN_LO:
                b = bi
                break
        if b is None or not (vr[i] >= W2_MIN_VR and yang[i]
                             and chg[i] >= W2_MIN_CHG and cross_all[i]):
            continue
        t2 = (spread[i - 1] <= W2_SPREAD if i >= 1 else False) or \
             (spread[i - 2] <= W2_SPREAD if i >= 2 else False)
        if not t2:
            continue
        hi_since = h[b + 1:i].max() if i > b + 1 else 0.0
        if hi_since <= 0:
            continue
        depth = c[b] / hi_since - 1
        if not (-W2_DEPTH_HI <= depth <= -W2_DEPTH_LO):
            continue
        if v[b + 1:i].mean() >= v[b]:
            continue
        wave2[i] = True

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
    out["spread"] = spread
    out["lo60"] = lo60
    out["b3"] = b3
    out["wave2"] = wave2
    return out.tail(tail).reset_index(drop=True)


def row_status(row: pd.Series, prev_b3: bool = False,
               max_10d: float = 150.0) -> tuple[str, str]:
    """两级模型状态：二波加仓（最优买点）/ B3 打底仓 / B3 待二波 / 观察。"""
    extended = pd.notna(row.get("chg10", float("nan"))) and row["chg10"] >= max_10d
    warn = f"（⚠10日已+{row['chg10']:.0f}%，涨幅过大勿追）" if extended else ""
    if bool(row.get("wave2", False)):
        return ("二波加仓", "最优买点：明日开盘加仓（1/3，总仓≤1/3）" + warn)
    if bool(row.get("b3", False)):
        return ("B3打底仓", "均线粘合爆量突破：明日开盘打底仓（计划仓位 2/3）" + warn)
    if prev_b3:
        return ("B3待二波", "B3 后等待深回调+均线再次粘合≤2%+缩量 → 二波加仓信号")
    return ("观察", "等待 B3（均线粘合爆量突破）/ 二波信号")


def scan_two_stage(panels: pd.DataFrame, date: str, names: dict) -> pd.DataFrame:
    """卡点名单扫描当日 B3（打底仓）/ 二波（加仓）信号。"""
    from tqdm import tqdm
    rows = []
    for code, g in tqdm(panels.groupby("code", sort=False), desc="市场扫描",
                        leave=False):
        if len(g) < 35:
            continue
        t = tail_features(g)
        if t is None:
            continue
        last = t.iloc[-1]
        if last["date"] != date:
            continue
        if bool(last.get("wave2", False)):
            rows.append({"code": code, "name": names.get(code, ""),
                         "date": date, "kind": "二波", "chg": last["chg"],
                         "vr": last["vol_ratio"], "chg10": last["chg10"]})
        elif bool(last.get("b3", False)):
            rows.append({"code": code, "name": names.get(code, ""),
                         "date": date, "kind": "B3", "chg": last["chg"],
                         "vr": last["vol_ratio"], "chg10": last["chg10"]})
    return pd.DataFrame(rows)
