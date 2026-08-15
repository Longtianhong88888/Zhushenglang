"""每周绩效小结（weekly）单元测试：消息构建（买入/卖出/持仓/信号/净值）。"""
from __future__ import annotations

import unittest

import pandas as pd

from mainrise import weekly

WEEK = ["2026-07-01", "2026-07-02", "2026-07-03"]


def fake_trades() -> pd.DataFrame:
    return pd.DataFrame([
        {"代码": "603662", "名称": "柯力传感", "主题": "半导体",
         "买入日期": WEEK[0], "买入价": 80.0, "卖出日期": WEEK[2],
         "卖出价": 69.6, "收益率": -0.132, "峰值收益率": 0.0,
         "持仓天数": 3, "状态": "已平仓", "入场方式": "硬规则", "score": 2},
        {"代码": "002859", "名称": "洁美科技", "主题": "半导体",
         "买入日期": WEEK[0], "买入价": 46.33, "卖出日期": WEEK[2],
         "卖出价": 85.99, "收益率": 0.854, "峰值收益率": 1.46,
         "持仓天数": 20, "状态": "已平仓", "入场方式": "硬规则", "score": 3},
        {"代码": "000938", "名称": "紫光股份", "主题": "AI硬件",
         "买入日期": WEEK[1], "买入价": 30.0, "卖出日期": WEEK[2],
         "卖出价": 30.0, "收益率": 0.0, "峰值收益率": 0.0,
         "持仓天数": 5, "状态": "未平仓", "入场方式": "硬规则", "score": 2},
    ])


def fake_nav() -> pd.DataFrame:
    return pd.DataFrame({
        "date": ["2026-06-26", "2026-06-29", "2026-06-30",
                 "2026-07-01", "2026-07-02", "2026-07-03"],
        "nav": [1.00, 1.01, 1.02, 1.03, 1.02, 1.05]})


class TestWeekly(unittest.TestCase):
    def test_build_weekly_content(self):
        cands = [{"code": "603662", "date": "2026-07-02", "cnt": 3,
                  "score": 2, "px": 70.0, "ma20": 71.0},
                 {"code": "002859", "date": "2026-05-01", "cnt": 7,
                  "score": 2, "px": 85.0, "ma20": 80.0}]
        title, desp = weekly.build_weekly(fake_trades(), fake_nav(), cands)
        self.assertIn("买入3", title)             # 本周三笔买入（07-01×2 + 07-02×1）
        self.assertIn("卖出2", title)             # 本周两笔平仓（07-03，紫光未平仓）
        self.assertIn("持仓1", title)
        self.assertIn("信号1", title)             # 07-02 信号
        self.assertIn("本周净值 1.050", desp)
        self.assertIn("合计收益 +72.2%", desp)     # -13.2% + 85.4%
        self.assertIn("柯力传感", desp)
        self.assertIn("紫光股份", desp)
        self.assertIn("603662(2026-07-02,评分2)", desp)

    def test_build_weekly_empty(self):
        title, desp = weekly.build_weekly(
            pd.DataFrame(), pd.DataFrame(), [])
        self.assertIn("持仓0", title)
        self.assertIn("本周无确认买入", desp)
        self.assertIn("当前空仓", desp)
        self.assertIn("本周无硬规则信号", desp)


if __name__ == "__main__":
    unittest.main()
