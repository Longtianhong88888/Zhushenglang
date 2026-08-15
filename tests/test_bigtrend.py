"""趋势大牛研究（bigtrend）单元测试。

覆盖：主题映射、信号上下文收集（大牛标签/特征无前视）、
退出规则触发（MA 跌破/高点回落/组合）。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from mainrise import bigtrend as bt


class TestTheme(unittest.TestCase):
    def test_theme_keywords(self):
        self.assertEqual(bt.load_theme().get("603256"), "AI硬件")   # 玻纤电子布
        self.assertEqual(bt.load_theme().get("000657"), "有色")     # 钨
        self.assertEqual(bt.load_theme().get("603259"), "创新药")   # CXO


class TestCollectSignals(unittest.TestCase):
    def test_big_label_and_features(self):
        # 构造：横盘后放量突破（T0 信号），随后大涨 → big=1；特征字段齐全
        rng = np.random.default_rng(3)
        n = 130
        closes = list(np.cumsum(rng.uniform(0.002, 0.006, n)) + 10)
        closes[95] = closes[94] * 1.06                 # T0 大阳线（i+30<n）
        for k in range(96, n):                          # 后续 1.5%/日 走强
            closes[k] = closes[k - 1] * 1.015
        highs = [c * 1.02 for c in closes]
        lows = [c * 0.98 for c in closes]
        dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
        p = pd.DataFrame({"code": ["603256"] * n, "date": dates,
                          "open": [c * 0.99 for c in closes],
                          "high": highs, "low": lows, "close": closes,
                          "volume": [1e6] * n,
                          "prev_close": [np.nan] + closes[:-1]})
        p["volume"] = [1e6] * 95 + [8e6] + [1e6] * (n - 96)
        t = bt.tail_features(p, tail=n)
        self.assertTrue(bool(t["signal"].iloc[95]))
        mkt = pd.DataFrame({"date": dates, "mkt_zt": 100.0,
                            "mkt_ret20": 3.0})
        D = bt.collect_signals(p, mkt, {"603256": "AI硬件"})
        self.assertEqual(len(D), 1)
        r = D.iloc[0]
        for col in ("theme", "peak_gain", "big", "mid", "chg10", "vr",
                    "new_hi60", "from_low60", "mkt_ret20", "mkt_zt"):
            self.assertIn(col, r.index)
        self.assertEqual(r["theme"], "AI硬件")
        self.assertEqual(r["big"], 1)       # 后续冲到 +60% 以上
        self.assertEqual(r["mkt_zt"], 100.0)


class TestExitRules(unittest.TestCase):
    def test_ma_break_and_pullback(self):
        # 构造大牛走势：入场后一路涨，最后收盘跌破 MA20 → ma20 规则触发
        closes = [10 + i * 0.05 for i in range(40)]       # 稳步上涨
        closes += [closes[-1] * 0.94]                     # 大跌破位
        n = len(closes)
        p = pd.DataFrame({"code": ["000001"] * n, "date": list(
            pd.bdate_range("2026-01-01", periods=n).strftime("%Y-%m-%d")),
            "open": closes, "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes], "close": closes,
            "volume": [1e6] * n, "prev_close": [np.nan] + closes[:-1]})
        rows = pd.DataFrame([{"code": "000001", "date": p["date"].iloc[0],
                              "big": 1, "i": 0}])
        R = bt.exit_rules(rows, {"000001": p})[1]
        g = R[R["rule"] == "ma20"]
        self.assertEqual(len(g), 1)
        self.assertGreater(g.iloc[0]["ret"], 0.05)   # 吃到主涨段
        self.assertGreater(g.iloc[0]["peak"], g.iloc[0]["ret"])
        g10 = R[R["rule"] == "r10"]
        self.assertEqual(len(g10), 1)


if __name__ == "__main__":
    unittest.main()
