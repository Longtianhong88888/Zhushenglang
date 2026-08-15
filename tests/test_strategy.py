"""启动加仓投资模型模拟测试。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from mainrise.strategy import sim_panel


def _panel(closes: list[float], mkt_zt: float = 100.0) -> pd.DataFrame:
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    prev = np.concatenate([[closes[0]], closes[:-1]])
    pct = (closes / prev - 1) * 100
    return pd.DataFrame({
        "date": [f"2026-01-{d:02d}" for d in range(1, n + 1)],
        "code": ["000001"] * n, "close": closes, "open": closes, "high": closes,
        "low": closes, "volume": [1_000_000.0] * n, "pct_chg": pct,
        "prev_close": prev, "limit_price": closes * 1.1, "mkt_zt": mkt_zt,
    })


class TestStrategy(unittest.TestCase):
    def test_stop_on_first_holding_day(self):
        p = _panel([10.0] * 40 + [10.5, 10.6, 11.0, 11.5, 11.8])
        p.loc[41, "open"] = 10.6
        p.loc[41, "low"] = 9.7          # 底仓第 1 天直接破 -7% 止损
        mask = pd.Series(False, index=p.index)
        mask.iloc[40] = True
        df = sim_panel(p, mask)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["reason"], "止损")
        self.assertFalse(bool(df.iloc[0]["added"]))
        self.assertLess(df.iloc[0]["ret"], -0.06)

    def test_add_then_take_profit(self):
        p = _panel([10.0] * 40 + [10.5, 10.8, 11.4, 10.5])
        p.loc[43, "high"] = 11.8         # 盘中冲高 11.8 → 收盘 10.5 回落超 10%
        # T+1 收 10.8（站上 MA10≈10.05）→ 加仓；T+3 收盘 10.5 ≤ 峰值11.8*0.9 → 止盈
        mask = pd.Series(False, index=p.index)
        mask.iloc[40] = True
        df = sim_panel(p, mask)
        self.assertEqual(len(df), 1)
        self.assertTrue(bool(df.iloc[0]["added"]))
        self.assertEqual(df.iloc[0]["reason"], "止盈")
        self.assertGreater(df.iloc[0]["ret"], -0.07)   # 止盈=利润回吐保护，非保证盈利

    def test_market_protect_exit(self):
        p = _panel([10.0] * 40 + [10.5, 10.8, 11.4, 11.8])
        p.loc[43, "mkt_zt"] = 40        # 退潮 → 次日开盘清仓
        p.loc[43, "open"] = 11.5
        mask = pd.Series(False, index=p.index)
        mask.iloc[40] = True
        df = sim_panel(p, mask)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["reason"], "退潮")

    def test_no_add_when_below_ma10(self):
        p = _panel([10.0] * 40 + [10.5, 10.02] + [10.2] * 12)
        mask = pd.Series(False, index=p.index)
        mask.iloc[40] = True
        df = sim_panel(p, mask)
        self.assertEqual(len(df), 1)
        self.assertFalse(bool(df.iloc[0]["added"]))
        self.assertEqual(df.iloc[0]["reason"], "时间")


if __name__ == "__main__":
    unittest.main()
