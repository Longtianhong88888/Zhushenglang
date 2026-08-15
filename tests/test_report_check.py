"""市场研报信号研究（report_check）单元测试：规范化 / 分月拉取 / 指标汇总 / 端到端。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from mainrise import report_check as rc


def fake_item(code="300750", date="2026-07-01 00:00:00.000", rating="买入",
              change="上调", is_new=0, ind="半导体", org="中金公司",
              title="深度报告") -> dict:
    return {"stockCode": code, "publishDate": date, "emRatingName": rating,
            "ratingChange": change, "indvIsNew": is_new, "industryName": ind,
            "orgSName": org, "title": title}


class TestReportCheck(unittest.TestCase):
    def test_norm(self):
        n = rc._norm(fake_item())
        self.assertEqual(n["code"], "300750")
        self.assertEqual(n["date"], "2026-07-01")
        self.assertEqual(n["rating"], "买入")
        self.assertEqual(n["rating_change"], "上调")
        self.assertEqual(n["is_new"], 0)
        self.assertEqual(n["industry"], "半导体")

    def test_norm_empty_code(self):
        self.assertEqual(rc._norm({"stockCode": ""}), {})

    def test_fetch_month_pagination(self):
        """两页数据：翻页拼接去重。"""
        pages = iter([
            {"data": [fake_item(code=f"30075{i}", date=f"2026-07-0{i+1} "
                         "00:00:00.000") for i in range(2)],
             "TotalPage": 2},
            {"data": [fake_item(code="300759", date="2026-07-03 00:00:00.000")],
             "TotalPage": 2},
        ])

        def fake_get(url, params, headers, timeout):
            return mock.Mock(json=lambda: next(pages))

        with mock.patch.object(rc.requests, "get", side_effect=fake_get):
            items = rc._fetch_month("2026-07")
        self.assertEqual(len(items), 3)

    def test_run_end_to_end(self):
        """小样本端到端：报告生成。"""
        fake_rep = pd.DataFrame([rc._norm(fake_item(
            code=code, date=f"2024-0{1+i%9}-0{1+i%9} 00:00:00.000",
            is_new=1 if i % 3 == 0 else 0))
            for i, code in enumerate(
                ["300750", "002594", "601012", "300274", "002460",
                 "600519", "000858", "600809", "000568"])])
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(rc, "fetch_all",
                                   return_value=fake_rep), \
                 mock.patch.object(rc.paths, "report_dir",
                                   lambda: Path(td)), \
                 mock.patch.object(rc, "load_chokepoint_codes",
                                   lambda: {"300750", "002594", "601012",
                                            "300274", "002460"}), \
                 mock.patch.object(rc, "load_all_panels",
                                   return_value=pd.DataFrame({
                                       "code": ["300750", "002594", "600519"],
                                       "date": ["2024-01-01"] * 3,
                                       "pct_chg": [1.0, 0.5, 0.2],
                                       "close": [100.0, 50.0, 1000.0],
                                       "limit_price": [110.0, 55.0, 1100.0],
                                       "is_st": [0, 0, 0],
                                       "is_paused": [0, 0, 0],
                                       "volume": [1e6, 2e6, 3e6]})), \
                 mock.patch.object(rc, "in_universe", lambda code: True):
                out = rc.run(refresh=False)
            self.assertTrue(Path(out).exists())
            text = Path(out).read_text(encoding="utf-8")
            self.assertIn("研报信号", text)
            self.assertIn("卡点池研报密度", text)
            self.assertIn("结论", text)


if __name__ == "__main__":
    unittest.main()
