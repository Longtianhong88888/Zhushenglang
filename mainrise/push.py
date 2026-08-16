"""飞书 + Server酱 推送：14:50 尾盘决策 / 17:30 收盘确认 / 告警 / 周报。

渠道（决策推送与告警统一：飞书优先 → Server酱兜底）：
- 飞书群机器人（免费不限量）：14:50/17:30 决策推送与告警的首选通道。webhook 配置：
  环境变量 FEISHU_WEBHOOK → settings.json feishu_webhook → ~/.feishu_webhook 文件。
- Server酱（方糖，免费 5 条/天）：最后兜底（配额有限，非必要不用）。
  （2026-08-16：企业微信通道已移除，用户确认未使用。）

14:50（mainrise push）：读 output/web/live.json（monitor 盘中最新状态）——
  今日买入 = 大牛模型候选盘中满足 T0 近似（涨幅≥5% 且量比≥1.5，或涨停，且站上
  MA20）→ bb_approx 标记；今日卖出 = **大牛模型当前持仓**（bigbull 交割单未平仓，
  bb_hold）盘中跌破 MA20 → 收盘确认卖出；市场状态 = 大盘 20 日涨幅（≤-5% 杀跌区
  → 停开新仓）。
17:30（mainrise push --close）：读 bigbull 每日落盘 output/state/
  bigbull_cands.json（updated / mkt.mkt_ret20）+ output/reports/大牛模型交割单_*.csv
  ——今日确认买入（信号日收盘价）/ 今日确认卖出（收盘跌破 MA20）/ 当前持仓 / 大盘
  状态；收盘口径以交割单为准，仅做文件读取（与 monitor 同理，不做全量重算）。

Key 读取顺序：环境变量 → settings.json → ~/.*_webhook（或 .serverchan_key）文件
            （均不进 git）。

用法:
    mainrise push                # 14:50 读 live.json 推送（飞书优先，非交易日自动跳过）
    mainrise push --close        # 17:30 收盘确认推送（飞书优先，读 bigbull 交割单）
    mainrise push --dry-run      # 只打印消息不发送
    mainrise push --test         # 推送测试消息（验证飞书可达）
    mainrise alert "标题" "正文"  # 告警：飞书优先，Server酱兜底
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

from mainrise import paths


def beijing_now() -> datetime:
    """北京时间（固定 UTC+8，不依赖服务器时区/tzdata）。"""
    return datetime.utcnow() + timedelta(hours=8)

SCT_URL = "https://sctapi.ftqq.com/{key}.send"
FEISHU_URL = "{hook}"   # 飞书机器人 webhook 完整 URL（open.feishu.cn/open-apis/bot/v2/hook/xxx）
LIVE_NAME = "live.json"
CANDS_NAME = "bigbull_cands.json"


def get_key() -> str:
    """Server酱 key：环境变量 → settings.json → ~/.serverchan_key。"""
    env = os.environ.get("SERVERCHAN_KEY", "").strip()
    if env:
        return env
    try:
        sf = Path(paths.home()) / "settings.json"
        if sf.exists():
            cfg = json.loads(sf.read_text(encoding="utf-8"))
            k = str(cfg.get("serverchan_key") or "").strip()
            if k:
                return k
    except Exception:  # noqa: BLE001
        pass
    for p in (Path.home() / ".serverchan_key",
              Path(paths.home()) / ".serverchan_key"):
        try:
            if p.exists():
                k = p.read_text(encoding="utf-8").strip()
                if k:
                    return k
        except Exception:  # noqa: BLE001
            pass
    return ""


def get_feishu_webhook() -> str:
    """飞书机器人 webhook：环境变量 → settings.json → ~/.feishu_webhook。

    飞书 webhook 是完整 URL（https://open.feishu.cn/open-apis/bot/v2/hook/xxx），
    与企微（只存 key）不同，直接存全 URL。
    """
    env = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if env:
        return env
    try:
        sf = Path(paths.home()) / "settings.json"
        if sf.exists():
            cfg = json.loads(sf.read_text(encoding="utf-8"))
            w = str(cfg.get("feishu_webhook") or "").strip()
            if w:
                return w
    except Exception:  # noqa: BLE001
        pass
    for p in (Path.home() / ".feishu_webhook",
              Path(paths.home()) / ".feishu_webhook"):
        try:
            if p.exists():
                w = p.read_text(encoding="utf-8").strip()
                if w:
                    return w
        except Exception:  # noqa: BLE001
            pass
    return ""


def send_feishu(content: str, webhook: str) -> bool:
    """飞书机器人文本推送（免费不限量）。content 纯文本。"""
    try:
        resp = requests.post(webhook,
                             json={"msg_type": "text",
                                   "content": {"text": content}},
                             timeout=15)
        if resp.status_code == 200:
            return (resp.json().get("code") or 0) == 0
        return False
    except Exception:  # noqa: BLE001
        return False


def send_decision(title: str, desp: str, dry_run: bool = False) -> str:
    """决策推送（14:50/17:30）：飞书优先 → Server酱兜底。

    2026-08-16 变更：原决策推送只走 Server酱（免费 5 条/天），常在收盘撞上限；
    改为与告警一致的多渠道降级，飞书免费不限量，Server酱仅兜底。
    2026-08-16：移除企业微信通道（用户确认未使用）。
    """
    if dry_run:
        print(f"[dry-run] {title}\n\n{desp}")
        return "ok"
    # 飞书（文本）
    fh = get_feishu_webhook()
    if fh and send_feishu(f"**{title}**\n{desp}", fh):
        return "ok"
    if fh:
        print("⚠ 飞书发送失败，降级 Server酱")
    # Server酱兜底
    key = get_key()
    if not key:
        print("⚠ 决策推送无可用渠道（未配置飞书 webhook / SERVERCHAN_KEY）")
        return "no-key"
    return "ok" if send_wechat(title[:64], desp, key) else "fail"


def send_alert(title: str, desp: str, dry_run: bool = False) -> str:
    """告警：飞书优先 → Server酱兜底（均免费/不限量优先）。
    2026-08-15 审计 M4：渠道失败必须逐级降级（曾只试飞书失败即 fail）。
    2026-08-16：移除企业微信通道（用户确认未使用）。"""
    if dry_run:
        print(f"[dry-run] ⚠ {title}\n{desp}")
        return "ok"
    # 飞书（文本）
    fh = get_feishu_webhook()
    if fh and send_feishu(f"⚠ {title}\n{desp}", fh):
        return "ok"
    if fh:
        print("⚠ 飞书发送失败，降级 Server酱")
    # Server酱兜底
    key = get_key()
    if not key:
        print("⚠ 告警无可用渠道（未配置飞书 webhook / SERVERCHAN_KEY）")
        return "no-key"
    return "ok" if send_wechat(title[:64], desp, key) else "fail"


def load_live() -> dict | None:
    """读取 monitor 最新 live.json；非今日数据返回 None（非交易日/服务未跑）。"""
    p = paths.web_dir() / LIVE_NAME
    if not p.exists():
        return None
    try:
        live = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    updated = str(live.get("updated_at") or "")
    today = beijing_now().strftime("%Y-%m-%d")
    if not updated.startswith(today):
        return None
    return live


def load_cands(state_dir: Path | None = None) -> dict | None:
    """读取 bigbull 落盘 output/state/bigbull_cands.json。

    返回 {"updated": 数据日期, "mkt": {"mkt_ret20": ...}, "cands": [...]}；
    文件缺失或解析失败返回 None。仅做文件读取（17:30 收盘确认的数据源之一）。
    """
    p = (Path(state_dir) if state_dir else paths.state_dir()) / CANDS_NAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    data.setdefault("updated", "")
    data.setdefault("mkt", {})
    data.setdefault("cands", [])
    return data


def _latest_trades_csv(report_dir: Path | None = None) -> tuple[str, Path] | None:
    """最新一份大牛模型交割单 → (数据日期, 路径)；无则 None。"""
    d = Path(report_dir) if report_dir else paths.report_dir()
    files = sorted(d.glob("大牛模型交割单_*.csv"), reverse=True)
    if not files:
        return None
    f = files[0]
    m = re.search(r"_(\d{4}-\d{2}-\d{2})\.csv$", f.name)
    return (m.group(1) if m else "", f)


def load_trades_csv(path: Path):
    """读取交割单 CSV（中文表头，utf-8-sig），补齐缺失列后返回 DataFrame。"""
    import pandas as pd
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"代码": str})
    for col in ("代码", "名称", "主题", "买入日期", "买入价", "卖出日期",
                "卖出价", "收益率", "峰值收益率", "状态"):
        if col not in df.columns:
            df[col] = ""
    df["状态"] = df["状态"].fillna("").astype(str)
    return df


def _isnum(v) -> bool:
    """是否为有效数值（排除 None / NaN / 空串）。"""
    try:
        f = float(v)
        return f == f
    except (TypeError, ValueError):
        return False


def build_message(live: dict) -> tuple[str, str]:
    """从 live 状态构建 (标题, 正文 markdown)。

    买入 = 大牛模型候选盘中 T0 近似（bb_approx）；卖出 = 大牛模型当前持仓
    （bigbull 交割单未平仓，bb_hold）盘中跌破 MA20 → 收盘确认卖出。
    """
    stocks = live.get("stocks") or []
    buys = [s for s in stocks if s.get("bb_approx")]
    sells = [s for s in stocks
             if s.get("group") == "大牛模型" and s.get("bb_hold")
             and "跌破MA20" in str(s.get("sell") or "")]
    ms = live.get("market_state") or {}
    mret = ms.get("mkt_ret20")
    weak = mret is not None and mret <= -5.0
    mkt_line = (f"大盘 20 日 {mret:+.1f}% → "
                f"**{'杀跌区 · 停开新仓' if weak else '正常 · 可开仓'}**"
                if mret is not None else "大盘 20 日 —")

    title = f"14:50 尾盘决策｜买入{len(buys)} 卖出{len(sells)}"
    L = [f"### 大牛模型 · 尾盘决策（{live.get('updated_at', '')}）", "",
         f"- {mkt_line}", ""]

    L.append("**🟢 今日买入（14:50 尾盘确认，1/3 仓）**")
    L.append("")
    if buys:
        L.append("| 代码 | 名称 | 现价 | 涨幅 | 量比 | 距MA20 | 评分 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for s in buys:
            px, chg, vr, ma20 = (s.get("price"), s.get("chg"),
                                 s.get("vr"), s.get("ma20"))
            px_t = "-" if px is None else f"{px:.2f}"
            chg_t = "-" if chg is None else f"{chg:+.1f}%"
            vr_t = "-" if vr is None else f"{vr:.1f}"
            ma_t = "-" if (px is None or not ma20) else f"{px/ma20-1:+.1%}"
            L.append(f"| {s['code']} | {s['name']} | {px_t} | {chg_t} | "
                     f"{vr_t} | {ma_t} | 评分{s.get('bb_score') or '-'} |")
    else:
        L.append("今日无满足 T0 近似（涨幅≥5% 且量比≥1.5 或涨停）的候选。")
    L.append("")

    L.append("**🔴 今日卖出（大牛模型持仓 · 盘中跌破 MA20）**")
    L.append("")
    if sells:
        L.append("| 代码 | 名称 | 现价 | 卖出信号 |")
        L.append("| --- | --- | --- | --- |")
        for s in sells:
            L.append(f"| {s['code']} | {s['name']} | {s.get('price') or '-'} | "
                     f"{s.get('sell')} |")
    else:
        L.append("大牛模型当前持仓无跌破 MA20（退出=收盘跌破 MA20，"
                 "14:50 盘中预警，17:30 以交割单确认）。")
    L.append("")
    L.append("> 盘中近似信号，收盘以 `mainrise bigbull` 交割单为准；"
             "杀跌区（大盘20日≤-5%）不买入。研究线索，不构成投资建议。")
    return title, "\n".join(L)


def build_close_message(updated: str, mkt_ret20, cands: list,
                        trades, hot_themes: list | None = None) -> tuple[str, str]:
    """17:30 收盘确认：从 bigbull 交割单构建 (标题, 正文 markdown)。

    今日确认买入 = 交割单中 买入日期==updated；今日确认卖出 = 卖出日期==updated
    且状态==已平仓；当前持仓 = 状态==未平仓（卖出价列为最新收盘，收益率为浮动盈亏）。
    cands 用于补今日买入的 评分/90日T0/距MA20（按代码匹配）。
    hot_themes: 当日动态热主题（bigbull_cands.json mkt.hot_themes）。
    """
    weak = _isnum(mkt_ret20) and float(mkt_ret20) <= -5.0
    mkt_line = (f"大盘 20 日 {mkt_ret20:+.1f}% → "
                f"**{'杀跌区 · 停开新仓' if weak else '正常 · 可开仓'}**"
                if _isnum(mkt_ret20) else "大盘 20 日 —")

    empty = trades.iloc[0:0]
    buys = trades[trades["买入日期"].astype(str) == updated] if len(trades) else empty
    sells = trades[(trades["卖出日期"].astype(str) == updated) &
                   (trades["状态"] == "已平仓")] if len(trades) else empty
    holds = trades[trades["状态"] == "未平仓"] if len(trades) else empty
    triggered = [c for c in cands if str(c.get("date")) == updated]
    cb = {str(c.get("code")): c for c in triggered}

    title = f"17:30 收盘确认｜买入{len(buys)} 卖出{len(sells)}"
    L = [f"### 大牛模型 · 收盘确认（{updated}）", "",
         f"- {mkt_line}"]
    if hot_themes:
        L.append(f"- 🔥 今日热主题（动态）：**{' / '.join(hot_themes)}**")
    L += [f"- 今日触发硬规则信号 **{len(triggered)}** 个 → 确认买入 "
          f"{len(buys)}（评分降序，最多 3 只并行）", ""]

    L.append("**🟢 今日确认买入（信号日收盘价，1/3 仓）**")
    L.append("")
    if len(buys):
        L.append("| 代码 | 名称 | 主题 | 收盘价 | 距MA20 | 评分 | 90日T0 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in buys.iterrows():
            c = cb.get(str(r["代码"]), {})
            px = float(r["买入价"]) if _isnum(r["买入价"]) else None
            ma = c.get("ma20")
            ma_t = (f"{px/float(ma)-1:+.1%}" if px is not None
                    and _isnum(ma) else "—")
            L.append(f"| {r['代码']} | {r['名称']} | {r['主题']} | "
                     f"{'' if px is None else f'{px:.2f}'} | {ma_t} | "
                     f"{c.get('score') or '-'} | {c.get('cnt') or '-'} |")
    else:
        L.append("今日无确认买入（无硬规则信号 / 满仓 / 杀跌区停开）。")
    L.append("")

    L.append("**🔴 今日确认卖出（收盘跌破 MA20）**")
    L.append("")
    if len(sells):
        L.append("| 代码 | 名称 | 主题 | 买入日 | 买入价 | 卖出价 | 收益 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in sells.iterrows():
            entry = float(r["买入价"]) if _isnum(r["买入价"]) else None
            exit_ = float(r["卖出价"]) if _isnum(r["卖出价"]) else None
            ret = float(r["收益率"]) if _isnum(r["收益率"]) else None
            L.append(f"| {r['代码']} | {r['名称']} | {r['主题']} | "
                     f"{r['买入日期']} | {'' if entry is None else f'{entry:.2f}'} "
                     f"| {'' if exit_ is None else f'{exit_:.2f}'} | "
                     f"{'' if ret is None else f'{ret:+.1%}'} |")
    else:
        L.append("今日无持仓触发卖出。")
    L.append("")

    L.append(f"**📊 当前持仓（{len(holds)} 只）**")
    L.append("")
    if len(holds):
        L.append("| 代码 | 名称 | 买入日 | 成本 | 最新收盘 | 浮动收益 | 峰值 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in holds.iterrows():
            entry = float(r["买入价"]) if _isnum(r["买入价"]) else None
            px = float(r["卖出价"]) if _isnum(r["卖出价"]) else None
            ret = float(r["收益率"]) if _isnum(r["收益率"]) else None
            pk = float(r["峰值收益率"]) if _isnum(r["峰值收益率"]) else None
            L.append(f"| {r['代码']} | {r['名称']} | {r['买入日期']} | "
                     f"{'' if entry is None else f'{entry:.2f}'} | "
                     f"{'' if px is None else f'{px:.2f}'} | "
                     f"{'' if ret is None else f'{ret:+.1%}'} | "
                     f"{'' if pk is None else f'{pk:+.0%}'} |")
    else:
        L.append("当前空仓。")
    L.append("")

    L.append("> 收盘口径以 `mainrise bigbull` 交割单为准；14:50 盘中近似仅供参考。"
             "研究线索，不构成投资建议。")
    return title, "\n".join(L)


def send_wechat(title: str, desp: str, key: str) -> bool:
    """Server酱发送（SCT 版本：POST sctapi.ftqq.com）。"""
    try:
        resp = requests.post(SCT_URL.format(key=key),
                             data={"title": title[:64], "desp": desp},
                             timeout=15)
        data = resp.json()
        ok = resp.status_code == 200 and data.get("code") == 0
        if not ok:
            print(f"⚠ 推送失败: {resp.status_code} {data.get('message')}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 推送异常: {e}")
        return False


def run_close(dry_run: bool = False) -> str:
    """17:30 收盘确认推送：读 bigbull 落盘文件（候选JSON + 交割单CSV）。

    数据日期（bigbull_cands.updated / 交割单文件名日期）非今日 → 跳过（非交易日
    或无当日行情）；dry_run 只打印不发送（不消耗 Server酱配额）。
    """
    today = beijing_now().strftime("%Y-%m-%d")
    data = load_cands()
    updated = str((data or {}).get("updated") or "")
    if updated != today:
        print(f"非交易日或无今日行情数据（bigbull 数据日期 "
              f"{updated or '—'}），跳过收盘确认")
        return "skip"
    got = _latest_trades_csv()
    if not got:
        print("未找到大牛模型交割单 CSV，跳过收盘确认")
        return "skip"
    csv_date, path = got
    if csv_date != today:
        print(f"交割单日期 {csv_date} ≠ 今日 {today}，跳过收盘确认")
        return "skip"
    mkt_ret20 = (data.get("mkt") or {}).get("mkt_ret20")
    hot_themes = (data.get("mkt") or {}).get("hot_themes")
    trades = load_trades_csv(path)
    title, desp = build_close_message(today, mkt_ret20,
                                      data.get("cands") or [], trades,
                                      hot_themes=hot_themes)
    return send_decision(title, desp, dry_run=dry_run)


def run(test: bool = False, close: bool = False, dry_run: bool = False) -> str:
    if close:
        return run_close(dry_run=dry_run)
    if test:
        return send_decision(
            "主升浪·推送通道测试",
            "## ✅ 推送通道正常\n\n大牛模型 14:50 尾盘决策 / 17:30 收盘确认 "
            "已接入飞书优先通道（免费不限量）。\n\n"
            "> 研究线索，不构成投资建议。", dry_run=dry_run)
    live = load_live()
    if live is None:
        print("非交易日或无盘中数据（live.json 非今日），跳过推送")
        return "skip"
    title, desp = build_message(live)
    return send_decision(title, desp, dry_run=dry_run)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="飞书/Server酱推送（14:50 尾盘决策 / 17:30 收盘确认，"
                    "飞书优先→Server酱兜底）")
    ap.add_argument("--test", action="store_true", help="推送测试消息")
    ap.add_argument("--close", action="store_true",
                    help="17:30 收盘确认推送（读 bigbull 交割单，收盘口径）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印消息不发送（不消耗配额）")
    args = ap.parse_args()
    run(test=args.test, close=args.close, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
