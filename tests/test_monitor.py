"""盘中盯盘测试：交易时段判断 / 提醒规则 / 限频 / 状态构建。"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

from mainrise.monitor import (
    _DAILY_FEATURES,
    _DAILY_FEATURES_DATE,
    _buy_sell_signals,
    _launch_signal,
    build_state,
    evaluate_alert,
    is_trading_time,
    load_daily_features,
    render_live_html,
)


class TestMonitor(unittest.TestCase):
    def tearDown(self):
        # 清掉按日期缓存的日线特征，避免污染其他用例
        from mainrise import monitor as _m
        _m._DAILY_FEATURES.clear()
        _m._DAILY_FEATURES_DATE = ""

    def test_load_daily_features_includes_low(self):
        """lo60 依赖 low 列：读取列必须包含 low（曾漏读导致 KeyError）。"""
        with tempfile.TemporaryDirectory() as tmp:
            zz = Path(tmp) / "zzshare_daily"
            zz.mkdir()
            px = 10.0
            for i in range(30):
                d = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
                stem = d.replace("-", "")
                (zz / f"{stem}.csv").write_text(
                    "date,code,open,high,low,close,volume\n"
                    f"{d},000001,{px:.2f},{px*1.01:.2f},{px*0.99:.2f},"
                    f"{px:.2f},10000\n",
                    encoding="utf-8")
                px *= 1.005
            with mock.patch("mainrise.monitor.paths.data_dir",
                            return_value=Path(tmp)):
                feat = load_daily_features({"000001"}, "2026-08-01")
        self.assertIn("000001", feat)
        self.assertGreater(feat["000001"]["lo60"], 0)

    def test_trading_time(self):
        dates = {"2026-08-10"}  # 周一
        self.assertTrue(is_trading_time(datetime(2026, 8, 10, 9, 30), dates))
        self.assertTrue(is_trading_time(datetime(2026, 8, 10, 14, 59), dates))
        self.assertFalse(is_trading_time(datetime(2026, 8, 10, 11, 30), dates))  # 午休
        self.assertFalse(is_trading_time(datetime(2026, 8, 10, 12, 0), dates))
        self.assertFalse(is_trading_time(datetime(2026, 8, 15, 10, 0), dates))  # 周六
        self.assertFalse(is_trading_time(datetime(2026, 8, 11, 10, 0), dates))  # 非交易日

    def test_alert_rules_and_cooldown(self):
        now = datetime(2026, 8, 10, 10, 0)
        cd = {}
        row = {"price": 9.4, "low": 9.3, "high": 10.5, "chg": 1.0,
               "buy_price": 10.0, "peak": 10.5, "sell": "⚠止损-4%",
               "buy": "持有中"}
        self.assertIn("止损", evaluate_alert(row, now, cd))
        # 同规则 10 分钟内限频
        self.assertIsNone(evaluate_alert(row, now + timedelta(seconds=60), cd))
        # 跌破 MA10 -> 卖出提醒（待买放弃）
        srow = {"price": 9.6, "low": None, "buy_price": None, "peak": None,
                "group": "观察", "sell": "跌破MA10（9.80）",
                "buy": "14:30后评估企稳"}
        self.assertIn("跌破MA10", evaluate_alert(srow, now, {}))
        # 破位 -> 放弃买入
        brow = {"price": 9.3, "low": None, "buy_price": None, "peak": None,
                "group": "观察", "sell": "破位", "buy": "破位，不买"}
        self.assertIn("放弃买入", evaluate_alert(brow, now, {}))
        # 两级模型：B3 -> 打底仓提醒；二波 -> 加仓提醒
        b3row = {"price": 9.85, "low": None, "buy_price": None, "peak": None,
                 "group": "两级", "status": "B3", "code": "600707",
                 "name": "彩虹股份", "sell": "—", "buy": "明日打底仓"}
        self.assertIn("B3", evaluate_alert(b3row, now, {}))
        w2row = {"price": 9.85, "low": None, "buy_price": None, "peak": None,
                 "group": "两级", "status": "二波", "code": "600707",
                 "name": "彩虹股份", "sell": "—", "buy": "明日加仓"}
        self.assertIn("二波", evaluate_alert(w2row, now, {}))
        # 涨跌幅噪音不再提醒
        noise = {"price": 10.6, "low": None, "buy_price": None, "peak": None,
                 "group": "观察", "sell": "—", "buy": "—", "chg": 6.2}
        self.assertIsNone(evaluate_alert(noise, now, {}))

    def test_cooldown_is_per_stock(self):
        """冷却键按票：两只不同持仓同日破位，止损提醒都报（H2 修复）。"""
        now = datetime(2026, 8, 10, 10, 0)
        cd = {}
        mk = lambda code: {  # noqa: E731
            "price": 9.4, "low": 9.3, "high": 10.5, "chg": 1.0,
            "buy_price": 10.0, "peak": 10.5, "sell": "⚠止损-4%",
            "buy": "持有中", "code": code, "name": code}
        a1 = evaluate_alert(mk("600001"), now, cd)
        b1 = evaluate_alert(mk("600002"), now, cd)
        self.assertIn("止损", a1)
        self.assertIn("止损", b1)   # 不同票不受另一只的冷却影响
        # 同票 10 分钟内限频
        self.assertIsNone(evaluate_alert(mk("600001"), now + timedelta(seconds=60), cd))

    def test_build_state(self):
        now = datetime(2026, 8, 10, 10, 0)
        quotes = pd.DataFrame([
            {"code": "600000", "close": 9.4, "price_change_ratio_pct": -6.0,
             "high": 10.2, "low": 9.3, "prev_close": 10.0, "error": ""},
            {"code": "000001", "close": 12.0, "price_change_ratio_pct": 2.0,
             "high": 12.1, "low": 11.9, "prev_close": 11.76, "error": ""},
        ])
        rows = {
            "600000": {"code": "600000", "name": "浦发银行", "group": "持仓",
                       "buy_price": 10.0, "peak": 10.2},
            "000001": {"code": "000001", "name": "平安银行", "group": "观察",
                       "buy_price": None, "peak": None},
        }
        state = {"alerts": []}
        live = build_state(quotes, rows, {}, state, now)
        self.assertEqual(len(live["stocks"]), 2)
        self.assertEqual(len(live["alerts"]), 1)  # 600000 触发止损
        self.assertIn("止损", live["alerts"][0]["message"])
        minutes = {"600000": [["09:30", 9.5], ["09:31", 9.4]],
                   "000001": [["09:30", 11.9], ["09:31", 12.0]]}
        html_text = render_live_html(live, now, "上午盘中", 1,
                                     minutes=minutes)
        self.assertIn("主升浪实时盯盘", html_text)
        self.assertIn("浦发银行", html_text)
        self.assertIn("sortT", html_text)      # 排序按钮/表头
        self.assertIn("/stock/600000", html_text)  # 点击代码进详情页
        self.assertIn("hardRefresh('live')", html_text)
        self.assertIn("svg class=\"spark\"", html_text)   # 分时缩略图
        self.assertIn('data-code="600000"', html_text)
        self.assertIn("每 3 秒实时更新", html_text)
        self.assertIn("POLL_MS=3000", html_text)
        self.assertIn("今日买入卖出信号", html_text)       # 买卖信号卡
        self.assertIn("sigtb", html_text)
        self.assertIn("名称/代码", html_text)               # 卡片名称+代码可点进K线
        self.assertIn("#mtb tr[data-code=", html_text)      # 主表更新限定 #mtb
        self.assertIn("#sigtb tr[data-code=", html_text)    # 卡片更新限定 #sigtb
        self.assertIn("代码/名称", html_text)              # 新 8 列表格排版
        self.assertIn("量比", html_text)
        self.assertIn("买入信号", html_text)
        self.assertIn("卖出信号", html_text)
        self.assertNotIn("今高", html_text)

    def test_buy_sell_signals(self):
        now = datetime(2026, 8, 10, 10, 0)
        d = {"avg5_vol": 1_000_000}
        # 持仓跌破 MA10 -> 卖出信号
        r1 = {"price": 9.7, "volume": 5000, "buy_price": 10.0, "peak": None,
              "group": "持仓", "status": "", "ma10": 9.8, "ma20": 9.5}
        buy, sell, _ = _buy_sell_signals(r1, d, now)
        self.assertIn("跌破MA10", sell)
        self.assertEqual(buy, "持有中")
        # 持仓高点回落 8% 优先于跌破 MA10
        buy6, sell6, _ = _buy_sell_signals(
            {**r1, "peak": 10.5, "price": 9.65}, d, now)
        self.assertIn("高点回落8%", sell6)
        # 市场自适应：主升区 → B3/二波 动作加"轻仓"前缀；无市场状态不加
        mkt_up = {"state": "主升区", "structure": "均衡", "amount_wl": 1.1}
        buy3, _, _ = _buy_sell_signals(
            {"price": 12.0, "volume": 5000, "buy_price": None, "peak": None,
             "group": "观察", "status": "二波加仓", "ma10": 11.8,
             "ma20": 11.5, "buy": "明日开盘加仓（1/3）"}, d, now,
            mkt_state=mkt_up)
        self.assertIn("主升区", buy3)
        buy4, _, _ = _buy_sell_signals(
            {"price": 12.0, "volume": 5000, "buy_price": None, "peak": None,
             "group": "观察", "status": "二波加仓", "ma10": 11.8,
             "ma20": 11.5, "buy": "明日开盘加仓（1/3）"}, d, now)
        self.assertNotIn("主升区", buy4)
        # 破位 -> 卖出/买入均提示放弃
        r5 = {"price": 9.4, "volume": 5000, "buy_price": None, "peak": None,
              "group": "观察", "status": "", "ma10": 9.8, "ma20": 9.5}
        buy5, sell5, _ = _buy_sell_signals(r5, d, now)
        self.assertIn("破位", sell5)

    def test_launch_action_adaptive(self):
        from mainrise import market_state as ms
        # 默认（无状态）→ 打底仓
        self.assertIn("打底仓", ms.launch_action(None, "r9A"))
        # 主升区 → 仅观察（不提醒打底仓）
        act = ms.launch_action({"state": "主升区", "structure": "科技强",
                                "amount_wl": 1.1}, "r9A")
        self.assertIn("仅观察", act)
        # 量能不足 → 等放量
        act2 = ms.launch_action({"state": "杀跌区", "structure": "均衡",
                                 "amount_wl": 0.85}, "r7mA")
        self.assertIn("量能不足", act2)
        # 杀跌区 + 量能足 → 打底仓
        act3 = ms.launch_action({"state": "杀跌区", "structure": "均衡",
                                 "amount_wl": 1.2}, "r9A")
        self.assertIn("打底仓", act3)

    def test_build_state_gates_launch_alert_in_main_zone(self):
        """主升区时启动信号只观察，不弹"打底仓"提醒。"""
        now = datetime(2026, 8, 10, 10, 0)
        quotes = pd.DataFrame([
            {"code": "600707", "close": 9.6, "price_change_ratio_pct": 6.7,
             "high": 9.8, "low": 9.5, "prev_close": 9.0,
             "volume": 1.875e4, "error": ""}])
        rows = {"600707": {"code": "600707", "name": "彩虹股份",
                           "group": "观察", "buy_price": None, "peak": None}}
        daily = {"600707": {
            "prev_close": 9.0, "close5": 10.0, "close10": 9.8,
            "lo60": 8.0, "ma5": 9.5, "ma10": 9.4, "ma20": 9.2,
            "hi20": 11.5, "avg5_vol": 1e7, "limit_pct": 0.10}}
        state = {"alerts": []}
        mkt_up = {"state": "主升区", "structure": "科技强", "amount_wl": 1.1}
        live = build_state(quotes, rows, {}, state, now, daily=daily,
                           mkt_zt=90, mkt_state=mkt_up)
        s = live["stocks"][0]
        self.assertIn("仅观察", s["buy"])
        self.assertFalse(any("打底仓" in a["message"] for a in live["alerts"]))
        # 杀跌区+量能足 → 正常打底仓提醒
        state2 = {"alerts": []}
        mkt_down = {"state": "杀跌区", "structure": "均衡", "amount_wl": 1.2}
        live2 = build_state(quotes, rows, {}, state2, now, daily=daily,
                            mkt_zt=90, mkt_state=mkt_down)
        self.assertTrue(any("打底仓" in a["message"] for a in live2["alerts"]))

    def test_launch_signal_thresholds(self):
        d = {"hi20": 10.0, "close5": 9.5}   # 现价 8.2：回撤-18%，前5日-13.7%
        r = {"price": 8.2, "chg": 6.0, "vr": 1.5}
        self.assertEqual(_launch_signal(r, d, 130), "r9A")
        self.assertEqual(_launch_signal(r, d, 100), "r7mA")
        self.assertEqual(_launch_signal(r, d, 80), "")        # 市场门限不足
        self.assertEqual(_launch_signal({**r, "vr": 2.5}, d, 100), "")  # 巨量非 r7mA
        self.assertEqual(_launch_signal({**r, "vr": 2.5}, d, 130), "r9A")
        self.assertEqual(_launch_signal({**r, "chg": 4.0}, d, 130), "")  # 涨幅不足
        self.assertEqual(_launch_signal({**r, "price": 9.6}, d, 130), "")  # 回撤不足
        self.assertEqual(_launch_signal(r, None, 100), "")    # 无日线特征
        self.assertEqual(_launch_signal(r, d, None), "")      # 无市场数据不判定

    def test_build_state_scan_launch(self):
        now = datetime(2026, 8, 10, 10, 30)
        quotes = pd.DataFrame([
            {"code": "300308", "close": 8.2,
             "price_change_ratio_pct": 6.0, "high": 8.4, "low": 7.9,
             "prev_close": 7.74, "volume": 3_750, "error": ""},
        ])
        daily = {"300308": {"hi20": 10.0, "close5": 9.5, "avg5_vol": 1_000_000}}
        state = {"alerts": []}
        live = build_state(quotes, {}, {}, state, now, daily=daily,
                           mkt_zt=100, scan_codes={"300308"},
                           scan_names={"300308": "中际旭创"})
        self.assertEqual(len(live["stocks"]), 1)
        s = live["stocks"][0]
        self.assertEqual(s["group"], "启动")
        self.assertEqual(s["launch"], "r7mA")
        self.assertTrue(s["in_card"])
        self.assertTrue(any("启动信号" in a["message"] for a in live["alerts"]))
        html_text = render_live_html(live, now, "上午盘中", 0)
        self.assertIn("启动加仓模型", html_text)
        self.assertIn("明日开盘打底仓", html_text)

    def test_build_state_bb_hold_ma20_break(self):
        """大牛模型持仓：盘中跌破 MA20 → 卖出信号 + 收盘确认卖出提醒。"""
        now = datetime(2026, 8, 10, 14, 50)
        quotes = pd.DataFrame([
            {"code": "603662", "close": 50.0, "price_change_ratio_pct": -3.0,
             "high": 53.0, "low": 49.9, "prev_close": 51.5,
             "volume": 5000, "error": ""},
        ])
        rows = {"603662": {"code": "603662", "name": "柯力传感",
                           "group": "大牛模型", "status": "大牛模型持仓",
                           "bb_hold": True, "bb_score": 3,
                           "bb_date": "2026-08-08",
                           "buy_price": None, "peak": None}}
        daily = {"603662": {"avg5_vol": 1_000_000, "ma10": 51.0, "ma20": 52.0}}
        ma_map = {"603662": (51.0, 52.0)}
        state = {"alerts": []}
        live = build_state(quotes, rows, ma_map, state, now, daily)
        s = [x for x in live["stocks"] if x["code"] == "603662"][0]
        self.assertTrue(s["bb_hold"])
        self.assertEqual(s["buy"], "持有中")
        self.assertIn("跌破MA20", s["sell"])
        self.assertIn("收盘确认卖出", s["sell"])
        self.assertTrue(any("大牛模型持仓" in a["message"] for a in live["alerts"]))
        html_text = render_live_html(live, now, "下午盘中", 1,
                                     minutes={"603662": [["14:50", 50.0]]})
        self.assertIn("持仓破MA20", html_text)       # 候选卡状态列

    def test_build_state_bb_hold_above_ma20(self):
        """大牛模型持仓：现价在 MA20 上方 → 守MA20，无卖出提醒。"""
        now = datetime(2026, 8, 10, 14, 50)
        quotes = pd.DataFrame([
            {"code": "603662", "close": 53.0, "price_change_ratio_pct": 1.0,
             "high": 53.5, "low": 52.0, "prev_close": 52.5,
             "volume": 5000, "error": ""},
        ])
        rows = {"603662": {"code": "603662", "name": "柯力传感",
                           "group": "大牛模型", "status": "大牛模型持仓",
                           "bb_hold": True, "bb_score": 3,
                           "bb_date": "2026-08-08",
                           "buy_price": None, "peak": None}}
        daily = {"603662": {"avg5_vol": 1_000_000, "ma10": 51.0, "ma20": 52.0}}
        ma_map = {"603662": (51.0, 52.0)}
        state = {"alerts": []}
        live = build_state(quotes, rows, ma_map, state, now, daily)
        s = [x for x in live["stocks"] if x["code"] == "603662"][0]
        self.assertIn("守MA20", s["sell"])
        self.assertNotIn("跌破MA20", s["sell"])
        self.assertFalse(live["alerts"])            # 无提醒

    def test_alerts_only_for_signal_card(self):
        now = datetime(2026, 8, 10, 14, 40)
        quotes = pd.DataFrame([
            {"code": "001309", "close": 380.0, "price_change_ratio_pct": -5.0,
             "high": 400.0, "low": 375.0, "prev_close": 400.0,
             "volume": 10000, "error": ""},
            {"code": "000001", "close": 9.3, "price_change_ratio_pct": -6.0,
             "high": 9.8, "low": 9.2, "prev_close": 9.9,
             "volume": 5000, "error": ""},
        ])
        rows = {
            "001309": {"code": "001309", "name": "德明利", "group": "观察",
                       "buy_price": None, "peak": None, "status": "破位"},
            "000001": {"code": "000001", "name": "平安银行", "group": "观察",
                       "buy_price": None, "peak": None, "status": "B3打底仓",
                       "twostage_action": "明日开盘打底仓（计划仓位 2/3）"},
        }
        daily = {"001309": {"avg5_vol": 1_000_000, "ma10": 390.0,
                            "ma20": 395.0},
                 "000001": {"avg5_vol": 1_000_000, "ma10": 9.8,
                            "ma20": 9.5}}
        ma_map = {"001309": (390.0, 395.0), "000001": (9.8, 9.5)}
        state = {"alerts": []}
        live = build_state(quotes, rows, ma_map, state, now, daily)
        alerts = [a["code"] for a in live["alerts"]]
        self.assertNotIn("001309", alerts)      # 破位票已排除，不提醒
        self.assertIn("000001", alerts)         # 待买票破位 → 放弃买入提醒


if __name__ == "__main__":
    unittest.main()
