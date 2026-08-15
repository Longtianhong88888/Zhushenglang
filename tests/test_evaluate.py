"""财务评估测试：东财指标映射与公告日期无前视。"""
import unittest
from unittest import mock

from mainrise import evaluate


class TestEvaluate(unittest.TestCase):
    def _finance_rows(self):
        return [
            {"SECURITY_CODE": "600519",
             "UPDATE_DATE": "2026-08-20 00:00:00",
             "REPORTDATE": "2026-06-30 00:00:00", "YSTZ": 15.0,
             "SJLTZ": 20.0, "XSMLL": 90.0, "WEIGHTAVG_ROE": 12.0},
            {"SECURITY_CODE": "600519",
             "UPDATE_DATE": "2026-04-25 00:00:00",
             "REPORTDATE": "2026-03-31 00:00:00", "YSTZ": 6.3,
             "SJLTZ": 1.5, "XSMLL": 89.8, "WEIGHTAVG_ROE": 10.6},
            {"SECURITY_CODE": "600519",
             "UPDATE_DATE": "2025-10-25 00:00:00",
             "REPORTDATE": "2025-09-30 00:00:00", "YSTZ": 8.0,
             "SJLTZ": 10.0, "XSMLL": 91.0, "WEIGHTAVG_ROE": 22.0},
        ]

    def _balance_rows(self):
        return [
            {"SECURITY_CODE": "600519",
             "REPORT_DATE": "2026-03-31 00:00:00",
             "TOTAL_ASSETS": 1000.0, "TOTAL_LIABILITIES": 300.0},
        ]

    def test_fetch_indicators_no_lookahead(self):
        def fake_em(report_name, code, sort_col, columns, page_size=12):
            if report_name == "RPT_LICO_FN_CPD":
                return self._finance_rows()
            return self._balance_rows()

        with mock.patch.object(evaluate, "_em_finance", side_effect=fake_em):
            f = evaluate.fetch_indicators("600519", "2026-05-01")
            self.assertEqual(f["report"], "2026-03-31")   # 8/20 中报未披露，不能用
            self.assertEqual(
                f["growth"]["calculate_operating_income_yoy_growth_ratio"], 6.3)
            self.assertEqual(
                f["profitability"]["index_weighted_avg_roe"], 10.6)
            self.assertEqual(f["solvency"]["assets_debt_ratio"], 30.0)
            # 信号日早于任何披露 -> 无可用数据
            self.assertIsNone(
                evaluate.fetch_indicators("600519", "2025-01-01"))

    def test_available_report(self):
        self.assertEqual(evaluate.available_report("2026-11-01"), "2026-3")
        self.assertEqual(evaluate.available_report("2026-06-01"), "2026-1")
        self.assertEqual(evaluate.available_report("2026-03-01"), "2025-4")


if __name__ == "__main__":
    unittest.main()
