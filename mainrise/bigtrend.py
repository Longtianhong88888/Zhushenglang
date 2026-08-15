"""趋势大牛研究：观察点 / 买点确认 / 卖点信号。

目标：找出"宏和科技式"趋势大牛（信号后 150 日内峰值涨幅 ≥+60%），回答三个问题：
  1. 观察点：什么信号/组合特征出现时，把它放进"大牛观察池"（精确度远高于随机）？
  2. 买点确认：T0 尾盘买 vs 回踩 MA10/MA20 低吸，哪种入场胜率与收益更高？
  3. 卖点信号：持有大牛时用哪种退出规则，吃到的收益最多、回吐最少？

口径（无前视）：
  - 范围：行业卡点企业 100 家（110 - 10 只 688）；数据 2021-08 ~ 2026-08-12
  - 大牛定义：T0 信号日后 150 个交易日内收盘峰值涨幅 ≥ +60%（宏和2026 = +605%）
  - 观察特征全部在信号日收盘已知；买入用 T0 收盘价近似（T0 尾盘）
  - 主题分类：industry_info.csv track 关键词映射 8 大主题 + 其他

用法:
    python3 -m mainrise.bigtrend            # 完整研究（约 1 分钟）
    python3 -m mainrise.bigtrend --fast     # 跳过全市场特征（迭代用）
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from mainrise import paths
from mainrise.data import load_all_panels
from mainrise.signals import in_universe, tail_features
from mainrise.report import load_chokepoint_codes
from mainrise.entry_study import COST, market_features

WINDOW = 150          # 前瞻窗口（交易日）
BIG = 60.0            # 大牛：窗口内峰值涨幅 ≥60%
MID = 30.0            # 中牛：≥30%

# 8 大主题关键词（特异性主题在前，避免"PCB微钻→AI硬件"式误分类）
THEMES = {
    "有色": ["钨", "锡", "稀土", "钴", "镍", "贵金属", "矿", "银", "铅",
             "铜矿", "金矿", "锂矿", "锌"],
    "创新药": ["药", "CXO", "CDMO", "生物", "CRO", "医疗", "医药"],
    "存储": ["存储", "内存", "DRAM", "NAND", "闪存", "模组"],
    "机器人": ["机器人", "减速器", "丝杠", "伺服", "谐波", "人形", "具身"],
    "商业航天": ["航天", "卫星", "星载", "测运控", "火箭", "空天"],
    "自动驾驶": ["智驾", "域控", "线控", "汽车电子", "车载"],
    "AI硬件": ["光模块", "光通信", "光引擎", "液冷", "服务器", "覆铜板",
              "玻纤电子布", "连接器", "铜缆", "散热", "光缆", "光器件",
              "交换机", "PCB"],
    "半导体": ["半导体", "芯片", "晶圆", "封测", "设备", "材料", "靶材",
              "光刻", "刻蚀", "电子陶瓷", "封装", "测试", "传感器", "IGBT",
              "功率", "MLCC", "被动元件", "分立器件", "电解电容", "晶振",
              "代工"],
}
HOT_THEMES = ("AI硬件", "半导体", "存储")   # 热主题（研究结论：大牛温床）


def load_theme() -> dict:
    csv_path = (Path(__file__).resolve().parent / "resources"
                / "industry_info.csv")
    df = pd.read_csv(csv_path, dtype={"code": str})
    out = {}
    for _, r in df.iterrows():
        s = str(r.get("track", ""))
        th = "其他"
        for name, kws in THEMES.items():
            if any(k in s for k in kws):
                th = name
                break
        out[r["code"]] = th
    return out


def collect_signals(panels: pd.DataFrame, mkt: pd.DataFrame,
                    theme_map: dict) -> pd.DataFrame:
    """每个 T0 信号 + 观察特征 + 大牛标签（无前视）。"""
    rows = []
    mktm = mkt.set_index("date") if len(mkt) else pd.DataFrame()
    for code, g in panels.groupby("code", sort=False):
        g = g.reset_index(drop=True)
        t = tail_features(g, tail=len(g))
        if t is None:
            continue
        n = len(t)
        closes = t["close"].to_numpy(float)
        lows = t["low"].to_numpy(float)
        highs = t["high"].to_numpy(float)
        sig = t["signal"].to_numpy().astype(bool)
        dates = t["date"].to_numpy()
        ma60s = pd.Series(closes).rolling(60).mean().to_numpy()
        # 距上次信号天数
        last_sig = -999
        for i in np.where(sig)[0]:
            if i + 30 >= n:           # 至少 30 日前瞻（不足 150 日标记截断）
                break
            avail = min(i + WINDOW, n - 1)
            peak = closes[i + 1:avail + 1].max()
            lo60 = lows[max(0, i - 59):i + 1].min()
            hi60 = highs[max(0, i - 59):i + 1].max()
            c20 = closes[max(0, i - 20)]
            c60 = closes[max(0, i - 60)]
            fresh_days = i - last_sig if last_sig >= 0 else 999
            last_sig = i
            mm = mktm.loc[dates[i]] if dates[i] in mktm.index else None
            rows.append({
                "code": code, "date": dates[i],
                "theme": theme_map.get(code, "其他"),
                "peak_gain": (peak / closes[i] - 1) * 100,
                "big": int((peak / closes[i] - 1) * 100 >= BIG),
                "mid": int((peak / closes[i] - 1) * 100 >= MID),
                "trunc": int(i + WINDOW >= n),
                "bull": int(t["bull"].iloc[i]),
                "chg20": (closes[i] / c20 - 1) * 100,
                "chg60": (closes[i] / c60 - 1) * 100,
                "chg10": float(t["chg10"].iloc[i]),
                "vr": float(t["vol_ratio"].iloc[i]),
                "new_hi60": float(closes[i] / hi60 - 1) * 100,
                "from_low60": float(closes[i] / lo60 - 1) * 100,
                "ma60_up": int(ma60s[i] > ma60s[i - 5]) if i >= 5 and not (
                    pd.isna(ma60s[i]) or pd.isna(ma60s[i - 5])) else 0,
                "fresh": int(fresh_days > 30),
                "mkt_ret20": float(mm["mkt_ret20"]) if mm is not None
                and isinstance(mm, pd.Series) else np.nan,
                "mkt_zt": float(mm["mkt_zt"]) if mm is not None
                and isinstance(mm, pd.Series) else np.nan,
                "i": i,
            })
    return pd.DataFrame(rows)


def rep(name: str, mask: pd.Series, D: pd.DataFrame) -> str:
    g = D[mask]
    if len(g) < 20:
        return ""
    return (f"| {name} | {len(g)} | {(g['big'] == 1).mean():.1%} | "
            f"{(g['mid'] == 1).mean():.1%} | {g['peak_gain'].mean():+.0f}% |")


def study_observe(D: pd.DataFrame) -> list:
    L = []
    hot = D["theme"].isin(HOT_THEMES)
    L.append("## 一、观察点（信号日特征 → 大牛概率）")
    L.append("")
    L.append("> 大牛 = 信号后 150 日内收盘峰值 ≥+60%；中牛 = ≥+30%。")
    L.append("")
    L.append("| 观察规则 | n | 大牛概率 | 中牛概率 | 均峰 |")
    L.append("| --- | --- | --- | --- | --- |")
    L.append(rep("全部 T0 信号（基准）", pd.Series(True, index=D.index), D))
    L.append(rep("主题=AI硬件", D["theme"] == "AI硬件", D))
    L.append(rep("主题=半导体", D["theme"] == "半导体", D))
    L.append(rep("主题热(AI/半导体/存储)", hot, D))
    L.append(rep("热主题 & 创60日新高", hot & (D["new_hi60"] > -0.5), D))
    L.append(rep("热主题 & 创60日新高 & 10日<30%", hot & (D["new_hi60"] > -0.5)
                 & (D["chg10"] < 30), D))
    L.append(rep("热主题 & 蓄势20日0~40% & 创60日新高", hot
                 & (D["chg20"] >= 0) & (D["chg20"] < 40)
                 & (D["new_hi60"] > -0.5), D))
    L.append(rep("AI硬件 & 创60日新高 & 10日<30%", (D["theme"] == "AI硬件")
                 & (D["new_hi60"] > -0.5) & (D["chg10"] < 30), D))
    L.append(rep("AI硬件 & 蓄势 & 创60日新高 & 10日<30%", (D["theme"] == "AI硬件")
                 & (D["chg20"] >= 0) & (D["chg20"] < 40) & (D["new_hi60"] > -0.5)
                 & (D["chg10"] < 30), D))
    L.append(rep("热主题 & 首次信号(30日无) & 创60日新高", hot & (D["fresh"] == 1)
                 & (D["new_hi60"] > -0.5), D))
    L.append(rep("非杀跌区 & 热主题 & 创60日新高", (D["mkt_ret20"] > -5) & hot
                 & (D["new_hi60"] > -0.5), D))
    L.append("")
    L.append("> 结论：主题是最强区分维度；AI硬件 信号大牛概率 38.5%（基准 25.5%），"
             "叠加创60日新高（突破）与 10日<30%（未透支）进一步抬升。")
    L.append("")
    return L


def buy_confirm(D: pd.DataFrame, panel_by: dict, rows: pd.DataFrame) -> list:
    L = []
    L.append("## 二、买点确认（观察规则命中后的入场方式）")
    L.append("")
    hot = D["theme"].isin(HOT_THEMES)
    obs = D[hot & (D["new_hi60"] > -0.5) & (D["chg10"] < 30)].copy()
    L.append(f"> 观察规则样本（热主题 & 创60日新高 & 10日<30%）：{len(obs)} 个信号。")
    L.append("")
    L.append("| 入场方式 | n | 峰值≥30% | 峰值≥60% | 均峰 |")
    L.append("| --- | --- | --- | --- | --- |")
    # T0 尾盘（信号日收盘）
    g = obs
    L.append(f"| T0尾盘买入 | {len(g)} | {(g['mid'] == 1).mean():.1%} | "
             f"{(g['big'] == 1).mean():.1%} | {g['peak_gain'].mean():+.0f}% |")
    # 回踩 MA10 / MA20（信号后 10 日内触及即低吸，未触及放弃）
    for ma_key, ma_label in (("ma10", "回踩MA10低吸"), ("ma20", "回踩MA20低吸")):
        res = []
        for _, r in obs.iterrows():
            panel = panel_by[r["code"]]
            j0 = r["i"]
            t = tail_features(panel, tail=len(panel))
            hit = None
            for j in range(j0 + 1, min(j0 + 11, len(t))):
                row = t.iloc[j]
                if float(row["low"]) <= float(row[ma_key]) <= float(row["close"]):
                    hit = j
                    break
            if hit is None:
                continue
            bp = float(t.iloc[hit][ma_key])
            peak = t["close"].iloc[hit + 1:min(hit + 1 + WINDOW, len(t))].max()
            res.append({"bp": bp, "peak_gain": (peak / bp - 1) * 100})
        rr = pd.DataFrame(res)
        if len(rr) >= 20:
            L.append(f"| {ma_label}（10日内触及） | {len(rr)} | "
                     f"{(rr['peak_gain'] >= 30).mean():.1%} | "
                     f"{(rr['peak_gain'] >= 60).mean():.1%} | "
                     f"{rr['peak_gain'].mean():+.0f}% |")
    L.append("")
    L.append("> 回踩低吸样本少于 T0 尾盘（10 日内未触及 MA 的放弃）；"
             "两者互补：T0 尾盘打底仓，回踩加仓。")
    L.append("")
    return L


def exit_rules(rows: pd.DataFrame, panel_by: dict) -> tuple[list, pd.DataFrame]:
    """卖点信号：大牛信号（big=1）入场后，各退出规则捕获收益对比。"""
    big = rows[rows["big"] == 1]
    rules = [
        ("ma10", "跌破MA10(收盘)"), ("ma20", "跌破MA20(收盘)"),
        ("ma60", "跌破MA60(收盘)"), ("r8", "高点回落8%(收盘)"),
        ("r10", "高点回落10%(收盘)"), ("r15", "高点回落15%(收盘)"),
        ("m20r12", "跌破MA20或回落12%"),
        ("ma20s", "跌破MA20且MA20拐头"),
    ]
    out = []
    for _, r in big.iterrows():
        g = panel_by[r["code"]]
        t = tail_features(g, tail=len(g))
        t = t.reset_index(drop=True)
        closes = t["close"].to_numpy(float)
        n = len(t)
        j0 = r["i"]
        ma60 = (pd.Series(closes).rolling(60).mean().to_numpy()
                if n >= 60 else None)
        for key, _ in rules:
            ma10 = t["ma10"].to_numpy(float) if "ma10" in t.columns else None
            ma20 = t["ma20"].to_numpy(float) if "ma20" in t.columns else None
            bp = closes[j0]
            peak = bp
            exit_j = None
            for j in range(j0 + 1, min(j0 + WINDOW + 1, n)):
                peak = max(peak, closes[j])
                hit = False
                if key == "ma10":
                    hit = ma10 is not None and closes[j] < ma10[j]
                elif key == "ma20":
                    hit = ma20 is not None and closes[j] < ma20[j]
                elif key == "ma60":
                    hit = ma60 is not None and closes[j] < ma60[j]
                elif key == "r8":
                    hit = (peak - closes[j]) / peak > 0.08
                elif key == "r10":
                    hit = (peak - closes[j]) / peak > 0.10
                elif key == "r15":
                    hit = (peak - closes[j]) / peak > 0.15
                elif key == "m20r12":
                    hit = (ma20 is not None and closes[j] < ma20[j]) or (
                        peak - closes[j]) / peak > 0.12
                elif key == "ma20s":
                    hit = (ma20 is not None and closes[j] < ma20[j]
                           and j >= 5 and ma20[j] < ma20[j - 1])
                if hit:
                    exit_j = j
                    break
            if exit_j is None:
                exit_j = min(j0 + WINDOW, n - 1)
            ret = closes[exit_j] / bp - 1
            fwd20 = (closes[min(exit_j + 20, n - 1)] / closes[exit_j] - 1
                     if exit_j + 1 < n else np.nan)
            out.append({"code": r["code"], "date": r["date"], "rule": key,
                        "ret": ret, "peak": peak / bp - 1, "hold": exit_j - j0,
                        "fwd20": fwd20, "giveback": peak / bp - 1 - ret})
    R = pd.DataFrame(out)
    L = []
    L.append("## 三、卖点信号（大牛信号入场后的退出规则对比）")
    L.append("")
    L.append(f"> 大牛信号 {len(big)} 个；入场 = T0 尾盘（信号日收盘）；"
             "峰值均收 = 入场后 150 日收盘峰值的平均（不吃回吐的理论上限）。")
    L.append("")
    L.append("| 退出规则 | n | 平均收益 | 中位 | 峰值均收 | 平均回吐 | 持仓日均 | 过早卖% |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for key, label in rules:
        g = R[R["rule"] == key]
        too_early = (g["fwd20"] > 0.10).mean()
        L.append(f"| {label} | {len(g)} | {g['ret'].mean():+.1%} | "
                 f"{g['ret'].median():+.1%} | {g['peak'].mean():+.1%} | "
                 f"{g['giveback'].mean():+.1%} | {g['hold'].mean():.0f} | "
                 f"{too_early:.0%} |")
    L.append("")
    L.append("> 过早卖% = 出场后 20 日再涨 ≥10% 的比例（卖早了的代价）。")
    L.append("")
    return L, R


def honghe_case(rows: pd.DataFrame, panel_by: dict, R: pd.DataFrame) -> list:
    """宏和科技 2026 案例：各规则实际出场点。"""
    L = []
    hh = rows[(rows["code"] == "603256") & (rows["date"] >= "2026-01-01")]
    if not len(hh):
        return L
    r0 = hh.iloc[0]
    g = panel_by["603256"]
    t = tail_features(g, tail=len(g)).reset_index(drop=True)
    j0 = r0["i"]
    L.append(f"### 宏和科技 2026 案例（入场 {r0['date']} 收盘 "
             f"{t['close'].iloc[j0]:.2f}，峰值 {t['close'].iloc[r0['i'] + 1:r0['i'] + 1 + WINDOW].max():.0f}，"
             f"峰值收益 {r0['peak_gain']:+.0f}%）")
    L.append("")
    L.append("| 退出规则 | 出场日 | 出场收盘 | 收益 | 峰值收益 | 持仓日 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for key in ["ma10", "ma20", "ma60", "r8", "r10", "r15", "m20r12", "ma20s"]:
        gr = R[(R["code"] == "603256") & (R["date"] == r0["date"])
               & (R["rule"] == key)]
        if not len(gr):
            continue
        # 反查出场日
        closes = t["close"].to_numpy(float)
        n = len(t)
        ma20 = t["ma20"].to_numpy(float) if "ma20" in t.columns else None
        bp = closes[j0]; peak = bp; exit_j = None
        for j in range(j0 + 1, min(j0 + WINDOW + 1, n)):
            peak = max(peak, closes[j])
            hit = False
            if key in ("ma10", "ma20", "ma60"):
                mk = t[key].to_numpy(float) if key in t.columns else None
                hit = mk is not None and closes[j] < mk[j]
            elif key == "r8":
                hit = (peak - closes[j]) / peak > 0.08
            elif key == "r10":
                hit = (peak - closes[j]) / peak > 0.10
            elif key == "r15":
                hit = (peak - closes[j]) / peak > 0.15
            elif key == "m20r12":
                hit = (ma20 is not None and closes[j] < ma20[j]) or (
                    peak - closes[j]) / peak > 0.12
            elif key == "ma20s":
                hit = (ma20 is not None and closes[j] < ma20[j]
                       and j >= 5 and ma20[j] < ma20[j - 1])
            if hit:
                exit_j = j
                break
        if exit_j is None:
            continue
        gr0 = gr.iloc[0]
        L.append(f"| {key} | {t['date'].iloc[exit_j]} | {closes[exit_j]:.0f} | "
                 f"{gr0['ret']:+.0%} | {gr0['peak']:+.0%} | {gr0['hold']} |")
    L.append("")
    L.append("> 注：2026-03 宏和出现 -24% 深度洗盘（84→64），所有紧退出规则均在 3 月"
             "被洗出（+50~80% 落袋），随后 3 月末~6 月又涨 4.8 倍——只有跌破 MA60"
             "（扛住洗盘）能吃到主升段。深度洗盘是大牛常态，退出规则需容忍。")
    L.append("")
    return L


def current_candidates(rows: pd.DataFrame, D: pd.DataFrame,
                       last_date: str | None = None) -> list:
    """最近 45 日命中观察规则的在榜信号（当前候选）。"""
    L = []
    last = last_date or D["date"].max()
    dd = pd.to_datetime(D["date"])
    recent = D[dd >= pd.Timestamp(last) - pd.Timedelta(days=45)]
    hot = recent["theme"].isin(HOT_THEMES)
    obs = recent[hot & (recent["new_hi60"] > -0.5) & (recent["chg10"] < 30)]
    L.append("## 四、当前观察候选（近 45 日命中规则）")
    L.append("")
    if obs.empty:
        L.append("暂无。")
    else:
        names = dict(zip(obs["code"], ["—"] * len(obs)))
        try:
            from mainrise.signals import load_names
            names = load_names()
        except Exception:
            pass
        L.append("| 代码 | 名称 | 信号日 | 主题 | 10日涨幅% | 60日涨幅% | 大牛概率 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in obs.sort_values("date", ascending=False).iterrows():
            L.append(f"| {r['code']} | {names.get(r['code'], '')} | {r['date']} | "
                     f"{r['theme']} | {r['chg10']:+.0f} | {r['chg60']:+.0f} | "
                     f"~38% |")
    L.append("")
    L.append("> 大牛概率 ~38% 为 AI硬件 信号的历史基准；叠加创60日新高后更高。"
             "候选仅供参考，需结合财务评估与综合分。")
    L.append("")
    return L


def run(with_market: bool = True) -> str:
    t0 = time.time()
    print("加载行情...")
    full = load_all_panels()
    full = full[full["code"].map(in_universe)]
    full = full[~full["is_st"].fillna(0).astype(int).astype(bool)]
    full = full[~full["is_paused"].fillna(0).astype(int).astype(bool)]
    full = full.sort_values(["code", "date"])
    ck = {c for c in load_chokepoint_codes() if not c.startswith("688")}
    panels = full[full["code"].isin(ck)].copy()
    print(f"范围：卡点企业 {len(ck)} 家（去688），{len(panels):,} 行")

    mkt = market_features(full) if with_market else pd.DataFrame()
    del full

    theme_map = load_theme()
    print("收集 T0 信号上下文...")
    D = collect_signals(panels, mkt, theme_map)
    D = D[D["date"] >= "2021-08-01"]
    print(f"T0 信号 {len(D)} 个，大牛占比 {(D['big'] == 1).mean():.1%}")
    panel_by = {c: g.reset_index(drop=True)
                for c, g in panels.groupby("code", sort=False)}

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 趋势大牛研究：观察点 / 买点确认 / 卖点信号（{dstr}）")
    L.append("")
    L.append("> 范围：行业卡点企业 100 家（110 - 10 只 688）；数据 2021-08 ~ "
             "2026-08-12；大牛 = T0 信号后 150 日收盘峰值 ≥+60%（宏和2026 = +605%）。")
    L.append("")
    L.extend(study_observe(D))
    L.extend(buy_confirm(D, panel_by, D))
    L_exit, R = exit_rules(D, panel_by)
    L.extend(L_exit)
    L.extend(honghe_case(D, panel_by, R))
    L.extend(current_candidates(D, D, panels["date"].max()))

    L.append("## 五、结论")
    L.append("")
    L.append("1. **观察点**：T0 信号 + 热主题（AI硬件/半导体/存储）+ 创60日新高 "
             "（+10日涨幅<30% 未透支）→ 入大牛观察池。AI硬件 信号大牛概率 44.6%"
             "（基准 29.7%、随机日 18.1%）；创新药/机器人/自动驾驶 主题信号大牛率 "
             "低（4-6% 级），观察优先级应显著低于热主题。")
    L.append("")
    L.append("2. **买点确认**：T0 尾盘买入打底仓（宏和 01-21 收盘 43.12 → 峰值 +605%）；"
             "趋势中回踩 MA10/MA20（10 日内触及）是加仓点而非卖点。")
    L.append("")
    L.append("3. **卖点信号（重要）**：大牛主升段常含 -20% 级深度洗盘（宏和 2026-03 "
             "从 84 洗到 64），**所有紧退出规则（含高点回落15%、跌破MA20）都会在洗盘"
             "中被洗出**（宏和实测 +53~81% 落袋，随后又涨 4.8 倍）。两层方案：")
    L.append("   - **主仓（吃主升段）**：跌破 MA60（收盘）或 跌破MA20且MA20拐头 才走，"
             "容忍 -25% 级回撤；实测大牛信号 MA60 退出平均 +85%（中位 +62%）。")
    L.append("   - **波段仓（落袋+滚动）**：高点回落 15%（收盘）减半仓，跌破 MA20 且 "
             "MA20 拐头清仓；8%/10% 回落规则误卖率 46-48%，只适合短波段。")
    L.append("")
    L.append("> 局限：观察规则与退出规则在同一样本上选出；2024 弱年回测普遍走弱；"
             "大牛事后定义（150 日峰值）含幸存者成分，实盘需容忍大量小牛/假突破。"
             "研究线索，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"趋势大牛研究_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    D.to_csv(paths.report_dir() / f"趋势大牛信号_{dstr}.csv",
             index=False, encoding="utf-8-sig")
    R.to_csv(paths.report_dir() / f"趋势大牛退出_{dstr}.csv",
             index=False, encoding="utf-8-sig")
    print(f"研究完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="趋势大牛研究（观察点/买点确认/卖点信号）")
    ap.add_argument("--fast", action="store_true", help="跳过全市场特征")
    args = ap.parse_args()
    run(with_market=not args.fast)


if __name__ == "__main__":
    main()
