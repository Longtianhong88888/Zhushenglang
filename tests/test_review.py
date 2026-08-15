"""当日复盘测试：连板/行业分组、情绪周期、SVG 趋势图、HTML 渲染、东财/本地取数。"""
import unittest
from datetime import datetime
from unittest import mock
from pathlib import Path
import shutil
import tempfile

from mainrise import review


def _item(ticker, name, cnt, reason, seal=1e8):
    return {"ticker": ticker, "name": name, "continue_day_cnt": cnt,
            "continue_day_text": f"{cnt}连板" if cnt > 1 else "首板",
            "limit_up_reason": reason, "limit_up_time": "09:30",
            "seal_money": seal, "price_change_ratio_pct": 10.0}


DAYS = ["2026-08-03", "2026-08-04", "2026-08-05",
        "2026-08-06", "2026-08-07", "2026-08-10"]


class TestReview(unittest.TestCase):
    def test_build_review_groups_and_render(self):
        pools = {d: [] for d in DAYS}
        pools["2026-08-10"] = [
            _item("000001", "平安银行", 3, "银行"),
            _item("000002", "万科A", 2, "地产"),
            _item("000003", "测试三", 1, "银行"),
        ]
        pools["2026-08-07"] = [_item("000009", "昨日首板", 1, "银行")]

        def fake_pool(date_str, key=None):
            return pools[date_str]

        def fake_dt(date_str, key=None):
            return {"trade_date": "2026-08-10", "count": 2,
                    "stock_items": [
                        {"name": "平安银行", "ticker": "000001",
                         "code": "000001", "net_value": 1e8, "buy": 3e8,
                         "sell": 2e8, "limit_reason": "日涨幅偏离值达7%"},
                        {"name": "万科A", "ticker": "000002", "code": "000002",
                         "net_value": -5e7, "buy": 1e8, "sell": 1.5e8,
                         "limit_reason": "AI"}]}

        with mock.patch.object(review, "fetch_limit_up_pool",
                               side_effect=fake_pool), \
                mock.patch.object(review, "fetch_dragon_tiger",
                                  side_effect=fake_dt), \
                mock.patch.object(review, "recent_trading_days", return_value=DAYS), \
                mock.patch.object(review, "fetch_fund_flow", return_value={
                    "minutes": [{"time": "09:31", "main": 1e8, "small": -5e7,
                                 "mid": -2e7, "big": 3e7, "super": 7e7}],
                    "latest": {"time": "09:31", "main": 1e8, "small": -5e7,
                               "mid": -2e7, "big": 3e7, "super": 7e7}}), \
                mock.patch.object(review, "fetch_sector_flow", return_value=[
                    {"name": "AI", "code": "BK0001", "chg": 2.0, "total": 50.0,
                     "main": 10.0,
                     "super": 4.0, "big": 6.0, "mid": 1.0, "small": -11.0,
                     "main_pct": 3.5}]):
            r = review.build_review("2026-08-10", key="k")

        self.assertEqual(r["by_count"][0]["board"], "3连板")
        self.assertEqual(len(r["by_count"]), 3)
        themes = {t["theme"] for t in r["by_theme"]}
        self.assertIn("银行", themes)
        self.assertIn("地产", themes)
        self.assertIn(r["metrics"]["cycle"],
                      ("冰点", "启动", "发酵", "高潮", "退潮", "震荡"))
        self.assertEqual(r["dragon"]["net_total"], 5e7)
        self.assertEqual(r["dragon"]["buy_total"], 4e8)

        html_text = review.render_review_html(r)
        self.assertIn("连板梯队", html_text)
        self.assertIn("平安银行", html_text)
        self.assertIn("<svg", html_text)
        self.assertIn("上榜净额合计", html_text)
        self.assertIn("总买入", html_text)
        self.assertIn("按行业/题材", html_text)
        self.assertIn("资金流向", html_text)
        self.assertIn("行业板块资金流", html_text)
        self.assertIn("hardRefresh('review')", html_text)
        self.assertIn('href="/stock/000001"', html_text)

    def test_em_zt_pool_parsing(self):
        fake = mock.Mock()
        fake.json.return_value = {"rc": 0, "data": {"tc": 2, "pool": [
            {"c": "300862", "n": "蓝盾光电", "lbc": 3, "fbt": 92500,
             "fund": 637629978, "hybk": "通用设备", "zdp": 19.99},
            {"c": "000001", "n": "平安银行", "lbc": 1, "fbt": 93000,
             "fund": 5e7, "hybk": "银行", "zdp": 10.02}]}}
        with mock.patch("mainrise.review.requests.get", return_value=fake):
            rows = review._em_zt_pool("2026-08-10")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["continue_day_cnt"], 3)
        self.assertEqual(rows[0]["limit_up_time"], "09:25")
        self.assertEqual(rows[0]["seal_money"], 637629978)
        self.assertEqual(rows[0]["limit_up_reason"], "通用设备")
        self.assertEqual(rows[1]["continue_day_text"], "首板")

    def test_local_limit_pool_streak(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        daily = tmp / "zzshare_daily"
        daily.mkdir()
        header = ("date,code,open,high,low,close,limit_price,low_price,"
                  "turnover,amount,pct_chg,prev_close,is_st,is_paused,volume\n")
        (daily / "20260809.csv").write_text(
            header +
            "2026-08-09,000001,10,10,10,11,11,9,1,100,10,10,0,0,100\n"
            "2026-08-09,000002,5,5,5,5.5,5.5,4.5,1,100,10,5,0,0,100\n",
            encoding="utf-8")
        (daily / "20260810.csv").write_text(
            header +
            "2026-08-10,000001,11,11,11,11,11,9,1,100,0,11,0,0,100\n"
            "2026-08-10,000002,5.5,5.5,5.5,5.8,6.05,4.5,1,100,5.5,5.5,0,0,100\n"
            "2026-08-10,000003,3,3,3,3.3,3.3,2.5,1,100,10,3,0,0,100\n",
            encoding="utf-8")
        with mock.patch.object(review.paths, "data_dir", return_value=tmp):
            review._LOCAL_STREAK.clear()
            d1 = review.local_limit_pool("2026-08-09")
            d2 = review.local_limit_pool("2026-08-10")
        self.assertEqual(len(d1), 2)
        by = {x["ticker"]: x for x in d2}
        self.assertEqual(by["000001"]["continue_day_cnt"], 2)   # 连续两日涨停
        self.assertNotIn("000002", by)                          # 当日未涨停
        self.assertEqual(by["000003"]["continue_day_cnt"], 1)   # 仅当日涨停

    def test_dragon_tiger_mapping(self):
        fake = mock.Mock()
        fake.json.return_value = {"result": {"pages": 1, "data": [
            {"TRADE_DATE": "2026-08-12 00:00:00",
             "SECURITY_CODE": "002031", "SECURITY_NAME_ABBR": "巨轮智能",
             "EXPLANATION": "日涨幅偏离值达7%的证券",
             "BILLBOARD_NET_AMT": 2.5e8, "BILLBOARD_BUY_AMT": 5e8,
             "BILLBOARD_SELL_AMT": 2.5e8}]}}
        with mock.patch("mainrise.review.requests.get", return_value=fake):
            d = review.fetch_dragon_tiger("2026-08-12")
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["stock_items"][0]["code"], "002031")
        self.assertEqual(d["stock_items"][0]["net_value"], 2.5e8)
        self.assertEqual(d["stock_items"][0]["limit_reason"],
                         "日涨幅偏离值达7%的证券")

    def test_eastmoney_fetchers(self):
        def fake_em(path, params):
            if "fflow" in path:
                return {"klines": [
                    "2026-08-10 09:31,100.0,-50.0,-20.0,30.0,70.0",
                    "2026-08-10 09:32,120.0,-60.0,-25.0,35.0,85.0"]}
            return {"diff": [{"f12": "BK0001", "f14": "AI", "f3": 2.0,
                              "f6": 5e10, "f62": 1e9, "f184": 3.5, "f66": 4e8,
                              "f72": 6e8, "f78": -11e8}]}
        with mock.patch.object(review, "_tx_sector_flow",
                               side_effect=RuntimeError("tx down")), \
                mock.patch.object(review, "_eastmoney", side_effect=fake_em):
            ff = review.fetch_fund_flow()
            self.assertEqual(len(ff["minutes"]), 2)
            self.assertEqual(ff["latest"]["main"], 240.0)  # 两市叠加
            self.assertIsNone(ff["market"])  # 腾讯聚合失败时市场净值为空
            cf = review.fetch_sector_flow(1)
            self.assertEqual(len(cf), 1)
            self.assertEqual(cf[0]["total"], 500.0)  # 总资金=成交额 50亿→5e10/1e8
            self.assertEqual(cf[0]["main"], 10.0)
            self.assertEqual(cf[0]["super"] + cf[0]["big"], cf[0]["main"])
            self.assertEqual(cf[0]["mid"], 1.0)  # -(main+small)

    def test_tx_sector_flow_parsing(self):
        fake = mock.Mock()
        fake.json.return_value = {"code": 0, "data": {"rank_list": [
            {"name": "电子", "code": "pt01", "zdf": "1.2",
             "turnover": "71113671", "zljlr": "-1965425.16"}]}}
        with mock.patch("mainrise.review.requests.get", return_value=fake):
            cf = review._tx_sector_flow(1)
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["name"], "电子")
        self.assertAlmostEqual(cf[0]["total"], 7111.4, places=1)   # 万->亿
        self.assertAlmostEqual(cf[0]["main"], -196.54, places=1)

    def test_market_flow_aggregation(self):
        with mock.patch.object(review, "_tx_sector_flow", return_value=[
                {"total": 7111.4, "main": -196.54},
                {"total": 2105.3, "main": -20.6}]):
            m = review.fetch_market_flow()
        self.assertEqual(m["boards"], 2)
        self.assertAlmostEqual(m["total"], 9216.7, places=1)
        self.assertAlmostEqual(m["main"], -217.14, places=1)

    def test_concept_board_filter_and_watch(self):
        fake = mock.Mock()
        fake.json.return_value = {"code": 0, "data": {"rank_list": [
            {"name": "昨日涨停", "turnover": "1000000", "zljlr": "1000",
             "zdf": "1", "code": "x0"},
            {"name": "芯片概念", "turnover": "100000000", "zljlr": "-1000000",
             "zdf": "2", "code": "x2"},
            {"name": "存储芯片", "turnover": "50000000", "zljlr": "50000",
             "zdf": "3", "code": "x3"},
            {"name": "有色金属", "turnover": "30000000", "zljlr": "3000",
             "zdf": "4", "code": "x4"},
        ]}}
        with mock.patch("mainrise.review.requests.get", return_value=fake):
            rows = review._tx_concept_flow(50)
        self.assertNotIn("昨日涨停", [r["name"] for r in rows])
        self.assertEqual(len(rows), 3)
        with mock.patch.object(review, "_tx_concept_flow", return_value=rows), \
                mock.patch.object(review, "_tx_sector_flow", return_value=[{
                    "name": "医药生物", "total": 2105.3, "main": -20.6,
                    "chg": 1.4}]):
            cb = review.build_concept_board()
        self.assertEqual(cb["top"][0]["name"], "芯片概念")
        watch = {w["theme"]: w["board"] for w in cb["watch"]}
        self.assertEqual(watch["存储"], "存储芯片")
        self.assertEqual(watch["有色"], "有色金属")
        self.assertEqual(watch["创新药"], "医药生物")
        sources = {w["theme"]: w.get("source") for w in cb["watch"]}
        self.assertEqual(sources["创新药"], "行业")

    def test_tokenize_reason(self):
        self.assertEqual(review.tokenize_reason("AI+算力、存储/芯片"),
                         ["AI", "算力", "存储", "芯片"])


if __name__ == "__main__":
    unittest.main()
