"""标的详情页：分时图 / 日K 线（实时盯盘页点击标的进入）。

数据源：
- 日K：腾讯 web.ifzq.gtimg.cn fqkline（前复权，稳定）
- 分时：腾讯 minute/query（盘中 1 分钟线），失败回退东财 trends2
- 盯盘缩略图：快照每 3 秒累积 + 腾讯分时每 5 分钟回填（live_minutes.json）

服务方式：monitor 进程内启动 stdlib HTTP 服务（127.0.0.1:8765），
Nginx 把 /stock/ 反代到该端口（沿用同一 Basic Auth）。按标的缓存 60 秒，
仅用户打开详情页时才取数。
"""
from __future__ import annotations

import json
import requests
import threading
import time
import urllib.parse
import csv as _csv
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mainrise import paths
from mainrise.monitor import TZ_CN
from mainrise.review import _eastmoney
from mainrise.snapshot import ths_code
from mainrise.web_dashboard import CSS

STOCK_PORT = 8765
CACHE_TTL = 60          # 每标的缓存秒数（腾讯优先后可近实时，60s 内更新）
CACHE: dict[str, tuple[float, dict]] = {}
CACHE_LOCK = threading.Lock()
RECENT: dict[str, float] = {}       # 最近访问的标的（code -> 时间戳）
RECENT_LOCK = threading.Lock()
RECENT_WINDOW = 1800                 # 30 分钟内访问过才算"热"
RECENT_MAX = 15                      # 每分钟续刷的上限（保护接口限流）
TX_MIN = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TRADES_CACHE: dict = {"mtime": 0.0, "by_code": {}}
POS_CACHE: dict = {"mtime": 0.0, "by_code": {}}
MIN_SERIES: dict[str, list[list]] = {}   # code -> [[时间, 价], ...]（盯盘缩略图）
MIN_LOCK = threading.Lock()
MIN_CAP = 720                            # 单只保留点数（3s 轮询约 6 小时）


def _num(v):
    try:
        return float(v) if v not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _secid(code: str) -> str:
    c = code.split(".")[0]
    return f"1.{c}" if ths_code(code).endswith(".SH") else f"0.{c}"


def _tx_code(code: str) -> str:
    c = code.split(".")[0]
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[ths_code(code).split(".")[1]]
    return prefix + c


def _tx_minute(code: str) -> list[dict]:
    """腾讯当日分时（HHMM 价格 成交量 成交额；均价线用累计额/量计算）。"""
    key = _tx_code(code)
    r = requests.get(TX_MIN, params={"code": key}, timeout=8)
    d = r.json()
    node = ((d.get("data") or {}).get(key) or {}).get("data") or {}
    out = []
    for line in node.get("data") or []:
        p = line.split()
        if len(p) >= 2 and p[0]:
            t = p[0]
            time_s = f"{t[:2]}:{t[2:]}"
            if time_s > "15:00":      # 去掉收盘后的多余快照点
                continue
            point = {"time": time_s, "price": float(p[1])}
            if len(p) >= 3:
                point["volume"] = float(p[2])
            if len(p) >= 4:
                point["amount"] = float(p[3])
            out.append(point)
    return out


def _tx_kline(code: str, limit: int = 120) -> list[dict]:
    """腾讯日K（前复权）。"""
    key = _tx_code(code)
    r = requests.get(TX_KLINE,
                     params={"param": f"{key},day,,,{limit},qfq"}, timeout=8)
    d = r.json()
    node = (d.get("data") or {}).get(key) or {}
    bars = node.get("qfqday") or node.get("day") or []
    out = []
    for b in bars:
        if len(b) >= 6:
            out.append({"date": b[0], "open": float(b[1]),
                        "close": float(b[2]), "high": float(b[3]),
                        "low": float(b[4]), "volume": float(b[5])})
    return out


def fetch_daily_kline(code: str, limit: int = 120) -> list[dict]:
    """日K（前复权）：腾讯 fqkline（稳定）。"""
    return _tx_kline(code, limit)


def _em_fast(path: str, params: dict) -> dict:
    """东财快速请求：单主机单次、6s 超时，失败立即抛错（不阻塞K线展示）。"""
    r = requests.get("https://push2his.eastmoney.com" + path,
                     params=params, timeout=6)
    d = r.json()
    if d.get("rc") != 0:
        raise RuntimeError(f"rc={d.get('rc')}")
    return d.get("data") or {}


def fetch_minute_trend(code: str) -> list[dict]:
    """当日分时（价格点）：腾讯优先（稳定），失败回退东财（快速失败）。"""
    try:
        trends = _tx_minute(code)
        if trends:
            return trends
    except Exception:  # noqa: BLE001
        pass
    d = _em_fast("/api/qt/stock/trends2/get", {
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ndays": 1, "iscr": 0})
    out = []
    for t in (d.get("trends") or []):
        p = t.split(",")
        if len(p) >= 2:
            out.append({"time": p[0][11:16] if len(p[0]) > 11 else p[0],
                        "price": _num(p[1])})
    return out


def _base(code: str) -> dict:
    from mainrise.signals import load_names
    return {"code": code, "name": load_names().get(code, "") or code,
            "kline": [], "trends": [], "signals": [], "error": "",
            "updated": datetime.now(TZ_CN).strftime("%H:%M:%S")}


def _signals_for(code: str) -> list[dict]:
    """两级模型信号明细（mainrise_trades.csv）：S_date=信号日，buy_date=买入日。"""
    p = paths.report_dir() / "mainrise_trades.csv"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    if TRADES_CACHE["mtime"] != mtime:
        by: dict[str, list[dict]] = {}
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                for row in _csv.DictReader(f):
                    c = (row.get("code") or "").strip()
                    if c:
                        by.setdefault(c, []).append({
                            "S_date": (row.get("S_date") or "").strip(),
                            "buy_date": (row.get("buy_date") or "").strip(),
                            "kind": (row.get("kind") or "B3").strip()})
        except OSError:
            by = {}
        TRADES_CACHE.update({"mtime": mtime, "by_code": by})
    return TRADES_CACHE["by_code"].get(code, [])


def _closes_for(code: str) -> list[dict]:
    """纸面持仓平仓记录（mainrise_positions.csv，status=closed）。按 mtime 缓存。"""
    p = paths.state_dir() / "mainrise_positions.csv"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    if POS_CACHE["mtime"] != mtime:
        by: dict[str, list[dict]] = {}
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                for row in _csv.DictReader(f):
                    if (row.get("status") or "").strip() != "closed":
                        continue
                    c = (row.get("code") or "").strip()
                    if c:
                        by.setdefault(c, []).append({
                            "date": (row.get("close_date") or "").strip(),
                            "reason": (row.get("reason") or "").strip()})
        except OSError:
            by = {}
        POS_CACHE.update({"mtime": mtime, "by_code": by})
    return POS_CACHE["by_code"].get(code, [])


def _touch_recent(code: str) -> None:
    with RECENT_LOCK:
        RECENT[code] = time.time()
        cutoff = time.time() - RECENT_WINDOW
        for c in [c for c, t in RECENT.items() if t < cutoff]:
            del RECENT[c]


def recent_viewed() -> list[str]:
    """最近访问过的标的（按访问时间倒序，最多 RECENT_MAX 只）。"""
    with RECENT_LOCK:
        cutoff = time.time() - RECENT_WINDOW
        items = [(c, t) for c, t in RECENT.items() if t >= cutoff]
        items.sort(key=lambda x: -x[1])
        return [c for c, _ in items[:RECENT_MAX]]


def warm_kline(code: str) -> None:
    """预热/续刷日K，合并进缓存；新条目连同分时一起取，避免残缺缓存。"""
    kline = fetch_daily_kline(code)
    with CACHE_LOCK:
        hit = CACHE.get(code)
        base = hit[1] if hit else None
    if base is None:
        try:
            trends = fetch_minute_trend(code)
        except Exception:  # noqa: BLE001
            trends = []
        base = _base(code)
        base["trends"] = trends
    base["kline"] = kline
    base["updated"] = datetime.now(TZ_CN).strftime("%H:%M:%S")
    with CACHE_LOCK:
        CACHE[code] = (time.time(), base)


def warm_trends(code: str) -> None:
    """续刷分时（腾讯优先，东财快速兜底），合并进缓存。"""
    trends = fetch_minute_trend(code)
    with CACHE_LOCK:
        hit = CACHE.get(code)
        base = hit[1] if hit else _base(code)
        base["trends"] = trends
        base["updated"] = datetime.now(TZ_CN).strftime("%H:%M:%S")
        CACHE[code] = (time.time(), base)


# ---------------------------------------------------------------- 盯盘分时缩略图序列

def _min_path() -> Path:
    return paths.state_dir() / "live_minutes.json"


def load_minutes() -> None:
    """启动时载入上次落盘的分钟序列（服务重启不丢盘中数据）。"""
    try:
        with open(_min_path(), encoding="utf-8") as f:
            data = json.load(f)
        with MIN_LOCK:
            MIN_SERIES.clear()
            for k, v in (data or {}).items():
                if isinstance(v, list):
                    MIN_SERIES[k] = [
                        [p[0], float(p[1])] for p in v[-MIN_CAP:]
                        if isinstance(p, list) and len(p) >= 2]
    except (OSError, ValueError, TypeError):
        pass


def save_minutes() -> None:
    try:
        _min_path().parent.mkdir(parents=True, exist_ok=True)
        with MIN_LOCK:
            payload = {k: list(v) for k, v in MIN_SERIES.items()}
        tmp = _min_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(_min_path())
    except OSError:
        pass


def append_minute_point(code: str, price: float | None, now=None) -> None:
    """盯盘每 3 秒快照累积一个点（同秒覆盖，避免重复点）。"""
    if price is None:
        return
    ts = (now or datetime.now(TZ_CN)).strftime("%H:%M:%S")
    with MIN_LOCK:
        s = MIN_SERIES.setdefault(code, [])
        if s and s[-1][0] == ts:
            s[-1][1] = price
            return
        s.append([ts, price])
        if len(s) > MIN_CAP:
            del s[:-MIN_CAP]


def backfill_minutes(codes: list[str]) -> int:
    """腾讯分时整日回填（1 分钟线），失败静默跳过；返回成功数。"""
    ok = 0
    for c in codes:
        try:
            pts = _tx_minute(c)
            if not pts:
                continue
            with MIN_LOCK:
                MIN_SERIES[c] = [[p["time"], p["price"]] for p in pts]
            ok += 1
        except Exception:  # noqa: BLE001
            pass
    return ok


def minute_series(code: str) -> list[list]:
    with MIN_LOCK:
        return list(MIN_SERIES.get(code, []))


def build_stock_data(code: str) -> dict:
    from mainrise.signals import load_names
    name = load_names().get(code, "") or code
    kline, trends = [], []
    signals: list[dict] = []
    closes: list[dict] = []
    errs = []
    try:
        kline = fetch_daily_kline(code)
    except Exception as e:  # noqa: BLE001
        errs.append(f"日K: {type(e).__name__}")
    try:
        trends = fetch_minute_trend(code)
    except Exception as e:  # noqa: BLE001
        errs.append(f"分时: {type(e).__name__}")
    # B3/二波 信号触发点（映射到 K线窗口内的日期）
    if kline:
        idx = {k["date"]: i for i, k in enumerate(kline)}
        for s in _signals_for(code)[-8:]:
            t0 = s["S_date"]
            if t0 not in idx:
                continue
            i0 = idx[t0]
            t1 = kline[i0 + 1]["date"] if i0 + 1 < len(kline) else None
            t2 = s["buy_date"] if s["buy_date"] in idx else (
                kline[i0 + 2]["date"] if i0 + 2 < len(kline) else None)
            signals.append({"t0": t0, "t1": t1, "t2": t2,
                            "kind": s.get("kind", "B3")})
        for s in _closes_for(code)[-8:]:
            if s["date"] in idx:
                closes.append({"date": s["date"], "reason": s["reason"]})
    return {"code": code, "name": name,
            "updated": datetime.now(TZ_CN).strftime("%H:%M:%S"),
            "kline": kline, "trends": trends, "signals": signals[-5:],
            "closes": closes[-5:],
            "error": "；".join(errs)}


PAGE_JS_LEGACY = """
/* ── 看盘软件风格图表const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
：分时(均价/昨收/量/十字光标) + K线(MA/量/OHLC提示) ── */
const FMT2=v=>v==null?'-':(v>=1e8?(v/1e8).toFixed(2)+'亿':(v>=1e4?(v/1e4).toFixed(2)+'万':v.toFixed(0)));
const FMTV=v=>v==null?'-':(v>=1e4?(v/1e4).toFixed(2)+'万手':v.toFixed(0)+'手');
function linePts(pairs){return pairs.filter(p=>p[1]!=null).map(p=>p[0]+','+p[1]).join(' ');}
function linReg(vals){
  const n=vals.length; if(n<2)return null;
  let sx=0,sy=0,sxy=0,sxx=0;
  for(let i=0;i<n;i++){sx+=i;sy+=vals[i];sxy+=i*vals[i];sxx+=i*i;}
  const den=n*sxx-sx*sx; if(!den)return null;
  const a=(n*sxy-sx*sy)/den, b=(sy-a*sx)/n;
  let se=0; for(let i=0;i<n;i++){const e=vals[i]-(a*i+b);se+=e*e;}
  return {a:a,b:b,se:Math.sqrt(se/n)};
}

function bindCross(box, svg, W, xOf, yTop, yBot, tipHtml){
  var line=document.createElementNS('http://www.w3.org/2000/svg','line');
  line.setAttribute('stroke','#8B949E'); line.setAttribute('stroke-dasharray','3,3'); line.setAttribute('opacity','0.8');
  svg.appendChild(line);
  var tip=document.createElement('div');
  tip.style.cssText='position:absolute;pointer-events:none;display:none;background:#0D1117;border:1px solid #30363D;border-radius:6px;padding:6px 8px;font-size:11px;color:#E6EDF3;white-space:nowrap;z-index:20;';
  box.appendChild(tip);
  box.addEventListener('mousemove',function(e){
    var r=box.getBoundingClientRect();
    var rx=(e.clientX-r.left)/r.width*W;
    var i=Math.round((rx-xOf(0))/(xOf(1)-xOf(0)));
    i=Math.max(0,Math.min(xOf.n-1,i));
    var cx=xOf(i);
    line.setAttribute('x1',cx); line.setAttribute('x2',cx);
    line.setAttribute('y1',yTop); line.setAttribute('y2',yBot);
    tip.innerHTML=tipHtml(i);
    var px=Math.min(r.width-8,tip.offsetWidth+8);
    tip.style.left=(rx/W*r.width-px/2)+'px';
    tip.style.top='8px'; tip.style.display='block';
  });
  box.addEventListener('mouseleave',function(){ line.setAttribute('opacity','0'); tip.style.display='none'; });
}

async function load(code){
  const box=document.getElementById('charts');
  box.innerHTML='<div class="empty">加载中…</div>';
  let d;
  try{ d=await (await fetch('/stock/'+code+'/data')).json(); }
  catch(e){ box.innerHTML='<div class="empty">数据加载失败</div>'; return; }
  if(document.getElementById('upd'))document.getElementById('upd').textContent=d.updated;
  const prev=d.kline&&d.kline.length>1?d.kline[d.kline.length-2].close:null;
  let html=d.error?('<div class="empty">'+esc(d.error)+'（K线/分时腾讯优先，分时东财兜底）</div>'):'';
  if(d.kline&&d.kline.length) html+=drawK(d.kline,esc(d.name,prev,d.signals,d.closes);
  if(d.trends&&d.trends.length) html+=drawT(d.trends,esc(d.name,prev);
  box.innerHTML=html||'<div class="empty">暂无数据</div>';
}

function drawT(ts,name,prev){
  const W=860,H=250,VH=64,P={l:62,r:16,t:16,b:26};
  const n=ts.length;
  const x=i=>P.l+(W-P.l-P.r)*i/Math.max(1,n-1); x.n=n;
  let cv=0,ca=0;
  const avg=ts.map(t=>{cv+=t.volume||0;ca+=t.amount||0;return (t.volume&&cv>0)?ca/(cv*100):null;});  // 量单位手→股
  const isNum=v=>typeof v==='number'&&isFinite(v);
  const all=[...ts.map(t=>t.price),...avg.filter(isNum),prev??ts[0].price].filter(isNum);
  const lo=Math.min(...all),hi=Math.max(...all);
  const pad=(hi-lo)*0.12||0.01; const L=lo-pad,H2=hi+pad;
  const y=v=>P.t+(H-P.t-P.b)*(1-(v-L)/(H2-L));
  const up=(ts[n-1].price||0)>=(prev??ts[0].price);
  const mainC=up?'#F85149':'#3FB950';
  const times=['09:30','10:30','11:30','13:00','14:00','15:00'];
  let grid='';
  for(let k=0;k<=4;k++){const yy=P.t+(H-P.t-P.b)*k/4;grid+=`<line x1="${P.l}" y1="${yy}" x2="${W-P.r}" y2="${yy}" stroke="#21262D"/>`;}
  times.forEach(t=>{
    let i=ts.findIndex(p=>p.time>=t); if(i<0)i=n-1;
    grid+=`<text x="${x(i)}" y="${H-8}" fill="#8B949E" font-size="10" text-anchor="middle">${t}</text>
    <line x1="${x(i)}" y1="${P.t}" x2="${x(i)}" y2="${H-VH-4}" stroke="#21262D" stroke-dasharray="2,4"/>`;
  });
  const area='M'+x(0)+','+y(ts[0].price)+' '+ts.map((t,i)=>'L'+x(i)+','+y(t.price)).join(' ')+' L'+x(n-1)+','+(H-VH-2)+' L'+x(0)+','+(H-VH-2)+' Z';
  const priceLine=linePts(ts.map((t,i)=>[x(i),y(t.price)]));
  const avgPts=avg.map((v,i)=>v==null?null:[x(i),y(v)]);
  let avgSeg='',seg=[];
  avgPts.forEach(p=>{if(p){seg.push(p);}else if(seg.length){avgSeg+=linePts(seg)+' ';seg=[];}});
  if(seg.length)avgSeg+=linePts(seg);
  const vmax=Math.max(...ts.map(t=>t.volume||0),1);
  const yv=v=>H-VH+(VH-14)*(1-(v||0)/vmax);
  let vols='';
  ts.forEach((t,i)=>{const c=(t.price||0)>=(prev??ts[0].price)?'#F85149':'#3FB950';
    const bw=Math.max(1,(W-P.l-P.r)/n*0.55);
    vols+=`<rect x="${x(i)-bw/2}" y="${yv(t.volume)}" width="${bw}" height="${H-VH-2-yv(t.volume)}" fill="${c}" opacity="0.55"/>`;});
  const pxTicks=[0,1,2,3,4].map(k=>L+(H2-L)*k/4);
  const axis=pxTicks.map(v=>`<text x="${W-P.r-2}" y="${y(v)+3}" fill="#8B949E" font-size="10" text-anchor="end">${v.toFixed(2)}</text>`).join('');
  const boxHtml='<div class="card"><h2>分时 · '+name+'（今）</h2><div class="body">'
    +'<div style="position:relative" id="boxT"><svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;display:block">'
    +grid+axis
    +'<line x1="'+P.l+'" y1="'+(prev!=null?y(prev):0)+'" x2="'+(W-P.r)+'" y2="'+(prev!=null?y(prev):0)+'" stroke="#8B949E" stroke-dasharray="4,4" opacity="0.7"/>'
    +'<path d="'+area+'" fill="'+mainC+'" opacity="0.08"/>'
    +'<polyline points="'+priceLine+'" fill="none" stroke="'+mainC+'" stroke-width="1.8"/>'
    +'<polyline points="'+avgSeg.trim()+'" fill="none" stroke="#FACC15" stroke-width="1.4"/>'
    +vols
    +'<text x="'+(W-P.r-2)+'" y="'+P.t+14+'" fill="'+mainC+'" font-size="11" text-anchor="end">现价 '+(ts[n-1].price||'').toFixed(2)+'</text>'
    +'<text x="'+(P.l+4)+'" y="'+P.t+14+'" fill="#FACC15" font-size="11">均价 '+(avg[n-1]!=null?avg[n-1].toFixed(2):'-')+'</text>'
    +'</svg></div><div class="note">红涨绿跌；黄色=均价线，灰虚线=昨收；下方为成交量（手）。</div></div></div>';
  setTimeout(function(){
    var box=document.getElementById('boxT'); var svg=box.querySelector('svg');
    bindCross(box,svg,W,x,P.t,H-VH-2,function(i){
      const t=ts[i],a=avg[i],chg=prev?(t.price/prev-1)*100:0;
      return t.time+'　价 '+t.price.toFixed(2)+'　均价 '+(a!=null?a.toFixed(2):'-')+'　涨跌 '+(chg>=0?'+':'')+chg.toFixed(2)+'%　量 '+FMTV(t.volume);
    });
  },0);
  return boxHtml;
}

function kZoom(dir){
  var st=window.kState; if(!st)return;
  st.cnt=Math.max(st.min,Math.min(st.max, dir>0?Math.round(st.cnt/1.25):Math.round(st.cnt*1.25)));
  if(st.end>st.ks.length)st.end=st.ks.length;
  st.redraw();
}
function kPan(dir){
  var st=window.kState; if(!st)return;
  var step=Math.max(1,Math.round(st.cnt*0.25));
  st.end=Math.max(st.cnt,Math.min(st.ks.length,st.end+dir*step));
  st.redraw();
}
function kReset(){
  var st=window.kState; if(!st)return;
  st.cnt=st.ks.length; st.end=st.ks.length; st.redraw();
}
function drawK(ks,name,prev,signals,closes){
  const W=860,H=290,VH=70,P={l:62,r:16,t:18,b:26};
  const N=ks.length;
  const st={ks:ks,prev:prev,signals:signals||[],closes:closes||[],mas:[],
            cnt:Math.min(60,N),end:N,min:20,max:Math.max(20,N)};
  const maArr=p=>{const a=[];for(let i=0;i<N;i++){if(i<p-1){a.push(null);continue;}let s2=0;for(let j=0;j<p;j++)s2+=ks[i-j].close;a.push(s2/p);}return a;};
  st.mas=[maArr(5),maArr(10),maArr(20),maArr(60)];
  const mcols=['#FACC15','#58A6FF','#BC8CFF','#3FB950'];

  function render(){
    const c0=Math.max(0,st.end-st.cnt),n=st.end-c0,seg=st.ks.slice(c0,st.end);
    const x=i=>P.l+(W-P.l-P.r)*(i+0.5)/n; x.n=n;
    const bw=Math.max(2,Math.min(14,(W-P.l-P.r)/n*0.62));
    const hi=Math.max(...seg.map(k=>k.high)),lo=Math.min(...seg.map(k=>k.low));
    const hi2=hi+(hi-lo)*0.12;
    const y=v=>P.t+(H-P.t-P.b-VH)*(1-(v-lo)/(hi2-lo||1));
    const vmax=Math.max(...seg.map(k=>k.volume||0),1);
    const yv=v=>H-VH+(VH-14)*(1-(v||0)/vmax);
    let s='';
    for(let k=0;k<=4;k++){const yy=P.t+(H-P.t-P.b-VH)*k/4;s+=`<line x1="${P.l}" y1="${yy}" x2="${W-P.r}" y2="${yy}" stroke="#21262D"/>`;}
    seg.forEach((k,j)=>{
      const up=(k.close||0)>=(k.open||0),c=up?'#F85149':'#3FB950';
      s+=`<line x1="${x(j)}" y1="${y(k.high)}" x2="${x(j)}" y2="${y(k.low)}" stroke="${c}" stroke-width="1.4"/>`;
      const yc=Math.min(y(k.open),y(k.close)),hc=Math.max(1.5,Math.abs(y(k.open)-y(k.close)));
      s+=`<rect x="${x(j)-bw/2}" y="${yc}" width="${bw}" height="${hc}" fill="${c}"/>`;
      s+=`<rect x="${x(j)-bw/2}" y="${yv(k.volume)}" width="${bw}" height="${H-VH-2-yv(k.volume)}" fill="${c}" opacity="0.45"/>`;
    });
    st.mas.forEach((arr,j)=>{
      let str='',seg2=[];
      for(let i=c0;i<st.end;i++){
        const v=arr[i];
        if(v==null){if(seg2.length){str+=linePts(seg2)+' ';seg2=[];}continue;}
        seg2.push([x(i-c0),y(v)]);
      }
      if(seg2.length)str+=linePts(seg2);
      if(str.trim())s+=`<polyline points="${str.trim()}" fill="none" stroke="${mcols[j]}" stroke-width="1.3"/>`;
    });
    function drawReg(Wd,solid){
      if(n<Wd+2)return;
      const r=linReg(seg.slice(n-Wd).map(k=>k.close));
      if(!r)return;
      const s0=n-Wd;
      const c1=y(r.b),c2=y(r.a*(Wd-1)+r.b);
      const u1=y(r.b+r.se),u2=y(r.a*(Wd-1)+r.b+r.se);
      const l1=y(r.b-r.se),l2=y(r.a*(Wd-1)+r.b-r.se);
      s+=`<line x1="${x(s0)}" y1="${c1}" x2="${x(n-1)}" y2="${c2}" stroke="#58A6FF" stroke-width="${solid?1.8:1.1}"${solid?'':' stroke-dasharray="4,3"'} opacity="0.9"/>`;
      s+=`<line x1="${x(s0)}" y1="${u1}" x2="${x(n-1)}" y2="${u2}" stroke="#58A6FF" stroke-width="1" stroke-dasharray="2,3" opacity="0.3"/>`;
      s+=`<line x1="${x(s0)}" y1="${l1}" x2="${x(n-1)}" y2="${l2}" stroke="#58A6FF" stroke-width="1" stroke-dasharray="2,3" opacity="0.3"/>`;
    }
    drawReg(20,true); drawReg(60,false);
    function swing(field,mode){
      const pts=[];
      for(let i=Math.max(1,n-61);i<n-1;i++){
        const v=seg[i][field];
        const ok=mode==='low'?(v<seg[i-1][field]&&v<seg[i+1][field]):(v>seg[i-1][field]&&v>seg[i+1][field]);
        if(ok)pts.push({i:i,v:v});
      }
      if(pts.length<2)return;
      const p1=pts[pts.length-1],p2=pts[pts.length-2];
      if(Math.abs(p1.i-p2.i)<3)return;
      s+=`<line x1="${x(p1.i)}" y1="${y(p1.v)}" x2="${x(p2.i)}" y2="${y(p2.v)}" stroke="#58A6FF" stroke-width="1.3" opacity="0.75"/>`;
      s+=`<circle cx="${x(p1.i)}" cy="${y(p1.v)}" r="2.5" fill="#58A6FF"/><circle cx="${x(p2.i)}" cy="${y(p2.v)}" r="2.5" fill="#58A6FF"/>`;
    }
    swing('low','low'); swing('high','high');
    const step=Math.max(1,Math.floor(n/8));
    seg.forEach((k,j)=>{ if(j%step===0||j===n-1)s+=`<text x="${x(j)}" y="${H-8}" fill="#8B949E" font-size="10" text-anchor="middle">${k.date.slice(5)}</text>`; });
    const idx={}; seg.forEach((k,j)=>idx[k.date]=c0+j);
    (st.signals).forEach(sig=>{
      [['t0',sig.kind||'B3'],['t1','确'],['t2','买']].forEach(function(pair){
        const d=sig[pair[0]];
        if(!d||!(d in idx))return;
        const j=idx[d]-c0;
        if(j<0||j>=n)return;
        const cx=x(j),cy=y(seg[j].high)-8;
        s+=`<line x1="${cx}" y1="${cy-7}" x2="${cx}" y2="${cy+3}" stroke="#F85149" stroke-width="1.6" marker-end="url(#arrR)"/>`
          +`<text x="${cx}" y="${cy-13}" fill="#F85149" font-size="10" font-weight="bold" text-anchor="middle">${pair[1]}</text>`;
      });
    });
    (st.closes||[]).forEach(c=>{
      if(!(c.date in idx))return;
      const j=idx[c.date]-c0;
      if(j<0||j>=n)return;
      const cx=x(j),cy=y(seg[j].high)-8;
      s+=`<line x1="${cx}" y1="${cy-7}" x2="${cx}" y2="${cy+3}" stroke="#3FB950" stroke-width="1.6" marker-end="url(#arrG)"/>`
        +`<text x="${cx}" y="${cy-13}" fill="#3FB950" font-size="10" font-weight="bold" text-anchor="middle">平</text>`;
    });
    const pxTicks=[0,1,2,3,4].map(k=>lo+(hi2-lo)*k/4);
    const axis=pxTicks.map(v=>`<text x="${W-P.r-2}" y="${y(v)+3}" fill="#8B949E" font-size="10" text-anchor="end">${v.toFixed(2)}</text>`).join('');
    return {svg:s+axis,c0:c0,n:n,x:x,y:y,seg:seg};
  }

  function bind(){
    const box=document.getElementById('boxK');
    const svg=box.querySelector('svg');
    const r=st._r;
    bindCross(box,svg,W,r.x,r.y,H-VH-2,function(j){
      const k=r.seg[j],c=r.seg[j>0?j-1:0].close;
      const chg=k.close/c*100-100;
      const gi=j+r.c0;
      return k.date+'　开 '+k.open.toFixed(2)+' 高 '+k.high.toFixed(2)+' 低 '+k.low.toFixed(2)+' 收 '+k.close.toFixed(2)
        +'　<span style="color:'+(chg>=0?'#F85149':'#3FB950')+'">'+(chg>=0?'+':'')+chg.toFixed(2)+'%</span>'
        +'　量 '+FMTV(k.volume)+'<br>MA5 '+(st.mas[0][gi]||0).toFixed(2)+' MA10 '+(st.mas[1][gi]||0).toFixed(2)
        +' MA20 '+(st.mas[2][gi]||0).toFixed(2)+' MA60 '+(st.mas[3][gi]||0).toFixed(2);
    });
  }

  function redraw(){
    st._r=render();
    const box=document.getElementById('boxK');
    box.innerHTML=st._r.svg;
    const lab=document.getElementById('kzoom');
    if(lab)lab.textContent='近 '+st.cnt+' 日'+(st.end<st.ks.length?'（第'+(st.end-st.cnt+1)+'-'+st.end+'根）':'');
    bind();
  }
  st.redraw=redraw;

  const last=st.ks[N-1],chg=st.prev?((last.close/st.prev-1)*100):null;
  const legend='MA5 '+(st.mas[0][N-1]||0).toFixed(2)+'　MA10 '+(st.mas[1][N-1]||0).toFixed(2)
    +'　MA20 '+(st.mas[2][N-1]||0).toFixed(2)+'　MA60 '+(st.mas[3][N-1]||0).toFixed(2);
  const closesInfo=(st.closes||[]).map(c=>c.date.slice(5)+' '+esc(c.reason)).join('；');
  const boxHtml='<div class="card"><h2>日K · '+name+'（前复权，近'+N+'日）</h2><div class="body">'
    +'<div style="color:#8B949E;font-size:11px;padding-bottom:4px">'+legend
    +(closesInfo?'　<span style="color:#3FB950">平仓：'+closesInfo+'</span>':'')
    +'　收盘 '+last.close.toFixed(2)+(chg!=null?'　<span style="color:'+(chg>=0?'#F85149':'#3FB950')+'">'+(chg>=0?'+':'')+chg.toFixed(2)+'%</span>':'')+'</div>'
    +'<div style="display:flex;gap:6px;align-items:center;padding:6px 0;flex-wrap:wrap">'
    +'<button onclick="kZoom(1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">＋ 放大</button>'
    +'<button onclick="kZoom(-1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">－ 缩小</button>'
    +'<button onclick="kReset()" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">全部</button>'
    +'<button onclick="kPan(-1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">‹ 更早</button>'
    +'<button onclick="kPan(1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">更近 ›</button>'
    +'<span id="kzoom" style="color:#8B949E;font-size:11px"></span>'
    +'<span style="color:#6E7681;font-size:11px">滚轮缩放 · 默认近60根</span></div>'
    +'<div style="position:relative" id="boxK"><svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;display:block"></svg></div>'
    +'<div class="note">红涨绿跌；蓝色=线性回归趋势线(20/60实/虚线)+通道与支撑/压力连线；红色箭头=B3/二波（确=确认，买=买入日），绿色箭头=平仓日；移动鼠标查看 OHLC。</div></div></div>';
  window.kState=st;
  setTimeout(function(){
    const box=document.getElementById('boxK');
    box.addEventListener('wheel',function(e){e.preventDefault();kZoom(e.deltaY>0?-1:1);},{passive:false});
    redraw();
  },0);
  return boxHtml;
}
"""


# ── K线改用 KLineChart v10（专业看盘套件，本地内联、无 CDN）──
# 缺 resources/klinecharts.min.js 时 stock_page_html 自动回退 PAGE_JS_LEGACY（旧 SVG）。
PAGE_JS = """
/* ── 分时(SVG) + K线(KLineChart 专业套件)const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
 ── */
const FMT2=v=>v==null?'-':(v>=1e8?(v/1e8).toFixed(2)+'亿':(v>=1e4?(v/1e4).toFixed(2)+'万':v.toFixed(0)));
const FMTV=v=>v==null?'-':(v>=1e4?(v/1e4).toFixed(2)+'万手':v.toFixed(0)+'手');
function linePts(pairs){return pairs.filter(p=>p[1]!=null).map(p=>p[0]+','+p[1]).join(' ');}
function linReg(vals){
  const n=vals.length; if(n<2)return null;
  let sx=0,sy=0,sxy=0,sxx=0;
  for(let i=0;i<n;i++){sx+=i;sy+=vals[i];sxy+=i*vals[i];sxx+=i*i;}
  const den=n*sxx-sx*sx; if(!den)return null;
  const a=(n*sxy-sx*sy)/den, b=(sy-a*sx)/n;
  let se=0; for(let i=0;i<n;i++){const e=vals[i]-(a*i+b);se+=e*e;}
  return {a:a,b:b,se:Math.sqrt(se/n)};
}

function bindCross(box, svg, W, xOf, yTop, yBot, tipHtml){
  var line=document.createElementNS('http://www.w3.org/2000/svg','line');
  line.setAttribute('stroke','#8B949E'); line.setAttribute('stroke-dasharray','3,3'); line.setAttribute('opacity','0.8');
  svg.appendChild(line);
  var tip=document.createElement('div');
  tip.style.cssText='position:absolute;pointer-events:none;display:none;background:#0D1117;border:1px solid #30363D;border-radius:6px;padding:6px 8px;font-size:11px;color:#E6EDF3;white-space:nowrap;z-index:20;';
  box.appendChild(tip);
  box.addEventListener('mousemove',function(e){
    var r=box.getBoundingClientRect();
    var rx=(e.clientX-r.left)/r.width*W;
    var i=Math.round((rx-xOf(0))/(xOf(1)-xOf(0)));
    i=Math.max(0,Math.min(xOf.n-1,i));
    var cx=xOf(i);
    line.setAttribute('x1',cx); line.setAttribute('x2',cx);
    line.setAttribute('y1',yTop); line.setAttribute('y2',yBot);
    tip.innerHTML=tipHtml(i);
    var px=Math.min(r.width-8,tip.offsetWidth+8);
    tip.style.left=(rx/W*r.width-px/2)+'px';
    tip.style.top='8px'; tip.style.display='block';
  });
  box.addEventListener('mouseleave',function(){ line.setAttribute('opacity','0'); tip.style.display='none'; });
}

async function load(code){
  const box=document.getElementById('charts');
  box.innerHTML='<div class="empty">加载中…</div>';
  let d;
  try{ d=await (await fetch('/stock/'+code+'/data')).json(); }
  catch(e){ box.innerHTML='<div class="empty">数据加载失败</div>'; return; }
  if(document.getElementById('upd'))document.getElementById('upd').textContent=d.updated;
  const prev=d.kline&&d.kline.length>1?d.kline[d.kline.length-2].close:null;
  let html=d.error?('<div class="empty">'+esc(d.error)+'（K线/分时腾讯优先，分时东财兜底）</div>'):'';
  if(d.kline&&d.kline.length) html+=drawK(d.kline,esc(d.name,prev,d.signals,d.closes,d.code);
  if(d.trends&&d.trends.length) html+=drawT(d.trends,esc(d.name,prev);
  box.innerHTML=html||'<div class="empty">暂无数据</div>';
}

function drawT(ts,name,prev){
  const W=860,H=250,VH=64,P={l:62,r:16,t:16,b:26};
  const n=ts.length;
  const x=i=>P.l+(W-P.l-P.r)*i/Math.max(1,n-1); x.n=n;
  let cv=0,ca=0;
  const avg=ts.map(t=>{cv+=t.volume||0;ca+=t.amount||0;return (t.volume&&cv>0)?ca/(cv*100):null;});  // 量单位手→股
  const isNum=v=>typeof v==='number'&&isFinite(v);
  const all=[...ts.map(t=>t.price),...avg.filter(isNum),prev??ts[0].price].filter(isNum);
  const lo=Math.min(...all),hi=Math.max(...all);
  const pad=(hi-lo)*0.12||0.01; const L=lo-pad,H2=hi+pad;
  const y=v=>P.t+(H-P.t-P.b)*(1-(v-L)/(H2-L));
  const up=(ts[n-1].price||0)>=(prev??ts[0].price);
  const mainC=up?'#F85149':'#3FB950';
  const times=['09:30','10:30','11:30','13:00','14:00','15:00'];
  let grid='';
  for(let k=0;k<=4;k++){const yy=P.t+(H-P.t-P.b)*k/4;grid+=`<line x1="${P.l}" y1="${yy}" x2="${W-P.r}" y2="${yy}" stroke="#21262D"/>`;}
  times.forEach(t=>{
    let i=ts.findIndex(p=>p.time>=t); if(i<0)i=n-1;
    grid+=`<text x="${x(i)}" y="${H-8}" fill="#8B949E" font-size="10" text-anchor="middle">${t}</text>
    <line x1="${x(i)}" y1="${P.t}" x2="${x(i)}" y2="${H-VH-4}" stroke="#21262D" stroke-dasharray="2,4"/>`;
  });
  const area='M'+x(0)+','+y(ts[0].price)+' '+ts.map((t,i)=>'L'+x(i)+','+y(t.price)).join(' ')+' L'+x(n-1)+','+(H-VH-2)+' L'+x(0)+','+(H-VH-2)+' Z';
  const priceLine=linePts(ts.map((t,i)=>[x(i),y(t.price)]));
  const avgPts=avg.map((v,i)=>v==null?null:[x(i),y(v)]);
  let avgSeg='',seg=[];
  avgPts.forEach(p=>{if(p){seg.push(p);}else if(seg.length){avgSeg+=linePts(seg)+' ';seg=[];}});
  if(seg.length)avgSeg+=linePts(seg);
  const vmax=Math.max(...ts.map(t=>t.volume||0),1);
  const yv=v=>H-VH+(VH-14)*(1-(v||0)/vmax);
  let vols='';
  ts.forEach((t,i)=>{const c=(t.price||0)>=(prev??ts[0].price)?'#F85149':'#3FB950';
    const bw=Math.max(1,(W-P.l-P.r)/n*0.55);
    vols+=`<rect x="${x(i)-bw/2}" y="${yv(t.volume)}" width="${bw}" height="${H-VH-2-yv(t.volume)}" fill="${c}" opacity="0.55"/>`;});
  const pxTicks=[0,1,2,3,4].map(k=>L+(H2-L)*k/4);
  const axis=pxTicks.map(v=>`<text x="${W-P.r-2}" y="${y(v)+3}" fill="#8B949E" font-size="10" text-anchor="end">${v.toFixed(2)}</text>`).join('');
  const boxHtml='<div class="card"><h2>分时 · '+name+'（今）</h2><div class="body">'
    +'<div style="position:relative" id="boxT"><svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;display:block">'
    +grid+axis
    +'<line x1="'+P.l+'" y1="'+(prev!=null?y(prev):0)+'" x2="'+(W-P.r)+'" y2="'+(prev!=null?y(prev):0)+'" stroke="#8B949E" stroke-dasharray="4,4" opacity="0.7"/>'
    +'<path d="'+area+'" fill="'+mainC+'" opacity="0.08"/>'
    +'<polyline points="'+priceLine+'" fill="none" stroke="'+mainC+'" stroke-width="1.8"/>'
    +'<polyline points="'+avgSeg.trim()+'" fill="none" stroke="#FACC15" stroke-width="1.4"/>'
    +vols
    +'<text x="'+(W-P.r-2)+'" y="'+P.t+14+'" fill="'+mainC+'" font-size="11" text-anchor="end">现价 '+(ts[n-1].price||'').toFixed(2)+'</text>'
    +'<text x="'+(P.l+4)+'" y="'+P.t+14+'" fill="#FACC15" font-size="11">均价 '+(avg[n-1]!=null?avg[n-1].toFixed(2):'-')+'</text>'
    +'</svg></div><div class="note">红涨绿跌；黄色=均价线，灰虚线=昨收；下方为成交量（手）。</div></div></div>';
  setTimeout(function(){
    var box=document.getElementById('boxT'); var svg=box.querySelector('svg');
    bindCross(box,svg,W,x,P.t,H-VH-2,function(i){
      const t=ts[i],a=avg[i],chg=prev?(t.price/prev-1)*100:0;
      return t.time+'　价 '+t.price.toFixed(2)+'　均价 '+(a!=null?a.toFixed(2):'-')+'　涨跌 '+(chg>=0?'+':'')+chg.toFixed(2)+'%　量 '+FMTV(t.volume);
    });
  },0);
  return boxHtml;
}

/* ═══ K线：KLineChart v10（本地内联套件；Quant Dark 配色） ═══ */
const KLC_STYLES={
  grid:{horizontal:{color:'#21262D'},vertical:{color:'#21262D'}},
  candle:{
    bar:{compareRule:'current_open',upColor:'#F85149',downColor:'#3FB950',noChangeColor:'#8B949E'},
    tooltip:{showRule:'follow_cross',showType:'rect',
      rect:{color:'rgba(13,17,23,0.92)',borderColor:'#30363D',borderSize:1,borderRadius:4},
      title:{color:'#E6EDF3',size:12},legend:{color:'#E6EDF3',size:11,defaultValue:'-'}}
  },
  indicator:{
    tooltip:{showRule:'follow_cross',showType:'rect',
      rect:{color:'rgba(13,17,23,0.92)',borderColor:'#30363D',borderSize:1,borderRadius:4},
      title:{color:'#E6EDF3',size:12},legend:{color:'#E6EDF3',size:11,defaultValue:'-'}}
  },
  xAxis:{axisLine:{color:'#30363D'},tickLine:{color:'#30363D'},tickText:{color:'#8B949E',size:10}},
  yAxis:{axisLine:{color:'#30363D'},tickLine:{color:'#30363D'},tickText:{color:'#8B949E',size:10}},
  separator:{color:'#21262D'},
  crosshair:{
    horizontal:{line:{color:'#8B949E',style:'dashed',dashedValue:[3,3]},
      text:{color:'#E6EDF3',borderColor:'#373A40',backgroundColor:'#373A40',borderRadius:3,size:10}},
    vertical:{line:{color:'#8B949E',style:'dashed',dashedValue:[3,3]},
      text:{color:'#E6EDF3',borderColor:'#373A40',backgroundColor:'#373A40',borderRadius:3,size:10}}
  },
  overlay:{
    line:{color:'#58A6FF',size:1},
    text:{color:'#E6EDF3',size:11,family:'Consolas,Menlo,monospace',backgroundColor:'rgba(13,17,23,0.85)',borderColor:'#30363D',borderSize:1,borderRadius:3}
  }
};

function tsIdx(bars,date){
  const t=Date.parse(date+'T00:00:00+08:00');
  for(let i=bars.length-1;i>=0;i--){if(bars[i].timestamp===t)return i;}
  return -1;
}
function kRender(st){
  if(!st.chart)return;
  const size=st.chart.getSize(); if(!size||!size.width)return;
  if(!st.chart.getDataList().length){
    st._r=(st._r||0)+1; if(st._r<15){setTimeout(function(){kRender(st);},120);}
    return;
  }
  st.chart.setBarSpace(size.width/st.cnt);
  st.chart.scrollToDataIndex(Math.max(0,st.end-1),0);
  const lab=document.getElementById('kzoom');
  if(lab)lab.textContent='近 '+st.cnt+' 日'+(st.end<st.n?'（第'+(st.end-st.cnt+1)+'-'+st.end+'根）':'');
}
function kZoom(dir){
  const st=window.kState; if(!st||!st.chart)return;
  st.cnt=Math.max(st.min,Math.min(st.max,dir>0?Math.round(st.cnt/1.25):Math.round(st.cnt*1.25)));
  if(st.end>st.n)st.end=st.n;
  kRender(st);
}
function kPan(dir){
  const st=window.kState; if(!st||!st.chart)return;
  const step=Math.max(1,Math.round(st.cnt*0.25));
  st.end=Math.max(st.cnt,Math.min(st.n,st.end+dir*step));
  kRender(st);
}
function kReset(){
  const st=window.kState; if(!st||!st.chart)return;
  const size=st.chart.getSize(); const w=size&&size.width?size.width:800;
  st.cnt=Math.min(st.n,Math.max(20,Math.floor(w/3)));
  st.end=st.n; kRender(st);
}
function addReg(chart,bars,win,solid){
  const n=bars.length; if(n<win+2)return;
  const vals=[]; for(let i=n-win;i<n;i++)vals.push(bars[i].close);
  const r=linReg(vals); if(!r)return;
  const pt=function(i,off){return {timestamp:bars[n-win+i].timestamp,value:r.a*i+r.b+off};};
  const mk=function(p1,p2,color,style,size){
    chart.createOverlay({name:'segment',lock:true,needDefaultPointFigure:false,
      needDefaultXAxisFigure:false,needDefaultYAxisFigure:false,
      points:[p1,p2],styles:{line:{color:color,size:size,style:style}}});
  };
  mk(pt(0,0),pt(win-1,0),'#58A6FF',solid?'solid':'dashed',solid?1.8:1.1);
  mk(pt(0,r.se),pt(win-1,r.se),'rgba(88,166,255,0.35)','dashed',1);
  mk(pt(0,-r.se),pt(win-1,-r.se),'rgba(88,166,255,0.35)','dashed',1);
}
function addSwing(chart,bars,field){
  const n=bars.length; const start=Math.max(1,n-61); const pts=[];
  for(let i=start;i<n-1;i++){
    const v=bars[i][field];
    const ok=field==='low'?(v<bars[i-1][field]&&v<bars[i+1][field]):(v>bars[i-1][field]&&v>bars[i+1][field]);
    if(ok)pts.push({i:i,v:v});
  }
  if(pts.length<2)return;
  const p1=pts[pts.length-1],p2=pts[pts.length-2];
  if(Math.abs(p1.i-p2.i)<3)return;
  chart.createOverlay({name:'segment',lock:true,needDefaultPointFigure:false,
    needDefaultXAxisFigure:false,needDefaultYAxisFigure:false,
    points:[{timestamp:bars[p2.i].timestamp,value:p2.v},{timestamp:bars[p1.i].timestamp,value:p1.v}],
    styles:{line:{color:'#58A6FF',size:1.3,style:'solid'}}});
}
function addAnns(chart,bars,signals,closes){
  const ann=function(i,text,color){
    if(i<0||i>=bars.length)return;
    chart.createOverlay({name:'simpleAnnotation',lock:true,needDefaultPointFigure:false,
      needDefaultXAxisFigure:false,needDefaultYAxisFigure:false,
      points:[{timestamp:bars[i].timestamp,value:bars[i].high}],extendData:text,
      styles:{line:{color:color,size:1.2},polygon:{color:color},
        text:{color:color,size:11,weight:'bold'}}});
  };
  (signals||[]).forEach(function(sig){
    [['t0',sig.kind||'B3'],['t1','确'],['t2','买']].forEach(function(pair){
      const d=sig[pair[0]]; if(!d)return;
      ann(tsIdx(bars,d),pair[1],'#F85149');
    });
  });
  (closes||[]).forEach(function(c){ ann(tsIdx(bars,c.date),'平','#3FB950'); });
}
function initKChart(id,ks,signals,closes,code){
  try{
    const chart=klinecharts.init(id,{
      locale:'zh-CN',timezone:'Asia/Shanghai',zoomAnchor:'last_bar',
      layout:{barSpaceLimit:{min:2,max:100},pane:{minHeight:70}},
      styles:KLC_STYLES
    });
    if(!chart)return;
    const st=window.kState; if(!st)return; st.chart=chart;
    const bars=ks.map(function(k){
      return {timestamp:Date.parse(k.date+'T00:00:00+08:00'),open:k.open,high:k.high,
        low:k.low,close:k.close,volume:k.volume};
    });
    chart.setSymbol({ticker:code||'K',pricePrecision:2,volumePrecision:0});
    chart.setPeriod({span:1,type:'day'});
    chart.setDataLoader({getBars:function(p){
      if(p.type==='init'){p.callback(bars,{forward:false,backward:false});}
      else{p.callback([],{forward:false,backward:false});}
    }});
    chart.subscribeAction('onVisibleRangeChange',function(data){
      const s=window.kState; if(!s)return;
      if(data&&typeof data.realFrom==='number'&&typeof data.realTo==='number'){
        s.cnt=Math.max(1,(data.realTo-data.realFrom+1)); s.end=data.realTo+1;
      } else if(data&&typeof data.from==='number'&&typeof data.to==='number'){
        s.cnt=Math.max(1,(data.to-data.from+1)); s.end=data.to+1;
      }
      const lab=document.getElementById('kzoom');
      if(lab)lab.textContent='近 '+s.cnt+' 日'+(s.end<s.n?'（第'+(s.end-s.cnt+1)+'-'+s.end+'根）':'');
    });
    setTimeout(function(){
      chart.createIndicator({name:'MA',paneId:'candle_pane',calcParams:[5,10,20,60],
        styles:{lines:[{color:'#FACC15',size:1},{color:'#58A6FF',size:1},{color:'#BC8CFF',size:1},{color:'#3FB950',size:1}]}},false);
      chart.createIndicator('VOL',false);
      addReg(chart,bars,20,true);
      addReg(chart,bars,60,false);
      addSwing(chart,bars,'low');
      addSwing(chart,bars,'high');
      addAnns(chart,bars,signals,closes);
      kRender(st);
    },0);
  }catch(e){
    const box=document.getElementById(id);
    if(box)box.innerHTML='<div class="empty" style="height:100%">K线组件初始化失败：'+(e&&e.message?e.message:e)+'</div>';
  }
}
function drawK(ks,name,prev,signals,closes,code){
  const N=ks.length;
  const st={chart:null,ks:ks,n:N,min:Math.min(20,N),max:Math.max(20,N),cnt:Math.min(60,N),end:N};
  const maArr=p=>{const a=[];for(let i=0;i<N;i++){if(i<p-1){a.push(null);continue;}
    let s2=0;for(let j=0;j<p;j++)s2+=ks[i-j].close;a.push(s2/p);}return a;};
  const mas=[maArr(5),maArr(10),maArr(20),maArr(60)];
  const last=ks[N-1],chg=prev?((last.close/prev-1)*100):null;
  const legend='MA5 '+(mas[0][N-1]||0).toFixed(2)+'　MA10 '+(mas[1][N-1]||0).toFixed(2)
    +'　MA20 '+(mas[2][N-1]||0).toFixed(2)+'　MA60 '+(mas[3][N-1]||0).toFixed(2);
  const closesInfo=(closes||[]).map(c=>c.date.slice(5)+' '+esc(c.reason)).join('；');
  const boxHtml='<div class="card"><h2>日K · '+name+'（前复权，近'+N+'日）</h2><div class="body">'
    +'<div style="color:#8B949E;font-size:11px;padding-bottom:4px">'+legend
    +(closesInfo?'　<span style="color:#3FB950">平仓：'+closesInfo+'</span>':'')
    +'　收盘 '+last.close.toFixed(2)+(chg!=null?'　<span style="color:'+(chg>=0?'#F85149':'#3FB950')+'">'+(chg>=0?'+':'')+chg.toFixed(2)+'%</span>':'')+'</div>'
    +'<div style="display:flex;gap:6px;align-items:center;padding:6px 0;flex-wrap:wrap">'
    +'<button onclick="kZoom(1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">＋ 放大</button>'
    +'<button onclick="kZoom(-1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">－ 缩小</button>'
    +'<button onclick="kReset()" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">全部</button>'
    +'<button onclick="kPan(-1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">‹ 更早</button>'
    +'<button onclick="kPan(1)" style="background:#21262D;color:#E6EDF3;border:1px solid #30363D;border-radius:6px;padding:3px 10px;cursor:pointer">更近 ›</button>'
    +'<span id="kzoom" style="color:#8B949E;font-size:11px"></span>'
    +'<span style="color:#6E7681;font-size:11px">滚轮/触摸缩放平移 · 默认近60根</span></div>'
    +'<div style="position:relative;width:100%;height:min(58vh,430px)" id="boxK"></div>'
    +'<div class="note">红涨绿跌；黄=MA5 蓝=MA10 紫=MA20 绿=MA60；蓝线=线性回归(20实/60虚)+通道与支撑/压力连线；红色箭头=B3/二波（确=确认，买=买入日），绿色箭头=平仓日；移动鼠标查看 OHLC。</div></div></div>';
  window.kState=st;
  setTimeout(function(){initKChart('boxK',ks,signals||[],closes||[],code);},0);
  return boxHtml;
}
"""


STOCK_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0D1117">
<meta http-equiv="refresh" content="60">
<title>{name} · 分时/K线</title>
<style>{css}</style></head><body><div class="wrap">
<header><h1>{name}（{code}）</h1>
<div class="sub"><a href="/live.html" style="color:#8B949E">← 返回实时盯盘</a>
｜ 每 60 秒自动刷新 ｜ 更新时间 <span class="date" id="upd">-</span></div></header>
<div id="charts"><div class="empty">加载中…</div></div>
<footer>数据源：K线/分时=腾讯优先（分时东财兜底）；仅供研究学习，不构成投资建议。</footer>
</div>
<div id="refreshBtn" onclick="hardRefresh()"
style="position:fixed;right:18px;bottom:18px;z-index:99;background:#161B22;
border:1px solid #58A6FF;color:#E6EDF3;border-radius:999px;padding:8px 18px;
font-size:13px;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.4)">🔄 刷新</div>
<script>{klc}</script>
<script>{js}
function hardRefresh(){{
  var b=document.getElementById('refreshBtn');
  b.textContent='刷新中…'; b.style.opacity=0.55; b.style.pointerEvents='none';
  fetch('/stock/'+CODE+'/data?force=1').catch(function(){{}}).then(function(){{
    setTimeout(function(){{ location.reload(); }}, 300);
  }});
}}
const CODE='{code}';
load(CODE);
</script></body></html>"""


def stock_page_html(code: str, name: str) -> str:
    klc = _klc_js()
    page_js = PAGE_JS if klc else PAGE_JS_LEGACY
    # 模板里已有 <script>{klc}</script> 外壳，这里只传库的原始 JS 内容，
    # 不能自带 <script> 标签（会双包导致第一个 </script> 提前截断脚本）。
    import html as _html
    # XSS 防护（2026-08-15 审计 M2）：name 来自外部数据（stock_list.csv），
    # 进 <title>/<h1>/JS const，必须完整转义（含引号）。
    return STOCK_PAGE.format(css=CSS, js=page_js, klc=klc,
                             code=code, name=_html.escape(name, quote=True))


KLC_FILE = Path(__file__).resolve().parent / "resources" / "klinecharts.min.js"
_KLC_CACHE: dict = {"js": None}


def _klc_js() -> str:
    """KLineChart 套件源码（内联进详情页；资源缺失时返回空串走旧版 SVG）。"""
    if _KLC_CACHE["js"] is None:
        try:
            _KLC_CACHE["js"] = KLC_FILE.read_text(encoding="utf-8")
        except OSError:
            _KLC_CACHE["js"] = ""
    return _KLC_CACHE["js"]


def get_stock_data(code: str, force: bool = False) -> dict:
    code = "".join(ch for ch in code if ch.isdigit())[:6]
    _touch_recent(code)
    now = time.time()
    with CACHE_LOCK:
        hit = CACHE.get(code)
        if hit and not force and now - hit[0] < CACHE_TTL:
            data = hit[1]
            # 残缺缓存（只有K线缺分时）→ 完整重建，避免分时图缺失
            if data.get("kline") and not data.get("trends"):
                hit = None
            else:
                return data
    data = build_stock_data(code)
    with CACHE_LOCK:
        CACHE[code] = (now, data)
    return data


def refresh_page(page: str) -> dict:
    """强制刷新：web=全部网页 / review=当日复盘 / live=实时盯盘。"""
    t0 = time.time()
    try:
        if page == "web":
            from mainrise import web_dashboard
            out = web_dashboard.update_web_dashboard()
            msg = f"已重新生成全部网页: {out}"
        elif page == "review":
            from mainrise import review
            out = review.update_review()
            msg = f"已重新生成当日复盘: {out}"
        elif page == "live":
            from mainrise import monitor
            from mainrise import paths
            live = monitor.run_one_cycle(paths.web_dir(), {"alerts": []})
            msg = f"已刷新盯盘 {len(live.get('stocks') or [])} 只"
        else:
            return {"ok": False, "page": page, "message": f"未知页面: {page}"}
        return {"ok": True, "page": page, "message": msg,
                "secs": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "page": page,
                "message": f"{type(e).__name__}: {e}",
                "secs": round(time.time() - t0, 1)}


class StockHandler(BaseHTTPRequestHandler):
    def _same_origin(self) -> bool:
        """CSRF 防护（2026-08-15 审计 M3）：校验 Origin/Referer 同源。
        Basic Auth 下浏览器自动带凭据，跨站请求可静默触发副作用端点，
        必须拒绝非本站来源。无 Origin/Referer（如 curl/内网）放行。"""
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if not v:
                continue
            try:
                from urllib.parse import urlparse
                host = urlparse(v).hostname or ""
                if host in ("129.28.95.59", "127.0.0.1", "localhost"):
                    return True
                return False
            except Exception:  # noqa: BLE001
                return False
        return True

    def do_GET(self):  # noqa: N802
        path, _, qs = self.path.partition("?")
        params = dict(urllib.parse.parse_qsl(qs))
        try:
            if path.startswith("/refresh"):
                # 副作用端点：GET 一律拒绝（405），只能 POST（见 do_POST）
                self.send_error(405)
            elif path.startswith("/stock/") and path.endswith("/data"):
                code = path[len("/stock/"):-len("/data")]
                self._send_json(get_stock_data(code, force=params.get("force") == "1"))
            elif path.startswith("/stock/"):
                code = path[len("/stock/"):].strip("/")
                code = "".join(ch for ch in code if ch.isdigit())[:6]
                if code:
                    from mainrise.signals import load_names
                    name = load_names().get(code, "") or code
                    self._send_html(stock_page_html(code, name))
                else:
                    self.send_error(404)
            else:
                self.send_error(404)
        except Exception:  # noqa: BLE001
            self.send_error(500)

    def do_POST(self):  # noqa: N802  (刷新按钮走 POST)
        path, _, qs = self.path.partition("?")
        params = dict(urllib.parse.parse_qsl(qs))
        try:
            if path.startswith("/refresh"):
                if not self._same_origin():
                    self.send_error(403)
                    return
                self._send_json(refresh_page(params.get("page", "")))
            else:
                self.send_error(404)
        except Exception:  # noqa: BLE001
            self.send_error(500)

    def _send_json(self, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_text: str) -> None:
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102
        pass


def make_server(port: int = STOCK_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), StockHandler)


def start_server(port: int = STOCK_PORT) -> ThreadingHTTPServer:
    """monitor 进程内启动（daemon 线程）。"""
    srv = make_server(port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    start_server()
    print(f"stock page server on 127.0.0.1:{STOCK_PORT}")
    threading.Event().wait()
