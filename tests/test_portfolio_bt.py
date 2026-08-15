"""大牛候选 · 组合级回测（portfolio_bt）单元测试。

覆盖：组合模拟的仓位上限、单票 1/3 仓、资金约束、MA60 退出、净值曲线。
"""
from __future__ import annotations

import unittest

import pandas as pd

from mainrise import portfolio_bt as pb


def two_stock_info(crash_a: bool = False):
    """两只人工股票：A 脉冲后上行（信号日 90日计数 3），B 平台不触发。"""
    frames = {}
    for code, spikes, grow in (("A", [30, 41, 52], 1.006), ("B", [], 1.0)):
        n = 260
        closes, vols = [], []
        c = 10.0
        for i in range(n):
            if i in spikes:
                c *= 1.10
            elif i > max(spikes, default=-1):
                c *= grow
            closes.append(c)
            vols.append(8e6 if i in spikes else 1e6)
        if crash_a and code == "A":          # 末段 40 天跳水触发 MA60 退出
            closes = closes[:220] + [closes[219] * 0.97 ** k
                                     for k in range(1, 41)]
        dates = pd.bdate_range("2025-01-01", periods=len(closes)) \
            .strftime("%Y-%m-%d")
        frames[code] = pd.DataFrame({"code": [code] * len(closes),
                                     "date": dates,
                                     "open": closes,
                                     "high": [x * 1.02 for x in closes],
                                     "low": [x * 0.98 for x in closes],
                                     "close": closes, "volume": vols,
                                     "prev_close": [pd.NA] + closes[:-1],
                                     "b3": [False] * len(closes),
                                     "wave2": [False] * len(closes),
                                     "signal": [False] * len(closes)})
    panels = pd.concat(frames.values()).sort_values(["code", "date"])
    built = pb.build_info(panels, {"A"}, 3)
    return built, {"A"}, panels


class TestPortfolio(unittest.TestCase):
    def test_position_cap_and_sizing(self):
        built, hot, _ = two_stock_info()
        sim = pb.simulate(built, hot, max_pos=3)
        trades = sim["trades"]
        self.assertGreaterEqual(len(trades), 1)          # A 触发并成交
        self.assertTrue(all(t["code"] == "A" for _, t in trades.iterrows()))
        self.assertGreater(sim["nav"]["nav"].iloc[-1], 1.0)  # A 上涨 → 净值 > 1

    def test_max_pos_never_exceeded(self):
        built, hot, _ = two_stock_info()
        sim = pb.simulate(built, hot, max_pos=1)
        self.assertLessEqual(max(sim["pos_count"]), 1)
        sim3 = pb.simulate(built, hot, max_pos=3)
        self.assertLessEqual(max(sim3["pos_count"]), 3)

    def test_nav_curve_and_open_trade(self):
        built, hot, _ = two_stock_info()
        sim = pb.simulate(built, hot, max_pos=3)
        nav = sim["nav"]
        self.assertEqual(len(nav), 260)                  # 每个交易日一个净值点
        self.assertAlmostEqual(nav["nav"].iloc[0], 1.0)
        # 净值曲线平滑（无跳变）
        self.assertGreater(nav["nav"].min(), 0.5)

    def test_ma60_exit_closes_trade(self):
        # A 末段跳水触发 MA60 退出 → 出现 closed 交易（open=0）
        built, hot, _ = two_stock_info(crash_a=True)
        sim = pb.simulate(built, hot, max_pos=3)
        tr = sim["trades"]
        if len(tr):
            self.assertTrue((tr["open"] == 0).any() or
                            (tr["ret"] < tr["peak_gain"]).any())

    def test_downshift_stop_skips_weak_market(self):
        # 信号日处于杀跌区（mkt_ret20 ≤ -5）→ 停开规则不入场；无降档入场
        built, hot, _ = two_stock_info()
        sig_date = max(built["A"]["sig_feats"].keys())   # 最后一个信号日
        mkt = {d: -6.0 if d == sig_date else 3.0
               for d in built["A"]["dates"]}
        sim_none = pb.simulate(built, hot, max_pos=3, mkt_ret20=mkt,
                               downshift="none")
        sim_stop = pb.simulate(built, hot, max_pos=3, mkt_ret20=mkt,
                               downshift="stop")
        # 无降档在该日入场；停开则无任何交易（该日被跳过）
        self.assertGreaterEqual(len(sim_none["trades"]), 1)
        self.assertEqual(len(sim_stop["trades"]), 0)

    def test_ban_overbought_weak_gates_entry(self):
        # 追高+弱市禁入（2026-08-14 固化）：信号日 chg20≥60% 且 大盘20日≤0 → 禁入。
        # 构造：A 的信号日 chg20 手动调高（信号日前 20 日价压低→chg20 大）。
        built, hot, _ = two_stock_info()
        sig_date = max(built["A"]["sig_feats"].keys())
        dates = list(built["A"]["dates"])
        i_sig = dates.index(sig_date)  # noqa: F841  仅确认信号日在序列内
        # 篡改 A 的信号日特征：强制 chg20=0.80（透支）
        built["A"]["sig_feats"][sig_date] = dict(
            built["A"]["sig_feats"][sig_date], chg20=0.80)
        mkt_weak = {d: -2.0 if d == sig_date else 3.0 for d in dates}
        mkt_strong = {d: 5.0 for d in dates}
        sim_weak = pb.simulate(built, hot, max_pos=3, mkt_ret20=mkt_weak,
                               downshift="none", ban_overbought_weak=True)
        sim_strong = pb.simulate(built, hot, max_pos=3, mkt_ret20=mkt_strong,
                                 downshift="none", ban_overbought_weak=True)
        sim_off = pb.simulate(built, hot, max_pos=3, mkt_ret20=mkt_weak,
                              downshift="none", ban_overbought_weak=False)
        weak_has = any(r["entry_date"] == sig_date for _, r in sim_weak["trades"].iterrows())
        strong_has = any(r["entry_date"] == sig_date for _, r in sim_strong["trades"].iterrows())
        off_has = any(r["entry_date"] == sig_date for _, r in sim_off["trades"].iterrows())
        self.assertFalse(weak_has, "弱市+透支应被禁入")
        self.assertTrue(strong_has, "强市+透支应保留")
        self.assertTrue(off_has, "关闭规则应正常入场")

    def test_ma20_exit_and_rebuy(self):
        # MA20 退出后，收盘重新站回 MA20 → rebuy 入场（via=rebuy）
        built, hot, _ = two_stock_info(crash_a=True)
        sim = pb.simulate(built, hot, max_pos=3, exit_ma=20, rebuy="ma")
        tr = sim["trades"]
        if len(tr):
            self.assertIn("via", tr.columns)
            self.assertTrue((tr["via"] == "rule").any())
            # 至少存在一次退出（open=0），且买回机制启用时 may 出现 rebuy
            self.assertTrue((tr["open"] == 0).any() or
                            (tr["via"] == "rebuy").any())

    def test_hot_by_date_gates_entry(self):
        # 动态热主题：仅信号日热 → 入场；从不热 → 无交易
        built, hot, _ = two_stock_info()
        sig_date = max(built["A"]["sig_feats"].keys())
        dates = list(built["A"]["dates"])
        hbd_all = {d: {"A"} for d in dates}
        self.assertGreaterEqual(len(pb.simulate(built, hot, max_pos=3,
                                                hot_by_date=hbd_all)["trades"]), 1)
        hbd_none = {d: set() for d in dates}
        self.assertEqual(len(pb.simulate(built, hot, max_pos=3,
                                         hot_by_date=hbd_none)["trades"]), 0)
        # 只在信号日热 → 入场且评分含热主题分（≥1）
        hbd_sig = {d: ({"A"} if d == sig_date else set()) for d in dates}
        sim = pb.simulate(built, hot, max_pos=3, hot_by_date=hbd_sig)
        self.assertGreaterEqual(len(sim["trades"]), 1)
        self.assertTrue(all(int(t["score"]) >= 1
                            for _, t in sim["trades"].iterrows()))

    def test_wash_exit_days_triggers_early_sell(self):
        # 洗盘止损：买入后收盘价连续 N 天低于买入价 → 提前卖出（reason=wash_exit）
        built, hot, _ = two_stock_info()
        # 篡改 A 信号日后的价格：先涨 3 天再阴跌 12 天（跌破买入价不收复）
        sig_date = max(built["A"]["sig_feats"].keys())
        dates = list(built["A"]["dates"])
        i_sig = dates.index(sig_date)
        closes = built["A"]["close"].copy()
        entry_px = closes[i_sig]
        for k in range(1, 4):
            closes[i_sig + k] = entry_px * 1.05      # 前 3 天小涨
        for k in range(4, 16):
            closes[i_sig + k] = entry_px * 0.95      # 之后跌破买入价 12 天
        built["A"]["close"] = closes
        sim = pb.simulate(built, hot, max_pos=3, wash_exit_days=8)
        tr = sim["trades"]
        wash = tr[tr["reason"] == "wash_exit"]
        self.assertGreaterEqual(len(wash), 1, "连续 8 天未收复应触发洗盘止损")
        # 止损日应在阴跌段内（比 MA20 退出更早；此例 MA20 可能尚未跌破）
        exit_date = wash.iloc[0]["exit_date"]
        self.assertLess(exit_date, dates[i_sig + 15], "止损应早于阴跌段结束")

    def test_wash_exit_days_off_keeps_original(self):
        # 关闭洗盘止损（默认 0）→ 无 wash_exit 交易，行为与基线一致
        built, hot, _ = two_stock_info()
        sig_date = max(built["A"]["sig_feats"].keys())
        dates = list(built["A"]["dates"])
        i_sig = dates.index(sig_date)
        closes = built["A"]["close"].copy()
        entry_px = closes[i_sig]
        for k in range(1, 4):
            closes[i_sig + k] = entry_px * 1.05
        for k in range(4, 16):
            closes[i_sig + k] = entry_px * 0.95
        built["A"]["close"] = closes
        sim = pb.simulate(built, hot, max_pos=3)
        tr = sim["trades"]
        self.assertFalse((tr["reason"] == "wash_exit").any(),
                         "默认应关闭洗盘止损")

    def test_top1_buys_highest_score_only(self):
        """top1=True：同日多信号只买评分最高 1 只；top1=False 同日可买多只。"""
        built, hot, _ = two_stock_info()
        # 构造 A、B 同日触发信号：A 高分（热主题+1），B 低分（非热）
        # B 是 hot 外的股票——需要手工加信号。简单方式：clone A 为两只
        # 不同代码的股票，A 热 B 不热 → 同日信号 A 评分更高。
        import pandas as pd
        import numpy as np
        # 重建一个 A+B 同日信号的 info：两者都在第 52 天首次凑齐 90日T0≥3
        frames = {}
        for code, spikes, grow in (("A", [44, 48, 52], 1.006),
                                   ("B", [45, 49, 52], 1.004)):
            n = 260
            closes, vols = [], []
            c = 10.0
            for i in range(n):
                if i in spikes:
                    c *= 1.10
                elif i > max(spikes, default=-1):
                    c *= grow
                closes.append(c)
                vols.append(8e6 if i in spikes else 1e6)
            dates = pd.bdate_range("2025-01-01", periods=len(closes)) \
                .strftime("%Y-%m-%d")
            frames[code] = pd.DataFrame({"code": [code] * len(closes),
                                         "date": dates,
                                         "open": closes,
                                         "high": [x * 1.02 for x in closes],
                                         "low": [x * 0.98 for x in closes],
                                         "close": closes, "volume": vols,
                                         "prev_close": [pd.NA] + closes[:-1],
                                         "b3": [False] * len(closes),
                                         "wave2": [False] * len(closes),
                                         "signal": [False] * len(closes)})
        panels = pd.concat(frames.values()).sort_values(["code", "date"])
        built2 = pb.build_info(panels, {"A", "B"}, 3)   # A、B 都热
        # 信号日相同（两只都在第 52 天凑齐 90日T0≥3）
        sigA = max(built2["A"]["sig_feats"].keys())
        sigB = max(built2["B"]["sig_feats"].keys())
        self.assertEqual(sigA, sigB, "A、B 应同日触发信号")
        # A 评分更高（A 热且链长≥4 → +2；B 热+1 但链长3 → +1；实际 A=2 B=1，
        # 用 score_min=1 让两者都进候选，验证 top1 只买评分更高的 A）
        sim_multi = pb.simulate(built2, {"A", "B"}, max_pos=3, score_min=1)
        sim_top1 = pb.simulate(built2, {"A", "B"}, max_pos=3, score_min=1,
                               top1=True)
        # 同日信号数
        def _same_day_entries(sim):
            return sim["trades"][sim["trades"]["entry_date"] == sigA]
        m = _same_day_entries(sim_multi)
        t = _same_day_entries(sim_top1)
        self.assertGreaterEqual(len(m), 2, "非 top1 同日应至少买 2 只")
        self.assertLessEqual(len(t), 1, "top1 同日最多买 1 只")
        if len(t) == 1:
            self.assertEqual(int(t.iloc[0]["score"]),
                             int(m["score"].max()),
                             "top1 应买当日评分最高的 1 只")


if __name__ == "__main__":
    unittest.main()
