"""信号引擎测试：tail_features / row_status / in_universe。"""
import unittest

import numpy as np
import pandas as pd

from mainrise.signals import in_universe, row_status, tail_features


def make_panel(closes, code="601899"):
    n = len(closes)
    return pd.DataFrame({
        "code": [code] * n,
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
        "prev_close": [closes[0]] + list(closes[:-1]),
    })


class TestInUniverse(unittest.TestCase):
    def test_prefixes(self):
        self.assertTrue(in_universe("600519"))
        self.assertTrue(in_universe("000001"))
        self.assertTrue(in_universe("300750"))
        self.assertTrue(in_universe("002594"))
        self.assertFalse(in_universe("920001"))
        self.assertFalse(in_universe("832000"))


class TestTailFeatures(unittest.TestCase):
    def test_bull_alignment(self):
        closes = list(range(10, 60))  # 单边上涨 -> 多头排列
        t = tail_features(make_panel(closes))
        self.assertIsNotNone(t)
        last = t.iloc[-1]
        self.assertTrue(bool(last["bull"]))
        self.assertFalse(bool(last["signal"]))  # 涨幅不足 5% 不触发信号

    def test_signal_on_surge(self):
        closes = list(range(10, 55))
        closes[-1] = closes[-2] * 1.08  # 尾日放量上攻
        t = tail_features(make_panel(closes))
        last = t.iloc[-1]
        self.assertTrue(bool(last["chg"] >= 5))
        self.assertTrue(bool(last["bull"]))


class TestRowStatus(unittest.TestCase):
    def _row(self, **kw):
        base = {"close": 10.0, "low": 9.9, "ma5": 9.8, "ma10": 9.5, "ma20": 9.0,
                "vol_ratio": 0.8, "chg10": 10.0, "signal": False, "prev_close": 9.9,
                "bull": True}
        base.update(kw)
        return pd.Series(base)

    def test_bull_hold(self):
        label, hint = row_status(self._row(), False)
        self.assertEqual(label, "多头持有")
        self.assertIn("MA10", hint)

    def test_pullback_buy(self):
        label, hint = row_status(self._row(close=9.6, low=9.1, ma5=9.8, vol_ratio=0.6), False)
        self.assertEqual(label, "回踩低吸")
        self.assertIn("买点2", hint)

    def test_broken_ma20(self):
        label, _ = row_status(self._row(close=8.5, ma20=9.0), False)
        self.assertEqual(label, "破位")

    def test_t0_signal(self):
        label, _ = row_status(self._row(signal=True), False)
        self.assertEqual(label, "T0新信号")

    def test_t1_confirm(self):
        label, _ = row_status(self._row(), True)
        self.assertEqual(label, "T1确认买点")

    def test_extended_10d_warning(self):
        label, hint = row_status(self._row(signal=True, chg10=120.0), False, max_10d=80)
        self.assertIn("涨幅过大", hint)


if __name__ == "__main__":
    unittest.main()
