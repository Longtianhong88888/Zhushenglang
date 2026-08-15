"""大牛模型（固化版）单元测试。

覆盖：模型规格常数、评分门槛逻辑、门户只含模型信息（无旧门户内容）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import bigbull as bb
from mainrise import portfolio_bt as pb


class TestModelSpec(unittest.TestCase):
    def test_final_constants(self):
        self.assertEqual(bb.SCORE_MIN, 2)          # 评分≥2
        self.assertEqual(bb.EXIT_MA, 20)           # MA20 退出
        self.assertEqual(bb.DOWNSHIFT, "stop")     # 杀跌区停开
        self.assertEqual(bb.MAX_POS, 3)
        self.assertEqual(bb.PORTAL_NAME, "index.html")

    def test_score_gate(self):
        # 硬规则下评分天然 ≥2：热主题 + 90日T0≥3 = 2 分
        feat = {"cnt": 5, "new_hi60": 0, "chg10": 50.0, "chain": 1}
        self.assertEqual(pb.big_score(feat, hot=True), 2)
        # 创60日新高+10日<30% +1、链长≥4 +1 → 满分 4
        feat2 = {"cnt": 5, "new_hi60": 1, "chg10": 10.0, "chain": 5}
        self.assertEqual(pb.big_score(feat2, hot=True), 4)
        # 非热主题但其余全中 → 3 分
        self.assertEqual(pb.big_score(feat2, hot=False), 3)


class TestPortal(unittest.TestCase):
    def _min_inputs(self, tr=None, ma20_last=None):
        """构造门户最小输入；ma20_last 控制持仓破位演示。"""
        dates = pd.bdate_range("2026-01-01", periods=260).strftime("%Y-%m-%d")
        nav = pd.DataFrame({"date": dates,
                            "nav": np.linspace(1.0, 3.0, 260)})
        if tr is None:
            tr = pd.DataFrame({
                "code": ["600172"], "entry_date": ["2026-09-01"],
                "entry": [4.2], "exit_date": ["2026-10-01"], "exit": [5.5],
                "ret": [0.3], "peak_gain": [0.4], "hold": [20],
                "open": [0], "score": [2]})
        ma20 = np.full(120, 4.8 if ma20_last is None else ma20_last)
        info = {"600172": {"dates": dates, "close": np.full(120, 5.0),
                           "ma20": ma20,
                           # 信号日取窗口内（last_date 前 45 日内），保证候选出现
                           "sig_feats": {dates[-5]: {"cnt": 4, "new_hi60": 1,
                                                     "chg10": 5.0, "chain": 3}},
                           "n": 120}}
        mkt = {dates[-1]: 3.0}
        return tr, nav, info, mkt, dates

    def _portal(self, tr, nav, info, mkt, dates, hot_by_date=None,
                hot_themes=None):
        with tempfile.TemporaryDirectory() as td:
            out = bb.write_portal(tr, nav, info, {"600172"}, mkt,
                                  {"600172": "黄河旋风"},
                                  {"600172": "AI硬件"}, dates[-1], out_dir=td,
                                  hot_by_date=hot_by_date,
                                  hot_themes=hot_themes)
            return Path(out).read_text(encoding="utf-8")

    def test_portal_only_model_info(self):
        tr, nav, info, mkt, dates = self._min_inputs()
        h = self._portal(tr, nav, info, mkt, dates)
        self.assertIn("大牛模型", h)
        self.assertIn("市场状态", h)
        self.assertIn("近期候选信号", h)
        self.assertIn("模型净值曲线", h)
        self.assertIn("最近交割记录", h)
        self.assertIn("600172", h)               # 交割记录含代码
        self.assertIn("黄河旋风", h)              # 名称
        # 买卖点提醒卡（预留位置 + 内容）
        self.assertIn("买卖点提醒", h)
        self.assertIn("今日无硬规则信号", h)
        self.assertIn("当前空仓", h)
        # 名称/代码合并单元格，代码直达看盘页
        self.assertIn('href="/stock/600172"', h)
        # 重置口径：净值 1.0 起、2026-08-01 标注（聚焦 8 月后收益）
        self.assertIn("净值 1.00", h)
        self.assertIn("2026-08-01", h)
        self.assertNotIn("2025-01-01", h)
        # 导航入口（2026-08-14 新增：每日报告/盯盘/复盘等，链接到对应页面）
        self.assertIn('href="reports.html"', h)
        self.assertIn('href="live.html"', h)
        # 旧门户"大按钮首页"内容不应出现（门户主体=大牛模型卡片区）
        for old in ("连板梯队", "门户首页", "选择入口", "主升浪信号跟踪 · 首页"):
            self.assertNotIn(old, h)
        # 追高弱市禁入规则卡（2026-08-14 研究固化）
        self.assertIn("追高弱市禁入", h)

    def test_portal_trade_list_desc(self):
        # 交割记录倒序：最近的（2026-11-01 卖出）在前
        tr, nav, info, mkt, dates = self._min_inputs()
        tr2 = pd.DataFrame([
            {"code": "600172", "entry_date": "2026-09-05", "entry": 4.0,
             "exit_date": "2026-10-01", "exit": 5.0, "ret": 0.2,
             "peak_gain": 0.3, "hold": 20, "open": 0, "score": 2},
            {"code": "600172", "entry_date": "2026-10-05", "entry": 5.0,
             "exit_date": "2026-11-01", "exit": 5.8, "ret": 0.15,
             "peak_gain": 0.25, "hold": 15, "open": 0, "score": 2},
        ])
        h = self._portal(tr2, nav, info, mkt, dates)
        self.assertLess(h.index("2026-11-01"), h.index("2026-10-01"))

    def test_portal_bb_reminder_holding(self):
        # 当前持仓卡：破 MA20 → 收盘确认卖出提醒
        tr, nav, info, mkt, dates = self._min_inputs(ma20_last=5.2)
        tr_hold = pd.DataFrame({
            "code": ["600172"], "entry_date": ["2026-09-01"],
            "entry": [4.2], "exit_date": [dates[-1]], "exit": [5.0],
            "ret": [0.19], "peak_gain": [0.2], "hold": [30],
            "open": [1], "score": [2]})
        h = self._portal(tr_hold, nav, info, mkt, dates)
        self.assertIn("当前持仓（1 只）", h)
        self.assertIn("破MA20", h)
        self.assertIn("收盘确认卖出", h)

    def test_portal_rebase_from_2026_08(self):
        # 净值曲线从 2026-08-01 重置：窗口首点净值 1.00，曲线起点日期=窗口首日
        tr, nav, info, mkt, dates = self._min_inputs()
        h = self._portal(tr, nav, info, mkt, dates)
        first = [d for d in dates if d >= "2026-08-01"][0]
        self.assertIn(f"{first}（净值 1.00）", h)      # 重置后从 1.0 起画
        self.assertIn("2026-08-01 起 · 净值 1.0 重置", h)

    def test_model_candidates_dedupe(self):
        # 同一代码多个信号日 → 只保留最近一次
        dates = pd.bdate_range("2026-01-01", periods=60).strftime("%Y-%m-%d")
        info = {"600172": {"dates": dates, "close": np.full(60, 5.0),
                           "ma20": np.full(60, 4.8),
                           "sig_feats": {dates[10]: {"cnt": 3, "new_hi60": 0,
                                                     "chg10": 50.0, "chain": 1},
                                         dates[30]: {"cnt": 4, "new_hi60": 1,
                                                     "chg10": 5.0, "chain": 3}},
                           "n": 60}}
        cands = bb.model_candidates(info, {"600172"}, last_date=dates[-1])
        self.assertEqual(len(cands), 1)          # 去重
        self.assertEqual(cands[0]["date"], dates[30])   # 保留最近一次
        self.assertEqual(cands[0]["cnt"], 4)

    def test_model_candidates_hot_by_date(self):
        # 动态热主题：仅信号日热 → 计入候选；该日不热 → 剔除
        dates = pd.bdate_range("2026-01-01", periods=60).strftime("%Y-%m-%d")
        info = {"600172": {"dates": dates, "close": np.full(60, 5.0),
                           "ma20": np.full(60, 4.8),
                           "sig_feats": {dates[40]: {"cnt": 4, "new_hi60": 1,
                                                     "chg10": 5.0, "chain": 3}},
                           "n": 60}}
        last = dates[-1]
        cands = bb.model_candidates(info, set(), last_date=last,
                                    hot_by_date={dates[40]: {"600172"}})
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["date"], dates[40])
        self.assertGreaterEqual(cands[0]["score"], 2)   # 含热主题分
        cands2 = bb.model_candidates(info, set(), last_date=last,
                                     hot_by_date={dates[40]: set()})
        self.assertEqual(len(cands2), 0)

    def test_model_candidates_ban_overbought_weak(self):
        # 追高+弱市禁入（2026-08-15 固化）：信号日 chg20≥60% 且 大盘20日≤0 →
        # 不列为候选（盯盘/推送与回测一致）；强市保留；关闭规则保留。
        dates = pd.bdate_range("2026-01-01", periods=60).strftime("%Y-%m-%d")
        sig = dates[40]
        info = {"600172": {"dates": dates, "close": np.full(60, 5.0),
                           "ma20": np.full(60, 4.8),
                           "sig_feats": {sig: {"cnt": 4, "new_hi60": 1,
                                               "chg10": 5.0, "chg20": 0.75,
                                               "chain": 3}},
                           "n": 60}}
        last = dates[-1]
        mkt_weak = {sig: -2.0}      # 弱市
        mkt_strong = {sig: 5.0}     # 强市
        c_weak = bb.model_candidates(info, {"600172"}, last_date=last,
                                     mkt_ret20=mkt_weak, ban_overbought_weak=True)
        c_strong = bb.model_candidates(info, {"600172"}, last_date=last,
                                       mkt_ret20=mkt_strong, ban_overbought_weak=True)
        c_off = bb.model_candidates(info, {"600172"}, last_date=last,
                                    mkt_ret20=mkt_weak, ban_overbought_weak=False)
        self.assertEqual(len(c_weak), 0, "弱市+透支候选应被过滤")
        self.assertEqual(len(c_strong), 1, "强市+透支候选应保留")
        self.assertEqual(len(c_off), 1, "关闭规则应保留候选")

    def test_portal_hot_themes(self):
        # 门户市场状态卡展示当前热主题（含模式标注）
        tr, nav, info, mkt, dates = self._min_inputs()
        h = self._portal(tr, nav, info, mkt, dates, hot_by_date={},
                         hot_themes=["半导体", "存储", "创新药"])
        self.assertIn("当前热主题", h)
        self.assertIn("半导体", h)
        self.assertIn("固定 · AI硬件/半导体/存储", h)

    def test_trades_csv_chinese_columns(self):
        # 交割单 CSV 表头必须为中文（代码/名称/主题 + 买卖字段），
        # 否则 push --close 收盘确认推送的代码列取空
        tr = pd.DataFrame({
            "code": ["600172"], "entry_date": ["2026-02-01"],
            "entry": [4.2], "exit_date": ["2026-03-01"], "exit": [5.5],
            "ret": [0.3], "peak_gain": [0.4], "hold": [20],
            "open": [0], "score": [2]})
        df = bb.trades_to_csv_frame(tr, {"600172": "黄河旋风"},
                                    {"600172": "AI硬件"})
        cols = list(df.columns)
        self.assertIn("代码", cols)
        self.assertIn("名称", cols)
        self.assertIn("主题", cols)
        self.assertNotIn("code", cols)            # 旧英文列必须移除
        self.assertIn("买入日期", cols)
        self.assertEqual(df.iloc[0]["状态"], "已平仓")
        self.assertEqual(df.iloc[0]["代码"], "600172")

    def test_write_cands_json_with_holdings(self):
        # 候选 JSON 含 holdings（大牛模型当前持仓，供盯盘/14:50 推送盘中口径）
        import json
        from mainrise import bigbull as bb_mod
        with tempfile.TemporaryDirectory() as td:
            p = bb_mod.write_cands_json(
                [{"code": "600172", "date": "2026-02-01", "cnt": 4,
                  "score": 2, "px": 5.0, "ma20": 4.8}],
                "2026-08-14", state_dir=td,
                mkt_ret20=3.2,
                holdings=[{"code": "600172", "name": "黄河旋风",
                           "theme": "AI硬件", "entry_date": "2026-08-01",
                           "entry": 4.2, "px": 5.0, "ma20": 4.8,
                           "ret": 0.19, "score": 2}])
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        self.assertEqual(data["updated"], "2026-08-14")
        self.assertEqual(data["mkt"]["mkt_ret20"], 3.2)
        self.assertEqual(len(data["holdings"]), 1)
        self.assertEqual(data["holdings"][0]["code"], "600172")


if __name__ == "__main__":
    unittest.main()
