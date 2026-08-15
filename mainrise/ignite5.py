"""盘中 5 分钟"资金点火进攻"信号检测（候选池标的，2026-08-15 用户方向）。

背景：候选池（大牛模型近45日硬规则信号，评分≥2）标的等收盘 T0 确认买入
太迟钝、且收盘价=涨停价买不进；改为**盘中 5 分钟级追踪**，抓"资金点火进攻"
信号——单根 5 分钟放量（>2×近5日单根均量）且（突破前20日高点 或 当日涨幅≥5%）
→ 提示"点火进攻，可盘中择机买入"，买价通常低于收盘 3~6%。

数据：腾讯 m5（ifzq.gtimg.cn，服务器/本机均可用，东财对服务器 IP 风控）。
只追踪候选池小名单（≤几十只），不做全市场——遵守 monitor OOM 教训。

输出：
  output/state/ignite5.json     当日点火信号 + 追踪标的实时状态（盯盘页读取）
  output/reports/点火信号_<date>.md  收盘后汇总报告

用法:
    python3 -m mainrise.ignite5             # 盘中跑一轮（读最新5分钟，检测点火）
    python3 -m mainrise.ignite5 --report    # 收盘后生成汇总报告
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from mainrise import paths
from mainrise.m5data import fetch_m5

# 点火判定参数（2026-08-15 11 样本研究基准，月度优化时校准）
VOL_MULT = 2.0        # 单根 5 分钟量 > VOL_MULT × 近5日单根均量
MIN_CHG = 0.03        # 点火需当日涨幅 ≥3%（放量但没涨=假点火，如下跌放量/高位换手）
NEW_HI_CHG = 0.05     # 破前高 且 涨幅≥5% 才算"突破型点火"（破前高但涨幅不足不单独触发）
# 近5日单根均量：用归档数据（data/m5daily）或日线量/48 近似
_DAILY_BASE = {}      # code -> 近5日单根5分钟均量（股）


def _daily_base(code: str) -> float | None:
    """近5日单根5分钟均量：优先归档5分钟，否则日线量/48 近似。

    避免反复 load_all_panels（700万行）——归档缺失时才用日线，且缓存。
    """
    if code in _DAILY_BASE:
        return _DAILY_BASE[code]
    # 归档 5 分钟（data/m5daily，已由 m5data 落盘）
    try:
        from mainrise import m5data
        df = m5data.load_m5(code)
        if len(df):
            recent = df[df["datetime"] >= (df["datetime"].max()
                                           - pd.Timedelta(days=8))]
            per_min = recent["volume"].mean()
            _DAILY_BASE[code] = float(per_min)
            return _DAILY_BASE[code]
    except Exception:  # noqa: BLE001
        pass
    _DAILY_BASE[code] = None   # 归档缺失则跳过（由调用方日线兜底）
    return None


def detect_ignite(code: str, day_m5: list[list], hi20: float,
                  prev_close: float, base_vol: float) -> dict | None:
    """对当日 5 分钟序列检测点火：返回首次点火 {time, px, vol_mult, chg} 或 None。

    day_m5: 当日 5 分钟 [[YYYYMMDDHHMM, o, c, h, l, vol_hand, ...], ...]（升序）。
    点火 = 单根量 > VOL_MULT×base_vol 且 当日涨幅 ≥MIN_CHG
          且（破 hi20 或 涨幅 ≥NEW_HI_CHG）——放量+上涨才点火，
          破前高但没涨（下跌放量/高位换手）不触发。
    """
    for x in day_m5:
        try:
            ts = str(x[0])
            o, c, h, l, v = (float(x[1]), float(x[2]), float(x[3]),
                             float(x[4]), float(x[5]))
        except (ValueError, IndexError):
            continue
        vol = v * 100
        if base_vol and base_vol > 0:
            vm = vol / base_vol
            big = vm > VOL_MULT
        else:
            big = False
        chg = h / prev_close - 1 if prev_close > 0 else 0
        new_hi = h > hi20
        if big and chg >= MIN_CHG and (new_hi or chg >= NEW_HI_CHG):
            t = f"{ts[8:10]}:{ts[10:12]}"
            return {"time": t, "px": c, "vol_mult": round(vm, 1),
                    "chg": round(chg * 100, 1), "new_hi": bool(new_hi)}
    return None


def run_once(cands: list | None = None) -> dict:
    """跑一轮盘中检测：跟踪标的拉最新5分钟 → 检测点火 → 落盘 ignite5.json。

    跟踪范围（2026-08-15 用户明确）：大牛候选池（bigbull_cands.json 的
    cands，已满足买入条件：热主题 + 90日T0≥3 + 评分≥2）。点火（5分钟
    放量上攻+破前高/大阳）对候选池个股 = 真买入信号。
    """
    # 交易日保护：非交易日（周末/节假日）不检测，避免把上一交易日的
    # 5 分钟数据误报成"今天"的点火信号（2026-08-15 周末残留事故）。
    try:
        from mainrise.data import trade_dates
        if _today() not in trade_dates():
            print(f"非交易日（{_today()}），跳过点火检测")
            empty = {"date": _today(), "signals": [], "tracked": [],
                     "note": "非交易日"}
            try:
                p = paths.state_dir() / "ignite5.json"
                p.write_text(json.dumps(empty, ensure_ascii=False, indent=1),
                             encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            return empty
    except Exception:  # noqa: BLE001
        pass
    if cands is None:
        cands = _dynamic_cands()
    codes = [str(c) for c in cands if c]
    if not codes:
        print("⚠ 候选池为空（bigbull_cands.json 无候选）")
        return {"date": _today(), "signals": [], "tracked": [], "error": "候选池为空"}

    # 前20日高点 / 昨收（日线）——只读候选代码的近期日线，避免
    # load_all_panels() 全量 700 万行（2026-08-15 审计 M1：盘中 cron 每
    # 10 分钟全量加载是 OOM 隐患，monitor OOM 事故同类模式）。
    from pathlib import Path as _P
    from mainrise import paths as _paths
    _files = sorted(_P(_paths.data_dir() / "zzshare_daily").glob("[0-9]*.csv"))
    # 只取最近 40 个交易日文件（够算 prev_close + hi20 + 5日均量，无前视）
    _want = [f for f in _files if f.stem <= _today().replace("-", "")][-40:]
    pb: dict[str, pd.DataFrame] = {}
    for _f in _want:
        try:
            _df = pd.read_csv(_f, dtype={"code": str},
                              usecols=["date", "code", "close", "high", "volume"])
            _df = _df[_df["code"].isin(codes)]
            for _c, _g in _df.groupby("code", sort=False):
                pb.setdefault(_c, []).append(_g)
        except Exception:  # noqa: BLE001
            continue
    for _c in pb:
        if pb[_c]:
            pb[_c] = pd.concat(pb[_c]).sort_values("date").reset_index(drop=True)
    # 日线近5日均量/48 作为单根均量兜底（一次性计算，避免反复加载）
    daily_base: dict[str, float] = {}
    for c, g in pb.items():
        if len(g) >= 6:
            v5 = float(g["volume"].tail(5).mean())
            daily_base[c] = v5 / 48

    signals, tracked = [], []
    for code in codes:
        g = pb.get(code)
        if g is None or len(g) < 22:
            continue
        mk = fetch_m5(code)
        # 用 5 分钟数据最新日期（盘前/周末=上一交易日），非当天日期
        day_prefix = ""
        for x in mk:
            day_prefix = str(x[0])[:8]
        if not day_prefix:
            tracked.append({"code": code, "status": "盘中无数据"})
            continue
        day_m5 = [x for x in mk if str(x[0]).startswith(day_prefix)]
        if not day_m5:
            tracked.append({"code": code, "status": "盘中无数据"})
            continue
        # 基准日对齐（2026-08-15 修复审计 H1）：prev_close/hi20 必须基于
        # 5分钟最新交易日（day_prefix）的前一交易日。
        # - 收盘后（日线已含当日）：g 最新日期 == day_prefix → prev=iloc[-2]
        # - 盘中/周末（日线只到昨日）：g 最新日期 < day_prefix → prev=iloc[-1]
        # 旧代码固定 iloc[-2]，盘中会取到前日（基准错位，实测偏差 -9%）。
        if len(g) and str(g["date"].iloc[-1]) == day_prefix:
            prev_close = float(g["close"].iloc[-2])
            hi20 = float(g["high"].iloc[-21:-1].max()) if len(g) > 21 else 0
        else:
            prev_close = float(g["close"].iloc[-1])
            hi20 = float(g["high"].iloc[-20:].max()) if len(g) >= 20 else 0
        # 单根均量：归档5分钟优先，日线兜底（一次性算好，避免反复全量加载）
        base_vol = _daily_base(code) or daily_base.get(code)
        sig = detect_ignite(code, day_m5, hi20, prev_close, base_vol)
        last = day_m5[-1]
        last_px = float(last[2])
        chg_now = (last_px / prev_close - 1) * 100 if prev_close else 0
        st = {"code": code, "name": _name(code), "px": last_px,
              "chg": round(chg_now, 1), "hi20": hi20, "prev_close": prev_close,
              "base_vol": base_vol, "ignite": sig,
              "status": "点火" if sig else "观察"}
        tracked.append(st)
        if sig:
            signals.append(st)
            print(f"🔥 点火信号 {code} {_name(code)}: {sig['time']} "
                  f"{sig['px']:.2f}（量比{sig['vol_mult']}× 涨{sig['chg']}% "
                  f"{'破前高' if sig['new_hi'] else '大阳'}）")
        time.sleep(0.8)

    out = {"date": _today(), "updated": datetime.now().strftime("%H:%M:%S"),
           "signals": signals, "tracked": tracked}
    p = paths.state_dir() / "ignite5.json"
    # 去重：对比上次已推送的信号（同 code+time 不重复推，避免盘中每10分钟重复提醒）
    prev_sigs = set()
    if p.exists():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
            for s in (prev.get("signals") or []):
                ig = s.get("ignite") or {}
                prev_sigs.add((str(s.get("code")), str(ig.get("time"))))
        except Exception:  # noqa: BLE001
            pass
    new_sigs = [s for s in signals
                if (str(s.get("code")),
                    str((s.get("ignite") or {}).get("time"))) not in prev_sigs]
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"点火检测完成：{len(signals)} 信号 / {len(tracked)} 追踪（{p}）")
    if new_sigs:
        push_signals(new_sigs)
    elif signals:
        print("点火信号已推过（去重），本次不重复推送")
    return out


def push_signals(signals: list) -> None:
    """点火信号推送（飞书优先，企业微信兜底；无 webhook 则仅落盘）。"""
    if not signals:
        return
    try:
        from mainrise import push
        lines = [f"🔥 盘中点火信号（{_today()}）"]
        for s in signals:
            ig = s.get("ignite") or {}
            nm = s.get("name") or _name(str(s.get("code")))
            lines.append(f"- {nm}({s.get('code')}) {ig.get('time','')} "
                         f"{ig.get('px','')}元 量比{ig.get('vol_mult','')}× "
                         f"涨{ig.get('chg','')}%"
                         f"{' 破前高' if ig.get('new_hi') else ' 大阳'}")
        text = "\n".join(lines)
        # 2026-08-15 审计 M4：飞书失败降级企微，均失败才落盘
        hook = push.get_feishu_webhook()
        if hook and push.send_feishu(text, hook):
            print(f"点火信号已推飞书（{len(signals)} 条）：成功")
            return
        if hook:
            print("⚠ 飞书发送失败，降级企业微信")
        wh = push.get_wecom_webhook()
        if wh:
            ok = push.send_wecom(text, wh)
            print(f"点火信号已推企业微信（{len(signals)} 条）：{'成功' if ok else '失败'}")
            return
        print("⚠ 未配置飞书/企业微信 webhook，点火信号仅落盘未推送")
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 点火推送失败（不影响落盘）: {e}")


def _dynamic_cands() -> list:
    """点火跟踪标的 = 大牛候选池（bigbull_cands.json 的 cands，已满足买入
    条件：热主题 + 90日T0≥3 + 评分≥2，45 日硬规则信号）。

    2026-08-15 用户明确：点火跟踪只针对大牛候选池的个股（当前 3 只：
    003031/000938/603662），不需要 90日T0≥2 的扩展跟踪。候选池由
    bigbull 每日 17:30 落盘，盘中新信号次日自动进入；买卖点卡/飞书
    推送的点火信号只出自这批个股。
    """
    cands: list[str] = []
    try:
        p = paths.state_dir() / "bigbull_cands.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        cands = [str(c.get("code")) for c in (data.get("cands") or [])]
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(cands))


def _name(code: str) -> str:
    try:
        from mainrise.signals import load_names
        return load_names().get(code, "")
    except Exception:  # noqa: BLE001
        return ""


def _today() -> str:
    from mainrise.push import beijing_now
    return beijing_now().strftime("%Y-%m-%d")


def write_report() -> str:
    """收盘后汇总报告（读 ignite5.json + 当日点火信号的后续表现）。"""
    p = paths.state_dir() / "ignite5.json"
    data = {}
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    L = [f"# 盘中点火信号（{_today()}）", "",
         "> 5 分钟级'资金点火进攻'检测（候选池标的）：单根放量>2×均量 且"
         "（突破前20日高 或 当日涨幅≥5%）→ 点火，盘中择机买入（买价通常低于收盘3~6%）。",
         "> 研究基准 2026-08-15（11 样本）：点火买入 +3.1% vs 收盘 -0.1%，10/11 更优。",
         ""]
    sigs = data.get("signals") or []
    L.append(f"## 当日点火信号（{len(sigs)} 个）")
    L.append("")
    if sigs:
        L.append("| 代码 | 名称 | 时点 | 点火价 | 量比× | 涨幅 | 破前高 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s in sigs:
            ig = s.get("ignite") or {}
            L.append(f"| {s['code']} | {s.get('name','')} | {ig.get('time','')} "
                     f"| {ig.get('px','')} | {ig.get('vol_mult','')}× "
                     f"| {ig.get('chg','')}% | {'是' if ig.get('new_hi') else '否'} |")
    else:
        L.append("（无点火信号）")
    L.append("")
    L.append(f"## 追踪标的实时状态（{len(data.get('tracked') or [])} 只）")
    L.append("")
    L.append("> 完整状态见 output/state/ignite5.json；月度优化见 m5optimize。")
    L.append("")
    L.append("> 研究线索，不构成投资建议。")
    md = paths.report_dir() / f"点火信号_{_today()}.md"
    md.write_text("\n".join(L), encoding="utf-8")
    return str(md)


def main() -> None:
    ap = argparse.ArgumentParser(description="盘中5分钟点火信号检测（候选池）")
    ap.add_argument("--report", action="store_true", help="收盘后生成汇总报告")
    args = ap.parse_args()
    if args.report:
        print(write_report())
    else:
        run_once()


if __name__ == "__main__":
    main()
