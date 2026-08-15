"""大周期追溯研究（cycle_check）单元测试：数据构造 / 等权净值 / 拼接 / 端到端。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from mainrise import cycle_check as cc

CN = timezone(timedelta(hours=8))


def fake_bars(n: int = 120, base: float = 10.0, step: float = 0.05,
              spike_days: tuple = ()) -> pd.DataFrame:
    """稳定上行 + 可选放量涨停日（触发 T0 信号）。"""
    dates = pd.bdate_range("2019-01-01", periods=n).strftime("%Y-%m-%d")
    close = base * (1 + step) ** np.arange(n)
    volume = 1e6 + np.arange(n) * 1e3
    for d in spike_days:
        if 1 <= d < n - 1:
            close[d] = close[d - 1] * 1.10
            close[d + 1:] = close[d] * (1 + step) ** np.arange(1, n - d)
            volume[d] = 5e7
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.99, "high": close * 1.02, "low": close * 0.98,
        "close": close, "volume": volume,
    })


class TestCycleCheck(unittest.TestCase):
    def test_to_panel_columns(self):
        df = fake_bars(60)
        p = cc._to_panel(df, "600519")
        for col in ["date", "code", "open", "high", "low", "close",
                    "limit_price", "turnover", "pct_chg", "prev_close",
                    "is_st", "is_paused", "volume"]:
            self.assertIn(col, p.columns)
        # prev_close = close.shift(1)；主板 limit = prev*1.1
        self.assertAlmostEqual(p["prev_close"].iloc[5], df["close"].iloc[4])
        self.assertAlmostEqual(p["limit_price"].iloc[5], df["close"].iloc[4] * 1.1)
        # 创业板 1.2
        p2 = cc._to_panel(df, "300750")
        self.assertAlmostEqual(p2["limit_price"].iloc[5], df["close"].iloc[4] * 1.2)

    def test_mkt_ret20(self):
        idx = fake_bars(60, base=3000, step=0.001)
        r = cc._mkt_ret20(idx)
        self.assertTrue(r)
        keys = sorted(r)
        self.assertGreaterEqual(len(keys), 40)
        # 上行市场 ret20 应为正
        self.assertGreater(r[keys[-1]], 0)

    def test_equal_weight_nav(self):
        nb = fake_bars(100, 20, 0.02).iloc[40:].reset_index(drop=True)  # 上市晚
        closes = {
            "600519": fake_bars(100, 10, 0.01).set_index("date")["close"],
            "300750": nb.set_index("date")["close"],
        }
        dates = sorted({d for s in closes.values() for d in s.index})
        nav = cc._equal_weight_nav(closes, dates)
        self.assertEqual(len(nav), 100)
        # 首日 1.0，全期上涨
        self.assertAlmostEqual(nav["nav"].iloc[0], 1.0, places=6)
        self.assertGreater(nav["nav"].iloc[-1], 1.0)
        # 上市晚的票参与后，均值连续（新股从 1.0 起拉低均值，跳变有限）
        self.assertTrue((nav["nav"].diff().dropna() > -0.6).all())

    def test_fetch_one_concat(self):
        def fake_items(s, e, n):
            dates = pd.date_range(
                datetime.fromtimestamp(s / 1000, tz=CN),
                datetime.fromtimestamp(e / 1000, tz=CN),
                periods=n).strftime("%Y-%m-%d")
            return [{"date_ms": int(datetime.strptime(d, "%Y-%m-%d")
                                    .replace(tzinfo=CN).timestamp() * 1000),
                     "open_price": 1.0, "high_price": 1.0, "low_price": 1.0,
                     "close_price": 1.0, "volume": 1.0} for d in dates]

        def fake_api(path, params, key, tries=3):
            n = 60
            return {"code": 0,
                    "data": {"item": fake_items(params["start"], params["end"], n)}}

        with mock.patch.object(cc.ti, "_api_get", side_effect=fake_api):
            df = cc._fetch_one("600519.SH", "k")
        self.assertIsNotNone(df)
        # 两段拼接后按日期去重升序
        self.assertEqual(df["date"].is_monotonic_increasing, True)
        self.assertGreater(len(df), 100)

    def test_run_end_to_end(self):
        """小池端到端：报告生成、模型/等权对照无异常。"""
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            with mock.patch.object(cc, "CACHE_DIR", cache), \
                 mock.patch.object(cc.paths, "report_dir",
                                   lambda: Path(td)), \
                 mock.patch.object(cc.paths, "state_dir", lambda: cache), \
                 mock.patch.object(cc, "POOLS", {
                     "白酒": {"600519": "贵州茅台", "000858": "五粮液"}}):
                def fake_load(code, key, refresh=False):
                    spikes = (40, 80, 120) if code != "000001.SH" else ()
                    return fake_bars(160, 10, 0.01, spike_days=spikes)
                with mock.patch.object(cc, "_load_kline",
                                       side_effect=fake_load):
                    # 上证指数
                    with mock.patch.object(cc, "SH_IDX", "000001.SH"):
                        out = cc.run()
            self.assertTrue(Path(out).exists())
            text = Path(out).read_text(encoding="utf-8")
            self.assertIn("白酒池", text)
            self.assertIn("模型全期", text)
            self.assertIn("对照", text)
            self.assertIn("分段", text)


if __name__ == "__main__":
    unittest.main()
