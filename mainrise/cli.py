"""mainrise 命令行入口。

用法示例:
  mainrise init                    首次初始化（自动下载全量行情，需联网）
  mainrise update                  增量更新行情
  mainrise backtest                回测并生成信号明细（mainrise_trades.csv）
  mainrise evaluate                财务评估（东财公开接口，无需 key）
  mainrise report                  综合评分 + 观察池
  mainrise track --no-scan         每日跟踪（买点/持仓/报告）
  mainrise dashboard               以 QuantDark 模板更新 KPI 仪表盘（track 后自动执行）
  mainrise web                     生成网页版仪表盘 output/web/index.html（track 后自动执行）
  mainrise monitor                 盘中实时盯盘（生成 output/web/live.html，盘后休眠）
  mainrise review                  当日复盘（连板梯队/行业/情绪周期/龙虎榜资金）
  mainrise reversal                暴跌反转研究（回撤30%后放量涨停+次日不破）
  mainrise launch                  起涨特征研究 + 全市场回测（约2-3分钟）
  mainrise strategy               启动加仓投资模型回测（底仓/加仓/止盈止损，约2分钟）
  mainrise snapshot 601899 000975  实时行情验证
  mainrise home                    显示数据目录
"""
from __future__ import annotations

import argparse
import json
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
    if getattr(args, "two_stage", False):
        backtest.two_stage_run()
    else:
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
    if not args.no_dashboard:
        from mainrise import dashboard
        try:
            dash = dashboard.update_dashboard()
            print(f"仪表盘已同步: {dash}")
        except Exception as exc:  # 仪表盘更新失败不阻塞跟踪报告本身
            print(f"⚠ 仪表盘更新失败: {exc}")
        from mainrise import web_dashboard
        try:
            web = web_dashboard.update_web_dashboard()
            print(f"网页仪表盘已同步: {web}")
        except Exception as exc:  # 网页仪表盘更新失败不阻塞跟踪报告本身
            print(f"⚠ 网页仪表盘更新失败: {exc}")
        from mainrise import review
        try:
            rv = review.update_review()
            print(f"当日复盘已同步: {rv}")
        except Exception as exc:  # 复盘失败不阻塞跟踪报告本身
            print(f"⚠ 当日复盘更新失败: {exc}")


def cmd_dashboard(args: argparse.Namespace) -> None:
    from mainrise import dashboard
    out = dashboard.update_dashboard(output=args.output)
    print(f"仪表盘已更新: {out}")


def cmd_web(args: argparse.Namespace) -> None:
    from mainrise import web_dashboard
    out = web_dashboard.update_web_dashboard(output=args.output)
    print(f"网页仪表盘已生成: {out}")


def cmd_monitor(args: argparse.Namespace) -> None:
    from mainrise import monitor
    monitor.monitor(interval=args.interval, once=args.once, out_dir=args.out)


def cmd_review(args: argparse.Namespace) -> None:
    from mainrise import review
    out = review.update_review(date_str=args.date)
    print(f"当日复盘已生成: {out}")


def cmd_market_state(_: argparse.Namespace) -> None:
    from mainrise import market_state
    s = market_state.compute_daily()
    r20 = s["mkt_ret20"]
    brd = s["breadth"]
    awl = s["amount_wl"]
    vwl = s["vol_wl"]
    print(f"大盘状态（{s['date']}）: {s['label']} → {s['advice']}")
    line2 = f"  等权20日涨幅: {r20}%" if r20 is not None else "  等权20日涨幅: —"
    line2 += (f" ｜ 市场宽度: {brd*100:.0f}%" if brd is not None
              else " ｜ 市场宽度: —")
    print(line2)
    line3 = f"  成交额水位: {awl}" if awl is not None else "  成交额水位: —"
    line3 += (f" ｜ 成交量水位: {vwl}" if vwl is not None
              else " ｜ 成交量水位: —")
    print(line3)
    print(f"  已写入: {market_state._state_path()}")


def cmd_reversal(_: argparse.Namespace) -> None:
    from mainrise import reversal
    reversal.run()


def cmd_launch(_: argparse.Namespace) -> None:
    from mainrise import launch
    launch.run()


def cmd_strategy(_: argparse.Namespace) -> None:
    from mainrise import strategy
    strategy.run()


def cmd_entry_study(args: argparse.Namespace) -> None:
    from mainrise import entry_study
    out = entry_study.run(with_market=not args.fast)
    print(f"买点提前与胜率研究: {out}")


def cmd_bigtrend(args: argparse.Namespace) -> None:
    from mainrise import bigtrend
    out = bigtrend.run(with_market=not args.fast)
    print(f"趋势大牛研究: {out}")


def cmd_wave(args: argparse.Namespace) -> None:
    from mainrise import wave
    out = wave.run(with_market=not args.fast)
    print(f"波段高抛低吸研究: {out}")


def cmd_bullcnt(args: argparse.Namespace) -> None:
    from mainrise import bullcnt
    out = bullcnt.run(with_market=not args.fast)
    print(f"买入信号计数大牛判定: {out}")


def cmd_candidate_bt(args: argparse.Namespace) -> None:
    from mainrise import candidate_bt
    out = candidate_bt.run(with_market=not args.fast)
    print(f"大牛候选规则回测: {out}")


def cmd_portfolio_bt(args: argparse.Namespace) -> None:
    from mainrise import portfolio_bt
    out = portfolio_bt.run(with_market=not args.fast)
    print(f"大牛候选组合级回测: {out}")


def cmd_bigbull(_: argparse.Namespace) -> None:
    from mainrise import bigbull
    out = bigbull.run()
    print(f"大牛模型回测: {out}")


def cmd_push(args: argparse.Namespace) -> None:
    from mainrise import push
    if args.wecom:
        if args.close:
            print("⚠ --wecom 与 --close 同时指定：企业微信渠道只发 14:50 尾盘消息，"
                  "--close 被忽略；收盘确认请用默认 Server酱渠道（push --close）")
        r = push.run_wecom(test=args.test, dry_run=args.dry_run)
    else:
        r = push.run(test=args.test, close=args.close, dry_run=args.dry_run)
    print(f"推送结果: {r}")


def cmd_alert(args: argparse.Namespace) -> None:
    from mainrise import push
    r = push.send_alert(args.title, args.desp, dry_run=args.dry_run)
    print(f"告警结果: {r}")


def cmd_weekly(args: argparse.Namespace) -> None:
    from mainrise import weekly
    r = weekly.run(dry_run=args.dry_run)
    print(f"周报结果: {r}")


def cmd_cycle_check(args: argparse.Namespace) -> None:
    from mainrise import cycle_check
    if args.fetch_only:
        cycle_check.fetch_only()
        return
    out = cycle_check.run(refresh=args.refresh)
    print(f"大周期追溯研究: {out}")


def cmd_style_check(args: argparse.Namespace) -> None:
    from mainrise import style_check
    out = style_check.run(with_market=not args.fast)
    print(f"市场风格检测研究: {out}")


def cmd_report_check(args: argparse.Namespace) -> None:
    from mainrise import report_check
    if args.fetch_only:
        report_check.fetch_all(refresh=True)
        return
    out = report_check.run(refresh=args.refresh)
    print(f"市场研报信号研究: {out}")


def cmd_cycle_state(args: argparse.Namespace) -> None:
    from mainrise import cycle_state
    if args.backtest:
        out = cycle_state.backtest()
        print(f"周期状态回测: {out}")
        return
    if args.json_only:
        st = cycle_state.compute()
        (paths.state_dir() / "cycle_state.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(st, ensure_ascii=False))
        return
    card = cycle_state.write_outputs()
    print(card)
    print(f"周期状态卡: {paths.web_dir() / 'cycle.html'}")


def cmd_theme_scope(_: argparse.Namespace) -> None:
    from mainrise import theme_scope
    out = theme_scope.run()
    print(f"热主题范围研究: {out}")


def cmd_theme_walkforward(_: argparse.Namespace) -> None:
    from mainrise import theme_walkforward
    out = theme_walkforward.run()
    print(f"主题walkforward检验: {out}")


def cmd_theme_freq(_: argparse.Namespace) -> None:
    from mainrise import theme_freq
    out = theme_freq.run()
    print(f"主题训练频率研究: {out}")


def cmd_theme_jan(_: argparse.Namespace) -> None:
    from mainrise import theme_jan
    out = theme_jan.run()
    print(f"每年1月选主题检验: {out}")


def cmd_industry_trend(_: argparse.Namespace) -> None:
    from mainrise import industry_trend
    out = industry_trend.run()
    print(f"产业趋势研究: {out}")


def cmd_price_watch(_: argparse.Namespace) -> None:
    from mainrise import price_watch
    out = price_watch.run()
    print(f"供需涨价监控: {out}")
    print(price_watch.render_card()[:200])


def cmd_price_events(_: argparse.Namespace) -> None:
    from mainrise import price_events
    out = price_events.run()
    print(f"产业链涨价追踪: {out}")


def cmd_m5data(args: argparse.Namespace) -> None:
    from mainrise import m5data
    codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
    st = m5data.archive(codes or None)
    print(f"5分钟归档: {st['ok']} 只有数据 / {st['empty']} 只空")


def cmd_ignite5(args: argparse.Namespace) -> None:
    from mainrise import ignite5
    if args.report:
        print(f"点火报告: {ignite5.write_report()}")
    else:
        out = ignite5.run_once()
        print(f"点火检测: {len(out.get('signals') or [])} 信号 / "
              f"{len(out.get('tracked') or [])} 追踪")


def cmd_m5optimize(args: argparse.Namespace) -> None:
    from mainrise import m5optimize
    out = m5optimize.run(args.vol, args.min_chg, args.new_hi_chg, args.grid)
    print(f"月度优化: {out}")



def cmd_themeindex(args: argparse.Namespace) -> None:
    from mainrise import themeindex
    if args.hot:
        print("当日热主题:", themeindex.today_hot(
            args.ma_short, args.ma_long, args.slope, args.min_days,
            args.rank_top, args.lookback))
        return
    hs, missing = themeindex.hot_themes_series(
        args.refresh, args.ma_short, args.ma_long, args.slope, args.min_days,
        args.rank_top, args.lookback)
    if hs:
        last = max(hs)
        print(f"截止 {last} 热主题: {hs[last]}")
        print(f"最近 10 日热主题切换: {dict(list(hs.items())[-10:])}")
    if missing:
        print("缺失:", missing)


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
    p.add_argument("--two-stage", action="store_true",
                   help="两级模型验证（B3 打底仓 / 二波加仓，带纪律模拟）")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("evaluate", help="信号日财务评估（东财公开接口）")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("report", help="综合评分 + 观察池")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("track", help="每日跟踪：买点/持仓/新信号")
    p.add_argument("--date", default=None)
    p.add_argument("--no-scan", action="store_true")
    p.add_argument("--max-10d-gain", type=float, default=150.0)
    p.add_argument("--no-dashboard", action="store_true",
                   help="跳过跟踪后的 Excel/网页仪表盘同步更新")
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("dashboard", help="以 QuantDark 模板更新 KPI 仪表盘")
    p.add_argument("--output", default=None, help="输出文件路径，默认 output/reports/主升浪跟踪仪表盘.xlsx")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("web", help="生成网页版仪表盘（output/web/index.html）")
    p.add_argument("--output", default=None,
                   help="输出 HTML 路径，默认 output/web/index.html")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("monitor", help="盘中实时盯盘（生成 output/web/live.html，3 秒轮询）")
    p.add_argument("--interval", type=int, default=3,
                   help="轮询间隔秒数，默认 3（秒级更新）")
    p.add_argument("--once", action="store_true",
                   help="立即跑一轮后退出（测试用）")
    p.add_argument("--out", default=None, help="输出目录，默认 output/web")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("review", help="当日复盘：连板梯队/题材/情绪周期/龙虎榜资金")
    p.add_argument("--date", default=None, help="复盘日期，默认今天")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("market-state",
                       help="大盘状态（三态轮动：等权20日/宽度/量能）")
    p.set_defaults(func=cmd_market_state)

    p = sub.add_parser("reversal", help="暴跌反转研究（回撤30%后放量涨停+次日不破）")
    p.set_defaults(func=cmd_reversal)

    p = sub.add_parser("launch", help="起涨特征研究 + 全市场回测（约2-3分钟）")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("strategy", help="启动加仓投资模型回测（约2分钟）")
    p.set_defaults(func=cmd_strategy)

    p = sub.add_parser("cycle-check", help="大周期追溯研究：新能源/白酒 × 大牛模型规则（约2分钟）")
    p.add_argument("--fetch-only", action="store_true", help="只拉数据缓存")
    p.add_argument("--refresh", action="store_true", help="强制刷新数据缓存")
    p.set_defaults(func=cmd_cycle_check)

    p = sub.add_parser("style-check", help="市场风格检测研究：候选风格指标预测力验证（约30秒）")
    p.add_argument("--fast", action="store_true", help="跳过全市场特征")
    p.set_defaults(func=cmd_style_check)

    p = sub.add_parser("report-check", help="市场研报信号研究：东财研报能否筛出可靠方向（首次约10分钟）")
    p.add_argument("--refresh", action="store_true", help="强制重拉研报缓存")
    p.add_argument("--fetch-only", action="store_true", help="只拉研报缓存")
    p.set_defaults(func=cmd_report_check)

    p = sub.add_parser("cycle-state", help="市场周期状态卡：当前主线/阶段/模型档位建议（秒级）")
    p.add_argument("--json-only", action="store_true", help="只写 JSON")
    p.add_argument("--backtest", action="store_true",
                   help="逐日回放周期状态判定（验证暴跌识别，约1分钟）")
    p.set_defaults(func=cmd_cycle_state)

    p = sub.add_parser("theme-scope", help="热主题范围研究：三主题→科技/全放开的对比回测（约30秒）")
    p.set_defaults(func=cmd_theme_scope)

    p = sub.add_parser("theme-wf", help="硬规则主题walkforward检验：每年初按过去表现选主题是否可行（约30秒）")
    p.set_defaults(func=cmd_theme_walkforward)

    p = sub.add_parser("theme-freq", help="主题训练频率研究：季度/半年/年重选 vs 固定（约30秒）")
    p.set_defaults(func=cmd_theme_freq)

    p = sub.add_parser("theme-jan", help="每年1月选主题（一年数据）精确检验（约30秒）")
    p.set_defaults(func=cmd_theme_jan)

    p = sub.add_parser("industry-trend", help="产业趋势研究：主题龙头财务景气度前瞻（首次约90秒）")
    p.set_defaults(func=cmd_industry_trend)

    p = sub.add_parser("price-watch", help="供需涨价监控：商品期货涨幅+见顶回落预警（约10秒）")
    p.set_defaults(func=cmd_price_watch)

    p = sub.add_parser("price-events", help="产业链涨价追踪：存储/覆铜板/MLCC等涨价新闻+标签（约20秒）")
    p.set_defaults(func=cmd_price_events)

    p = sub.add_parser("m5data", help="5分钟K线归档：腾讯m5增量存 data/m5daily（候选池+卡点企业）")
    p.add_argument("--codes", default="", help="逗号分隔代码，默认全部卡点企业")
    p.set_defaults(func=cmd_m5data)

    p = sub.add_parser("ignite5", help="盘中5分钟点火信号检测（候选池+当日放量标的）")
    p.add_argument("--report", action="store_true", help="收盘后生成汇总报告")
    p.set_defaults(func=cmd_ignite5)

    p = sub.add_parser("m5optimize", help="月度买卖点优化：用归档5分钟数据校准点火阈值")
    p.add_argument("--vol", type=float, default=2.0, help="量比阈值（默认2.0）")
    p.add_argument("--min-chg", type=float, default=0.03, help="最小涨幅（默认0.03）")
    p.add_argument("--new-hi-chg", type=float, default=0.05, help="突破型涨幅")
    p.add_argument("--grid", action="store_true", help="网格搜索最优阈值")
    p.set_defaults(func=cmd_m5optimize)

    p = sub.add_parser("entry-study", help="买点提前与胜率研究（约30秒）")
    p.add_argument("--fast", action="store_true", help="跳过全市场特征")
    p.set_defaults(func=cmd_entry_study)

    p = sub.add_parser("bigtrend", help="趋势大牛研究：观察点/买点确认/卖点信号（约30秒）")
    p.add_argument("--fast", action="store_true", help="跳过全市场特征")
    p.set_defaults(func=cmd_bigtrend)

    p = sub.add_parser("wave", help="波段高抛低吸研究：死拿 vs 波段（约30秒）")
    p.add_argument("--fast", action="store_true", help="跳过全市场特征")
    p.set_defaults(func=cmd_wave)

    p = sub.add_parser("bullcnt", help="连续买入信号计数 → 大牛属性判定（约30秒）")
    p.add_argument("--fast", action="store_true", help="跳过全市场特征")
    p.set_defaults(func=cmd_bullcnt)

    p = sub.add_parser("candidate-bt", help="大牛候选规则回测（约15秒）")
    p.add_argument("--fast", action="store_true", help="跳过全市场特征")
    p.set_defaults(func=cmd_candidate_bt)

    p = sub.add_parser("portfolio-bt", help="大牛候选组合级回测：1/3仓+3只上限+净值曲线（约15秒）")
    p.add_argument("--fast", action="store_true", help="跳过全市场特征")
    p.set_defaults(func=cmd_portfolio_bt)

    p = sub.add_parser("bigbull", help="大牛模型固化回测 + 门户（约15秒）")
    p.set_defaults(func=cmd_bigbull)

    p = sub.add_parser("push", help="微信推送（14:50 尾盘决策 / 17:30 收盘确认 / 企业微信渠道）")
    p.add_argument("--test", action="store_true", help="推送测试消息")
    p.add_argument("--close", action="store_true",
                   help="17:30 收盘确认推送（读 bigbull 交割单，收盘口径）")
    p.add_argument("--wecom", action="store_true",
                   help="改用企业微信渠道（免费不限量）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印消息不发送（不消耗配额）")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("alert", help="微信告警（企业微信优先，Server酱兜底）")
    p.add_argument("title", help="告警标题")
    p.add_argument("desp", help="告警正文（markdown）")
    p.add_argument("--dry-run", action="store_true", help="只打印不发送")
    p.set_defaults(func=cmd_alert)

    p = sub.add_parser("weekly", help="每周绩效小结推送（交割/净值/信号摘要）")
    p.add_argument("--dry-run", action="store_true", help="只打印不发送")
    p.set_defaults(func=cmd_weekly)

    p = sub.add_parser("themeindex", help="动态热主题（同花顺全A行业指数）")
    p.add_argument("--refresh", action="store_true", help="强制刷新缓存")
    p.add_argument("--ma-short", type=int, default=20)
    p.add_argument("--ma-long", type=int, default=60)
    p.add_argument("--slope", action="store_true")
    p.add_argument("--min-days", type=int, default=0)
    p.add_argument("--rank-top", type=int, default=None,
                   help="月线级排名取前 N 为热主题（如 10）")
    p.add_argument("--lookback", type=int, default=60,
                   help="排名涨幅回看天数（月线级别）")
    p.add_argument("--hot", action="store_true", help="只打印当日热主题")
    p.set_defaults(func=cmd_themeindex)

    p = sub.add_parser("snapshot", help="实时行情快照（腾讯接口，无需 key）")
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
