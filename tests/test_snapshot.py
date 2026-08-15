"""快照代码路由测试：股票 / ETF / 北交所 后缀识别。"""
import unittest
from unittest import mock

from mainrise.snapshot import fetch_snapshot, is_etf, ths_code, tx_code


class TestThsCode(unittest.TestCase):
    def test_stocks(self):
        self.assertEqual(ths_code("600519"), "600519.SH")
        self.assertEqual(ths_code("000001"), "000001.SZ")
        self.assertEqual(ths_code("300750"), "300750.SZ")
        self.assertEqual(ths_code("688981"), "688981.SH")

    def test_etf(self):
        self.assertEqual(ths_code("589020"), "589020.SH")
        self.assertEqual(ths_code("588000"), "588000.SH")
        self.assertEqual(ths_code("510300"), "510300.SH")
        self.assertEqual(ths_code("159915"), "159915.SZ")
        self.assertTrue(is_etf("589020"))
        self.assertTrue(is_etf("159915"))
        self.assertFalse(is_etf("600519"))

    def test_bj(self):
        self.assertEqual(ths_code("920001"), "920001.BJ")
        self.assertEqual(ths_code("832000"), "832000.BJ")

    def test_tx_code(self):
        self.assertEqual(tx_code("600519"), "sh600519")
        self.assertEqual(tx_code("000001"), "sz000001")
        self.assertEqual(tx_code("159915"), "sz159915")
        self.assertEqual(tx_code("920001"), "bj920001")

    def test_fetch_snapshot_parsing(self):
        f = ["1", "贵州茅台", "600519", "1700.00", "1690.00", "1695.00",
             "100", "50", "50"]
        f += ["0"] * 22                     # 9..30（时间等）
        f += ["10.00", "0.59", "1720.00", "1680.00", "1", "2", "99999.0"]
        fake = mock.Mock()
        fake.text = 'v_sh600519="' + "~".join(f) + '";'
        with mock.patch("mainrise.snapshot.requests.get", return_value=fake):
            df = fetch_snapshot(["600519"])
        row = df.iloc[0]
        self.assertEqual(row["code"], "600519")
        self.assertEqual(row["close"], 1700.0)
        self.assertAlmostEqual(row["price_change"], 10.0)
        self.assertAlmostEqual(row["price_change_ratio_pct"], 0.59)
        self.assertEqual(row["open"], 1695.0)
        self.assertEqual(row["high"], 1720.0)
        self.assertEqual(row["low"], 1680.0)
        self.assertEqual(row["volume"], 100)
        self.assertEqual(row["turnover"], 99999.0)


if __name__ == "__main__":
    unittest.main()
