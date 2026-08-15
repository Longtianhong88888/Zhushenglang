"""大牛候选规则回测（candidate_bt）单元测试。

覆盖：单股模拟（信号日入场、MA60 退出、去重）、90 日 T0 计数触发条件。
"""
from __future__ import annotations

import unittest

import pandas as pd

from mainrise import candidate_bt as cbt


def trend_panel(spike_days: list, n: int = 300, crash: bool = False) -> pd.DataFrame:
    """平台 + 涨停脉冲（触发 T0 信号）+ 脉冲后稳步上行（保持多头）。

    crash=True 时末段连续阴跌触发跌破 MA60 退出。
    """
    closes, vols = [], []
    c = 10.0
    last_spike = max(spike_days)
    for i in range(n):
        if i in spike_days:
            c *= 1.10
            closes.append(c)
            vols.append(8e6)
        elif i > last_spike + 60 and crash:
            c *= 0.97
            closes.append(c)
            vols.append(1e6)
        elif i > last_spike:
            c *= 1.006
            closes.append(c)
            vols.append(1e6)
        else:
            closes.append(c)
            vols.append(1e6)
    dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({"code": ["000001"] * n, "date": dates,
                         "open": closes, "high": [x * 1.02 for x in closes],
                         "low": [x * 0.98 for x in closes], "close": closes,
                         "volume": vols,
                         "prev_close": [pd.NA] + closes[:-1],
                         "b3": [False] * n, "wave2": [False] * n,
                         "signal": [False] * n})


class TestSimStock(unittest.TestCase):
    def test_trigger_requires_signal_day(self):
        # 3 个脉冲间隔 11 日 → 第 3 个脉冲（信号日）触发；此前不触发
        df = trend_panel([30, 41, 52])
        trades = cbt.sim_stock(df, "AI硬件", True, "ma60", 3)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_date"], df["date"].iloc[52])
        self.assertGreater(trades[0]["peak_gain"], 0.30)

    def test_min_t0_gate(self):
        # 仅 2 个脉冲 → min_t0=3 不触发；min_t0=2 触发
        df = trend_panel([30, 41])
        self.assertEqual(cbt.sim_stock(df, "AI硬件", True, "ma60", 3), [])
        trades = cbt.sim_stock(df, "AI硬件", True, "ma60", 2)
        self.assertEqual(len(trades), 1)

    def test_hot_gate(self):
        df = trend_panel([30, 41, 52])
        self.assertEqual(cbt.sim_stock(df, "其他", False, "ma60", 3), [])

    def test_ma60_exit(self):
        # 脉冲后冲高再跳水 → 跌破 MA60 退出；收益为正且峰值 > 收益
        df = trend_panel([30, 41, 52], crash=True)
        trades = cbt.sim_stock(df, "AI硬件", True, "ma60", 3)
        self.assertEqual(len(trades), 1)
        self.assertGreater(trades[0]["ret"], 0)
        self.assertGreater(trades[0]["peak_gain"], trades[0]["ret"])
        self.assertFalse(trades[0]["open"])


if __name__ == "__main__":
    unittest.main()
