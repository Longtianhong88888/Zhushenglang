"""主升浪信号跟踪软件（PyQt5 图形界面）。

界面稳定渲染（macOS Tk 不可靠，改用 PyQt5）：
  数据: 初始化全量 / 更新行情
  模型: 回测 / 财务评估 / 综合评分
  跟踪: 每日跟踪 / 实时行情快照
  报告: 打开报告目录 / 打开最新报告
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt5.QtCore import QDir, QLockFile, QObject, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from mainrise import paths
from mainrise.help_content import PAGES

SETTINGS_PATH = Path.home() / ".mainrise" / "settings.json"


def _apply_saved_settings() -> None:
    """启动时读取上次的数据目录与 API Key（须在 paths.home() 首次调用前执行）。"""
    try:
        d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if d.get("home"):
            os.environ["MAINRISE_HOME"] = d["home"]
        if d.get("api_key"):
            os.environ["MAINRISE_API_KEY"] = d["api_key"]
    except Exception:  # noqa: BLE001
        pass


def _save_settings(api_key: str = "") -> None:
    """保存数据目录与 API Key（~/.mainrise/settings.json，权限 600）。"""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps({
            "home": os.environ.get("MAINRISE_HOME", ""),
            "api_key": api_key,
        }), encoding="utf-8")
        try:
            os.chmod(SETTINGS_PATH, 0o600)  # 仅当前用户可读写
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def data_status() -> tuple[int, str] | None:
    """返回 (缓存天数, 最新日期)，无数据返回 None。"""
    files = sorted(paths.zzshare_dir().glob("[0-9]*.csv"))
    if not files:
        return None
    latest = files[-1].stem
    return len(files), f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"


class Worker(QObject):
    """跨线程日志信号：任务线程 emit，主线程接收刷新界面。"""
    log = pyqtSignal(str)
    replace = pyqtSignal(str)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    snapshot_data = pyqtSignal(object)


class SignalWriter:
    """把 print/tqdm 输出转发到 Worker 信号。"""

    def __init__(self, worker: Worker):
        self.worker = worker

    def write(self, s: str) -> int:
        for line in s.split("\n"):
            if line.endswith("\r"):
                self.worker.replace.emit(line.rstrip("\r"))
            elif line:
                self.worker.log.emit(line)
        return len(s)

    def flush(self) -> None:
        pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = Worker()
        self.busy = False
        self.setWindowTitle("主升浪信号跟踪")
        self.resize(1020, 680)
        self.setMinimumSize(900, 580)
        self.data_stat = data_status()
        self._connect_signals()
        self._build_ui()
        self._refresh_data_label()

    def _connect_signals(self) -> None:
        self.worker.log.connect(self._append_log)
        self.worker.replace.connect(self._replace_last)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.snapshot_data.connect(self._show_snapshot_table)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 顶部：数据目录 / API Key
        top = QHBoxLayout()
        top.addWidget(QLabel("数据目录:"))
        self.home_lbl = QLabel(str(paths.home()))
        self.home_lbl.setStyleSheet("color:#2563eb;")
        top.addWidget(self.home_lbl)
        self.data_lbl = QLabel()
        self.data_lbl.setStyleSheet("color:#16a34a;")
        top.addWidget(self.data_lbl)
        btn_change = QPushButton("更改")
        btn_change.clicked.connect(self._pick_home)
        top.addWidget(btn_change)
        top.addSpacing(20)
        top.addWidget(QLabel("API Key:"))
        self.key_edit = QLineEdit(os.environ.get("MAINRISE_API_KEY", ""))
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setFixedWidth(240)
        self.key_edit.editingFinished.connect(self._save_key)
        top.addWidget(self.key_edit)
        btn_help = QPushButton("使用说明")
        btn_help.clicked.connect(self._open_help)
        top.addWidget(btn_help)
        top.addStretch(1)
        root.addLayout(top)

        menubar = self.menuBar()
        help_menu = menubar.addMenu("帮助")
        act_help = help_menu.addAction("使用说明")
        act_help.triggered.connect(self._open_help)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # 左侧按钮面板
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 8, 0)
        self._add_group(lv, "数据", [
            ("初始化全量数据", self._task_init),
            ("更新行情", self._task_update),
        ])
        self._add_group(lv, "模型分析", [
            ("回测", self._task_backtest),
            ("财务评估", self._task_evaluate),
            ("综合评分", self._task_report),
        ])
        self._add_group(lv, "每日跟踪", [
            ("生成跟踪报告", self._task_track),
        ])
        snap_box = QGroupBox("实时行情")
        sv = QVBoxLayout(snap_box)
        sv.addWidget(QLabel("代码(空格分隔):"))
        self.codes_edit = QLineEdit("601899 000975 600489")
        self.codes_edit.returnPressed.connect(self._task_snapshot)
        sv.addWidget(self.codes_edit)
        btn_snap = QPushButton("查询行情")
        btn_snap.clicked.connect(self._task_snapshot)
        sv.addWidget(btn_snap)
        lv.addWidget(snap_box)
        self._add_group(lv, "报告", [
            ("更新仪表盘", self._task_dashboard),
            ("打开报告目录", self._open_reports),
            ("打开最新跟踪报告", self._open_latest),
            ("打开 Excel 报告", self._open_latest_excel),
        ])
        lv.addStretch(1)
        splitter.addWidget(left)

        # 右侧日志区
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Menlo", 11))
        splitter.addWidget(self.log)
        splitter.setSizes([280, 720])

        disclaimer = QLabel(
            "⚠ 免责声明：本软件所有输出（信号、评分、买点提示、报告）仅用于研究学习，"
            "不构成任何投资建议，请勿作为直接投资依据。股市有风险，决策需谨慎。")
        disclaimer.setStyleSheet(
            "color:#9a9a9a; background:#f5f5f5; padding:6px 10px; border-top:1px solid #e0e0e0;")
        disclaimer.setWordWrap(True)
        root.addWidget(disclaimer)

        self.statusBar().showMessage("就绪")

    def _refresh_data_label(self) -> None:
        self.data_stat = data_status()
        if self.data_stat is None:
            self.data_lbl.setText("数据: 无（请选择目录或初始化）")
            self.data_lbl.setStyleSheet("color:#dc2626;")
        else:
            days, latest = self.data_stat
            self.data_lbl.setText(f"数据: {days} 天 (至 {latest})")
            self.data_lbl.setStyleSheet("color:#16a34a;")

    def prompt_data_setup(self) -> None:
        """启动时无数据：引导选择目录或初始化。"""
        if self.data_stat is not None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("未找到行情数据")
        box.setText(
            f"软件在 {paths.home()} 未找到行情数据。\n\n"
            "选择已有数据目录（例如项目根目录 /Users/user/Desktop/chaoduan），\n"
            "或直接初始化下载新数据（约 650MB，需联网）。")
        b_choose = box.addButton("选择已有数据", QMessageBox.AcceptRole)
        b_init = box.addButton("初始化新数据", QMessageBox.ActionRole)
        b_later = box.addButton("稍后再说", QMessageBox.RejectRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is b_choose:
            self._pick_home()
            if self.data_stat is None:
                self.prompt_data_setup()
        elif clicked is b_init:
            self._task_init()

    def _add_group(self, parent, title: str, items) -> None:
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        for text, cb in items:
            b = QPushButton(text)
            b.clicked.connect(cb)
            v.addWidget(b)
        parent.addWidget(box)

    # ---------- 任务执行 ----------
    def _run(self, fn, label: str, need_key: bool = False) -> None:
        if self.busy:
            QMessageBox.information(self, "提示", "有任务正在运行，请等待完成")
            return
        if need_key and not self.key_edit.text().strip():
            QMessageBox.warning(self, "缺少 API Key",
                                "请先在上方填写 API Key（财务评估/实时行情需要）")
            return
        os.environ["MAINRISE_API_KEY"] = self.key_edit.text().strip()
        self.busy = True
        self.statusBar().showMessage(f"运行中: {label}")
        self._append_log(f"\n===== {label} =====")
        t = threading.Thread(target=self._worker_run, args=(fn, label), daemon=True)
        t.start()

    def _worker_run(self, fn, label: str) -> None:
        writer = SignalWriter(self.worker)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                fn()
            self.worker.done.emit(label)
        except BaseException as e:  # noqa: BLE001  (含 SystemExit，避免线程静默卡死)
            self.worker.log.emit(f"错误: {type(e).__name__}: {e}")
            self.worker.failed.emit(label)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _replace_last(self, text: str) -> None:
        now = time.monotonic()
        if now - getattr(self, "_last_replace", 0) < 0.15:
            return  # 进度条限频，避免高频重绘卡界面
        self._last_replace = now
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.select(QTextCursor.LineUnderCursor)
        cursor.removeSelectedText()
        cursor.insertText(text)
        self.log.setTextCursor(cursor)

    def _on_done(self, label: str) -> None:
        self.busy = False
        self.statusBar().showMessage(f"完成: {label}")
        self._append_log(f"✔ {label} 完成")
        if label == "解压内置行情数据":
            self._refresh_data_label()

    def _on_failed(self, label: str) -> None:
        self.busy = False
        self.statusBar().showMessage(f"失败: {label}")

    # ---------- 功能 ----------
    def _do_init(self) -> None:
        from mainrise import data
        paths.ensure_dirs()
        print(f"数据目录: {paths.home()}")
        print("拉取交易日历...")
        print(f"交易日历: {data.init_calendar()} 天 (2021 起)")
        print("拉取股票名册...")
        print(f"股票名册: {data.init_stock_list()} 只")
        ok, empty = data.fetch_all_panels("2021-01-01")
        print(f"初始化完成: {len(ok)} 个交易日有数据，{len(empty)} 空")
        print("下一步: 生成跟踪报告")

    def _do_update(self) -> None:
        from mainrise import data
        cached = sorted(p.stem for p in paths.zzshare_dir().glob("[0-9]*.csv"))
        start = cached[-1][:4] + "-" + cached[-1][4:6] + "-" + cached[-1][6:] if cached \
            else data.latest_trading_day()
        ok, empty = data.fetch_all_panels(start)
        print(f"更新完成: {len(ok)} 个交易日有数据，{len(empty)} 空/滞后")

    def _do_backtest(self) -> None:
        from mainrise import backtest
        backtest.run()

    def _do_evaluate(self) -> None:
        from mainrise import evaluate
        evaluate.run()

    def _do_report(self) -> None:
        from mainrise import report
        report.run()

    def _do_track(self) -> None:
        from mainrise import tracker
        out = tracker.run()
        print(f"跟踪报告: {out['report']}")
        print(f"Excel报告: {out.get('excel', '-')}")
        print(f"持仓: {out['active']} 活跃 / {out['pending']} 待买入 / {out['closed']} 已平仓")
        for _, r in out["buy_points"].iterrows():
            print(f"  {r['code']} {r['name']} [{r['status']}] {r['hint']}")
        try:
            from mainrise import dashboard
            dash = dashboard.update_dashboard()
            print(f"仪表盘已同步: {dash}")
        except Exception as exc:
            print(f"⚠ 仪表盘更新失败: {exc}")

    def _do_dashboard(self) -> None:
        from mainrise import dashboard
        dash = dashboard.update_dashboard()
        print(f"仪表盘已更新: {dash}")

    def try_bundled_data(self) -> bool:
        """数据目录无行情时，尝试解压软件内置的数据包。返回是否有内置包可解压。"""
        from mainrise import data
        if data.bundled_zip_path() is None:
            return False
        self._run(self._do_unpack, "解压内置行情数据")
        return True

    def _do_unpack(self) -> None:
        from mainrise import data
        data.ensure_bundled_data()

    def _do_snapshot(self, codes) -> None:
        from mainrise import snapshot
        df = snapshot.fetch_snapshot(codes)
        if df.empty:
            print("快照为空")
            return
        print(df.to_string(index=False))
        self.worker.snapshot_data.emit(df)

    def _build_snapshot_table(self, df):
        """纵向行情表：行=字段，列=股票（避免横向表格错位）。"""
        from PyQt5.QtWidgets import QTableWidget
        fields = [
            ("code", "代码"),
            ("close", "现价"),
            ("price_change", "涨跌额"),
            ("price_change_ratio_pct", "涨跌幅%"),
            ("open", "今开"),
            ("high", "最高"),
            ("low", "最低"),
            ("prev_close", "昨收"),
            ("volume", "成交量"),
            ("turnover", "成交额"),
            ("error", "说明"),
        ]
        table = QTableWidget(len(fields), len(df))
        table.setVerticalHeaderLabels([label for _, label in fields])
        table.setHorizontalHeaderLabels([str(c) for c in df["code"]])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setDefaultSectionSize(30)
        for ci in range(len(df)):
            r = df.iloc[ci]
            for ri, (field, _) in enumerate(fields):
                v = r.get(field)
                text = "-"
                if isinstance(v, float):
                    if not math.isnan(v):
                        if field in ("volume",):
                            text = f"{v:,.0f}"
                        elif field == "turnover":
                            text = f"{v / 1e8:.2f} 亿"
                        else:
                            text = f"{v:.2f}"
                elif v is not None and str(v).strip():
                    text = str(v)
                if field == "price_change_ratio_pct" and text != "-":
                    try:
                        pct = float(v)
                        item = QTableWidgetItem(text)
                        item.setForeground(
                            __import__("PyQt5.QtGui", fromlist=["QColor"]).QColor(
                                "#c00000" if pct > 0 else ("#008000" if pct < 0 else "#333333")))
                        table.setItem(ri, ci, item)
                        continue
                    except (TypeError, ValueError):
                        pass
                elif field == "error" and text != "-":
                    item = QTableWidgetItem(text)
                    item.setForeground(
                        __import__("PyQt5.QtGui", fromlist=["QColor"]).QColor("#c00000"))
                    table.setItem(ri, ci, item)
                    continue
                table.setItem(ri, ci, QTableWidgetItem(text))
        return table

    def _show_snapshot_table(self, df) -> None:
        from PyQt5.QtWidgets import QDialog, QVBoxLayout
        win = QDialog(self)
        win.setWindowTitle("实时行情")
        win.resize(680, 460)
        lay = QVBoxLayout(win)
        table = self._build_snapshot_table(df)
        lay.addWidget(table)
        win.exec_()

    def _task_init(self) -> None:
        self._run(self._do_init, "初始化全量数据")

    def _task_update(self) -> None:
        self._run(self._do_update, "更新行情")

    def _task_backtest(self) -> None:
        self._run(self._do_backtest, "回测")

    def _task_evaluate(self) -> None:
        self._run(self._do_evaluate, "财务评估", need_key=True)

    def _task_report(self) -> None:
        self._run(self._do_report, "综合评分")

    def _task_track(self) -> None:
        self._run(self._do_track, "生成跟踪报告")

    def _task_dashboard(self) -> None:
        self._run(self._do_dashboard, "更新仪表盘")

    def _task_snapshot(self) -> None:
        codes = [c.strip() for c in self.codes_edit.text().replace(",", " ").split()
                 if c.strip()]
        if not codes:
            QMessageBox.warning(self, "提示", "请输入股票代码")
            return
        self._run(lambda: self._do_snapshot(codes), "实时行情", need_key=True)

    def _pick_home(self) -> None:
        p = QFileDialog.getExistingDirectory(
            self, "选择数据目录（选项目根目录可复用现有数据）")
        if p:
            os.environ["MAINRISE_HOME"] = p
            self.home_lbl.setText(str(paths.home()))
            self._refresh_data_label()
            _save_settings(self.key_edit.text().strip())

    def _save_key(self) -> None:
        """输入框编辑完成后保存 API Key，下次启动自动填入。"""
        key = self.key_edit.text().strip()
        os.environ["MAINRISE_API_KEY"] = key
        _save_settings(key)
        self.statusBar().showMessage("API Key 已保存（下次启动自动填入）")

    def _open_reports(self) -> None:
        d = paths.report_dir()
        d.mkdir(parents=True, exist_ok=True)
        self._open_path(d)

    def _open_help(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("使用说明")
        dlg.resize(780, 580)
        lay = QVBoxLayout(dlg)
        tabs = QTabWidget(dlg)
        for title, html in PAGES:
            browser = QTextBrowser()
            browser.setHtml(html)
            browser.setOpenExternalLinks(True)
            tabs.addTab(browser, title)
        lay.addWidget(tabs)
        dlg.exec_()

    def _open_latest(self) -> None:
        cands = sorted(paths.report_dir().glob("主升浪跟踪_*.md"))
        if not cands:
            QMessageBox.information(self, "提示",
                                    "还没有跟踪报告，请先运行【生成跟踪报告】")
            return
        self._open_path(cands[-1])

    def _open_latest_excel(self) -> None:
        cands = sorted(paths.report_dir().glob("主升浪跟踪_*.xlsx"))
        if not cands:
            QMessageBox.information(self, "提示",
                                    "还没有 Excel 报告，请先运行【生成跟踪报告】")
            return
        self._open_path(cands[-1])

    @staticmethod
    def _open_path(p: Path) -> None:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif os.name == "nt":
                subprocess.Popen(["start", str(p)], shell=True)
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(None, "打开失败", str(e))


def main() -> None:
    import time
    t0 = time.time()
    # 单实例保护：已有实例运行时，新启动的实例直接退出，避免重复打开
    lock = QLockFile(QDir.temp().filePath("mainrise_quant_gui.lock"))
    lock.setStaleLockTime(60000)  # 60 秒后自动过期：异常退出不阻塞下次启动
    if not lock.tryLock(0):
        print("主升浪跟踪已在运行，本实例自动退出")
        return
    _apply_saved_settings()
    app = QApplication(sys.argv)
    app.setApplicationName("主升浪信号跟踪")
    win = MainWindow()
    if win.data_stat is None and not win.try_bundled_data():
        win.prompt_data_setup()
    win.show()
    try:
        with open("/tmp/mainrise_boot.log", "a") as f:
            f.write(f"UI ready in {time.time() - t0:.2f}s\n")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(app.exec_())
    lock.unlock()


if __name__ == "__main__":
    main()
