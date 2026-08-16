"""Server酱推送（push）单元测试。

覆盖：Key 读取顺序、消息构建（买入/卖出/市场状态）、live 日期校验、
17:30 收盘确认（交割单 CSV 解析 / 收盘消息构建 / 非交易日跳过 / dry-run 全流程）。
不发起真实网络请求。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from mainrise import push


def fake_live(updated: str = "2026-08-14 14:50:00") -> dict:
    return {
        "updated_at": updated,
        "market_state": {"mkt_ret20": 4.8},
        "stocks": [
            {"code": "603662", "name": "柯力传感", "group": "大牛模型",
             "price": 55.0, "chg": 6.2, "vr": 2.1, "ma20": 52.0,
             "bb_approx": True, "bb_score": 2},
            {"code": "600549", "name": "厦门钨业", "group": "大牛模型",
             "price": 50.0, "chg": -2.0, "vr": 1.0, "ma20": 52.0,
             "bb_hold": True, "bb_approx": False,
             "sell": "⚠ 跌破MA20（现价 50.00 < MA20 52.00）→ 收盘确认卖出"},
            # 框架纸面持仓：仍被 monitor 监控，但 14:50 推送卖出已切大牛模型口径，
            # 该持仓不应出现在推送里
            {"code": "000021", "name": "深科技", "group": "持仓",
             "price": 39.5, "chg": -2.0, "vr": 1.0, "ma20": 40.0,
             "sell": "跌破MA20（40.00）", "bb_approx": False},
        ],
    }


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fake_cands_data(updated: str | None = None) -> dict:
    updated = updated or _today()
    return {
        "updated": updated,
        "mkt": {"mkt_ret20": 2.5},
        "cands": [
            {"code": "603662", "date": updated, "cnt": 4, "score": 3,
             "px": 55.0, "ma20": 52.0},
            {"code": "600549", "date": "2026-08-01", "cnt": 3, "score": 2,
             "px": 50.0, "ma20": 52.0},
        ],
    }


def fake_trades_df(updated: str | None = None) -> pd.DataFrame:
    """交割单 DataFrame：603662 今日买入+未平仓，600549 今日卖出已平仓。"""
    updated = updated or _today()
    return pd.DataFrame([
        {"代码": "603662", "名称": "柯力传感", "主题": "机器人",
         "买入日期": updated, "买入价": 55.0, "卖出日期": updated,
         "卖出价": 55.0, "收益率": 0.0, "峰值收益率": 0.0, "持仓天数": 0,
         "状态": "未平仓", "入场方式": "硬规则", "score": 3},
        {"代码": "600549", "名称": "厦门钨业", "主题": "有色",
         "买入日期": "2026-08-01", "买入价": 53.0, "卖出日期": updated,
         "卖出价": 50.0, "收益率": -0.056, "峰值收益率": 0.05, "持仓天数": 14,
         "状态": "已平仓", "入场方式": "硬规则", "score": 2},
    ])


class TestKey(unittest.TestCase):
    def test_env_key_priority(self):
        import os
        os.environ["SERVERCHAN_KEY"] = "SCT_ENV_KEY"
        try:
            self.assertEqual(push.get_key(), "SCT_ENV_KEY")
        finally:
            del os.environ["SERVERCHAN_KEY"]

    def test_file_key_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".serverchan_key"
            p.write_text("SCT_FILE_KEY", encoding="utf-8")
            # 模拟 home 指向临时目录
            import mainrise.paths as paths_mod
            old = paths_mod.home
            try:
                paths_mod.home = lambda: Path(td)
                self.assertEqual(push.get_key(), "SCT_FILE_KEY")
            finally:
                paths_mod.home = old

    def test_no_key(self):
        import mainrise.paths as paths_mod
        with tempfile.TemporaryDirectory() as td:
            old = paths_mod.home
            try:
                paths_mod.home = lambda: Path(td)
                self.assertEqual(push.get_key(), "")
            finally:
                paths_mod.home = old


class TestMessage(unittest.TestCase):
    def test_build_message_buy_sell(self):
        title, desp = push.build_message(fake_live())
        self.assertIn("买入1 卖出1", title)
        self.assertIn("柯力传感", desp)
        self.assertIn("+6.2%", desp)              # 买入候选涨幅
        self.assertIn("14:50 尾盘确认", desp)
        self.assertIn("厦门钨业", desp)            # 大牛模型持仓跌破MA20
        self.assertIn("跌破MA20", desp)
        self.assertIn("大牛模型持仓", desp)         # 卖出口径已切换
        self.assertNotIn("深科技", desp)           # 框架纸面持仓不再出现在推送
        self.assertIn("正常 · 可开仓", desp)

    def test_build_message_bb_hold_not_broken(self):
        # 大牛模型持仓未跌破 MA20（守MA20）→ 不计入卖出
        live = fake_live()
        for s in live["stocks"]:
            if s.get("bb_hold"):
                s["sell"] = "守MA20（52.00）"
        title, desp = push.build_message(live)
        self.assertIn("卖出0", title)
        self.assertIn("当前持仓无跌破 MA20", desp)

    def test_build_message_weak_market(self):
        live = fake_live()
        live["market_state"] = {"mkt_ret20": -6.0}
        _, desp = push.build_message(live)
        self.assertIn("杀跌区 · 停开新仓", desp)

    def test_load_live_stale_date(self):
        # 非今日数据 → None（跳过推送）
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "live.json").write_text(
                json.dumps(fake_live("2026-08-10 14:50:00")),
                encoding="utf-8")
            import mainrise.paths as paths_mod
            old = paths_mod.web_dir
            try:
                paths_mod.web_dir = lambda: Path(td)
                self.assertIsNone(push.load_live())
            finally:
                paths_mod.web_dir = old


class TestCloseConfirm(unittest.TestCase):
    def test_build_close_message(self):
        title, desp = push.build_close_message(
            _today(), 2.5, fake_cands_data()["cands"], fake_trades_df())
        self.assertIn("买入1 卖出1", title)
        self.assertIn("收盘确认", desp)
        self.assertIn("柯力传感", desp)          # 今日确认买入 + 当前持仓
        self.assertIn("+5.8%", desp)             # 距MA20 = 55/52-1
        self.assertIn("| 3 | 4 |", desp)         # 评分 3 / 90日T0 4
        self.assertIn("厦门钨业", desp)          # 今日确认卖出
        self.assertIn("-5.6%", desp)             # 卖出收益
        self.assertIn("当前持仓（1 只）", desp)
        self.assertIn("正常 · 可开仓", desp)

    def test_build_close_message_weak_market(self):
        _, desp = push.build_close_message(_today(), -6.0, [],
                                           fake_trades_df().iloc[0:0])
        self.assertIn("杀跌区 · 停开新仓", desp)
        self.assertIn("今日无确认买入", desp)
        self.assertIn("今日无持仓触发卖出", desp)
        self.assertIn("当前空仓", desp)

    def test_load_trades_csv(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.csv"
            fake_trades_df().to_csv(p, index=False, encoding="utf-8-sig")
            df = push.load_trades_csv(p)
            self.assertEqual(len(df), 2)
            self.assertEqual(df.iloc[1]["状态"], "已平仓")
            self.assertEqual(str(df.iloc[0]["代码"]), "603662")

    def test_run_close_skip_stale(self):
        # bigbull 数据日期非今日（非交易日/无当日行情）→ skip，不发请求
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "state"
            sd.mkdir()
            (sd / "bigbull_cands.json").write_text(
                json.dumps(fake_cands_data("2026-08-10")), encoding="utf-8")
            import mainrise.paths as paths_mod
            old_s, old_r = paths_mod.state_dir, paths_mod.report_dir
            try:
                paths_mod.state_dir = lambda: sd
                paths_mod.report_dir = lambda: Path(td) / "reports"
                self.assertEqual(push.run_close(), "skip")
            finally:
                paths_mod.state_dir, paths_mod.report_dir = old_s, old_r

    def test_run_close_missing_csv(self):
        # 有今日候选但缺交割单 CSV → skip
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "state"
            sd.mkdir()
            (sd / "bigbull_cands.json").write_text(
                json.dumps(fake_cands_data()), encoding="utf-8")
            import mainrise.paths as paths_mod
            old_s, old_r = paths_mod.state_dir, paths_mod.report_dir
            try:
                paths_mod.state_dir = lambda: sd
                paths_mod.report_dir = lambda: Path(td) / "reports"
                self.assertEqual(push.run_close(), "skip")
            finally:
                paths_mod.state_dir, paths_mod.report_dir = old_s, old_r

    def test_run_close_dry_run_full(self):
        # 完整链路 dry-run：今日候选 + 今日交割单 → ok（不发送、不耗配额）
        today = _today()
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "state"
            sd.mkdir()
            (sd / "bigbull_cands.json").write_text(
                json.dumps(fake_cands_data(today)), encoding="utf-8")
            rd = Path(td) / "reports"
            rd.mkdir()
            fake_trades_df(today).to_csv(
                rd / f"大牛模型交割单_{today}.csv", index=False,
                encoding="utf-8-sig")
            import mainrise.paths as paths_mod
            old_s, old_r = paths_mod.state_dir, paths_mod.report_dir
            try:
                paths_mod.state_dir = lambda: sd
                paths_mod.report_dir = lambda: rd
                self.assertEqual(push.run_close(dry_run=True), "ok")
            finally:
                paths_mod.state_dir, paths_mod.report_dir = old_s, old_r


if __name__ == "__main__":
    unittest.main()
