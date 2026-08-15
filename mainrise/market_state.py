"""大盘状态（三态轮动）：全市场等权 20 日涨幅 + 市场宽度 + 成交额水位。

研究结论（docs/双模型轮动研究.md）：
- 杀跌区（等权20日涨幅 ≤ -5%）：启动加仓模型全开（+ 成交额水位≥1.0 过滤）
- 震荡区（-5% ~ +5%）：双模型并行（启动加仓 + T0 主升浪）
- 主升区（> +5% 或 市场宽度≥70%）：启动加仓停开，T0 降档防追高

输出 output/state/market_state.json：
- mkt_ret20：全市场等权 20 日累计涨幅（zzshare pct_chg，收盘口径，
  **已清理 prev_close≤0 脏数据并裁剪 ±21%**——新股/复牌 prev_close=0 会把
  均值严重虚增，实测 2026-08-11 三只脏数据把当日均值从 ~0.5% 拉成 +11.4%，
  20 日累计从 +4.6% 虚增到 +29.3%）
- tech20 / other20 / diff：科技卡点 vs 非科技 等权 20 日涨幅及强弱差
  （结构维度：科技强≥+5pp / 均衡 / 非科技强≤-5pp。历史验证：科技强+量能足
  是超跌反弹模型的黄金环境（非2026 91.7%胜率）；2026 是离群年，结构差也救不了
  超跌模型，需切换到 T0 趋势模型）
- index_ret20：上证指数 20 日涨幅（腾讯日K，盘中可实时刷新，5 分钟缓存）
- breadth：站上 MA20 的个股占比（收盘口径）
- amount_wl / vol_wl：全市场成交额/成交量 相对 20 日均值的水位（收盘口径）

每日流水线（mainrise track）自动生成；盯盘页每轮读取并刷新上证指数。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from mainrise import paths

TZ_CN = timezone(timedelta(hours=8))          # 与 monitor.TZ_CN 同口径（避免循环导入）
INDEX_TX = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
INDEX_CACHE = {"ts": 0.0, "ret20": None}
INDEX_TTL = 300                                # 上证指数 20 日涨幅缓存（秒）


def classify(mkt_ret20: float | None, breadth: float | None,
             amount_wl: float | None) -> dict:
    """按等权 20 日涨幅 + 市场宽度判定三态，返回状态/文案/建议/颜色。"""
    if mkt_ret20 is None:
        state, color = "数据不足", "#8B949E"
    elif mkt_ret20 <= -5:
        state, color = "杀跌区", "#3FB950"          # 绿=跌（国内红涨绿跌）
    elif mkt_ret20 > 5 or (breadth is not None and breadth >= 0.70):
        state, color = "主升区", "#F85149"          # 红=涨
    else:
        state, color = "震荡区", "#D29922"
    if state == "杀跌区":
        advice = "启动加仓全开（+量能≥1.0 过滤）｜ 两级模型(B3/二波)谨慎"
    elif state == "震荡区":
        advice = "双模型并行（启动加仓 + T0）"
    elif state == "主升区":
        advice = "启动加仓停开 ｜ B3/二波 降档防追高"
    else:
        advice = "数据不足，按保守模式运行"
    if amount_wl is not None and amount_wl < 1.0:
        advice += " ｜ ⚠量能不足：启动加仓需等放量确认"
    label = (f"{state}（20日{'+' if (mkt_ret20 or 0) >= 0 else ''}"
             f"{mkt_ret20:.1f}%）" if mkt_ret20 is not None else state)
    return {"state": state, "label": label, "advice": advice, "color": color}


def classify_structure(diff: float | None) -> dict:
    """科技 vs 非科技 20 日强弱差 → 结构状态与建议。"""
    if diff is None:
        return {"structure": "结构未知", "advice": ""}
    if diff >= 5:
        return {"structure": "科技强",
                "advice": "结构偏科技：主升浪/T0（卡点科技）为主，超跌反弹降档"}
    if diff <= -5:
        return {"structure": "非科技强",
                "advice": "结构偏非科技：环境整体偏弱，双模型降档"}
    return {"structure": "均衡",
            "advice": "结构均衡：双模型正常"}


def launch_action(state: dict | None, rule: str = "") -> str:
    """启动加仓信号日动作（按大盘状态自适应）：
    主升区→仅观察（模型停开）；量能<1.0→等放量；非科技强→轻仓；否则明日打底仓。"""
    head = f"启动{rule}：" if rule else ""
    if not state:
        return head + "明日开盘打底仓"
    if state.get("state") == "主升区":
        return head + "仅观察（主升区停开）"
    notes = []
    if (state.get("amount_wl") or 1.0) < 1.0:
        notes.append("量能不足等放量")
    if state.get("structure") == "非科技强":
        notes.append("环境偏弱轻仓")
    if notes:
        return head + "，".join(notes)
    return head + "明日开盘打底仓"


def signal_note(state: dict | None) -> str:
    """B3/二波 买点的市场前缀：主升区→轻仓降档；科技强→优先；非科技强→降档。"""
    if not state:
        return ""
    if state.get("state") == "主升区":
        return "⚠主升区·轻仓"
    if state.get("structure") == "科技强":
        return "科技强·优先"
    if state.get("structure") == "非科技强":
        return "⚠非科技强·降档"
    return ""


def _state_path() -> object:
    return paths.state_dir() / "market_state.json"


def _hist_path() -> object:
    return paths.state_dir() / "market_state_history.csv"


def compute_daily(date: str | None = None,
                  panels: pd.DataFrame | None = None) -> dict:
    """从全市场日线计算大盘状态并落盘（track 每日流水线调用）。

    panels 缺省时自行加载（独立命令用）；track 传入已过滤的 panels 复用。
    """
    if panels is None:
        from mainrise.data import load_all_panels
        panels = load_all_panels()
    if date is not None:
        panels = panels[panels["date"] <= date]
    if panels.empty:
        raise ValueError("无行情数据")
    panels = panels.dropna(subset=["pct_chg", "close"])
    if "prev_close" in panels.columns:
        panels = panels[panels["prev_close"] > 0]   # 剔除新股/复牌脏数据
    # 统一口径：与回测一致，剔除 ST/停牌（CLI 与 track 传入的 panels 过滤不同，
    # 必须在这里强制统一，否则 20 日涨幅会在 ±5% 边界上跳变）
    try:
        from mainrise.signals import in_universe
        panels = panels[panels["code"].map(in_universe)]
    except Exception:  # noqa: BLE001
        pass
    if "is_st" in panels.columns:
        panels = panels[~panels["is_st"].fillna(0).astype(int).astype(bool)]
    if "is_paused" in panels.columns:
        panels = panels[~panels["is_paused"].fillna(0).astype(int).astype(bool)]
    last_date = str(panels["date"].max())

    # 全市场等权 20 日累计涨幅（裁剪 ±21%，防新股/北交所极端值拉偏均值）
    dret = (panels.assign(pct=panels["pct_chg"].clip(-21, 21))
            .groupby("date")["pct"].mean().sort_index())
    mkt_ret20 = float(dret.rolling(20).sum().iloc[-1]) if len(dret) >= 20 else np.nan

    # 结构维度：科技卡点 vs 非科技 等权 20 日涨幅差
    tech20 = other20 = diff = None
    try:
        from mainrise.report import load_chokepoint_codes
        ck = load_chokepoint_codes()
        p = panels.assign(pct=panels["pct_chg"].clip(-21, 21))
        p["grp"] = np.where(p["code"].isin(ck), "tech", "other")
        g20 = (p.groupby(["date", "grp"])["pct"].mean().unstack()
               .rolling(20).sum())
        if len(g20) >= 20:
            tech20 = round(float(g20["tech"].iloc[-1]), 2)
            other20 = round(float(g20["other"].iloc[-1]), 2)
            diff = round(tech20 - other20, 2)
    except Exception:  # noqa: BLE001
        pass

    # 市场宽度：站上 MA20 个股占比（当日）
    close = panels[["date", "code", "close"]].drop_duplicates(["date", "code"])
    close = close.sort_values(["code", "date"])
    close["ma20"] = close.groupby("code")["close"].transform(
        lambda s: s.rolling(20, min_periods=15).mean())
    above = (close["close"] > close["ma20"]).astype(int)
    g = close.assign(above=above).groupby("date").agg(
        n=("above", "count"), above=("above", "sum"))
    breadth = float((g["above"] / g["n"]).iloc[-1]) if len(g) else np.nan

    # 成交额 / 成交量水位（相对 20 日均值）
    amt = panels.groupby("date")["amount"].sum().sort_index()
    vol = panels.groupby("date")["volume"].sum().sort_index()
    amount_wl = (float(amt.iloc[-1] / amt.rolling(20, min_periods=10).mean().iloc[-1])
                 if len(amt) >= 10 else np.nan)
    vol_wl = (float(vol.iloc[-1] / vol.rolling(20, min_periods=10).mean().iloc[-1])
              if len(vol) >= 10 else np.nan)

    st = classify(mkt_ret20, breadth, amount_wl)
    st2 = classify_structure(diff)
    advice = "；".join(x for x in (st["advice"], st2["advice"]) if x)
    out = {
        "date": last_date,
        "mkt_ret20": round(mkt_ret20, 2) if not np.isnan(mkt_ret20) else None,
        "tech20": tech20,
        "other20": other20,
        "diff": diff,
        "structure": st2["structure"],
        "index_ret20": None,           # 盘中由 live_state 刷新
        "breadth": round(breadth, 4) if not np.isnan(breadth) else None,
        "amount_wl": round(amount_wl, 3) if not np.isnan(amount_wl) else None,
        "vol_wl": round(vol_wl, 3) if not np.isnan(vol_wl) else None,
        "state": st["state"], "label": st["label"],
        "advice": advice, "color": st["color"],
        "updated_at": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
    }
    paths.ensure_dirs()
    _state_path().write_text(json.dumps(out, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    # 历史留档（供后续仪表盘/研究）
    hist = _hist_path()
    row = pd.DataFrame([{k: out.get(k) for k in
                         ("date", "mkt_ret20", "breadth", "amount_wl",
                          "vol_wl", "state")}])
    if hist.exists():
        old = pd.read_csv(hist, dtype={"date": str})
        row = pd.concat([old, row], ignore_index=True)
    row.drop_duplicates("date", keep="last").to_csv(hist, index=False,
                                                    encoding="utf-8-sig")
    return out


def _index_ret20(force: bool = False) -> float | None:
    """上证指数 20 日涨幅（腾讯日K，5 分钟缓存；失败保留缓存并退避）。

    失败时也推进 INDEX_CACHE["ts"]：接口宕机期间不再每 3 秒打腾讯
    （M23 修复），5 分钟后再试；force 仅手动刷新用，异常时同样退避。
    """
    now = time.time()
    if not force and INDEX_CACHE["ret20"] is not None \
            and now - INDEX_CACHE["ts"] < INDEX_TTL:
        return INDEX_CACHE["ret20"]
    try:
        r = requests.get(INDEX_TX,
                         params={"param": "sh000001,day,,,25,qfq"}, timeout=8)
        d = r.json()
        node = (d.get("data") or {}).get("sh000001") or {}
        bars = node.get("qfqday") or node.get("day") or []
        if len(bars) < 21:
            raise ValueError("上证指数日K不足 21 根")
        closes = [float(b[2]) for b in bars[-21:]]
        ret20 = (closes[-1] / closes[0] - 1) * 100
        INDEX_CACHE.update({"ts": now, "ret20": round(ret20, 2)})
        return INDEX_CACHE["ret20"]
    except Exception:  # noqa: BLE001
        # 失败：记录时间戳退避，下次调用不再立即重试
        INDEX_CACHE["ts"] = now
        if INDEX_CACHE["ret20"] is not None:
            return INDEX_CACHE["ret20"]   # 保留上次成功值
        raise


def load_state() -> dict | None:
    """读取最近收盘口径的大盘状态（无文件返回 None）。"""
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def live_state(state: dict | None, force: bool = False) -> dict | None:
    """盘中刷新上证指数 20 日涨幅（腾讯日K，5 分钟缓存），其余保持收盘口径。"""
    if not state:
        return None
    try:
        state["index_ret20"] = _index_ret20(force)
    except Exception:  # noqa: BLE001  接口失败保留收盘口径
        pass
    state["updated_at"] = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    return state


def render_card(state: dict | None) -> str:
    """盯盘页大盘状态卡（HTML，Quant Dark 风格）。"""
    if not state:
        return ('<section class="card"><h2>大盘状态（三态轮动）</h2>'
                '<div class="body"><div class="empty">大盘状态数据未生成'
                '（每日 17:30 流水线自动更新）。</div></div></section>')
    color = state.get("color") or "#8B949E"
    st = state.get("state") or "—"
    ret20 = state.get("mkt_ret20")
    ret20_s = f"{ret20:+.1f}%" if ret20 is not None else "—"
    idx20 = state.get("index_ret20")
    idx_s = f"{idx20:+.1f}%" if idx20 is not None else "—"
    brd = state.get("breadth")
    brd_s = f"{brd*100:.0f}%" if brd is not None else "—"
    awl = state.get("amount_wl")
    awl_s = f"{awl:.2f}" if awl is not None else "—"
    tech20 = state.get("tech20")
    other20 = state.get("other20")
    diff = state.get("diff")
    struct = state.get("structure") or "—"
    t_s = f"{tech20:+.1f}%" if tech20 is not None else "—"
    o_s = f"{other20:+.1f}%" if other20 is not None else "—"
    d_s = f"{diff:+.1f}pp" if diff is not None else "—"
    return (f'<section class="card"><h2>大盘状态（三态轮动）</h2>'
            f'<div class="body"><div style="display:flex;gap:10px;'
            f'align-items:center;flex-wrap:wrap">'
            f'<span style="background:{color}22;border:1px solid {color};'
            f'color:{color};border-radius:999px;padding:2px 14px;'
            f'font-weight:600">{st}</span>'
            f'<span style="color:#8B949E">等权20日 <b style="color:#E6EDF3">'
            f'{ret20_s}</b></span>'
            f'<span style="color:#8B949E">上证20日 <b style="color:#E6EDF3">'
            f'{idx_s}</b></span>'
            f'<span style="color:#8B949E">宽度 <b style="color:#E6EDF3">'
            f'{brd_s}</b></span>'
            f'<span style="color:#8B949E">量能水位 <b style="color:#E6EDF3">'
            f'{awl_s}</b></span></div>'
            f'<div style="display:flex;gap:10px;align-items:center;'
            f'flex-wrap:wrap;margin-top:6px">'
            f'<span style="background:#BC8CFF22;border:1px solid #BC8CFF;'
            f'color:#BC8CFF;border-radius:999px;padding:2px 12px;'
            f'font-weight:600">{struct}</span>'
            f'<span style="color:#8B949E">科技20日 <b style="color:#E6EDF3">'
            f'{t_s}</b></span>'
            f'<span style="color:#8B949E">非科技20日 <b style="color:#E6EDF3">'
            f'{o_s}</b></span>'
            f'<span style="color:#8B949E">强弱差 <b style="color:#E6EDF3">'
            f'{d_s}</b></span></div>'
            f'<div style="margin-top:8px;color:#E6EDF3">'
            f'模型建议：{state.get("advice") or "—"}</div>'
            f'<div class="note">口径：等权20日/宽度/量能为收盘口径'
            f'（每日17:30更新）；上证20日盘中实时（5分钟缓存）。'
            f'三态：杀跌≤-5% 开启动加仓；震荡±5% 双模型并行；'
            f'主升&gt;+5% 或宽度≥70% 停启动加仓、T0 降档。'
            f'结构：科技强→T0 主升浪为主；非科技强→环境偏弱降档；'
            f'均衡→双模型正常（阈值 ±5pp）。</div>'
            f'</div></section>')
