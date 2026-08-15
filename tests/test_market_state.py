"""大盘状态（三态轮动）测试：三态判定 / 每日计算落盘 / 上证指数缓存。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from mainrise import market_state


def _synth_panels(days: int = 30,
                  codes=("000021", "000002", "000003")) -> pd.DataFrame:
    # 000021 在科技卡点名单内，000002/000003 非卡点 → 结构维度可计算
    rows = []
    for ci, code in enumerate(codes):
        px = 10.0
        prev = None
        for i in range(days):
            date = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
            if i and i % 3 == 0:
                px *= 0.99
            elif i:
                px *= 1.01
            pct = 0.0 if prev is None else (px / prev - 1) * 100
            rows.append({
                "date": date, "code": code, "open": px, "high": px * 1.01,
                "low": px * 0.99, "close": px,
                "pct_chg": pct, "prev_close": prev if prev is not None else px,
                "amount": 1e10 + ci * 1e8, "volume": 1e7 + ci * 1e5,
            })
            prev = px
    return pd.DataFrame(rows)


class TestMarketState(unittest.TestCase):
    def test_classify_three_states(self):
        crash = market_state.classify(-6.0, 0.3, 1.1)
        self.assertEqual(crash["state"], "杀跌区")
        self.assertIn("启动加仓全开", crash["advice"])

        osc = market_state.classify(2.0, 0.5, 1.1)
        self.assertEqual(osc["state"], "震荡区")
        self.assertIn("双模型并行", osc["advice"])

        up = market_state.classify(6.0, 0.6, 1.2)
        self.assertEqual(up["state"], "主升区")
        self.assertIn("停开", up["advice"])

        # 宽度≥70% 即使涨幅未超5%也判主升区
        wide = market_state.classify(3.0, 0.75, 1.0)
        self.assertEqual(wide["state"], "主升区")

    def test_classify_low_amount_warning(self):
        st = market_state.classify(-6.0, 0.3, 0.85)
        self.assertIn("量能不足", st["advice"])

    def test_classify_structure(self):
        s1 = market_state.classify_structure(8.0)
        self.assertEqual(s1["structure"], "科技强")
        self.assertIn("T0", s1["advice"])
        s2 = market_state.classify_structure(-6.0)
        self.assertEqual(s2["structure"], "非科技强")
        s3 = market_state.classify_structure(0.0)
        self.assertEqual(s3["structure"], "均衡")
        s4 = market_state.classify_structure(None)
        self.assertEqual(s4["structure"], "结构未知")

    def test_compute_daily_writes_json_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with mock.patch.object(market_state, "_state_path",
                                   lambda: tmp_p / "market_state.json"), \
                    mock.patch.object(market_state, "_hist_path",
                                      lambda: tmp_p / "history.csv"):
                out = market_state.compute_daily(panels=_synth_panels())
            self.assertIn(out["state"], ("杀跌区", "震荡区", "主升区"))
            self.assertIsInstance(out["mkt_ret20"], float)
            self.assertIsInstance(out["breadth"], float)
            self.assertIsInstance(out["amount_wl"], float)
            self.assertIn("tech20", out)
            self.assertIn("diff", out)
            self.assertIn(out["structure"], ("科技强", "均衡", "非科技强"))
            saved = json.loads(
                (tmp_p / "market_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["date"], out["date"])
            self.assertTrue((tmp_p / "history.csv").exists())

    def test_compute_daily_ignores_garbage_prev_close(self):
        """prev_close=0 的脏数据（新股/复牌）不得虚增等权 20 日涨幅。"""
        panels = _synth_panels()
        bad = pd.DataFrame([{
            "date": panels["date"].max(), "code": "999999",
            "open": 1, "high": 1, "low": 1, "close": 503.0,
            "pct_chg": 50200.0, "prev_close": 0.0,
            "amount": 1e8, "volume": 1e6,
        }])
        panels = pd.concat([panels, bad], ignore_index=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with mock.patch.object(market_state, "_state_path",
                                   lambda: tmp_p / "market_state.json"), \
                    mock.patch.object(market_state, "_hist_path",
                                      lambda: tmp_p / "history.csv"):
                clean = market_state.compute_daily(panels=panels)
        self.assertLess(clean["mkt_ret20"], 30)   # 脏数据被剔除，不会被拉到 +500%

    def test_render_card(self):
        html = market_state.render_card({
            "state": "杀跌区", "label": "杀跌区（20日-6.3%）",
            "advice": "启动加仓全开（+量能≥1.0 过滤）｜ T0 少做",
            "color": "#3FB950", "mkt_ret20": -6.3, "index_ret20": -5.1,
            "breadth": 0.32, "amount_wl": 1.12,
            "tech20": 20.0, "other20": -5.0, "diff": 25.0,
            "structure": "科技强",
        })
        self.assertIn("杀跌区", html)
        self.assertIn("启动加仓全开", html)
        self.assertIn("等权20日", html)
        self.assertIn("科技强", html)
        self.assertIn("强弱差", html)

        empty = market_state.render_card(None)
        self.assertIn("大盘状态数据未生成", empty)

    def test_index_ret20_cache(self):
        bars = [["2026-01-01", 1, 100 + i, 1, 1, 1] for i in range(25)]

        class FakeResp:
            def json(self):
                return {"data": {"sh000001": {"qfqday": bars}}}

        with mock.patch("mainrise.market_state.requests.get",
                        return_value=FakeResp()):
            market_state.INDEX_CACHE.update({"ts": 0.0, "ret20": None})
            ret = market_state._index_ret20()
        self.assertEqual(ret, round((bars[-1][2] / bars[-21][2] - 1) * 100, 2))
        self.assertIsNotNone(market_state.INDEX_CACHE["ret20"])


if __name__ == "__main__":
    unittest.main()
