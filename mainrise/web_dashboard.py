"""网页版仪表盘生成。

读取 output/reports/主升浪跟踪_*.csv 与 output/state/ 下的持仓/观察池，
渲染为单个自包含 HTML（Quant Dark 配色、手机自适应、无外部依赖），
输出 output/web/index.html，供 Nginx / 静态托管直接访问。

用法:
    python3 -m mainrise.cli web     # 手动生成网页仪表盘
    mainrise track                  # 跟踪报告生成后自动同步（失败不阻塞）

部署（腾讯云）:
    cron 定时跑 deploy/daily_update.sh，Nginx 指向 output/web/ 即可。
"""
from __future__ import annotations

import csv
import html
import sys
from datetime import date, datetime
from pathlib import Path

from mainrise import paths

BUY_STATUSES = ("T1确认买点", "T0新信号", "回踩低吸")
STATUSES = ("空头", "破位", "多头持有", "回踩低吸", "多头回踩", "T1确认买点", "T0新信号")
DEFAULT_MAX_10D_GAIN = 150.0
OUTPUT_NAME = "index.html"
DASHBOARD_NAME = "dashboard.html"
REPORTS_NAME = "reports.html"
TRACK_PREFIX = "主升浪跟踪_"

CODE_LINK_JS = """<script>
/* 所有页面通用：6位数字单元格自动转为股票代码链接（点击看K线/分时） */
(function(){
  function isCode(t){return /^\\d{6}$/.test(t);}
  function bind(){
    var tds=document.querySelectorAll('td');
    for(var i=0;i<tds.length;i++){
      var td=tds[i];
      if(td.querySelector('a'))continue;
      var t=(td.textContent||'').replace(/\\s/g,'');
      if(isCode(t)){
        td.innerHTML='<a href="/stock/'+t+'" style="color:#58A6FF;text-decoration:none">'+t+'</a>';
      }
    }
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',bind);}
  else{bind();}
})();
</script>"""


def _refresh_ui(page: str) -> str:
    """悬浮刷新按钮：POST /refresh?page=... 触发服务器端重新生成后重载。"""
    return f"""<div id="refreshBtn" onclick="hardRefresh('{page}')"
style="position:fixed;right:18px;bottom:18px;z-index:99;background:#161B22;
border:1px solid #58A6FF;color:#E6EDF3;border-radius:999px;padding:8px 18px;
font-size:13px;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.4)">🔄 刷新</div>
<script>
function hardRefresh(p){{
  var b=document.getElementById('refreshBtn');
  b.textContent='刷新中…'; b.style.opacity=0.55; b.style.pointerEvents='none';
  fetch('/refresh?page='+p,{{method:'POST'}}).catch(function(){{}}).then(function(){{
    setTimeout(function(){{ location.reload(); }}, 400);
  }});
}}
</script>"""


# ---------------------------------------------------------------- 数据读取

def _num(value) -> float | None:
    """CSV 数值转 float；空串/非法返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _tracking_csvs(reports_dir: Path) -> list[tuple[date, Path]]:
    out = []
    for p in sorted(reports_dir.glob(f"{TRACK_PREFIX}*.csv")):
        stem = p.stem[len(TRACK_PREFIX):]  # 2026-08-06
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue
        out.append((d, p))
    return out


def _csv_rows(path: Path) -> list[dict]:
    """CSV -> dict 列表（首行为表头）。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        if not r or not str(r[0]).strip():
            continue
        d = {}
        for i, key in enumerate(header):
            d[key.strip()] = r[i].strip() if i < len(r) else ""
        out.append(d)
    return out


def _watch_rows(rows: list[dict]) -> list[dict]:
    """观察池行 = 有综合分的行。"""
    return [r for r in rows if _num(r.get("composite")) is not None]


def _new_signal_rows(rows: list[dict]) -> list[dict]:
    """新信号行 = 无综合分的行（观察池为空时并入 CSV 兜底）。"""
    return [r for r in rows if _num(r.get("composite")) is None]


def _load_watchlist(state_dir: Path) -> dict[str, dict]:
    p = state_dir / "mainrise_watchlist.csv"
    if not p.exists():
        return {}
    out = {}
    for r in _csv_rows(p):
        code = str(r.get("code", "")).strip()
        if code:
            out[code] = r
    return out


def _load_positions(state_dir: Path, latest_watch: dict[str, dict]) -> dict:
    p = state_dir / "mainrise_positions.csv"
    if not p.exists():
        return {"active": [], "pending": [], "closed": []}
    items = {"active": [], "pending": [], "closed": []}
    for r in _csv_rows(p):
        status = str(r.get("status", "")).strip()
        code = str(r.get("code", "")).strip()
        buy_price = _num(r.get("buy_price"))
        peak = _num(r.get("peak"))
        close_price = _num(r.get("close_price"))
        last_close = None
        if status in ("active", "pending"):
            lr = latest_watch.get(code)
            last_close = _num(lr.get("close")) if lr else None
        pnl = None
        if buy_price:
            base = last_close if status == "active" else close_price
            if status == "pending":
                pnl = None
            elif base is not None:
                pnl = (base / buy_price - 1) * 100
        item = {
            "code": code,
            "name": str(r.get("name", "")).strip() or "-",
            "buy_date": str(r.get("buy_date", "")).strip(),
            "buy_price": buy_price,
            "last_close": last_close,
            "close_price": close_price,
            "pnl": pnl,
            "peak": peak,
            "status": status,
            "reason": str(r.get("reason", "")).strip(),
        }
        bucket = items.get(status) if status in items else None
        if bucket is not None:
            bucket.append(item)
    return items


def _collect(reports_dir: Path, state_dir: Path) -> dict:
    """聚合全部数据为网页展示所需的 dict。"""
    csvs = _tracking_csvs(reports_dir)
    days = []
    for d, p in csvs:
        rows = _csv_rows(p)
        watch = _watch_rows(rows)
        news = _new_signal_rows(rows)
        scores = [_num(r.get("composite")) for r in watch]
        scores = [s for s in scores if s is not None]
        chgs = [_num(r.get("chg")) for r in watch]
        chgs = [c for c in chgs if c is not None]
        buys = [
            r for r in watch
            if r.get("status") in BUY_STATUSES and (
                _num(r.get("chg10")) is None
                or _num(r.get("chg10")) < DEFAULT_MAX_10D_GAIN)
        ]
        days.append({
            "date": d,
            "watch": watch,
            "news": news,
            "watch_size": len(watch),
            "avg_score": sum(scores) / len(scores) if scores else None,
            "avg_chg": sum(chgs) / len(chgs) if chgs else None,
            "up_count": sum(1 for c in chgs if c > 0),
            "buy_count": len(buys),
        })

    latest = days[-1] if days else None
    latest_watch = {r["code"]: r for r in (latest["watch"] if latest else [])}
    positions = _load_positions(state_dir, latest_watch)
    watchlist = _load_watchlist(state_dir)

    buy_points = []
    if latest:
        buy_points = [
            r for r in latest["watch"]
            if r.get("status") in BUY_STATUSES and (
                _num(r.get("chg10")) is None
                or _num(r.get("chg10")) < DEFAULT_MAX_10D_GAIN)
        ]
        buy_points.sort(
            key=lambda r: (_num(r.get("composite")) or 0), reverse=True)

    status_counts: dict[str, int] = {s: 0 for s in STATUSES}
    if latest:
        for r in latest["watch"]:
            st = str(r.get("status", "")).strip()
            if st in status_counts:
                status_counts[st] += 1

    # 观察池展示：最新日 watch 行 + 产业/财务信息（来自 watchlist.csv）
    watch_view = []
    for i, r in enumerate((latest["watch"] if latest else []), 1):
        wl = watchlist.get(str(r.get("code", "")).strip(), {})
        watch_view.append({
            "rank": i,
            "code": r.get("code", ""),
            "name": r.get("name", "") or wl.get("name", "") or "-",
            "composite": _num(r.get("composite")),
            "close": _num(r.get("close")),
            "chg": _num(r.get("chg")),
            "status": r.get("status", ""),
            "hint": r.get("hint", ""),
            "track": wl.get("track", ""),
        })

    return {
        "latest_date": latest["date"] if latest else None,
        "days": days,
        "latest": latest,
        "watch_view": watch_view,
        "buy_points": buy_points,
        "positions": positions,
        "status_counts": status_counts,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------- 渲染

CSS = """
:root{
  --bg:#0D1117; --surface:#161B22; --border:#30363D; --line:#21262D;
  --title:#39D2C0; --blue:#58A6FF; --green:#3FB950; --red:#F85149;
  --amber:#D29922; --purple:#BC8CFF; --text:#E6EDF3; --sub:#8B949E;
  --foot:#6E7681;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
  font-family:Consolas,"SF Mono","Menlo","PingFang SC","Microsoft YaHei",monospace;
  font-size:14px;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 40px}
header h1{font-size:22px;font-weight:700;color:var(--title);letter-spacing:1px}
header .sub{color:var(--sub);font-size:12px;margin-top:6px}
header .date{color:var(--blue);font-weight:700}
.nav{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}
.nav a{color:var(--sub);text-decoration:none;font-size:12px;padding:5px 12px;
  border:1px solid var(--border);border-radius:999px;background:var(--surface)}
.nav a:hover{color:var(--title);border-color:var(--title)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
.kpi{background:var(--surface);border:1px solid var(--border);border-bottom:3px solid var(--blue);
  border-radius:10px;padding:12px 14px}
.kpi .label{color:var(--sub);font-size:11px}
.kpi .value{font-size:26px;font-weight:700;margin-top:4px;color:var(--blue)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  margin:16px 0;overflow:hidden}
.card h2{font-size:15px;color:var(--title);padding:12px 16px;border-bottom:1px solid var(--line)}
.card .body{padding:4px 16px 12px}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}
th{color:var(--sub);text-align:left;font-size:12px;padding:8px 10px;
  border-bottom:2px solid var(--blue)}
td{padding:8px 10px;border-bottom:1px solid var(--line)}
tr:nth-child(even) td{background:rgba(255,255,255,0.015)}
.up{color:var(--red);font-weight:700}
.down{color:var(--green);font-weight:700}
.flat{color:var(--sub)}
.chip{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700}
.empty{color:var(--foot);padding:14px 16px;font-size:13px}
.pos{color:var(--green)}
.neg{color:var(--red)}
.note{color:var(--sub);font-size:12px;padding:10px 16px}
footer{margin-top:24px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--foot);font-size:11px}
@media(max-width:640px){
  body{font-size:13px}
  .kpis{grid-template-columns:repeat(2,1fr);gap:8px}
  .kpi .value{font-size:22px}
  table{font-size:12px}
}
"""


def _esc(v) -> str:
    return "-" if v is None else html.escape(str(v))


def _fmt(v, nd: int = 2, suffix: str = "") -> str:
    if v is None:
        return "-"
    return f"{v:.{nd}f}{suffix}"


def _chg_span(v) -> str:
    if v is None:
        return '<span class="flat">-</span>'
    cls = "up" if v > 0 else ("down" if v < 0 else "flat")
    return f'<span class="{cls}">{v:+.2f}%</span>'


STATUS_COLORS = {
    "空头": "#8B949E", "破位": "#F85149", "多头持有": "#3FB950",
    "回踩低吸": "#D29922", "多头回踩": "#39D2C0",
    "T1确认买点": "#D29922", "T0新信号": "#F85149",
}


def _chip(status: str) -> str:
    color = STATUS_COLORS.get(status, "#8B949E")
    return f'<span class="chip" style="color:{color};border:1px solid {color}">{html.escape(status)}</span>'


def _code_link(code) -> str:
    """股票代码单元格：服务端直接生成链接，不依赖前端 JS。"""
    c = html.escape(str(code))
    return (f'<a href="/stock/{c}" style="color:#58A6FF;'
            f'text-decoration:none">{c}</a>')


def _pnl_span(pnl) -> str:
    if pnl is None:
        return '<span class="flat">--</span>'
    cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "flat")
    return f'<span class="{cls}">{pnl:+.2f}%</span>'


def _kpi_card(label: str, value: str, color: str) -> str:
    return (f'<div class="kpi" style="border-bottom-color:{color}">'
            f'<div class="label">{html.escape(label)}</div>'
            f'<div class="value" style="color:{color}">{value}</div></div>')


def _buy_points_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty">今日无触发买点的标的。</div>'
    trs = []
    for r in rows:
        trs.append(
            f"<tr><td>{_code_link(r.get('code'))}</td><td>{_esc(r.get('name'))}</td>"
            f"<td>{_fmt(_num(r.get('composite')), 1)}</td>"
            f"<td>{_chip(str(r.get('status','')))}</td>"
            f"<td>{_esc(r.get('hint'))}</td></tr>")
    return ('<div class="tbl-wrap"><table>'
            "<tr><th>代码</th><th>名称</th><th>综合分</th><th>状态</th><th>提示</th></tr>"
            + "".join(trs) + "</table></div>")


def _positions_table(pos: dict) -> str:
    active, pending, closed = pos["active"], pos["pending"], pos["closed"]
    if not (active or pending or closed):
        return '<div class="empty">当前无纸面持仓。</div>'
    trs = []
    for r in active + pending + closed:
        st = r["status"]
        st_chip = _chip("多头持有" if st == "active" else (
            "回踩低吸" if st == "pending" else "破位"))
        px = (r["last_close"] if st == "active" else r["close_price"])
        trs.append(
            f"<tr><td>{_code_link(r['code'])}</td><td>{_esc(r['name'])}</td>"
            f"<td>{_esc(r['buy_date'])}</td><td>{_fmt(r['buy_price'])}</td>"
            f"<td>{_fmt(px)}</td><td>{_pnl_span(r['pnl'])}</td>"
            f"<td>{_fmt(r['peak'])}</td><td>{st_chip}</td>"
            f"<td>{_esc(r['reason'] or ('待买入' if st == 'pending' else ''))}</td></tr>")
    return ('<div class="tbl-wrap"><table>'
            "<tr><th>代码</th><th>名称</th><th>买入日</th><th>买入价</th>"
            "<th>现价/平仓价</th><th>盈亏%</th><th>峰值</th><th>状态</th><th>说明</th></tr>"
            + "".join(trs) + "</table></div>")


def _watch_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty">观察池为空。</div>'
    trs = []
    for r in rows:
        trs.append(
            f"<tr><td>{r['rank']}</td><td>{_code_link(r['code'])}</td><td>{_esc(r['name'])}</td>"
            f"<td>{_fmt(r['composite'], 1)}</td><td>{_fmt(r['close'])}</td>"
            f"<td>{_chg_span(r['chg'])}</td><td>{_chip(r['status'])}</td>"
            f"<td style='white-space:normal'>{_esc(r['hint'])}</td></tr>")
    return ('<div class="tbl-wrap"><table>'
            "<tr><th>#</th><th>代码</th><th>名称</th><th>综合分</th><th>收盘</th>"
            "<th>涨跌%</th><th>状态</th><th>提示</th></tr>"
            + "".join(trs) + "</table></div>")


def _news_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty">当日无新信号行（观察池非空时新信号不并入跟踪 CSV）。</div>'
    trs = []
    for r in rows:
        trs.append(
            f"<tr><td>{_code_link(r.get('code'))}</td><td>{_esc(r.get('name'))}</td>"
            f"<td>{_chg_span(_num(r.get('chg')))}</td>"
            f"<td>{_fmt(_num(r.get('vr')), 2)}</td>"
            f"<td>{_chip(str(r.get('status','')))}</td></tr>")
    return ('<div class="tbl-wrap"><table>'
            "<tr><th>代码</th><th>名称</th><th>涨幅%</th><th>量比</th><th>状态</th></tr>"
            + "".join(trs) + "</table></div>")


def _daily_table(days: list[dict]) -> str:
    if not days:
        return '<div class="empty">暂无跟踪数据。</div>'
    trs = []
    for d in days:
        trs.append(
            f"<tr><td>{d['date'].isoformat()}</td><td>{d['watch_size']}</td>"
            f"<td>{_fmt(d['avg_score'], 1)}</td>"
            f"<td>{_chg_span(d['avg_chg'])}</td>"
            f"<td>{d['up_count']}</td><td>{d['buy_count']}</td></tr>")
    return ('<div class="tbl-wrap"><table>'
            "<tr><th>日期</th><th>观察池</th><th>平均综合分</th><th>平均涨跌%</th>"
            "<th>上涨家数</th><th>买点数</th></tr>"
            + "".join(trs) + "</table></div>")


def _status_table(counts: dict[str, int]) -> str:
    trs = []
    for st in STATUSES:
        n = counts.get(st, 0)
        trs.append(f"<tr><td>{_chip(st)}</td><td>{n}</td></tr>")
    return ('<div class="tbl-wrap"><table style="max-width:420px">'
            "<tr><th>状态</th><th>数量</th></tr>" + "".join(trs) + "</table></div>")


def _render_html(data: dict) -> str:
    latest = data["latest"]
    pos = data["positions"]
    active_n = len(pos["active"])
    pending_n = len(pos["pending"])
    closed_n = len(pos["closed"])
    buy_n = len(data["buy_points"])
    news_n = len(latest["news"]) if latest else 0

    date_str = data["latest_date"].isoformat() if data["latest_date"] else "暂无数据"
    kpis = []
    kpis.append(_kpi_card("观察天数", str(len(data["days"])), "#58A6FF"))
    kpis.append(_kpi_card("观察池", str(latest["watch_size"] if latest else 0), "#39D2C0"))
    kpis.append(_kpi_card("今日买点", str(buy_n), "#D29922" if buy_n else "#8B949E"))
    kpis.append(_kpi_card("平均综合分", _fmt(latest["avg_score"] if latest else None, 1), "#58A6FF"))
    avg_chg = latest["avg_chg"] if latest else None
    chg_color = "#F85149" if (avg_chg or 0) > 0 else ("#3FB950" if (avg_chg or 0) < 0 else "#8B949E")
    kpis.append(_kpi_card("平均涨跌%", _fmt(avg_chg, 2) if avg_chg is not None else "-", chg_color))
    kpis.append(_kpi_card("上涨家数", str(latest["up_count"] if latest else 0), "#3FB950"))
    kpis.append(_kpi_card("活跃持仓", f"{active_n}/{pending_n}", "#BC8CFF" if active_n or pending_n else "#8B949E"))
    kpis.append(_kpi_card("今日新信号", str(news_n), "#F85149" if news_n else "#8B949E"))

    nav = ('<a href="index.html">首页</a>'
           '<a href="live.html">实时盯盘</a>' + "".join(
        f'<a href="#sec-{i}">{label}</a>'
        for i, label in enumerate(
            ["今日买点", "持仓管理", "观察池", "全市场新信号", "每日概况", "状态分布"])))

    body = f"""
<div class="wrap">
  <header>
    <h1>主升浪信号跟踪 · 网页仪表盘</h1>
    <div class="sub">数据截止 <span class="date">{date_str}</span> ｜ 规则：均线多头+创20日新高+放量上攻；买点1=次日确认开盘买，买点2=回踩MA10低吸；止损-4%/破MA10，止盈高点回落8%，5日时间止损；单票≤1/3仓，最多3只并行</div>
  </header>
  <div class="nav">{nav}</div>
  <div class="kpis">{''.join(kpis)}</div>

  <section class="card" id="sec-0">
    <h2>今日买点提示（{date_str}）</h2>
    <div class="body">{_buy_points_table(data['buy_points'])}</div>
  </section>

  <section class="card" id="sec-1">
    <h2>持仓管理（活跃 {active_n} / 待买入 {pending_n} / 已平仓 {closed_n}）</h2>
    <div class="body">{_positions_table(pos)}</div>
  </section>

  <section class="card" id="sec-2">
    <h2>观察池状态（{latest['watch_size'] if latest else 0} 只，按综合分排序）</h2>
    <div class="body">{_watch_table(data['watch_view'])}</div>
  </section>

  <section class="card" id="sec-3">
    <h2>全市场新信号（当日）</h2>
    <div class="body">{_news_table(latest['news'] if latest else [])}</div>
  </section>

  <section class="card" id="sec-4">
    <h2>每日概况</h2>
    <div class="body">{_daily_table(data['days'])}</div>
  </section>

  <section class="card" id="sec-5">
    <h2>状态分布（观察池，最新日）</h2>
    <div class="body">{_status_table(data['status_counts'])}</div>
  </section>

  <div class="note">数据源：output/reports/主升浪跟踪_*.csv + output/state/mainrise_positions.csv、
  mainrise_watchlist.csv；涨跌幅/10日涨幅为百分数；买点提示=观察池中 T0/T1/回踩低吸
  且 10日涨幅&lt;{DEFAULT_MAX_10D_GAIN:g}%（防追高）。</div>
  <footer>
    免责声明：本页面所有输出（信号、评分、买点提示、持仓）仅用于研究学习，
    不构成任何投资建议，请勿作为直接投资依据。股市有风险，决策需谨慎。<br>
    生成时间：{data['generated_at']} ｜ 主升浪信号跟踪
  </footer>
</div>
"""
    return (f"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"theme-color\" content=\"#0D1117\">"
            f"<title>主升浪信号跟踪 · 网页仪表盘</title>"
            f"<style>{CSS}</style></head><body>{body}{CODE_LINK_JS}"
            f"{_refresh_ui('web')}</body></html>")


# ---------------------------------------------------------------- 门户/报告页

def _md_to_html(text: str) -> str:
    """极简 md -> HTML（支持 #/##/### 标题、表格、引用、列表；先转义防注入）。"""
    lines = text.splitlines()
    out = []
    in_table = False
    for raw in lines:
        line = raw.strip()
        if not line:
            if in_table:
                out.append("</table>")
                in_table = False
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and all(set(c) <= set("-: ") for c in cells):
                continue  # 表头分隔行
            if not in_table:
                out.append("<table>")
                in_table = True
                tag = "th"
            else:
                tag = "td"
            out.append("<tr>" + "".join(
                f"<{tag}>{html.escape(c)}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("### "):
            out.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<p class='quote'>{html.escape(line[2:])}</p>")
        elif line.startswith("- "):
            out.append(f"<li>{html.escape(line[2:])}</li>")
        else:
            out.append(f"<p>{html.escape(line)}</p>")
    if in_table:
        out.append("</table>")
    return "".join(out)


def _page(title: str, body: str, refresh: str = "web") -> str:
    return (f"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"theme-color\" content=\"#0D1117\">"
            f"<title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body>"
            f"<div class=\"wrap\">{body}</div>{CODE_LINK_JS}"
            f"{_refresh_ui(refresh)}</body></html>")


def _render_portal(latest_date: str | None) -> str:
    date_str = latest_date or "暂无数据"
    buttons = [
        ("KPI 仪表盘", "dashboard.html", "信号 / 持仓 / 买点 / 每日概况", "#58A6FF"),
        ("实时盯盘", "live.html", "盘中持仓与观察池异动，30 秒自动刷新", "#D29922"),
        ("当日复盘", "review.html", "连板梯队 / 题材 / 情绪周期 / 龙虎榜资金", "#F85149"),
        ("每日报告", "reports.html", "跟踪报告 / 综合评估 / 财务评估原文", "#39D2C0"),
        ("使用说明", "about.html", "模型规则：信号 / 买点 / 止损 / 止盈 / 仓位", "#BC8CFF"),
    ]
    cards = "".join(
        f'<a class="pbtn" style="--accent:{color}" href="{href}">'
        f'<div class="pt">{html.escape(title)}</div>'
        f'<div class="pd">{html.escape(desc)}</div>'
        f'<div class="parrow">→</div></a>'
        for title, href, desc, color in buttons)
    body = f"""
<style>
.portal{{text-align:center;padding-top:8vh}}
.portal h1{{font-size:26px;color:#39D2C0;letter-spacing:2px}}
.portal .sub{{color:#8B949E;font-size:13px;margin:8px 0 28px}}
.pbtns{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:14px;max-width:860px;margin:0 auto}}
.pbtn{{background:#161B22;border:1px solid #30363D;border-bottom:3px solid var(--accent);
  border-radius:12px;padding:22px 18px;text-align:left;text-decoration:none;display:block}}
.pbtn:hover{{border-color:var(--accent);transform:translateY(-2px)}}
.pt{{color:#E6EDF3;font-size:17px;font-weight:700}}
.pd{{color:#8B949E;font-size:12px;margin-top:6px}}
.parrow{{color:var(--accent);font-size:18px;margin-top:10px}}
</style>
<div class="portal">
  <h1>主升浪信号跟踪</h1>
  <div class="sub">数据截止 {date_str} ｜ 选择入口</div>
  <div class="pbtns">{cards}</div>
  <footer>免责声明：所有输出仅用于研究学习，不构成投资建议。股市有风险，决策需谨慎。</footer>
</div>
"""
    return _page("主升浪信号跟踪 · 首页", body)


def _render_report_page(title: str, md_text: str) -> str:
    body = (f'<header><h1>{html.escape(title)}</h1>'
            '<div class="sub"><a href="../reports.html" style="color:#8B949E">← 返回报告列表</a>'
            ' ｜ <a href="../index.html" style="color:#8B949E">首页</a></div></header>'
            f'<div class="card"><div class="body report-body">{_md_to_html(md_text)}</div></div>')
    return _page(title, body)


def _render_reports_page(reports: Path) -> str:
    groups = []
    for pat, label in (("主升浪跟踪_*.md", "每日跟踪报告"),
                       ("主升浪信号综合评估_*.md", "信号综合评估"),
                       ("信号评估_*.md", "财务评估")):
        items = []
        for p in sorted(reports.glob(pat), reverse=True):
            date = p.stem.split("_")[-1]
            items.append(f'<a href="reports_view/{p.stem}.html">{date}</a>')
        groups.append((label, items[:20]))
    sections = ""
    for label, items in groups:
        links = "".join(f'<span class="rep">{i}</span>' for i in items) if items else "<span style='color:#6E7681'>暂无</span>"
        sections += f"<h2>{html.escape(label)}</h2><div class='reps'>{links}</div>"
    body = (f"<style>.reps{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}}"
            f".rep a{{color:#58A6FF;text-decoration:none;font-size:13px}}"
            f".rep a:hover{{color:#39D2C0}}.report-body table{{margin:8px 0}}</style>"
            '<header><h1>每日报告</h1>'
            '<div class="sub"><a href="index.html" style="color:#8B949E">← 返回首页</a></div></header>'
            f'<div class="card"><div class="body">{sections}</div></div>')
    return _page("主升浪信号跟踪 · 每日报告", body)


ABOUT_HTML = """
<header><h1>使用说明</h1>
<div class="sub"><a href="index.html" style="color:#8B949E">← 返回首页</a></div></header>
<div class="card"><div class="body">
<h2>核心信号规则</h2>
<table><tr><th>环节</th><th>规则</th></tr>
<tr><td>信号日 T0</td><td>均线多头（MA5&gt;MA10&gt;MA20）+ 创 20 日新高 +（涨幅≥5% 且量比≥1.5）或涨停</td></tr>
<tr><td>买点1</td><td>T0 次日确认（收&gt;MA5 且 低点≥T0收盘×0.97）→ 次日开盘买入</td></tr>
<tr><td>买点2</td><td>回踩 MA10 缩量企稳（低点≥MA20×0.99）→ 低吸</td></tr>
<tr><td>止损</td><td>买入价 -4%（盘中低点触发）</td></tr>
<tr><td>止盈</td><td>盘中最高点到收盘回落 8%（收盘判断）</td></tr>
<tr><td>时间止损</td><td>持仓 5 个交易日</td></tr>
<tr><td>防追高</td><td>10 日涨幅 ≥80% 不进买点提示</td></tr>
<tr><td>仓位</td><td>单票 ≤1/3，最多 3 只并行，优先综合分 Top10</td></tr>
</table>
<h2>页面说明</h2>
<ul><li>KPI 仪表盘：信号/持仓/买点/每日概况/状态分布</li>
<li>实时盯盘：交易日盘中 30 秒刷新，持仓止损-4%、回落8%、观察池±5%、回踩 MA10/MA20</li>
<li>每日报告：跟踪报告、综合评估、财务评估原文</li></ul>
<p class="quote">免责：所有输出仅用于研究学习，不构成投资建议。</p>
</div></div>
"""


# ---------------------------------------------------------------- 对外入口

def update_web_dashboard(reports_dir=None, state_dir=None, output=None) -> Path:
    """生成门户首页 + KPI 仪表盘 + 每日报告，返回门户首页路径。"""
    reports = Path(reports_dir) if reports_dir else paths.report_dir()
    states = Path(state_dir) if state_dir else paths.state_dir()
    out = Path(output) if output else paths.web_dir() / OUTPUT_NAME
    web = out.parent
    web.mkdir(parents=True, exist_ok=True)
    data = _collect(reports, states)
    (web / DASHBOARD_NAME).write_text(_render_html(data), encoding="utf-8")

    # 每日报告：列表页 + 每份 md 渲染为静态 HTML
    view_dir = web / "reports_view"
    view_dir.mkdir(exist_ok=True)
    for p in sorted(reports.glob("主升浪跟踪_*.md")) + \
            sorted(reports.glob("主升浪信号综合评估_*.md")) + \
            sorted(reports.glob("信号评估_*.md")):
        title = p.stem.replace("_", " ").replace("主升浪跟踪", "每日跟踪")
        (view_dir / f"{p.stem}.html").write_text(
            _render_report_page(title, p.read_text(encoding="utf-8")),
            encoding="utf-8")
    (web / REPORTS_NAME).write_text(
        _render_reports_page(reports), encoding="utf-8")

    (web / "about.html").write_text(_page("主升浪信号跟踪 · 使用说明", ABOUT_HTML),
                                    encoding="utf-8")
    latest = data["latest_date"]
    out.write_text(_render_portal(latest.isoformat() if latest else None),
                   encoding="utf-8")
    return out


if __name__ == "__main__":
    print(update_web_dashboard())
