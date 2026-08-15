"""波段高抛低吸研究（wave）单元测试。

覆盖：zigzag 转折识别（含确认日索引）、波段交易（确认日成交/费用/净值）。
"""
from __future__ import annotations

import unittest

import numpy as np

from mainrise import wave as wv


class TestZigzag(unittest.TestCase):
    def test_zigzag_peaks_troughs_with_confirm(self):
        # 上涨→下跌 8%→上涨：应产出 1 个顶（确认日在回落跌破 8% 那天）+ 1 个底
        closes = np.array([10, 10.5, 11, 11.5, 12, 12.2, 12.5, 11.6, 10.9,
                           10.2, 9.6, 10.5, 11.4])
        zz = wv.zigzag(closes, 0.08)
        self.assertEqual(len(zz), 2)
        top = zz[0]
        self.assertEqual(top[2], 1)                    # 顶
        self.assertEqual(top[0], 6)                    # 极值日 = 12.5
        self.assertGreater(top[1], top[0])             # 确认日 > 极值日
        self.assertAlmostEqual(closes[top[1]], 10.9)   # 确认日收盘 ≤ 12.5×0.92
        bot = zz[1]
        self.assertEqual(bot[2], -1)                   # 底
        self.assertEqual(bot[0], 10)                   # 谷值日 = 9.6
        self.assertGreater(bot[1], bot[0])             # 确认日在后
        self.assertAlmostEqual(closes[bot[1]], 10.5)   # 确认日收盘 ≥ 9.6×1.08

    def test_zigzag_no_pivot_flat(self):
        closes = np.ones(20) * 10.0
        self.assertEqual(wv.zigzag(closes, 0.08), [])


class TestWaveTrade(unittest.TestCase):
    def test_wave_trade_confirmation_fill(self):
        # 上升→下跌→上升：初始持仓，顶确认卖出（非极值价）、底确认买回
        closes = np.array([10, 10.5, 11, 11.5, 12, 12.2, 12.5, 11.6, 10.9,
                           10.2, 9.6, 10.5, 11.4, 12.0, 12.6, 12.2])
        trades, nav = wv.wave_trade(closes, 0, len(closes) - 1, 0.08)
        sells = [t for t in trades if t["t"] == "卖"]
        buys = [t for t in trades if t["t"] == "买"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(len(buys), 1)
        # 卖出价是确认日收盘（10.9），不是极值日 12.5
        self.assertAlmostEqual(sells[0]["px"], 10.9)
        # 买入价是底确认日收盘（10.5），不是谷值 9.6
        self.assertAlmostEqual(buys[0]["px"], 10.5)
        # 净值 = (10.9/10-cost) × (12.2末收盘/10.5-cost)
        exp = (10.9 / 10 - wv.COST) * (12.2 / 10.5 - wv.COST)
        self.assertAlmostEqual(nav, exp, places=6)

    def test_wave_trade_hold_flat(self):
        closes = np.ones(15) * 10.0
        trades, nav = wv.wave_trade(closes, 0, 14, 0.08)
        self.assertEqual([t["t"] for t in trades], ["持"])
        self.assertAlmostEqual(nav, 1.0 - wv.COST, places=6)


if __name__ == "__main__":
    unittest.main()
