"""每日跟踪模块测试：两级模型 B3/二波 扫描与状态。"""
import unittest

import pandas as pd

from mainrise import tracker
from mainrise.signals import row_status, scan_two_stage, tail_features


def _panel(n=100):
    closes = [10.0 + (i % 5) * 0.01 for i in range(n)]
    closes[-1] = 10.30                       # 尾日：放量阳线上穿
    opens = [10.0] * (n - 1) + [10.10]
    vols = [1_000_000] * (n - 1) + [2_200_000]
    return pd.DataFrame({
        "code": ["601899"] * n,
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": opens,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": vols,
        "prev_close": [closes[0]] + list(closes[:-1]),
    })


class TestTwoStage(unittest.TestCase):
    def test_scan_two_stage_finds_b3(self):
        panel = _panel()
        date = panel["date"].iloc[-1]
        found = scan_two_stage(panel, date, {"601899": "紫金矿业"})
        self.assertFalse(found.empty)
        row = found.iloc[0]
        self.assertEqual(row["kind"], "B3")
        self.assertEqual(row["name"], "紫金矿业")

    def test_row_status_two_levels(self):
        t = tail_features(_panel())
        last = t.iloc[-1]
        label, hint = row_status(last, False)
        self.assertEqual(label, "B3打底仓")
        self.assertIn("打底仓", hint)


class TestPositionUpdate(unittest.TestCase):
    def test_b3_entry_waits_for_next_day_open(self):
        """M3 修复：B3 信号日只登记 pending（不按收盘价当天买入），
        次日数据到位后用开盘价建仓。"""
        # 信号日（末行）出现 B3；次日数据（加一行）才到位
        panel = _panel()
        sig_date = panel["date"].iloc[-1]
        # 次日：正常波动 K 线（开盘 10.50，信号日收盘 10.30 → 隔夜跳空）
        nxt = pd.DataFrame({
            "code": ["601899"], "date": ["2026-05-20"],
            "open": [10.50], "high": [10.80], "low": [10.40],
            "close": [10.70], "volume": [1_500_000], "prev_close": [10.30]})
        watch = pd.DataFrame({"code": ["601899"], "name": ["紫金矿业"],
                              "composite": [80.0], "track": ["存储芯片"]})
        # 用空持仓开始
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        from mainrise import paths
        paths._OVERRIDE = tmp
        try:
            pos = pd.DataFrame(columns=tracker.load_positions().columns)
            # 信号日：仅信号日数据（B3 在末行），B3 触发 → 只登记 pending
            pb1 = {c: g for c, g in panel.groupby("code", sort=False)}
            p1 = tracker.update_positions(pos, pb1, watch, sig_date,
                                          {"601899": "紫金矿业"})
            self.assertEqual(len(p1), 1)
            self.assertEqual(p1.iloc[0]["status"], "pending")
            self.assertTrue(pd.isna(p1.iloc[0]["buy_price"]))
            self.assertTrue(pd.isna(p1.iloc[0]["buy_date"]))
            # 次日：数据到位，pending → 用开盘价 10.50 建仓（非信号日收盘 10.30）
            full = pd.concat([panel, nxt], ignore_index=True)
            pb2 = {c: g for c, g in full.groupby("code", sort=False)}
            p2 = tracker.update_positions(p1, pb2, watch, "2026-05-20",
                                          {"601899": "紫金矿业"})
            self.assertEqual(p2.iloc[0]["status"], "active")
            self.assertEqual(float(p2.iloc[0]["buy_price"]), 10.50)
            self.assertEqual(p2.iloc[0]["buy_date"], "2026-05-20")
        finally:
            paths._OVERRIDE = None


if __name__ == "__main__":
    unittest.main()
