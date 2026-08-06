"""mainrise 命令行入口。

用法示例:
  mainrise init                    首次初始化（自动下载全量行情，需联网）
  mainrise update                  增量更新行情
  mainrise backtest                回测并生成信号明细（mainrise_trades.csv）
  mainrise evaluate                财务评估（需 MAINRISE_API_KEY）
  mainrise report                  综合评分 + 观察池
  mainrise track --no-scan         每日跟踪（买点/持仓/报告）
  mainrise snapshot 601899 000975  实时行情验证
  mainrise home                    显示数据目录
"""
from __future__ import annotations

import argparse
import sys

from mainrise import paths


def cmd_init(args: argparse.Namespace) -> None:
    from mainrise import data
    paths.ensure_dirs()
    print(f"数据目录: {paths.home()}")
    unpacked = data.ensure_bundled_data()
    if data.cached_days() == 0:
        print("拉取交易日历...")
        n = data.init_calendar()
        print(f"交易日历: {n} 天 (2021 起)")
        print("拉取股票名册...")
        n = data.init_stock_list()
        print(f"股票名册: {n} 只")
        ok, empty = data.fetch_all_panels(args.start or "2021-01-01")
        print(f"初始化完成: {len(ok)} 个交易日有数据，{len(empty)} 空")
    else:
        cached = sorted(p.stem for p in paths.zzshare_dir().glob("[0-9]*.csv"))
        start = f"{cached[-1][:4]}-{cached[-1][4:6]}-{cached[-1][6:]}"
        print(f"已有内置历史数据（{len(cached)} 天，至 {start}），仅补齐最近行情...")
        ok, empty = data.fetch_all_panels(start)
        print(f"补齐完成: {len(ok)} 个交易日有数据，{len(empty)} 空/滞后")
    print("下一步: mainrise track")


def cmd_update(_: argparse.Namespace) -> None:
    from mainrise import data
    start = data.latest_trading_day()
    # 从缓存最新日期继续，避免重复抓取
    cached = sorted(p.stem for p in paths.zzshare_dir().glob("[0-9]*.csv"))
    if cached:
        start = cached[-1][:4] + "-" + cached[-1][4:6] + "-" + cached[-1][6:]
    ok, empty = data.fetch_all_panels(start)
    print(f"更新完成: {len(ok)} 个交易日有数据，{len(empty)} 空/滞后")


def cmd_backtest(args: argparse.Namespace) -> None:
    from mainrise import backtest
    backtest.run(getattr(args, "grid", False))


def cmd_evaluate(_: argparse.Namespace) -> None:
    from mainrise import evaluate
    evaluate.run()


def cmd_report(_: argparse.Namespace) -> None:
    from mainrise import report
    report.run()


def cmd_track(args: argparse.Namespace) -> None:
    from mainrise import tracker
    out = tracker.run(args.date, args.no_scan, args.max_10d_gain)
    print(f"跟踪报告: {out['report']}")
    print(f"持仓: {out['active']} 活跃 / {out['pending']} 待买入 / {out['closed']} 已平仓")
    for _, r in out["buy_points"].iterrows():
        print(f"  {r['code']} {r['name']} [{r['status']}] {r['hint']}")


def cmd_snapshot(args: argparse.Namespace) -> None:
    from mainrise import snapshot
    df = snapshot.fetch_snapshot(args.codes)
    if df.empty:
        print("快照为空")
        return
    pd = __import__("pandas")
    with pd.option_context("display.max_rows", None):
        print(df.to_string(index=False))


def cmd_home(_: argparse.Namespace) -> None:
    print(paths.home())


def cmd_gui(_: argparse.Namespace) -> None:
    from mainrise.gui_pyqt import main as gui_main
    gui_main()


def main() -> None:
    ap = argparse.ArgumentParser(prog="mainrise",
                                 description="主升浪信号跟踪模型（数据目录见 mainrise home）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="首次初始化：交易日历+股票名册+全量行情")
    p.add_argument("--start", default=None, help="起始日期，默认 2021-01-01")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("update", help="增量更新行情")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("backtest", help="回测（生成信号明细供评估）")
    p.add_argument("--grid", action="store_true",
                   help="精细参数扫描（约200组，30-40分钟）")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("evaluate", help="信号日财务评估（需 MAINRISE_API_KEY）")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("report", help="综合评分 + 观察池")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("track", help="每日跟踪：买点/持仓/新信号")
    p.add_argument("--date", default=None)
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--max-10d-gain", type=float, default=80.0)
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("snapshot", help="实时行情快照（需 MAINRISE_API_KEY）")
    p.add_argument("codes", nargs="+")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("home", help="显示数据目录")
    p.set_defaults(func=cmd_home)

    p = sub.add_parser("gui", help="打开图形界面软件")
    p.set_defaults(func=cmd_gui)

    args = ap.parse_args()
    if not hasattr(args, "func"):
        ap.print_help()
        sys.exit(1)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"错误: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
