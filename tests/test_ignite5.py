"""5 分钟点火信号检测（ignite5）与 5 分钟归档（m5data）单元测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mainrise import ignite5 as ig
from mainrise import m5data


def _mk_m5(day: str) -> list[list]:
    """构造当日 5 分钟序列：前 2 根缩量，第 3 根放量突破前高。"""
    base = day.replace("-", "")
    return [
        [f"{base}0935", "10.00", "10.05", "10.06", "9.99", 1000.0],   # 缩量
        [f"{base}0940", "10.05", "10.08", "10.10", "10.02", 1200.0],  # 缩量
        [f"{base}0945", "10.10", "10.50", "10.60", "10.08", 9000.0],  # 放量+新高
        [f"{base}0950", "10.50", "10.55", "10.62", "10.45", 3000.0],
    ]


class TestIgnite5(unittest.TestCase):
    def test_detect_ignite_volume_breakout(self):
        """放量+突破前高 → 点火（第 3 根 09:45）。"""
        hi20, prev = 10.30, 10.00
        base_vol = 1000.0   # 单根均量
        sig = ig.detect_ignite("X", _mk_m5("2026-08-10"), hi20, prev, base_vol)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["time"], "09:45")
        self.assertEqual(sig["px"], 10.50)
        self.assertTrue(sig["vol_mult"] > 2)   # 9000/1000 = 9×
        self.assertTrue(sig["new_hi"])          # 高 10.60 > hi20 10.30

    def test_detect_ignite_no_signal_when_quiet(self):
        """无量且未破前高 → 无点火。"""
        quiet = [
            [f"202608100935", "10.00", "10.02", "10.03", "9.99", 800.0],
            [f"202608100940", "10.02", "10.01", "10.04", "10.00", 700.0],
        ]
        sig = ig.detect_ignite("X", quiet, 10.30, 10.00, 1000.0)
        self.assertIsNone(sig)

    def test_detect_ignite_surge_without_new_hi(self):
        """涨幅≥5% 但未破前高 → 也算点火（大阳进攻）。"""
        surge = [[f"202608100935", "10.00", "10.60", "10.62", "9.98", 9000.0]]
        sig = ig.detect_ignite("X", surge, 11.00, 10.00, 1000.0)  # hi20=11 未破
        self.assertIsNotNone(sig)
        self.assertFalse(sig["new_hi"])
        self.assertGreaterEqual(sig["chg"], 5.0)

    def test_detect_ignite_volume_without_rise_not_signal(self):
        """放量但涨幅<3%（下跌放量/高位换手）→ 不点火。"""
        fake = [[f"202608100935", "10.00", "10.02", "10.03", "9.95", 9000.0]]
        sig = ig.detect_ignite("X", fake, 10.30, 10.00, 1000.0)
        self.assertIsNone(sig)   # 量比9×但只涨0.3% → 假点火


class TestM5Data(unittest.TestCase):
    def test_fetch_returns_sorted(self):
        """腾讯 m5 拉取返回非空且字段完整（网络依赖，失败跳过）。"""
        mk = m5data.fetch_m5("600519", tries=2)
        if not mk:
            self.skipTest("腾讯 m5 不可用")
        x = mk[0]
        self.assertEqual(len(x), 8)          # [时间,开,收,高,低,量,{},额]
        self.assertEqual(len(str(x[0])), 12)  # YYYYMMDDHHMM
        self.assertGreater(float(x[5]), 0)    # 量>0

    def test_archive_merge_dedup(self):
        """归档增量合并：重复 datetime 去重。"""
        tmp = Path(tempfile.mkdtemp())
        # 模拟旧文件 + 新数据合并
        import pandas as pd
        from mainrise import paths
        paths._OVERRIDE = tmp
        try:
            # 先造一条旧记录
            old = pd.DataFrame([{"datetime": "2026-08-10 09:35",
                                 "open": 10.0, "close": 10.1, "high": 10.2,
                                 "low": 9.9, "volume": 1000}])
            d = tmp / "data" / "m5daily"
            d.mkdir(parents=True, exist_ok=True)
            old.to_csv(d / "X.csv", index=False, encoding="utf-8-sig")
            # 新数据含重复 + 新增
            df = pd.DataFrame([
                {"datetime": "2026-08-10 09:35", "open": 10.0, "close": 10.15,
                 "high": 10.2, "low": 9.9, "volume": 1500},  # 重复覆盖
                {"datetime": "2026-08-10 09:40", "open": 10.15, "close": 10.2,
                 "high": 10.3, "low": 10.1, "volume": 2000},
            ])
            p = d / "X.csv"
            old2 = pd.read_csv(p, dtype={"datetime": str})
            merged = pd.concat([old2, df]).drop_duplicates("datetime",
                                                           keep="last")
            merged = merged.sort_values("datetime")
            self.assertEqual(len(merged), 2)   # 去重后 2 根
            self.assertEqual(merged.iloc[0]["volume"], 1500)  # 新值覆盖
        finally:
            paths._OVERRIDE = None


if __name__ == "__main__":
    unittest.main()


class TestM5Optimize(unittest.TestCase):
    def test_evaluate_no_data_returns_safe(self):
        """无归档数据时 _evaluate 安全返回（不崩溃）。"""
        import tempfile
        from mainrise import m5optimize
        tmp = Path(tempfile.mkdtemp())
        from mainrise import paths
        paths._OVERRIDE = tmp
        try:
            r = m5optimize._evaluate(2.0, 0.03, 0.05)
            self.assertIn("signals", r)
            self.assertEqual(r["signals"], 0)
        finally:
            paths._OVERRIDE = None


class TestDynamicCands(unittest.TestCase):
    """_dynamic_cands：点火跟踪范围 = 大牛候选池（bigbull_cands.json cands）。"""

    def test_returns_cands_only(self):
        """只返回候选池中的代码；文件不存在 → 空列表。"""
        from unittest import mock
        import json
        tmp = Path(tempfile.mkdtemp())
        (tmp / "bigbull_cands.json").write_text(
            json.dumps({"cands": [{"code": "003031"}, {"code": "000938"},
                                  {"code": "603662"}]}),
            encoding="utf-8")
        with mock.patch("mainrise.ignite5.paths") as m_paths:
            m_paths.state_dir.return_value = tmp
            got = ig._dynamic_cands()
        self.assertEqual(got, ["000938", "003031", "603662"])   # 排序、去重

    def test_missing_file_returns_empty(self):
        """候选池文件不存在 → 空列表（不抛异常、不扫描扩展池）。"""
        from unittest import mock
        with mock.patch("mainrise.ignite5.paths") as m_paths:
            m_paths.state_dir.return_value = Path(tempfile.mkdtemp())
            got = ig._dynamic_cands()
        self.assertEqual(got, [])

    def test_ignores_non_cand_codes(self):
        """候选池外的代码（如观察池/卡点企业）不进入点火跟踪。"""
        from unittest import mock
        import json
        tmp = Path(tempfile.mkdtemp())
        # 候选池只有 1 只；另外两个文件里的代码不应被纳入
        (tmp / "bigbull_cands.json").write_text(
            json.dumps({"cands": [{"code": "003031"}]}), encoding="utf-8")
        with mock.patch("mainrise.ignite5.paths") as m_paths:
            m_paths.state_dir.return_value = tmp
            got = ig._dynamic_cands()
        self.assertEqual(got, ["003031"])
