"""标的详情页测试：日K/分时取数与降级、页面渲染。"""
import unittest
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from mainrise import stockpage


class TestStockPage(unittest.TestCase):
    def test_build_stock_data(self):
        def fake_ths(path, params=None, key=None):
            return {"code": 0, "data": {"item": [
                {"date_ms": 1760000000000, "open_price": 10.0,
                 "close_price": 10.5, "high_price": 10.8,
                 "low_price": 9.9, "volume": 1000},
                {"date_ms": 1760086400000, "open_price": 10.5,
                 "close_price": 10.2, "high_price": 10.9,
                 "low_price": 10.1, "volume": 1200}]}}

        def fake_em(path, params):
            return {"trends": ["2026-08-10 09:30,10.0",
                               "2026-08-10 09:31,10.2"]}

        tx_kline = [{"date": "2026-08-10", "open": 35.18, "close": 35.45,
                     "high": 35.6, "low": 34.71, "volume": 2736505}]
        with mock.patch.object(stockpage, "_tx_kline", return_value=tx_kline), \
                mock.patch.object(stockpage, "_tx_minute", return_value=[]), \
                mock.patch.object(stockpage, "_signals_for", return_value=[]), \
                mock.patch.object(stockpage, "_closes_for", return_value=[]), \
                mock.patch.object(stockpage, "_em_fast", side_effect=fake_em):
            d = stockpage.build_stock_data("601899")
        self.assertEqual(len(d["kline"]), 1)
        self.assertEqual(d["kline"][0]["close"], 35.45)
        self.assertEqual(len(d["trends"]), 2)
        self.assertEqual(d["trends"][1]["price"], 10.2)
        self.assertEqual(d["error"], "")

        # 东财失败不影响日K（降级）
        with mock.patch.object(stockpage, "_tx_kline", return_value=tx_kline), \
                mock.patch.object(stockpage, "_tx_minute",
                                  side_effect=RuntimeError("tx down")), \
                mock.patch.object(stockpage, "_signals_for", return_value=[]), \
                mock.patch.object(stockpage, "_closes_for", return_value=[]), \
                mock.patch.object(stockpage, "_em_fast",
                                  side_effect=RuntimeError("em down")):
            d2 = stockpage.build_stock_data("601899")
        self.assertEqual(len(d2["kline"]), 1)
        self.assertIn("分时", d2["error"])

        # 腾讯K线失败 -> 日K 置空并提示（不再回退同花顺）
        with mock.patch.object(stockpage, "_tx_kline",
                               side_effect=RuntimeError("tx down")), \
                mock.patch.object(stockpage, "_signals_for", return_value=[]), \
                mock.patch.object(stockpage, "_closes_for", return_value=[]), \
                mock.patch.object(stockpage, "_tx_minute", return_value=[]), \
                mock.patch.object(stockpage, "_em_fast", side_effect=fake_em):
            d3 = stockpage.build_stock_data("601899")
        self.assertEqual(len(d3["kline"]), 0)
        self.assertIn("日K", d3["error"])

    def test_page_html(self):
        html_text = stockpage.stock_page_html("601899", "紫金矿业")
        self.assertIn("紫金矿业", html_text)
        self.assertIn("drawK", html_text)
        self.assertIn("drawT", html_text)
        self.assertIn("hardRefresh", html_text)
        # KLineChart 套件已内联（无 CDN），信号标注走 simpleAnnotation 覆盖物
        self.assertIn("klinecharts.init", html_text)
        self.assertIn("simpleAnnotation", html_text)
        self.assertIn("initKChart", html_text)
        self.assertIn("setDataLoader", html_text)
        self.assertNotIn("<script><script>", html_text)   # 防双包标签导致库被截断
        self.assertEqual(html_text.count("<script>"),
                         html_text.count("</script>"))   # script 标签成对闭合

    def test_page_html_fallback_without_klinecharts(self):
        """resources/klinecharts.min.js 缺失时回退旧版 SVG 渲染（不白屏）。"""
        with mock.patch.object(stockpage, "KLC_FILE",
                               stockpage.Path("/nonexistent/klinecharts.min.js")):
            stockpage._KLC_CACHE["js"] = None
            html_text = stockpage.stock_page_html("601899", "紫金矿业")
        self.assertIn("drawK", html_text)
        self.assertIn("arrR", html_text)      # 旧版 T0/T1/T2 红色箭头
        self.assertIn("linReg", html_text)    # 旧版蓝色趋势线
        stockpage._KLC_CACHE["js"] = None     # 恢复缓存，避免影响后续用例

    def test_signals_mapping(self):
        kline = [
            {"date": "2026-08-03", "open": 1, "close": 1, "high": 1,
             "low": 1, "volume": 1},
            {"date": "2026-08-04", "open": 1, "close": 1, "high": 1,
             "low": 1, "volume": 1},
            {"date": "2026-08-05", "open": 1, "close": 1, "high": 1,
             "low": 1, "volume": 1},
            {"date": "2026-08-06", "open": 1, "close": 1, "high": 1,
             "low": 1, "volume": 1},
        ]
        with mock.patch.object(stockpage, "_tx_kline", return_value=kline), \
                mock.patch.object(stockpage, "_tx_minute", return_value=[]), \
                mock.patch.object(stockpage, "_em_fast",
                                  return_value={"trends": []}), \
                mock.patch.object(stockpage, "_signals_for", return_value=[
                    {"S_date": "2026-08-04", "buy_date": "2026-08-06"}]), \
                mock.patch.object(stockpage, "_closes_for", return_value=[
                    {"date": "2026-08-06", "reason": "高点回落8%"}]):
            d = stockpage.build_stock_data("601899")
        self.assertEqual(d["signals"], [
            {"t0": "2026-08-04", "t1": "2026-08-05", "t2": "2026-08-06"}])
        self.assertEqual(d["closes"], [
            {"date": "2026-08-06", "reason": "高点回落8%"}])

    def test_refresh_page_routing(self):
        with mock.patch("mainrise.web_dashboard.update_web_dashboard",
                        return_value=object()):
            r = stockpage.refresh_page("web")
        self.assertTrue(r["ok"])
        self.assertEqual(r["page"], "web")
        self.assertFalse(stockpage.refresh_page("unknown")["ok"])

    def test_warm_and_recent(self):
        with mock.patch.object(stockpage, "fetch_daily_kline",
                               return_value=[{"date": "2026-08-10",
                                              "close": 10.0}]), \
                mock.patch.object(stockpage, "fetch_minute_trend",
                                  return_value=[{"time": "09:30",
                                                 "price": 10.0}]):
            stockpage.warm_kline("601899")
            stockpage.warm_trends("601899")
        data = stockpage.get_stock_data("601899")  # 命中预热缓存
        self.assertEqual(len(data["kline"]), 1)
        self.assertEqual(len(data["trends"]), 1)
        self.assertIn("601899", stockpage.recent_viewed())

    def test_minute_series_append_and_backfill(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        with mock.patch.object(stockpage.paths, "state_dir",
                               return_value=tmp):
            stockpage.load_minutes()
            stockpage.append_minute_point(
                "601899", 10.0, datetime(2026, 8, 10, 10, 0, 0))
            stockpage.append_minute_point(
                "601899", 10.1, datetime(2026, 8, 10, 10, 0, 3))
            stockpage.save_minutes()
            stockpage.MIN_SERIES.clear()
            stockpage.load_minutes()
        self.assertEqual(len(stockpage.minute_series("601899")), 2)
        with mock.patch.object(stockpage, "_tx_minute", return_value=[
                {"time": "09:30", "price": 9.5},
                {"time": "09:31", "price": 9.6}]):
            ok = stockpage.backfill_minutes(["601899"])
        self.assertEqual(ok, 1)
        self.assertEqual(stockpage.minute_series("601899")[0],
                         ["09:30", 9.5])

    def test_tx_minute_parsing(self):
        fake = mock.Mock()
        fake.json.return_value = {"code": 0, "data": {"sh601899": {
            "data": {"data": ["0930 10.00 100 1000.0",
                              "0931 10.05 200 2000.0",
                              "1530 10.10 300 3000.0"]}}}}
        with mock.patch("mainrise.stockpage.requests.get",
                        return_value=fake):
            rows = stockpage._tx_minute("601899")
        self.assertEqual(len(rows), 2)   # 15:30 收盘后快照被过滤
        self.assertEqual(rows[0]["time"], "09:30")
        self.assertEqual(rows[1]["price"], 10.05)
        self.assertEqual(rows[0]["volume"], 100)
        self.assertEqual(rows[1]["amount"], 2000.0)


if __name__ == "__main__":
    unittest.main()
