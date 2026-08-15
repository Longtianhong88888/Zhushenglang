"""动态热主题（themeindex，同花顺全A行业指数）单元测试：Key 读取 / 趋势判定 / 代码映射。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from mainrise import themeindex as ti


def fake_idx() -> pd.DataFrame:
    """两主题：半导体上行、有色横盘（40 日）。"""
    dates = pd.bdate_range("2026-01-01", periods=40).strftime("%Y-%m-%d")
    n = len(dates)
    return pd.DataFrame({
        "date": dates,
        "半导体": np.linspace(1.0, 1.5, n),     # 持续上行
        "有色": np.full(n, 1.0),                # 横盘
    }).set_index("date")


class TestThemeindex(unittest.TestCase):
    def test_get_key_from_settings(self):
        import mainrise.paths as paths_mod
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "settings.json").write_text(
                '{"api_key": "THS_KEY_123"}', encoding="utf-8")
            old = paths_mod.home
            try:
                paths_mod.home = lambda: Path(td)
                self.assertEqual(ti.get_key(), "THS_KEY_123")
            finally:
                paths_mod.home = old

    def test_hot_themes_series_trend(self):
        # 半导体指数上行 → 热；有色横盘 → 不热（无前视：只用 ≤ 当日数据）
        with mock.patch("mainrise.themeindex.get_key",
                        return_value="K"), \
                mock.patch("mainrise.themeindex.theme_indices",
                           return_value=(fake_idx(), {})):
            hs, missing = ti.hot_themes_series(ma_short=10, ma_long=20)
        self.assertEqual(missing, {})
        last = max(hs)
        self.assertIn("半导体", hs[last])
        self.assertNotIn("有色", hs[last])

    def test_hot_codes_by_date(self):
        theme_map = {"600001": "半导体", "600002": "半导体", "600003": "有色"}
        hbd = ti.hot_codes_by_date({"2026-02-20": ["半导体"]}, theme_map)
        self.assertEqual(hbd["2026-02-20"], {"600001", "600002"})


if __name__ == "__main__":
    unittest.main()
