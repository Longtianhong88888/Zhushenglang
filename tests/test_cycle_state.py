"""市场周期状态卡（cycle_state）单元测试：阶段判定 / 主线确认 / 渲染。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from mainrise import cycle_state as cs


def fake_series(n: int = 400, drift: float = 0.002) -> pd.Series:
    """确定性趋势 + 正弦波动（60 日涨幅有分布，分位判定稳定）。"""
    dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
    k = np.arange(n)
    close = 100 * (1 + drift) ** k * (1 + 0.08 * np.sin(k / 8))
    return pd.Series(close, index=dates)


class TestCycleState(unittest.TestCase):
    def test_stage_classify(self):
        # 持续上涨 → 60日涨幅分位高 → 主升/高潮
        self.assertIn(cs._stage_of(fake_series(300, 0.003)), ("主升", "高潮"))
        # 持续下跌 → 60日涨幅分位低 → 退潮
        self.assertEqual(cs._stage_of(fake_series(300, -0.003)), "退潮")

    def test_theme_series_from_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "ths_kline"
            cache.mkdir()
            n = 200
            dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
            close = np.linspace(100, 200, n)
            (cache / "881121.TI.csv").write_text(
                pd.DataFrame({"date": dates, "close": close})
                .to_csv(index=False), encoding="utf-8")
            with mock.patch.object(cs.paths, "state_dir", lambda: Path(td)):
                idx = cs._theme_series()
            self.assertIn("半导体", idx.columns)
            self.assertGreater(len(idx), 100)

    def test_compute_and_render(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "ths_kline"
            cache.mkdir()
            cyc = Path(td) / "cycle_ths"
            cyc.mkdir()
            n = 400
            dates = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
            # 半导体持续上行（主升）、有色下行（退潮）
            for code, drift in (("881121.TI", 0.003), ("881168.TI", -0.002),
                                ("881129.TI", 0.002), ("881122.TI", 0.002),
                                ("881130.TI", 0.002)):
                r = np.random.default_rng(5).normal(drift, 0.008, n)
                close = 100 * np.cumprod(1 + r)
                (cache / f"{code}.csv").write_text(
                    pd.DataFrame({"date": dates, "close": close})
                    .to_csv(index=False), encoding="utf-8")
            (cyc / "000001.SH.csv").write_text(
                pd.DataFrame({"date": dates,
                              "close": 3000 * np.cumprod(
                                  1 + np.random.default_rng(7)
                                  .normal(0.0005, 0.006, n))})
                .to_csv(index=False), encoding="utf-8")
            with mock.patch.object(cs.paths, "state_dir", lambda: Path(td)), \
                 mock.patch.object(cs.paths, "web_dir", lambda: Path(td)):
                st = cs.compute()
            self.assertIn("mainline", st)
            self.assertIn("themes", st)
            self.assertIn(st["level"], ("L0", "L1", "L2", "L3"))
            # 渲染
            card = cs.render_card(st)
            self.assertIn("市场周期状态", card)
            self.assertIn("主线", card)
            page = cs.render_page(st)
            self.assertIn("不构成投资建议", page)

    def test_render_error_state(self):
        st = {"error": "主题指数缓存缺失"}
        self.assertIn("主题指数缓存缺失", cs.render_card(st))


if __name__ == "__main__":
    unittest.main()
