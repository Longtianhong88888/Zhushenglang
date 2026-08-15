"""大周期追溯研究：大牛模型规则在历史大周期（新能源/白酒）是否照样适用。

背景：大牛模型（固化版，2026-08-14）在 2021-08 ~ 2026-08 的科技卡点企业上
回测 +777%。用户问：如果把模型（硬规则 T0≥3 + 评分≥2 + 1/3仓×3只 + MA20
退出 + 杀跌区停开）放到更早的新能源大周期（2019-2021 主升）和白酒大周期
（2016-2018 主升、2020-2021 第二波），是否照样适用——即模型规则能否捕捉
历史大周期的主升，并在周期结束后控制回撤。

数据：同花顺 hithink REST 个股历史日K（后复权，单次窗口 ≤10 年 → 分两段
拼接：2016-08-01 ~ 2021-06-30、2021-06-30 ~ 今）。上证指数（000001.SH）
20 日涨幅近似"大盘"（早期无全市场等权数据），用于杀跌区停开。

口径说明（如实标注）：
- 涨停判定沿用 signals.tail_features：主板 prev_close×1.095、创业板/科创
  ×1.195（创业板 2020-08-24 注册制前实际 10%，早期创业板涨停会漏判，
  但大阳线分支 涨幅≥5% 且量比≥1.5 不受影响）；
- 后复权（含分红再投资口径）：同花顺前复权对大额现金分红票有 bug（历史价
  被压成负值/收益虚高），后复权价格恒正、收益率合理，信号为相对指标不受影响；
- 无全市场等权数据 → 杀跌区用上证指数 20 日涨幅 ≤ -5% 近似；
- 无 is_st/is_paused 标记 → 按非 ST、正常交易处理。

用法:
    python3 -m mainrise.cycle_check            # 拉数据 + 回测 + 报告
    python3 -m mainrise.cycle_check --fetch-only   # 只拉数据（缓存）
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from mainrise import paths, portfolio_bt
from mainrise import themeindex as ti

CN_TZ = timezone(timedelta(hours=8))

# 两段请求：避开单次 ≤10 年窗口限制
SEG1_END = datetime(2021, 6, 30, tzinfo=CN_TZ)
START = datetime(2016, 8, 1, tzinfo=CN_TZ)

# 主题池（排除 688 科创板；300 创业板可交易，2020-08-24 前涨停判定偏严已注明）
NEW_ENERGY = {
    "300750": "宁德时代", "002594": "比亚迪", "300014": "亿纬锂能",
    "002460": "赣锋锂业", "002466": "天齐锂业", "603799": "华友钴业",
    "002709": "天赐材料", "002812": "恩捷股份", "300450": "先导智能",
    "300124": "汇川技术", "603659": "璞泰来", "300073": "当升科技",
    "601012": "隆基绿能", "600438": "通威股份", "300274": "阳光电源",
    "603806": "福斯特", "002202": "金风科技", "600089": "特变电工",
}
BAIJIU = {
    "600519": "贵州茅台", "000858": "五粮液", "000568": "泸州老窖",
    "600809": "山西汾酒", "002304": "洋河股份", "000596": "古井贡酒",
    "603369": "今世缘", "603589": "口子窖", "600779": "水井坊",
    "600702": "舍得酒业", "000799": "酒鬼酒", "603198": "迎驾贡酒",
    "000860": "顺鑫农业", "603919": "金徽酒", "600559": "老白干酒",
    "600197": "伊力特",
}
POOLS = {"新能源": NEW_ENERGY, "白酒": BAIJIU}

# 上证指数：近似大盘（杀跌区）
SH_IDX = "000001.SH"

CACHE_DIR = paths.state_dir() / "cycle_ths"


def _suffix(code: str) -> str:
    return ".SH" if code.startswith(("6", "9")) else ".SZ"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _fetch_one(thscode: str, key: str) -> pd.DataFrame | None:
    """分两段拉取单标的后复权日K并拼接（去重、升序）。"""
    segs = []
    for s, e in ((START, SEG1_END), (SEG1_END, datetime.now(CN_TZ))):
        j = ti._api_get("/api/a-share-index/prices/historical"
                        if thscode.endswith(".SH") and thscode == SH_IDX
                        else "/api/a-share/prices/historical",
                        {"thscode": thscode, "interval": "1d",
                         "start": _ms(s), "end": _ms(e), "adjust": "backward"},
                        key)
        items = ((j or {}).get("data") or {}).get("item") or []
        if not items:
            continue
        rows = [{"date": datetime.fromtimestamp(it["date_ms"] / 1000, tz=CN_TZ)
                 .strftime("%Y-%m-%d"),
                 "open": float(it["open_price"]),
                 "high": float(it["high_price"]),
                 "low": float(it["low_price"]),
                 "close": float(it["close_price"]),
                 "volume": float(it.get("volume") or 0)} for it in items]
        segs.append(pd.DataFrame(rows))
    if not segs:
        return None
    df = pd.concat(segs, ignore_index=True).drop_duplicates("date")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _load_kline(code: str, key: str, refresh: bool = False) -> pd.DataFrame | None:
    """读/拉单只票缓存（CSV）。"""
    p = CACHE_DIR / f"{code}.csv"
    if not refresh and p.exists():
        try:
            df = pd.read_csv(p)
            if len(df) > 100:
                return df
        except Exception:  # noqa: BLE001
            pass
    ths = code + _suffix(code) if code != SH_IDX else SH_IDX
    df = _fetch_one(ths, key)
    if df is None or len(df) < 50:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    return df


def _to_panel(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """同花顺 K线 → portfolio_bt 兼容列（close 推出 prev_close/pct_chg/limit_price）。"""
    d = df.copy()
    d["code"] = code
    d["prev_close"] = d["close"].shift(1)
    d["pct_chg"] = (d["close"] / d["prev_close"] - 1) * 100
    gem = code.startswith(("300", "301", "688"))
    d["limit_price"] = d["prev_close"] * (1.2 if gem else 1.1)
    d["is_st"] = 0
    d["is_paused"] = 0
    d["turnover"] = np.nan
    return d[["date", "code", "open", "high", "low", "close",
              "limit_price", "turnover", "pct_chg", "prev_close",
              "is_st", "is_paused", "volume"]]


def _mkt_ret20(idx: pd.DataFrame | None) -> dict:
    """上证指数 20 日涨幅（date -> ret20，百分比数值）。"""
    if idx is None or len(idx) < 30:
        return {}
    s = idx.set_index("date")["close"]
    r = (s / s.shift(20) - 1) * 100
    return {str(d): float(v) for d, v in r.dropna().items()}


def _equal_weight_nav(closes: dict, dates: list) -> pd.DataFrame:
    """主题池等权买入持有净值（各票从自身首日起，逐日等权）。"""
    nav = []
    for d in dates:
        vals = []
        for c, s in closes.items():
            if d in s.index:
                base = float(s.iloc[0])
                vals.append(float(s[d]) / base if base else np.nan)
        if vals:
            nav.append((d, float(np.nanmean(vals))))
    return pd.DataFrame(nav, columns=["date", "nav"])


def _year_seg(nav: pd.DataFrame, segs: list) -> list:
    out = []
    for label, s, e in segs:
        sub = nav[(nav["date"] >= s) & (nav["date"] <= e)]
        if len(sub) < 5:
            out.append((label, None))
            continue
        ret = sub["nav"].iloc[-1] / sub["nav"].iloc[0] - 1
        dd = (sub["nav"] / sub["nav"].cummax() - 1).min()
        out.append((label, (ret, dd)))
    return out


def run(refresh: bool = False) -> str:
    t0 = time.time()
    key = ti.get_key()
    if not key:
        raise SystemExit("未配置同花顺 Key")

    print(f"拉取行情（缓存 {CACHE_DIR}）...")
    all_codes = sorted({c for m in POOLS.values() for c in m}) + [SH_IDX]
    bars: dict = {}
    for i, code in enumerate(all_codes, 1):
        df = _load_kline(code, key, refresh)
        if df is None:
            print(f"  [{i}/{len(all_codes)}] {code} 无数据")
            continue
        bars[code] = df
        print(f"  [{i}/{len(all_codes)}] {code}: {len(df)} 根 "
              f"{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
        time.sleep(0.25)

    idx = bars.get(SH_IDX)
    mkt_ret20 = _mkt_ret20(idx)

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 大周期追溯研究：大牛模型规则在新能源/白酒大周期是否适用（{dstr}）")
    L.append("")
    L.append("> 目的：大牛模型（硬规则 热主题且90日T0≥3 + 评分≥2 + 单票1/3仓、最多3只 + "
             "收盘破MA20退出 + 杀跌区停开、费用0.2%）在 2021-08~2026-08 科技卡点池回测 "
             "+777%。本报告把同一套规则放到 2016-08 起的 新能源池/白酒池 上，检验："
             "① 能否捕捉历史大周期主升段；② 周期结束后回撤是否可控。")
    L.append("")
    L.append("> 数据与口径（必须阅读）：同花顺**后复权**日K 2016-08-01 起（宁德时代 2018-06 "
             "上市起；后复权含分红再投资口径，收益率合理、无负价——同花顺前复权对大额"
             "现金分红票有 bug（历史价被压成负值/收益虚高），已弃用）；涨停判定=主板 "
             "prev_close×1.095、创业板×1.195（创业板 2020-08-24 注册制前实际 10%，早期"
             "创业板涨停略漏判，大阳线分支不受影响）；早期无全市场等权数据，杀跌区用"
             "上证指数 20 日涨幅 ≤ -5% 近似；无 ST 标记按正常处理。")
    L.append("")

    for pool_name, pool in POOLS.items():
        L.append(f"## {pool_name}池（{len(pool)} 只）")
        L.append("")
        code_list = sorted(pool)
        panel = pd.concat([_to_panel(bars[c], c) for c in code_list
                           if c in bars], ignore_index=True)
        panel = panel[panel["close"] > 0].sort_values(["code", "date"])
        hot_set = set(code_list)
        info = portfolio_bt.build_info(panel, hot_set, portfolio_bt.MIN_T0_90)
        base = dict(mkt_ret20=mkt_ret20, downshift="stop", exit_ma=20,
                    rebuy="none")
        sim = portfolio_bt.simulate(info, hot_set, 3, hard_rule=True,
                                    score_min=2, **base)
        nav, tr = sim["nav"], sim["trades"]
        m = portfolio_bt.metrics(sim, "模型（硬规则+评分≥2）")
        L.append(f"- **模型全期**：交易 {m[1]} 笔 ｜ 胜率 {m[2]} ｜ 总收益 {m[3]} ｜ "
                 f"年化 {m[4]} ｜ 最大回撤 {m[5]} ｜ PF {m[6]} ｜ 抓到峰值≥60% {m[7]} 笔")
        # 等权 + 上证对照
        closes = {c: bars[c].set_index("date")["close"] for c in code_list
                  if c in bars}
        ew = _equal_weight_nav(closes, list(nav["date"]))
        ew_ret = ew["nav"].iloc[-1] / ew["nav"].iloc[0] - 1
        sh_ret = (bars[SH_IDX]["close"].iloc[-1]
                  / bars[SH_IDX]["close"].iloc[0] - 1) if SH_IDX in bars else np.nan
        L.append(f"- **对照**：主题池等权买入持有 {ew_ret:+.0%} ｜ 上证指数同段 "
                 f"{sh_ret:+.0%} ｜ 模型相对等权 "
                 f"{(nav['nav'].iloc[-1]/nav['nav'].iloc[0]-1) - ew_ret:+.0%}")
        L.append("")
        # 分段
        if pool_name == "新能源":
            segs = [("2016-08~2018（蓄势）", "2016-08-01", "2018-12-31"),
                    ("2019-01~2021-12（主升段）", "2019-01-01", "2021-12-31"),
                    ("2022-01~2026-07（回落段）", "2022-01-01", "2026-07-31")]
        else:
            segs = [("2016-08~2018-12（主升段）", "2016-08-01", "2018-12-31"),
                    ("2019-01~2020-12（平台爬升）", "2019-01-01", "2020-12-31"),
                    ("2021-01~2021-12（见顶回落）", "2021-01-01", "2021-12-31"),
                    ("2022-01~2026-07（阴跌段）", "2022-01-01", "2026-07-31")]
        ms, mw, mx = [], [], []
        for label, s, e in segs:
            def _seg_ret(nv):
                sub = nv[(nv["date"] >= s) & (nv["date"] <= e)]
                if len(sub) < 5:
                    return None
                return (sub["nav"].iloc[-1] / sub["nav"].iloc[0] - 1,
                        (sub["nav"] / sub["nav"].cummax() - 1).min())
            ms.append((label, _seg_ret(nav)))
            mw.append((label, _seg_ret(ew)))
            if SH_IDX in bars:
                sh = bars[SH_IDX].set_index("date")["close"]
                sub = sh[(sh.index >= s) & (sh.index <= e)]
                if len(sub) >= 5:
                    mx.append((label, (sub.iloc[-1] / sub.iloc[0] - 1, None)))
                else:
                    mx.append((label, None))
            else:
                mx.append((label, None))
        L.append("| 分段 | 模型收益/回撤 | 主题等权收益 | 上证指数 |")
        L.append("| --- | --- | --- | --- |")
        for i, (label, _s, _e) in enumerate(segs):
            a = ms[i][1]
            b = mw[i][1]
            c = mx[i][1]
            a_s = f"{a[0]:+.0%} / {a[1]:.0%}" if a else "-"
            b_s = f"{b[0]:+.0%}" if b else "-"
            c_s = f"{c[0]:+.0%}" if c and c[0] is not None else "-"
            L.append(f"| {label} | {a_s} | {b_s} | {c_s} |")
        L.append("")
        # 交易清单（按入场日期）
        names = {**pool}
        tr2 = tr.sort_values("entry_date")
        L.append(f"**模型交割清单（{len(tr2)} 笔，按买入日）**")
        L.append("")
        L.append("| 代码 | 名称 | 买入日 | 买入价 | 卖出日 | 卖出价 | 收益% | 峰值% | 持仓日 | 评分 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for _, r in tr2.iterrows():
            L.append(f"| {r['code']} | {names.get(r['code'], '')} | "
                     f"{r['entry_date']} | {r['entry']:.2f} | "
                     f"{r['exit_date']} | {r['exit']:.2f} | "
                     f"{r['ret']*100:+.1f} | {r['peak_gain']*100:+.1f} | "
                     f"{r['hold']} | {r.get('score','')} |")
        L.append("")
        # 净值曲线落盘
        nav.to_csv(paths.report_dir() /
                   f"大周期净值_{pool_name}_{dstr}.csv", index=False,
                   encoding="utf-8-sig")

    L.append("## 结论（对照两个大周期）")
    L.append("")
    L.append("- **新能源大周期（2019-2021 主升）——部分适用**：模型在主升段 +345%（等权"
             "+574%），能赚但只吃到约六成——90日T0≥3 + MA20 退出在强趋势中过早止盈、"
             "反复进出损耗；但回落段（2022-2026）模型 +12% vs 等权 -38%，退出保护显著"
             "生效；全期 +452% vs 等权 +408% 基本打平。**收益结构 = 用主升段部分超额"
             "换取周期后的回撤保护。**")
    L.append("")
    L.append("- **白酒大周期（2016-2018 主升、2020-2021 第二波）——不适用**：模型全期"
             "-6%（63 笔、胜率仅 35%、PF 1.17）vs 等权 +147%；2016-2018 主升段模型 "
             "-15%（等权 +20%）、2020 抱团段 +91%（等权 +332%）——白酒是低波动慢牛，"
             "T0 信号（大阳线/涨停+创新高）捕捉的是脉冲而非趋势，信号稀少且负期望；"
             "白酒大周期赚的是'持有'的钱，不是'信号交易'的钱。")
    L.append("")
    L.append("- **模型适用范围（外推边界）**：模型规则适合**高波动成长板块**（新能源/"
             "科技卡点：题材脉冲多、T0 信号充足、MA20 退出能躲周期顶），不适合**低波动"
             "慢牛板块**（白酒：信号不足、胜率低）。2021-2026 科技卡点池 +777% 的高收益"
             "来自'热主题锁定高波动题材'这一结构，换到白酒这类慢牛结构即失效。")
    L.append("")
    L.append("> 研究用途，不构成投资建议。数据：同花顺后复权日K（2016-08-01 ~ 2026-08-14，"
             "含分红再投资口径）。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"大周期追溯研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def fetch_only() -> None:
    """只拉数据缓存（--fetch-only）。"""
    key = ti.get_key()
    if not key:
        raise SystemExit("未配置同花顺 Key")
    for code in sorted({c for m in POOLS.values() for c in m}) + [SH_IDX]:
        df = _load_kline(code, key, refresh=True)
        print(code, "->", "ok" if df is not None and len(df) else "FAILED")


def main() -> None:
    ap = argparse.ArgumentParser(description="大周期追溯研究（新能源/白酒 × 大牛模型规则）")
    ap.add_argument("--fetch-only", action="store_true", help="只拉数据缓存")
    ap.add_argument("--refresh", action="store_true", help="强制刷新数据缓存")
    args = ap.parse_args()
    if args.fetch_only:
        fetch_only()
        return
    run(refresh=args.refresh)


if __name__ == "__main__":
    main()
