"""市场风格检测研究（style_check）单元测试：指标计算 / 分组统计 / 端到端。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from mainrise import style_check as sc


def fake_full(n_days: int = 200, n_stocks: int = 40) -> pd.DataFrame:
    """合成全市场面板：一半卡点（科技，波动大）、一半其他。"""
    dates = pd.bdate_range("2024-01-01", periods=n_days).strftime("%Y-%m-%d")
    rows = []
    spike_days = {40, 80, 120}         # 放量涨停日（间隔<90交易日，90日窗口内凑3个）
    for i in range(n_stocks):
        tech = i < n_stocks // 2
        code = f"{300000 + i}"
        base = 10.0 + i
        drift = 0.004 if tech else 0.001
        for j, d in enumerate(dates):
            if j in spike_days:
                close = rows[-1]["close"] * 1.10 if rows else base * 1.10
                prev = close / 1.10
            else:
                close = base * (1 + drift) ** j * (1 + 0.02 * np.sin(j / 5))
                prev = close / (1 + drift)
            vol = 5e7 if j in spike_days else 1e6 + j * 1e3
            rows.append({
                "date": d, "code": code, "open": prev, "high": close * 1.01,
                "low": close * 0.99, "close": close,
                "limit_price": prev * 1.1, "pct_chg": (close / prev - 1) * 100,
                "prev_close": prev, "volume": vol,
                "is_st": 0, "is_paused": 0, "amount": 1e8 + j * 1e5,
            })
    return pd.DataFrame(rows)


class TestStyleCheck(unittest.TestCase):
    def test_quint_stats(self):
        rng = np.random.default_rng(7)
        fwd = rng.normal(0.01, 0.02, 50)
        grp = np.repeat(np.arange(5), 10)
        stats = sc._quint_stats(fwd, grp)
        self.assertEqual(len(stats), 5)
        for q in range(5):
            self.assertEqual(stats[q][1], 10)
            self.assertFalse(np.isnan(stats[q][2]))

    def test_run_end_to_end(self):
        """小样本端到端：报告生成、指标表、fwd20 无异常。"""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(sc.paths, "report_dir",
                                   lambda: Path(td)), \
                 mock.patch.object(sc, "load_all_panels",
                                   fake_full), \
                 mock.patch.object(sc, "in_universe",
                                   lambda code: True), \
                 mock.patch.object(sc, "load_chokepoint_codes",
                                   lambda: {f"{300000+i}" for i in range(20)}), \
                 mock.patch.object(sc.bigtrend, "load_theme",
                                   lambda: {f"{300000+i}": "AI硬件"
                                            for i in range(20)}):
                out = sc.run(with_market=True)
            self.assertTrue(Path(out).exists())
            text = Path(out).read_text(encoding="utf-8")
            self.assertIn("市场风格检测研究", text)
            self.assertIn("fwd20", text)
            self.assertIn("逐年", text)
            self.assertIn("结论", text)


if __name__ == "__main__":
    unittest.main()
