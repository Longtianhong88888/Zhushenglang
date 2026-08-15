"""产业链涨价事件（price_events）单元测试：产品词搜索 / 标签判定 / JSON 输出。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from mainrise import paths
from mainrise import price_events as pe


def _mk_news(title: str, date: str) -> dict:
    return {"title": title, "date": date + " 10:00:00"}


class TestPriceEvents(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._tmpdir = Path(self._tmp.name)
        # 路径隔离：注入 override 根目录，测试数据写入临时目录，
        # 不污染真实 output/state 与 output/reports（见 paths._OVERRIDE）
        paths._OVERRIDE = self._tmpdir

    def tearDown(self):
        paths._OVERRIDE = None
        self._tmp.cleanup()

    def test_items_cover_user_products(self):
        """用户点名产品（光纤/覆铜板/MLCC/HBM/DDR4）必须在产品词列表内。"""
        names = {n for n, _ in pe.ITEMS}
        for p in ("光纤", "覆铜板", "MLCC", "HBM", "DDR4"):
            self.assertIn(p, names, f"产品词缺少 {p}")

    def test_search_title_filter(self):
        """标题须同时含产品词 + 涨价意图词；日期在 90 天内。"""
        today = "2026-08-14"
        res = [
            _mk_news("覆铜板厂商集体提价，行业景气度回升", "2026-08-10"),
            _mk_news("覆铜板需求旺盛，龙头扩产满产", "2026-08-01"),
            _mk_news("覆铜板行业论坛本周举办", "2026-08-05"),   # 无意图词
            _mk_news("MLCC涨价带动被动元件行情", "2026-07-01"),  # 产品词不匹配
            _mk_news("覆铜板提价通知落地", "2026-04-01"),       # 超 90 天
        ]
        kw, theme = "覆铜板", "AI硬件"
        events = pe._filter_events(res, kw, cutoff="2026-05-16", cutoff30="2026-07-15")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(kw in t for _, t in events))

    def test_theme_label_rules(self):
        """标签：量价齐升 / 持续涨价 / 价格见顶 / 启动。"""
        t = {"n90": 5, "n30": 2, "last": "2026-08-10", "items": ["覆铜板5"]}
        self.assertEqual(pe._label_for(t, ret20=3.0), "量价齐升")
        self.assertEqual(pe._label_for(t, ret20=-2.0), "持续涨价")
        t2 = {"n90": 4, "n30": 0, "last": "2026-06-01", "items": ["MLCC4"]}
        self.assertEqual(pe._label_for(t2, ret20=1.0), "价格见顶")
        t3 = {"n90": 1, "n30": 1, "last": "2026-08-12", "items": ["HBM1"]}
        self.assertEqual(pe._label_for(t3, ret20=None), "启动")

    def test_run_writes_json(self):
        """run() 全流程：mock 搜索 → JSON 含主题标签（结构兼容 bigbull）。"""
        hit = lambda kw: [  # noqa: E731
            _mk_news(f"{kw}涨价带动产业链景气回升", "2026-08-12"),
            _mk_news(f"{kw}厂商上调报价，供不应求", "2026-08-01"),
        ]
        with mock.patch.object(pe, "_search", side_effect=hit), \
             mock.patch.object(pe, "_topic_ret20", return_value=2.0):
            pe.run()
        j = json.loads((paths.state_dir() / "price_events.json").read_text("utf-8"))
        self.assertEqual(j["date"], pd.Timestamp.now().strftime("%Y-%m-%d"))
        self.assertIn("themes", j)
        for th, t in j["themes"].items():
            self.assertIn("label", t)
            self.assertIn("n90", t)
            self.assertIn("detail", t)

    def test_search_fallback_tencent(self):
        """东财被风控降级（result 无 cmsArticleWebOld）→ 回退腾讯。"""
        em = {"result": {"passportWeb": [{"x": 1}]}}  # 风控降级响应
        tx_json = {"ret": 0, "hasMore": 0, "secList": [{"newsList": [
            {"title": "MLCC涨价潮再起", "time": "2026-08-11 10:00:00"},
            {"title": "MLCC龙头满产扩产", "time": "2026-08-10 09:00:00"},
        ]}]}
        with mock.patch.object(pe, "_search_em", return_value=em), \
             mock.patch.object(pe, "_search_sogou",
                               return_value=[{"title": "MLCC涨价潮再起",
                                              "date": "2026-08-11"}]), \
             mock.patch.object(pe.time, "sleep"):
            res = pe._search("MLCC 涨价")
            # 风控响应不含 cmsArticleWebOld → 触发回退
        self.assertEqual(len(res), 1)

    def test_search_tencent_parse(self):
        """腾讯接口 JSON 解析：secList[].newsList[] → title/date ISO。"""
        tx_json = {"ret": 0, "hasMore": 0, "secList": [{"newsList": [
            {"title": "MLCC涨价", "time": "2026-08-11 10:00:00"},
        ]}]}
        with mock.patch.object(pe.requests, "get") as m:
            m.return_value.status_code = 200
            m.return_value.json.return_value = tx_json
            out = pe._search_sogou("MLCC 涨价")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "MLCC涨价")
        self.assertEqual(out[0]["date"], "2026-08-11")

    def test_em_network_error_falls_back(self):
        """东财网络异常 → _search_em 返回 None → _search 回退腾讯（M24）。"""
        with mock.patch.object(pe.requests, "get",
                               side_effect=pe.requests.ConnectionError("net down")):
            self.assertIsNone(pe._search_em("MLCC 涨价"))
        # 网络异常时 _search 走腾讯回退
        with mock.patch.object(pe, "_search_em",
                               side_effect=pe.requests.ConnectionError("net down")), \
             mock.patch.object(pe, "_search_sogou",
                               return_value=[{"title": "MLCC涨价潮再起",
                                              "date": "2026-08-11"}]), \
             mock.patch.object(pe.time, "sleep"):
            res = pe._search("MLCC 涨价")
        self.assertEqual(len(res), 1)


if __name__ == "__main__":
    unittest.main()
