"""信号引擎测试：tail_features / row_status / in_universe。"""
import unittest

import numpy as np
import pandas as pd

from mainrise.signals import in_universe, row_status, tail_features


def make_panel(closes, code="601899", volumes=None):
    n = len(closes)
    vols = volumes or [1_000_000] * n
    return pd.DataFrame({
        "code": [code] * n,
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": vols,
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

    def test_limit_up_requires_volume(self):
        closes = list(range(10, 55))
        closes[-1] = closes[-2] * 1.10          # 涨停
        # 缩量涨停（量比<1.0）不计信号
        t = tail_features(make_panel(closes, volumes=[1_000_000] * 44 + [50_000]))
        self.assertFalse(bool(t.iloc[-1]["signal"]))
        # 放量涨停（量比>=1.0）计信号
        t2 = tail_features(make_panel(closes, volumes=[1_000_000] * 44 + [2_000_000]))
        self.assertTrue(bool(t2.iloc[-1]["signal"]))

    def test_b3_flag(self):
        """B3：均线粘合 + 放量阳线站上三均线 + 低位。"""
        n = 100
        closes = [10.0 + (i % 5) * 0.01 for i in range(n)]   # 横盘 → 均线粘合
        closes[-1] = 10.30                                   # 尾日放量阳线上穿
        opens = [10.0] * (n - 1) + [10.10]
        vols = [1_000_000] * (n - 1) + [2_200_000]
        panel = make_panel(closes, volumes=vols)
        panel["open"] = opens
        t = tail_features(panel)
        last = t.iloc[-1]
        self.assertTrue(bool(last["b3"]))
        self.assertFalse(bool(last["wave2"]))

    def test_wave2_after_b3_pullback(self):
        """二波：B3 后回调、均线再次粘合、缩量 → 再放量阳线启动。"""
        n = 140
        closes = [10.0 + (i % 5) * 0.01 for i in range(n)]
        b3d = n - 30
        closes[b3d] = 10.30                    # B3 日
        # B3 后先冲一浪高点（10.55）再回调约 7%（到 9.80），缩量横盘后二波启动
        closes[b3d + 1] = 10.55
        for k in range(2, 10):
            closes[b3d + k] = 10.55 * (1 - 0.01 * (k - 1))
        for k in range(10, n - b3d - 1):
            closes[b3d + k] = 9.80
        closes[-1] = 10.05                     # 触发日：放量阳线站上均线
        opens = [10.0] * (n - 1) + [9.85]
        vols = [1_000_000] * n
        vols[b3d] = 2_200_000
        for k in range(1, n - b3d - 1):
            vols[b3d + k] = max(500_000, int(1_000_000 * (1 - 0.05 * k)))
        vols[-1] = 1_600_000
        panel = make_panel(closes, volumes=vols)
        panel["open"] = opens
        t = tail_features(panel)
        self.assertTrue(bool(t.iloc[-1]["wave2"]))


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

    def test_pullback_quality_filters(self):
        # 深回踩（日跌幅 -4%）不算企稳
        label, _ = row_status(self._row(close=9.6, low=9.1, ma5=9.8,
                                        vol_ratio=0.6, chg=-4.0), False)
        self.assertNotEqual(label, "回踩低吸")
        # 收盘跌破 MA20 不算企稳
        label2, _ = row_status(self._row(close=8.95, low=8.9, ma5=9.8,
                                         ma20=9.0, vol_ratio=0.6, chg=-1.0),
                               False)
        self.assertNotEqual(label2, "回踩低吸")
        # 浅回踩 + 缩量 -> 正常买点2
        label3, hint = row_status(self._row(close=9.6, low=9.1, ma5=9.8,
                                            vol_ratio=0.6, chg=-1.5), False)
        self.assertEqual(label3, "回踩低吸")
        self.assertIn("回踩-1.5%", hint)

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

    def test_max_10d_default_relaxed(self):
        # 默认阈值 150%：120% 不再提示"涨幅过大"
        label, hint = row_status(self._row(signal=True, chg10=120.0), False)
        self.assertNotIn("涨幅过大", hint)
        label, hint = row_status(self._row(signal=True, chg10=160.0), False)
        self.assertIn("涨幅过大", hint)


if __name__ == "__main__":
    unittest.main()
