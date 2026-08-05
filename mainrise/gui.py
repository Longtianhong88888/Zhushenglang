"""主升浪信号跟踪软件（图形界面版）。

双击打开后通过按钮执行各功能：
  数据: 初始化全量 / 更新行情
  模型: 回测 / 财务评估 / 综合评分
  跟踪: 每日跟踪 / 实时行情快照
  报告: 打开报告目录 / 打开最新报告
"""
from __future__ import annotations

import contextlib
import io
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from mainrise import paths


class QueueWriter(io.TextIOBase):
    """把 print/tqdm 输出转发到队列，由主线程刷新到日志区。"""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s: str) -> int:
        for line in s.split("\n"):
            if line.endswith("\r"):
                self.q.put(("replace", line.rstrip("\r")))
            elif line:
                self.q.put(("append", line))
        return len(s)

    def flush(self) -> None:
        pass


class MainriseApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.msg_q: queue.Queue = queue.Queue()
        self.busy = False
        root.title("主升浪信号跟踪")
        root.geometry("980x640")
        root.minsize(860, 560)
        self._apply_theme()
        self._build_ui()
        self._poll_queue()

    # ---------- UI ----------
    def _apply_theme(self) -> None:
        """macOS 自带 Tk 8.5 的 Aqua 主题渲染 ttk 控件可能空白，
        强制使用 clam 主题（纯画布绘制，跨平台稳定）。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("PingFang SC", 12))
        style.configure("TButton", padding=6)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="数据目录:").pack(side="left")
        self.home_lbl = ttk.Label(top, text=str(paths.home()), foreground="#2563eb")
        self.home_lbl.pack(side="left", padx=4)
        ttk.Button(top, text="更改", command=self._pick_home).pack(side="left")
        ttk.Button(top, text="刷新", command=self._refresh_home).pack(side="left", padx=4)
        ttk.Label(top, text="API Key:").pack(side="left", padx=(20, 4))
        self.key_var = tk.StringVar(value=os.environ.get("MAINRISE_API_KEY", ""))
        self.key_entry = ttk.Entry(top, textvariable=self.key_var, width=26, show="*")
        self.key_entry.pack(side="left")

        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 8))
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        self._btn_group(left, "数据", [
            ("初始化全量数据", self._task_init),
            ("更新行情", self._task_update),
        ])
        self._btn_group(left, "模型分析", [
            ("回测", self._task_backtest),
            ("财务评估", self._task_evaluate),
            ("综合评分", self._task_report),
        ])
        self._btn_group(left, "每日跟踪", [
            ("生成跟踪报告", self._task_track),
        ])
        snap = ttk.LabelFrame(left, text="实时行情", padding=6)
        snap.pack(fill="x", pady=4)
        ttk.Label(snap, text="代码(空格分隔):").pack(anchor="w")
        self.codes_var = tk.StringVar(value="601899 000975 600489")
        e = ttk.Entry(snap, textvariable=self.codes_var)
        e.pack(fill="x")
        e.bind("<Return>", lambda _: self._task_snapshot())
        ttk.Button(snap, text="查询行情", command=self._task_snapshot).pack(fill="x", pady=(4, 0))

        self._btn_group(left, "报告", [
            ("打开报告目录", self._open_reports),
            ("打开最新跟踪报告", self._open_latest),
        ])

        logf = ttk.LabelFrame(right, text="运行日志", padding=4)
        logf.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(logf, wrap="word", state="disabled",
                                             font=("Menlo", 11), bg="#f6f8fa")
        self.log.pack(fill="both", expand=True)

        tk_ver = f"Tk {self.root.tk.call('info', 'patchlevel')}"
        self.status = ttk.Label(self.root,
                                text=f"就绪 | {tk_ver} | 数据目录 {paths.home()}",
                                anchor="w",
                                padding=(10, 4), relief="sunken")
        self.status.pack(fill="x", side="bottom")

    def _btn_group(self, parent, title: str, items) -> None:
        f = ttk.LabelFrame(parent, text=title, padding=6)
        f.pack(fill="x", pady=4)
        for text, cb in items:
            ttk.Button(f, text=text, command=cb).pack(fill="x", pady=2)

    # ---------- 任务执行 ----------
    def _run(self, fn, label: str, need_key: bool = False) -> None:
        if self.busy:
            messagebox.showinfo("提示", "有任务正在运行，请等待完成")
            return
        if need_key and not self.key_var.get().strip():
            messagebox.showwarning("缺少 API Key", "请先在上方填写 API Key（财务评估/实时行情需要）")
            return
        os.environ["MAINRISE_API_KEY"] = self.key_var.get().strip()
        self.busy = True
        self.status.config(text=f"运行中: {label}")
        self._log(f"\n===== {label} =====")
        t = threading.Thread(target=self._worker, args=(fn, label), daemon=True)
        t.start()

    def _worker(self, fn, label: str) -> None:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(QueueWriter(self.msg_q)), \
                 contextlib.redirect_stderr(QueueWriter(self.msg_q)):
                fn()
            self.msg_q.put(("done", label))
        except Exception as e:  # noqa: BLE001
            buf.write(f"错误: {type(e).__name__}: {e}")
            self.msg_q.put(("append", f"错误: {type(e).__name__}: {e}"))
            self.msg_q.put(("fail", label))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "append":
                    self._log(payload)
                elif kind == "replace":
                    self._replace_last_log(payload)
                elif kind == "done":
                    self.busy = False
                    self.status.config(text=f"完成: {payload}")
                    self._log(f"✔ {payload} 完成")
                elif kind == "fail":
                    self.busy = False
                    self.status.config(text="失败")
                    self._log(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _replace_last_log(self, text: str) -> None:
        self.log.config(state="normal")
        content = self.log.get("1.0", "end-1c").split("\n")
        if content and content[-1].startswith("\r") or (content and not content[-1]):
            content[-1] = text
        else:
            content.append(text)
        self.log.delete("1.0", "end")
        self.log.insert("1.0", "\n".join(content))
        self.log.see("end")
        self.log.config(state="disabled")

    # ---------- 功能 ----------
    def _task_init(self) -> None:
        self._run(self._do_init, "初始化全量数据")

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

    def _task_update(self) -> None:
        self._run(self._do_update, "更新行情")

    def _do_update(self) -> None:
        from mainrise import data
        cached = sorted(p.stem for p in paths.zzshare_dir().glob("[0-9]*.csv"))
        start = cached[-1][:4] + "-" + cached[-1][4:6] + "-" + cached[-1][6:] if cached \
            else data.latest_trading_day()
        ok, empty = data.fetch_all_panels(start)
        print(f"更新完成: {len(ok)} 个交易日有数据，{len(empty)} 空/滞后")

    def _task_backtest(self) -> None:
        self._run(self._do_backtest, "回测")

    def _do_backtest(self) -> None:
        from mainrise import backtest
        backtest.run()

    def _task_evaluate(self) -> None:
        self._run(self._do_evaluate, "财务评估", need_key=True)

    def _do_evaluate(self) -> None:
        from mainrise import evaluate
        evaluate.run()

    def _task_report(self) -> None:
        self._run(self._do_report, "综合评分")

    def _do_report(self) -> None:
        from mainrise import report
        report.run()

    def _task_track(self) -> None:
        self._run(self._do_track, "生成跟踪报告")

    def _do_track(self) -> None:
        from mainrise import tracker
        out = tracker.run()
        print(f"跟踪报告: {out['report']}")
        print(f"持仓: {out['active']} 活跃 / {out['pending']} 待买入 / {out['closed']} 已平仓")
        for _, r in out["buy_points"].iterrows():
            print(f"  {r['code']} {r['name']} [{r['status']}] {r['hint']}")

    def _task_snapshot(self) -> None:
        codes = [c.strip() for c in self.codes_var.get().replace(",", " ").split()
                 if c.strip()]
        if not codes:
            messagebox.showwarning("提示", "请输入股票代码")
            return
        self._run(lambda: self._do_snapshot(codes), "实时行情", need_key=True)

    def _do_snapshot(self, codes) -> None:
        from mainrise import snapshot
        df = snapshot.fetch_snapshot(codes)
        if df.empty:
            print("快照为空")
            return
        print(df.to_string(index=False))
        self.root.after(0, lambda: self._show_snapshot_table(df))

    def _show_snapshot_table(self, df) -> None:
        win = tk.Toplevel(self.root)
        win.title("实时行情")
        win.geometry("760x260")
        tree = ttk.Treeview(win, columns=list(df.columns), show="headings")
        for c in df.columns:
            tree.heading(c, text=c)
            tree.column(c, width=100, anchor="e")
        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for _, r in df.iterrows():
            tree.insert("", "end", values=[f"{v:.4g}" if isinstance(v, float) else v
                                           for v in r.tolist()])

    def _pick_home(self) -> None:
        p = filedialog.askdirectory(title="选择数据目录（选项目根目录可复用现有数据）")
        if p:
            os.environ["MAINRISE_HOME"] = p
            self._refresh_home()

    def _refresh_home(self) -> None:
        self.home_lbl.config(text=str(paths.home()))

    def _open_reports(self) -> None:
        d = paths.report_dir()
        d.mkdir(parents=True, exist_ok=True)
        self._open_path(d)

    def _open_latest(self) -> None:
        cands = sorted(paths.report_dir().glob("主升浪跟踪_*.md"))
        if not cands:
            messagebox.showinfo("提示", "还没有跟踪报告，请先运行【生成跟踪报告】")
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
            messagebox.showerror("打开失败", str(e))


def main() -> None:
    root = tk.Tk()
    MainriseApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
