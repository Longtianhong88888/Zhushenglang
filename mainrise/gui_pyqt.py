"""主升浪信号跟踪软件（PyQt5 图形界面，Apple 风格）。

界面稳定渲染（macOS Tk 不可靠，改用 PyQt5）：
  数据: 初始化全量 / 更新行情
  模型: 回测 / 财务评估 / 综合评分
  跟踪: 每日跟踪 / 实时行情快照
  报告: 打开报告目录 / 打开最新报告

设计规范（Apple 风格卡片式）：
  背景 #F5F5F7、卡片白、主色 #007AFF，窗口按屏幕可用区域 80% 动态计算，
  启动画面与主窗口同尺寸（cover 裁切填满），淡出过渡衔接主窗口。
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt5.QtCore import QDir, QEasingCurve, QLockFile, QObject, QPropertyAnimation, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPixmap, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSplashScreen,
    QSizePolicy,
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


def resource_path(name: str) -> Path:
    """定位打包资源：开发环境用 mainrise/resources，PyInstaller 用 _MEIPASS/mainrise/resources。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS) / "mainrise" / "resources"
    else:
        base = Path(__file__).resolve().parent / "resources"
    return base / name


# ── Apple 设计体系 ──────────────────────────────────────────────
C_BG     = "#F5F5F7"   # 窗口背景（Apple 标志性浅灰）
C_CARD   = "#FFFFFF"   # 卡片底色
C_PRIME  = "#007AFF"   # 主色调（Apple Blue）
C_PRIME_H= "#0062CC"   # 主色调 hover
C_TEXT   = "#1D1D1F"   # 主文字
C_SUB    = "#86868B"   # 辅助文字
C_BORDER = "#E5E5EA"   # 边框/分割线
C_INPUT_BG = "#F9F9F9" # 输入框底板

# 字体（macOS → SF Pro / PingFang，Windows → Segoe UI / 微软雅黑）
FONT_FAMILY = (
    '"Helvetica Neue", "PingFang SC", "Segoe UI", "Microsoft YaHei", sans-serif'
)
FONT_MONO = '"Menlo", "Consolas", "Cascadia Code", "SF Mono", monospace'

# 间距
GAP_SECTION = 10       # 卡片间距
GAP_ROW     = 6        # 行内控件间距
GAP_INNER   = 6        # 标签-控件间距
CARD_PAD    = 12       # 卡片内边距
RADIUS      = 8        # 圆角

WINDOW_DEFAULT_SIZE = (1020, 680)
WINDOW_SCREEN_RATIO = 0.8  # 主窗口占屏幕可用区域的百分比
WINDOW_MIN = (900, 620)    # 最小窗口尺寸


def window_target_size(ratio: float = WINDOW_SCREEN_RATIO) -> tuple[int, int]:
    """按当前屏幕可用区域计算主窗口目标尺寸（默认 80%）。"""
    screen = QApplication.primaryScreen()
    if screen is None:
        return WINDOW_DEFAULT_SIZE
    geo = screen.availableGeometry()
    w = max(WINDOW_MIN[0], int(geo.width() * ratio))
    h = max(WINDOW_MIN[1], int(geo.height() * ratio))
    return w, h


# ── 全局样式表 ──────────────────────────────────────────────────
APPLE_QSS = f"""
/* ─── 全局 ─── */
QMainWindow, QDialog {{
    background-color: {C_BG};
}}
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: 13px;
    color: {C_TEXT};
}}

/* ─── 卡片容器 ─── */
QWidget[card="true"] {{
    background-color: {C_CARD};
    border: 1px solid {C_BORDER};
    border-radius: {RADIUS}px;
}}

/* ─── 分组卡片（左侧功能区） ─── */
QGroupBox {{
    background-color: {C_CARD};
    border: 1px solid {C_BORDER};
    border-radius: {RADIUS}px;
    margin-top: 16px;
    padding: 6px 4px 4px 4px;
    font-weight: 600;
    font-size: 13px;
    color: {C_TEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 1px;
    padding: 0 6px;
}}

/* ─── 标签 ─── */
QLabel[heading="true"] {{
    font-size: 15px;
    font-weight: bold;
    color: {C_TEXT};
    padding: 0;
}}
QLabel[subtitle="true"] {{
    font-size: 12px;
    color: {C_SUB};
}}

/* ─── 主按钮（实心蓝） ─── */
QPushButton[primary="true"] {{
    background-color: {C_PRIME};
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 7px 18px;
    min-height: 30px;
    font-weight: bold;
    font-size: 13px;
}}
QPushButton[primary="true"]:hover {{
    background-color: {C_PRIME_H};
}}
QPushButton[primary="true"]:pressed {{
    background-color: #0055AA;
}}
QPushButton[primary="true"]:disabled {{
    background-color: #B8D4FF;
    color: #FFFFFF;
}}

/* ─── 次按钮（浅灰底，默认按钮样式） ─── */
QPushButton {{
    background-color: #F0F0F2;
    color: {C_TEXT};
    border: none;
    border-radius: 6px;
    padding: 6px 12px;
    min-height: 30px;
    font-size: 13px;
}}
QPushButton:hover {{
    background-color: #E4E4E8;
}}
QPushButton:pressed {{
    background-color: #D8D8DC;
}}
QPushButton:disabled {{
    background-color: #F0F0F2;
    color: #AEAEB2;
}}

/* ─── 文字按钮（蓝色链接风） ─── */
QPushButton[link="true"] {{
    background: transparent;
    color: {C_PRIME};
    border: none;
    padding: 6px 12px;
    font-size: 13px;
}}
QPushButton[link="true"]:hover {{
    color: {C_PRIME_H};
    text-decoration: underline;
}}

/* ─── 输入框 ─── */
QLineEdit {{
    background-color: {C_INPUT_BG};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
    color: {C_TEXT};
}}
QLineEdit:focus {{
    border: 1.5px solid {C_PRIME};
    background-color: #FFFFFF;
}}
QLineEdit[readOnly="true"] {{
    background-color: {C_BG};
    color: {C_SUB};
}}

/* ─── 日志区 ─── */
QPlainTextEdit {{
    background-color: #FAFAFA;
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    padding: 8px;
    font-family: {FONT_MONO};
    font-size: 12px;
    color: {C_TEXT};
}}

/* ─── 行情表格 ─── */
QTableWidget {{
    background-color: #FFFFFF;
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    gridline-color: {C_BORDER};
}}
QHeaderView::section {{
    background-color: {C_BG};
    border: none;
    border-bottom: 1px solid {C_BORDER};
    padding: 6px 8px;
    font-weight: 600;
    color: {C_SUB};
}}

/* ─── 滚动条 ─── */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #D0D0D6;
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #B0B0B6;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

/* ─── 分割条 ─── */
QSplitter::handle {{
    background: transparent;
}}

/* ─── 菜单栏 ─── */
QMenuBar {{
    background-color: #FFFFFF;
    border-bottom: 1px solid {C_BORDER};
    padding: 2px 0;
}}
QMenuBar::item {{
    padding: 6px 14px;
    border-radius: 5px;
}}
QMenuBar::item:selected {{
    background-color: #F0F0F2;
}}
QMenu {{
    background-color: #FFFFFF;
    border: 1px solid {C_BORDER};
    border-radius: 8px;
    padding: 6px 0;
}}
QMenu::item {{
    padding: 7px 32px 7px 20px;
}}
QMenu::item:selected {{
    background-color: {C_PRIME};
    color: #FFFFFF;
    border-radius: 4px;
}}

/* ─── 状态栏 ─── */
QStatusBar {{
    background-color: #FFFFFF;
    border-top: 1px solid {C_BORDER};
    font-size: 12px;
    color: {C_SUB};
    padding: 2px 12px;
}}

/* ─── 提示框 ─── */
QToolTip {{
    background-color: #FFFFFF;
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    color: {C_TEXT};
}}
"""


def _card(title: str, layout_or_widget) -> QWidget:
    """创建 Apple 风格卡片：白色圆角背景 + 可选标题。"""
    card = QWidget()
    card.setProperty("card", True)
    card.setAttribute(Qt.WA_StyledBackground, True)
    inner = QVBoxLayout(card)
    inner.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
    inner.setSpacing(GAP_ROW)
    if title:
        heading = QLabel(title)
        heading.setProperty("heading", True)
        inner.addWidget(heading)
    if isinstance(layout_or_widget, QLayout):
        inner.addLayout(layout_or_widget, 1)
    else:
        inner.addWidget(layout_or_widget, 1)
    return card


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
        w, h = window_target_size()
        self.resize(w, h)
        self.setMinimumSize(*WINDOW_MIN)
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
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(GAP_SECTION)

        # ─── 设置卡片：数据目录 / API Key / 使用说明 ───
        settings = QWidget()
        settings.setProperty("card", True)
        settings.setAttribute(Qt.WA_StyledBackground, True)
        grid = QGridLayout(settings)
        grid.setContentsMargins(CARD_PAD, 10, CARD_PAD, 10)
        grid.setVerticalSpacing(GAP_ROW)
        grid.setHorizontalSpacing(GAP_INNER)
        grid.setColumnStretch(1, 1)

        grid.addWidget(QLabel("数据目录"), 0, 0)
        self.home_lbl = QLabel(str(paths.home()))
        self.home_lbl.setStyleSheet(f"color:{C_PRIME};")
        grid.addWidget(self.home_lbl, 0, 1)
        btn_change = QPushButton("更改")
        btn_change.setProperty("secondary", True)
        btn_change.clicked.connect(self._pick_home)
        grid.addWidget(btn_change, 0, 2)
        btn_help = QPushButton("使用说明")
        btn_help.setProperty("link", True)
        btn_help.clicked.connect(self._open_help)
        grid.addWidget(btn_help, 0, 3)

        grid.addWidget(QLabel("API Key"), 1, 0)
        self.key_edit = QLineEdit(os.environ.get("MAINRISE_API_KEY", ""))
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("API Key（腾讯/东财公开接口已无需填写）")
        self.key_edit.editingFinished.connect(self._save_key)
        grid.addWidget(self.key_edit, 1, 1)
        key_hint = QLabel("回车即保存，下次启动自动填入")
        key_hint.setProperty("subtitle", True)
        grid.addWidget(key_hint, 1, 2, 1, 2)

        self.data_lbl = QLabel()
        self.data_lbl.setProperty("subtitle", True)
        grid.addWidget(self.data_lbl, 2, 0, 1, 4)
        root.addWidget(settings)

        menubar = self.menuBar()
        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("使用说明", self._open_help)
        help_menu.addAction("关于", self._show_about)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(10)
        root.addWidget(splitter, 1)

        # 左侧功能卡片区：每行两列按钮，行间 stretch 均分高度，紧凑不压扁；
        # 窗口极矮时滚动兜底
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(290)
        left_content = QWidget()
        lv = QVBoxLayout(left_content)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        func_card = QWidget()
        func_card.setProperty("card", True)
        func_card.setAttribute(Qt.WA_StyledBackground, True)
        fv = QVBoxLayout(func_card)
        fv.setContentsMargins(CARD_PAD, 10, CARD_PAD, CARD_PAD)
        fv.setSpacing(GAP_ROW)
        heading = QLabel("功能")
        heading.setProperty("heading", True)
        heading.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        fv.addWidget(heading)

        def _btn(text, cb, primary=False):
            b = QPushButton(text)
            if primary:
                b.setProperty("primary", True)
            b.clicked.connect(cb)
            return b

        def _row(btns):
            row = QHBoxLayout()
            row.setSpacing(GAP_ROW)
            for b in btns:
                row.addWidget(b, 1)
            return row

        fv.addStretch(1)
        fv.addLayout(_row([_btn("初始化全量数据", self._task_init),
                           _btn("更新行情", self._task_update, True)]))
        fv.addStretch(1)
        fv.addLayout(_row([_btn("回测", self._task_backtest),
                           _btn("财务评估", self._task_evaluate)]))
        fv.addStretch(1)
        fv.addLayout(_row([_btn("综合评分", self._task_report),
                           _btn("生成跟踪报告", self._task_track, True)]))
        fv.addStretch(1)
        fv.addLayout(_row([_btn("更新仪表盘", self._task_dashboard),
                           _btn("报告目录", self._open_reports)]))
        fv.addStretch(1)
        fv.addLayout(_row([_btn("最新跟踪报告", self._open_latest),
                           _btn("Excel 报告", self._open_latest_excel)]))
        fv.addStretch(1)
        fv.addLayout(_row([_btn("网页仪表盘", self._task_web),
                           _btn("打开网页", self._open_web)]))
        fv.addStretch(1)

        snap_row = QHBoxLayout()
        snap_row.setSpacing(GAP_ROW)
        self.codes_edit = QLineEdit("601899 000975 600489")
        self.codes_edit.setPlaceholderText("股票代码，空格分隔")
        self.codes_edit.returnPressed.connect(self._task_snapshot)
        snap_row.addWidget(self.codes_edit, 1)
        btn_snap = QPushButton("查询行情")
        btn_snap.setProperty("primary", True)
        btn_snap.clicked.connect(self._task_snapshot)
        snap_row.addWidget(btn_snap)
        fv.addLayout(snap_row)
        lv.addWidget(func_card)
        left_scroll.setWidget(left_content)
        splitter.addWidget(left_scroll)

        # 右侧日志卡片
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(0, 0, 0, 0)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Menlo", 11))
        log_layout.addWidget(self.log)
        splitter.addWidget(_card("运行日志", log_layout))
        splitter.setSizes([300, 700])

        disclaimer = QLabel(
            "⚠ 免责声明：本软件所有输出（信号、评分、买点提示、报告）仅用于研究学习，"
            "不构成任何投资建议，请勿作为直接投资依据。股市有风险，决策需谨慎。")
        disclaimer.setStyleSheet(
            f"color:{C_SUB}; background:transparent; padding:2px 8px; font-size:12px;")
        disclaimer.setWordWrap(True)
        root.addWidget(disclaimer)

        sb = self.statusBar()
        sb.showMessage("就绪")
        right = QLabel("© 2026 主升浪信号跟踪")
        right.setStyleSheet(f"color:{C_SUB}; font-size:11px;")
        sb.addPermanentWidget(right)

    def _refresh_data_label(self) -> None:
        self.data_stat = data_status()
        if self.data_stat is None:
            self.data_lbl.setText("数据: 无（请选择目录或初始化）")
            self.data_lbl.setStyleSheet("color:#dc2626; font-size:12px;")
        else:
            days, latest = self.data_stat
            self.data_lbl.setText(f"数据: {days} 天 (至 {latest})")
            self.data_lbl.setStyleSheet("color:#16a34a; font-size:12px;")

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
        try:
            from mainrise import web_dashboard
            web = web_dashboard.update_web_dashboard()
            print(f"网页仪表盘已同步: {web}")
        except Exception as exc:
            print(f"⚠ 网页仪表盘更新失败: {exc}")

    def _do_dashboard(self) -> None:
        from mainrise import dashboard
        dash = dashboard.update_dashboard()
        print(f"仪表盘已更新: {dash}")

    def _do_web(self) -> None:
        from mainrise import web_dashboard
        out = web_dashboard.update_web_dashboard()
        print(f"网页仪表盘已生成: {out}")

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
                            QColor("#c00000" if pct > 0 else ("#008000" if pct < 0 else "#333333")))
                        table.setItem(ri, ci, item)
                        continue
                    except (TypeError, ValueError):
                        pass
                elif field == "error" and text != "-":
                    item = QTableWidgetItem(text)
                    item.setForeground(QColor("#c00000"))
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
        self._run(self._do_evaluate, "财务评估", need_key=False)

    def _task_report(self) -> None:
        self._run(self._do_report, "综合评分")

    def _task_track(self) -> None:
        self._run(self._do_track, "生成跟踪报告")

    def _task_dashboard(self) -> None:
        self._run(self._do_dashboard, "更新仪表盘")

    def _task_web(self) -> None:
        self._run(self._do_web, "生成网页仪表盘")

    def _open_web(self) -> None:
        p = paths.web_dir() / "index.html"
        if not p.exists():
            QMessageBox.information(self, "提示",
                                    "还没有网页仪表盘，请先运行【生成网页仪表盘】")
            return
        self._open_path(p)

    def _task_snapshot(self) -> None:
        codes = [c.strip() for c in self.codes_edit.text().replace(",", " ").split()
                 if c.strip()]
        if not codes:
            QMessageBox.warning(self, "提示", "请输入股票代码")
            return
        self._run(lambda: self._do_snapshot(codes), "实时行情", need_key=False)

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

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 主升浪信号跟踪",
            "<h3>主升浪信号跟踪</h3>"
            "<p>A 股主升浪信号跟踪模型：均线多头 + 创 20 日新高 + 放量/涨停。</p>"
            "<p>功能：信号扫描 / 财务评估 / 综合评分 / 每日跟踪 / 实时行情 / Excel 报告。</p>"
            f"<p style='color:{C_SUB}'>© 2026 · 仅供研究学习，不构成投资建议</p>",
        )

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


# ── 启动画面 ─────────────────────────────────────────────────────
SPLASH_MIN_MS = 1000  # 启动画面最短展示时间
FADE_MS = 300         # 淡出过渡时长
_ANIMS = set()        # 持有运行中的动画引用，防止被提前回收


def create_splash(target_size=None):
    """根据 splash.png 生成启动画面，尺寸与主窗口一致（cover 裁切填满）。"""
    pixmap = QPixmap(str(resource_path("splash.png")))
    if pixmap.isNull():
        return None
    w, h = target_size or window_target_size()
    img = pixmap.toImage()
    scale = max(w / img.width(), h / img.height())
    sw, sh = int(img.width() * scale), int(img.height() * scale)
    img = img.scaled(sw, sh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    img = img.copy(x, y, w, h)
    return QSplashScreen(QPixmap.fromImage(img))


def _crossfade(splash, window, duration: int = FADE_MS) -> None:
    """启动画面平滑淡出，露出下方已就绪的主窗口，避免生硬切换。"""
    fade_out = QPropertyAnimation(splash, b"windowOpacity")
    fade_out.setDuration(duration)
    fade_out.setStartValue(1.0)
    fade_out.setEndValue(0.0)
    fade_out.setEasingCurve(QEasingCurve.InOutQuad)
    _ANIMS.add(fade_out)

    def _finish():
        splash.hide()
        splash.finish(window)
        _ANIMS.discard(fade_out)

    fade_out.finished.connect(_finish)
    fade_out.start()


def _post_start(win: MainWindow) -> None:
    """启动画面结束后处理数据引导（避免弹窗压住淡出过渡）。"""
    if win.data_stat is None and not win.try_bundled_data():
        win.prompt_data_setup()


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
    # 高分屏缩放必须放在 QApplication 创建之前（Windows 缩放下文字/控件才清晰）
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("主升浪信号跟踪")
    app.setStyleSheet(APPLE_QSS)
    icon = QIcon(str(resource_path("app_icon.png")))
    if not icon.isNull():
        app.setWindowIcon(icon)

    splash_start = time.monotonic()
    splash = create_splash()
    if splash is not None:
        splash.show()
        splash.showMessage(
            "正在启动 主升浪信号跟踪 ...",
            Qt.AlignBottom | Qt.AlignHCenter,
            QColor("#8E8E93"),
        )
        app.processEvents()

    win = MainWindow()
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.show()

    if splash is not None:
        elapsed_ms = int((time.monotonic() - splash_start) * 1000)
        remaining_ms = max(0, SPLASH_MIN_MS - elapsed_ms)
        QTimer.singleShot(remaining_ms, lambda: _crossfade(splash, win))
        QTimer.singleShot(remaining_ms + FADE_MS + 80, lambda: _post_start(win))
    else:
        _post_start(win)

    try:
        with open("/tmp/mainrise_boot.log", "a") as f:
            f.write(f"UI ready in {time.time() - t0:.2f}s\n")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(app.exec_())
    lock.unlock()


if __name__ == "__main__":
    main()
