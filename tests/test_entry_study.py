"""买点提前与胜率研究（entry_study）单元测试。

覆盖：退出引擎（止损/止盈/时间止损）、固定持有与站岗指标、
入场变体的信号收集（确认标志/跳空字段）。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from mainrise import entry_study as es


def make_panel(closes, highs=None, lows=None, opens=None, date0="2026-01-05"):
    n = len(closes)
    highs = highs if highs is not None else [c * 1.02 for c in closes]
    lows = lows if lows is not None else [c * 0.98 for c in closes]
    opens = opens if opens is not None else [c * 0.99 for c in closes]
    dates = pd.bdate_range(date0, periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": [1e6] * n,
    })


class TestExitRet(unittest.TestCase):
    def test_stop_loss(self):
        # 买入后第 2 日收盘跌破 -4% → 按 -4% 理想价成交
        p = make_panel([10.0, 10.2, 9.5, 9.8, 10.0, 10.1, 10.2])
        ret, trunc = es.exit_ret(p, 0, 10.0)
        self.assertAlmostEqual(ret, (10.0 * es.STOP - 10.0) / 10.0 - es.COST)
        self.assertFalse(trunc)

    def test_pullback_take_profit(self):
        # 高点 10.5（+5%）→ 收盘回落至 9.6（-8.57%）→ 止盈按收盘成交
        p = make_panel([10.0, 10.5, 9.6, 9.8],
                       highs=[10.1, 10.6, 9.7, 9.9],
                       lows=[9.9, 10.2, 9.5, 9.7])
        ret, trunc = es.exit_ret(p, 0, 10.0)
        self.assertAlmostEqual(ret, (9.6 - 10.0) / 10.0 - es.COST)
        self.assertFalse(trunc)

    def test_time_stop(self):
        # 5 日不触发止损/止盈 → 第 5 个交易日收盘退出
        p = make_panel([10.0, 10.1, 10.0, 10.1, 10.0, 10.1, 10.0])
        ret, trunc = es.exit_ret(p, 0, 10.0)
        self.assertAlmostEqual(ret, (10.1 - 10.0) / 10.0 - es.COST)
        self.assertFalse(trunc)


class TestFwdMetrics(unittest.TestCase):
    def test_under3_and_mad5(self):
        p = make_panel([10.0, 10.5, 9.8, 9.5, 10.2, 10.6],
                       highs=[10.1, 10.6, 9.9, 9.6, 10.3, 10.7],
                       lows=[9.9, 10.3, 9.6, 9.4, 10.1, 10.4])
        fm = es.fwd_metrics(p, 0, 10.0)
        self.assertEqual(fm["fwd3"], 9.5 / 10.0 - 1)
        self.assertEqual(fm["fwd5"], 10.6 / 10.0 - 1)
        self.assertEqual(fm["under3"], 1.0)      # 第 3 日收盘 9.5 < 10
        self.assertAlmostEqual(fm["mad5"], 9.4 / 10.0 - 1)


class TestCollectSignals(unittest.TestCase):
    def test_confirmation_and_gap_fields(self):
        # 构造 42 根 K 线：缓慢上行后放量大阳线创 20 日新高（T0 信号，留出 T1/T2）
        rng = np.random.default_rng(7)
        closes = list(np.cumsum(rng.uniform(0.002, 0.008, 42)) + 10)
        closes[39] = closes[38] * 1.06            # T0 大阳线（i=39，i+2=41<42）
        p = make_panel(closes)
        p["volume"] = [2e6] * 39 + [8e6] + [2e6, 2e6]   # T0 放量
        p["code"] = "000001"
        p["prev_close"] = p["close"].shift(1)
        t = es.tail_features(p, tail=len(p))
        self.assertTrue(bool(t["signal"].iloc[39]))
        # 跑信号收集（无需全市场特征）
        sig = es.collect_signals(pd.DataFrame(
            {"code": ["000001"] * len(p), "date": p["date"],
             "open": p["open"], "high": p["high"], "low": p["low"],
             "close": p["close"], "volume": p["volume"],
             "prev_close": p["prev_close"]}), None)
        self.assertEqual(len(sig), 1)
        r = sig.iloc[0]
        self.assertIn("gap1", r)                 # T1 跳空字段存在
        self.assertIn("conf", r)
        self.assertIn("is_limit", r)
        self.assertIn("bg", r)                   # 底部涨幅存在


if __name__ == "__main__":
    unittest.main()
