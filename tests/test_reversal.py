"""暴跌反转研究测试：回撤→放量涨停→次日不破 的信号检测。"""
import unittest

import numpy as np
import pandas as pd

from mainrise.report import load_chokepoint_codes
from mainrise.reversal import detect


def make_panel(closes, code="000636", volumes=None):
    n = len(closes)
    vols = volumes or [1_000_000] * n
    prev = [closes[0]] + list(closes[:-1])
    return pd.DataFrame({
        "code": [code] * n,
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": vols,
        "prev_close": prev,
        "limit_price": [p * 1.1 for p in prev],
    })


def crash_panel(tail3):
    """40 日涨到 20 + 10 日跌到 13 + 自定义 3 日尾部。"""
    closes = list(np.linspace(10, 20, 40)) + list(np.linspace(20, 13, 10))
    closes.extend(tail3)
    return closes


class TestReversal(unittest.TestCase):
    def test_chokepoint_universe(self):
        codes = load_chokepoint_codes()
        self.assertGreaterEqual(len(codes), 30)     # 卡点企业名单非空
        self.assertTrue(all(c.isdigit() and len(c) == 6 for c in codes))
        self.assertNotIn("600519", codes)           # 贵州茅台不在卡点名单

    def test_signal_on_confirm(self):
        closes = crash_panel([13 * 1.1, 14.5, 14.8])   # 涨停+确认+T+2
        vols = [1_000_000] * 50 + [2_000_000, 1_000_000, 1_000_000]
        df = make_panel(closes, volumes=vols)
        sig = detect(df)
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig.iloc[0]["S_date"], df["date"].iloc[-3])
        self.assertEqual(sig.iloc[0]["buy_date"], df["date"].iloc[-1])
        self.assertGreater(sig.iloc[0]["vr"], 1.0)
        self.assertLess(sig.iloc[0]["crash_depth"], -25)   # 涨停日仍在暴跌区（前日已 -35%）

    def test_no_signal_without_confirm(self):
        closes = crash_panel([13 * 1.1, 14.0, 14.8])   # 次日收盘<涨停收盘
        self.assertTrue(detect(make_panel(closes)).empty)

    def test_shrink_limit_up_excluded(self):
        closes = crash_panel([13 * 1.1, 14.5, 14.8])
        vols = [1_000_000] * 50 + [50_000, 1_000_000, 1_000_000]  # 缩量涨停
        self.assertTrue(detect(make_panel(closes, volumes=vols)).empty)


if __name__ == "__main__":
    unittest.main()
