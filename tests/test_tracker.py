"""纸面持仓平仓逻辑测试（重点：高点回落按收盘价而非盘中低点）。"""
import unittest

import numpy as np
import pandas as pd

from mainrise.tracker import update_positions


def make_panel(tail_closes, code="601899", tail_low=None):
    """前 30 天 10->19 铺垫 + 自定义尾部，共 40 天。"""
    head = np.linspace(10, 19, 30).tolist()
    closes = head + list(tail_closes)
    n = len(closes)
    df = pd.DataFrame({
        "code": [code] * n,
        "date": pd.date_range("2026-05-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.97 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
        "prev_close": [closes[0]] + list(closes[:-1]),
    })
    if tail_low is not None:
        df.loc[df.index[-1], "low"] = tail_low
    return df


def active_pos(code, buy_date, buy_price):
    return pd.DataFrame([{
        "code": code, "name": "测试", "signal_date": np.nan, "confirm_date": np.nan,
        "buy_kind": 2, "buy_date": buy_date, "buy_price": buy_price,
        "peak": buy_price, "peak_date": buy_date, "status": "active",
        "close_date": None, "close_price": None, "reason": ""}])


class TestUpdatePositions(unittest.TestCase):
    def _run(self, tail_closes, buy_idx=0, buy_price=None, tail_low=None):
        code = "601899"
        panel = make_panel(tail_closes, code, tail_low)
        dates = panel["date"].tolist()
        pos = active_pos(code, dates[buy_idx], buy_price or panel.iloc[buy_idx]["close"])
        out = update_positions(pos, {code: panel}, pd.DataFrame(), dates[-1], {})
        return out.iloc[0]

    def test_close_pullback_8pct_closes(self):
        # 峰值 ~20.2，最后一天收盘 18（回落 >8%）-> 高点回落平仓
        tail = [19.5, 20.0, 19.9, 19.6, 19.2, 18.8, 18.4, 18.1, 18.0]
        row = self._run(tail)
        self.assertEqual(row["status"], "closed")
        self.assertIn("高点回落8%", row["reason"])

    def test_low_dip_but_close_holds_no_exit(self):
        # 最后一天盘中低点 18（会触发旧"低点回落"），但收盘 19.8 仅回落 -2%
        # 且未触发止损/MA10/时间止损 -> 不应平仓（本次修复点）
        tail = [19.8] * 10
        row = self._run(tail, buy_idx=35, buy_price=10.0, tail_low=18.0)
        self.assertEqual(row["status"], "active")

    def test_stop_loss_5pct(self):
        # 尾日收盘 8.5 <= 买入 10 * 0.95 -> 止损
        tail = [19.5, 19.0, 18.0, 17.0, 16.0, 15.0, 14.0, 13.0, 12.0, 11.0, 9.0, 8.5]
        row = self._run(tail, buy_price=10.0)
        self.assertEqual(row["status"], "closed")
        self.assertIn("止损", row["reason"])

    def test_break_ma10(self):
        # 高位缓跌跌破 MA10，但未触发止损/回落 -> 跌破MA10 平仓
        tail = [19.5, 19.8, 19.6, 19.7, 19.5, 19.4, 20.0, 19.8, 19.5, 19.2]
        row = self._run(tail, buy_idx=36, buy_price=15.0)
        self.assertEqual(row["status"], "closed")
        self.assertIn("跌破MA10", row["reason"])


if __name__ == "__main__":
    unittest.main()
