"""起涨特征研究模块测试。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from mainrise.launch import _launch_episodes, _signal_mask


def _panel(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    if volumes is None:
        volumes = [1_000_000.0] * n
    prev = np.concatenate([[closes[0]], closes[:-1]])
    pct = (closes / prev - 1) * 100
    return pd.DataFrame({
        "close": closes, "high": closes, "low": closes, "open": closes,
        "volume": volumes, "pct_chg": pct, "prev_close": prev,
        "limit_price": closes * 1.1, "mkt_zt": 100,
    })


class TestLaunch(unittest.TestCase):
    def test_episode_detection(self):
        # 60 天横盘（前期20日涨幅≈0）→ 20 天爬升 30%：横盘末段为起涨段落起点
        closes = [10.0] * 60 + list(np.linspace(10.0, 13.0, 20))
        ep = _launch_episodes(_panel(closes))
        self.assertTrue(any(50 <= i <= 55 for i in ep))

    def test_signal_mask_r4(self):
        # 冲高 30 后回撤到 25（-16.7%），当日 +6% 且量比 1.6 → r4/r4m/r7 触发，t0 不触发
        closes = list(np.linspace(20, 30, 20)) + [29.5, 29, 28, 27, 26, 25.5,
                                                  25.2, 25.0, 25.0, 25.0, 26.5]
        vols = [1_000_000.0] * (len(closes) - 1) + [1_600_000.0]
        df = _panel(closes, vols)
        masks = _signal_mask(df)
        self.assertTrue(bool(masks["r4"].iloc[-1]))
        self.assertTrue(bool(masks["r4m"].iloc[-1]))
        self.assertTrue(bool(masks["r7"].iloc[-1]))       # mkt_zt=100 ≥ 90
        self.assertFalse(bool(masks["r9"].iloc[-1]))      # mkt_zt=100 < 130
        self.assertFalse(bool(masks["t0"].iloc[-1]))      # 未创 20 日新高

    def test_signal_mask_market_gate(self):
        closes = [10.0] * 20 + [8.2] * 16
        closes.append(8.2 * 1.06)
        vols = [1_000_000.0] * (len(closes) - 1) + [1_500_000.0]
        df = _panel(closes, vols)
        df["mkt_zt"] = 50                                  # 涨停家数不足
        masks = _signal_mask(df)
        self.assertTrue(bool(masks["r4"].iloc[-1]))
        self.assertFalse(bool(masks["r7"].iloc[-1]))       # 市场门限拦截
        self.assertFalse(bool(masks["r9"].iloc[-1]))

    def test_pre_features_enhanced(self):
        from mainrise.launch import _pre_features
        # 前 5 日净跌 + 当日创新 20 日低点反包 → r7mA / r7mE 同时触发
        closes = ([10.0] * 20 + [9.5, 9.2, 8.9, 8.6, 8.3])
        vols = [1_000_000.0] * len(closes)
        p = _panel(closes, vols)
        p.loc[len(p)] = {"close": 8.8, "high": 8.9, "low": 8.1, "open": 8.3,
                         "volume": 1_500_000.0, "pct_chg": 6.024, "limit_price": 9.68,
                         "mkt_zt": 100, "prev_close": 8.3}
        p = _pre_features(p)
        masks = _signal_mask(p)
        self.assertTrue(bool(masks["r7mA"].iloc[-1]))
        self.assertTrue(bool(masks["r7mE"].iloc[-1]))
        self.assertTrue(bool(masks["r7m"].iloc[-1]))
        self.assertFalse(bool(masks["r9A"].iloc[-1]))       # mkt_zt=100 < 130
        self.assertFalse(bool(masks["r9E"].iloc[-1]))


if __name__ == "__main__":
    unittest.main()
