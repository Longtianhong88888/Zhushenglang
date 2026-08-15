"""网页仪表盘生成测试：HTML 输出包含关键区块与数据，空数据不报错。"""
import csv
import tempfile
import unittest
from pathlib import Path

from mainrise.web_dashboard import update_web_dashboard


def _watch_csv(day: str) -> list:
    header = ["code", "name", "composite", "close", "chg", "status", "hint",
              "ma10", "ma20", "vr", "chg10"]
    rows = [header]
    base = [("000001", "平安银行", 86.3), ("000002", "万科A", 81.8),
            ("000003", "测试股三", 78.0), ("000004", "测试股四", 74.5)]
    for i, (code, name, comp) in enumerate(base * 9):
        if i >= 36:
            break
        status, hint, chg10 = "空头", "均线未多头，观望", 2.0
        if code == "000002":
            status, hint, chg10 = "T1确认买点", "买点1：明日开盘买入", 5.0
        elif code == "000003":
            status, hint, chg10 = "回踩低吸", "买点2：MA10附近缩量企稳", 10.0
        rows.append([code, name, comp, 10.5, 1.0, status, hint,
                     10.0, 9.5, 1.2, chg10])
    return rows


def _positions_csv() -> list:
    return [["code", "name", "signal_date", "confirm_date", "buy_kind",
             "buy_date", "buy_price", "peak", "peak_date", "status",
             "close_date", "close_price", "reason"],
            ["000002", "万科A", "", "", 2, "2026-08-04", 10.0,
             11.0, "2026-08-05", "active", "", "", "回踩低吸"]]


class TestWebDashboard(unittest.TestCase):
    def _setup(self, td: Path) -> tuple[Path, Path]:
        reports, states = td / "reports", td / "state"
        reports.mkdir(), states.mkdir()
        for day in ("2026-08-04", "2026-08-05"):
            with open(reports / f"主升浪跟踪_{day}.csv", "w", newline="",
                      encoding="utf-8") as f:
                csv.writer(f).writerows(_watch_csv(day))
        with open(states / "mainrise_positions.csv", "w", newline="",
                  encoding="utf-8") as f:
            csv.writer(f).writerows(_positions_csv())
        return reports, states

    def test_generates_html(self):
        with tempfile.TemporaryDirectory() as td:
            reports, states = self._setup(Path(td))
            out = update_web_dashboard(reports_dir=reports, state_dir=states)
            # 门户首页
            html_text = out.read_text(encoding="utf-8")
            self.assertTrue(out.exists())
            self.assertIn("主升浪信号跟踪", html_text)
            self.assertIn("KPI 仪表盘", html_text)
            self.assertIn("实时盯盘", html_text)
            self.assertIn("每日报告", html_text)
            # KPI 仪表盘
            dash = (out.parent / "dashboard.html").read_text(encoding="utf-8")
            self.assertIn("主升浪信号跟踪 · 网页仪表盘", dash)
            self.assertIn("今日买点提示", dash)
            self.assertIn("万科A", dash)
            self.assertIn("平安银行", dash)
            self.assertIn("观察池", dash)
            self.assertIn("免责声明", dash)
            # 36 只观察池 + 2 天观察天数 + 2 个买点（万科A T1 + 测试股三 回踩）
            self.assertIn("<td>36</td>", dash)
            self.assertIn(">2</div>", dash)
            self.assertIn("T1确认买点", dash)
            # 每日报告页
            reps = (out.parent / "reports.html").read_text(encoding="utf-8")
            self.assertIn("每日跟踪报告", reps)
            # 通用代码链接脚本（点击代码看K线/分时）
            self.assertIn("/stock/", dash)
            self.assertIn("isCode", dash)
            self.assertIn("hardRefresh", dash)
            self.assertIn("hardRefresh", out.read_text(encoding="utf-8"))
            # 股票代码服务端直链（不依赖前端 JS）
            self.assertIn('href="/stock/000002"', dash)

    def test_empty_data_no_error(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            reports, states = td / "reports", td / "state"
            reports.mkdir(), states.mkdir()
            out = update_web_dashboard(reports_dir=reports, state_dir=states)
            dash = (out.parent / "dashboard.html").read_text(encoding="utf-8")
            self.assertIn("暂无数据", dash)
            self.assertIn("观察池为空", dash)
            self.assertIn("暂无数据", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
