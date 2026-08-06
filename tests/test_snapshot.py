"""快照代码路由测试：股票 / ETF / 北交所 后缀识别。"""
import unittest

from mainrise.snapshot import is_etf, ths_code


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


if __name__ == "__main__":
    unittest.main()
