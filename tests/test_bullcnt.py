"""买入信号计数 → 大牛判定（bullcnt）单元测试。

覆盖：链长计数（间隔≤20 日连续）、90 日密度计数、大牛标签（150 日峰值≥60%）。
信号日由"平台整理 + 放量涨停脉冲"精确构造（每脉冲触发 1 个 T0/B3 信号）。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from mainrise import bullcnt as bc


def spike_panel(spike_idxs: list, n_after: int = 80) -> pd.DataFrame:
    """平台整理 + 放量涨停脉冲，spike_idxs 处触发信号。

    构造：先 30 天平台（10.0），每到一个脉冲日价格 ×1.10 且量 8e6，
    其余日平量 1e6 持平。脉冲后持续持平 n_after 天（供 150 日窗口）。
    """
    closes, vols = [], []
    c = 10.0
    for i in range(400):
        if i in spike_idxs:
            c *= 1.10
            closes.append(c)
            vols.append(8e6)
        else:
            closes.append(c)
            vols.append(1e6)
        if i >= max(spike_idxs) + n_after:
            break
    n = len(closes)
    dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    df = pd.DataFrame({"code": ["000001"] * n, "date": dates,
                       "open": closes, "high": [x * 1.02 for x in closes],
                       "low": [x * 0.98 for x in closes], "close": closes,
                       "volume": vols,
                       "prev_close": [np.nan] + closes[:-1],
                       "b3": [False] * n, "wave2": [False] * n,
                       "signal": [False] * n})
    return df


class TestCollect(unittest.TestCase):
    def test_chain_and_density(self):
        # 第 30/41/52 天脉冲（间隔 11 ≤20）→ 第 3 个信号链长 3、90 日计数 3
        df = spike_panel([30, 41, 52])
        D = bc.collect(df, pd.DataFrame())
        rows = D.sort_values("date")
        self.assertEqual(len(rows), 3)
        last = rows.iloc[-1]
        self.assertEqual(last["chain"], 3)          # 连续 3 个
        self.assertEqual(last["n90_all"], 3)        # 90 日内 3 个
        self.assertEqual(last["n90_t0"], 3)
        first = rows.iloc[0]
        self.assertEqual(first["chain"], 1)         # 首个信号链长 1
        self.assertEqual(first["n90_all"], 1)

    def test_chain_reset_after_gap(self):
        # 第 30 与 90 天脉冲（间隔 60 > 20）→ 链长重置为 1
        df = spike_panel([30, 90])
        D = bc.collect(df, pd.DataFrame())
        rows = D.sort_values("date")
        self.assertEqual(len(rows), 2)
        self.assertEqual(int(rows.iloc[0]["chain"]), 1)
        self.assertEqual(int(rows.iloc[1]["chain"]), 1)   # 重置
        self.assertEqual(int(rows.iloc[1]["n90_all"]), 2)  # 90 日内仍计 2

    def test_big_label(self):
        # 连续 8 个 +10% 脉冲 → 价格 10→21.4（+114%）→ 150 日峰值 ≥60% → big=1
        df = spike_panel([30, 41, 52, 63, 74, 85, 96, 107], n_after=60)
        D = bc.collect(df, pd.DataFrame())
        self.assertEqual(len(D), 8)
        self.assertEqual(int(D.iloc[0]["big"]), 1)


if __name__ == "__main__":
    unittest.main()
