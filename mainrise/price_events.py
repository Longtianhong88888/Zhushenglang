"""产业链涨价事件追踪：光纤/覆铜板/MLCC/HBM/DDR4/DRAM 等卡点产品涨价新闻。

切入点（用户 2026-08-14）：除大宗期货外，科技产业链涨价（存储/覆铜板/光纤/
MLCC/HBM/DDR4 等）是缺货/供需紧张的直接证据——无商品期货，用**涨价新闻事件**
追踪。搜索维度：**产品名+涨价**（按企业名搜 0 命中——新闻标题很少同含公司名+
涨价词；产品词才有命中率，覆盖 97 只卡点企业主营产品线）。

数据：东财新闻搜索（search-api-web，免费）。每产品搜"产品名 涨价"（AND），
标题过滤：含产品词 且 含涨价意图词（涨价/提价/上调/紧缺/短缺/缺货/供不应求/
景气/量价/扩产/满产）。近 90 天事件计数 + 30 天有无新事件。

标签判定：
  持续涨价  = 90天事件 ≥3 且 30天有新事件
  量价齐升  = 持续涨价 且 对应主题行业指数 20 日涨幅 > 0（themeindex 缓存）
  价格见顶  = 90天事件 ≥3 但 30天无新事件（涨价新闻停滞）
  启动      = 90天事件 1-2 条
  平静      = 0 条

主题映射（ITEMS 内联）：DRAM/DDR4/DDR5/HBM/NAND/闪存/存储芯片→存储；
光纤/光模块/覆铜板/铜箔/电子布/PCB/液冷/散热/高速铜缆→AI硬件；
晶圆/硅片/光刻胶/封测/MCU/模拟芯片/功率半导体/IGBT/碳化硅/MLCC/被动元件→半导体；
稀土/钨/锂/钴→有色；减速器/伺服/丝杠→机器人；卫星/火箭/相控阵→商业航天；
激光雷达/域控→自动驾驶；创新药/原料药/CXO→创新药。

输出：报告 + price_events.json + 门户产业景气度卡各主题行涨价标签。

用法:
    python3 -m mainrise.price_events
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

import pandas as pd
import requests

from mainrise import paths

SEARCH = "https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param="
HDR = {"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com"}
DAYS = 90
INTENT = ("涨价|提价|上调|上涨|飙升|暴涨|涨幅|上行|紧缺|短缺|缺货|供不应求|"
          "景气|量价|扩产|满产|库存下降|供需")

# 产品关键词 → 主题（覆盖 97 只卡点企业主营产品线；搜"产品+涨价"）
ITEMS = [
    # 存储：内存条/颗粒
    ("DRAM", "存储"), ("DDR4", "存储"), ("DDR5", "存储"), ("HBM", "存储"),
    ("NAND", "存储"), ("闪存", "存储"), ("存储芯片", "存储"), ("SSD", "存储"),
    # AI硬件：光通信/PCB 产业链
    ("光纤", "AI硬件"), ("光模块", "AI硬件"), ("CPO", "AI硬件"),
    ("覆铜板", "AI硬件"), ("铜箔", "AI硬件"), ("电子布", "AI硬件"),
    ("PCB", "AI硬件"), ("液冷", "AI硬件"), ("散热", "AI硬件"),
    ("高速铜缆", "AI硬件"), ("玻璃基板", "AI硬件"), ("金刚石", "AI硬件"),
    # 半导体：材料/设备/元件
    ("晶圆", "半导体"), ("硅片", "半导体"), ("光刻胶", "半导体"),
    ("靶材", "半导体"), ("半导体设备", "半导体"), ("封测", "半导体"),
    ("MCU", "半导体"), ("模拟芯片", "半导体"), ("功率半导体", "半导体"),
    ("IGBT", "半导体"), ("碳化硅", "半导体"), ("MLCC", "半导体"),
    ("被动元件", "半导体"),
    # 有色
    ("稀土", "有色"), ("钨", "有色"), ("锂", "有色"), ("钴", "有色"),
    # 机器人/商业航天/自动驾驶/创新药
    ("减速器", "机器人"), ("伺服", "机器人"), ("丝杠", "机器人"),
    ("卫星", "商业航天"), ("火箭", "商业航天"), ("相控阵", "商业航天"),
    ("激光雷达", "自动驾驶"), ("域控", "自动驾驶"),
    ("创新药", "创新药"), ("原料药", "创新药"), ("CXO", "创新药"),
]


def _search_em(kw: str, pages: int = 3) -> list:
    """东财搜索前 pages 页（每页50条，相关度排序）合并去重。

    坑：sort=time 返回时间倒序泛新闻（前 50 条仅 2-8 条含关键词，
    英文词 DRAM/HBM 等几乎为 0）；sort=default 才是关键词相关度排序
    （DRAM 命中率 1/150 → 22/150）。

    服务器 IP 坑：东财对部分机房 IP 风控搜索接口——返回 200 但
    result 只有 passportWeb（广告位）没有 cmsArticleWebOld，此时
    返回 None 由 _search 回退腾讯新闻。
    """
    seen, out = set(), []
    for page in range(1, pages + 1):
        param = {"uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
                 "client": "web", "clientType": "web", "clientVersion": "curr",
                 "param": {"cmsArticleWebOld": {"searchScope": "default",
                                                "sort": "default",
                                                "pageIndex": page,
                                                "pageSize": 50,
                                                "preTag": "<em>",
                                                "postTag": "</em>"}}}
        url = SEARCH + urllib.parse.quote(json.dumps(param, ensure_ascii=False))
        try:
            r = requests.get(url, headers=HDR, timeout=15)
            txt = r.text
            s, e = txt.find("("), txt.rfind(")")
            if s == -1 or e == -1:
                return None  # 响应异常（非 jsonp）→ 回退腾讯新闻
            d = json.loads(txt[s + 1:e])
            res = d.get("result") or {}
            if "cmsArticleWebOld" not in res:
                return None  # 被风控降级（passportWeb）→ 回退腾讯新闻
            items = res.get("cmsArticleWebOld") or []
        except Exception:  # noqa: BLE001
            return None  # 网络异常 → 回退腾讯新闻（M24：不再静默记 0 条）
        for it in items:
            t = re.sub(r"<[^>]+>", "", it.get("title", ""))
            if t and t not in seen:
                seen.add(t)
                out.append(it)
        if len(items) < 50:
            break
        time.sleep(0.25)
    return out


SOGOU = "https://i.news.qq.com/gw/pc_search/result"
_SG_HDR = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36"),
           "Referer": "https://new.qq.com/search?query=%E6%B6%A8%E4%BB%B7"}


def _search_sogou(kw: str, pages: int = 1) -> list | None:
    """腾讯新闻搜索（服务器兜底源：东财对机房 IP 风控搜索接口时）。

    接口 i.news.qq.com/gw/pc_search/result 返回结构化 JSON：
    secList[].newsList[] 每条含 title / time(YYYY-MM-DD HH:MM:SS) / url。
    实测服务器（129.28.95.59）可用、返回精准涨价新闻（搜狗被反爬
    antispider 拦截，故不用搜狗）。

    限流应对（用户 2026-08-14 指示）：高频请求触发 ret=400 需验证码，
    故**每 30 秒只搜 1 次**（间隔由 _search 回退分支控制），每产品 1 页
    （10 条够标签判定）。失败返回 None。
    """
    try:
        out = []
        for page in range(pages):
            params = {"page": str(page), "query": f"{kw} 涨价",
                      "ticket": "", "randstr": "", "is_pc": 1,
                      "hippy_custom_version": 25, "search_type": "all",
                      "search_count_limit": 10,
                      "appver": "15.5_qqnews_7.1.80", "suid": ""}
            r = requests.get(SOGOU, headers=_SG_HDR, params=params,
                             timeout=15)
            d = r.json()
            if d.get("ret") != 0:
                break
            news = []
            for sec in (d.get("secList") or []):
                news += sec.get("newsList") or []
            if not news:
                break
            for n in news:
                title = (n.get("title") or "").strip()
                t = (n.get("time") or "")[:10]
                if title and not any(x["title"] == title for x in out):
                    out.append({"title": title, "date": t})
            if not d.get("hasMore"):
                break
        return out or None
    except Exception:  # noqa: BLE001
        return None


def _search(kw: str, pages: int = 3) -> list:
    """东财优先，被风控降级/网络异常时回退腾讯新闻。

    回退限流：腾讯高频请求会触发验证码（ret=400），故每次腾讯搜索前
    固定等待 30 秒（每 30 秒搜 1 次，用户 2026-08-14 指示）。
    """
    try:
        res = _search_em(kw, pages)
    except Exception:  # noqa: BLE001  网络异常 → 与风控同等处理（回退）
        res = None
    if res is None:
        print(f"    东财搜索不可用（风控/网络异常），30 秒后回退腾讯新闻…")
        time.sleep(30)
        res = _search_sogou(kw) or []
    return res


def _topic_ret20(theme: str) -> float | None:
    """主题行业指数 20 日涨幅（cycle_state 读缓存；无则 None）。"""
    try:
        from mainrise import cycle_state
        idx = cycle_state._theme_series()
        if theme in idx.columns and len(idx) > 20:
            s = idx[theme]
            return float(s.iloc[-1] / s.iloc[-20] - 1) * 100
    except Exception:  # noqa: BLE001
        pass
    return None


def _filter_events(res: list, kw: str, cutoff: str,
                   cutoff30: str) -> list:
    """标题含产品词 + 涨价意图词 → [(日期, 标题前50)]。

    日期为空（腾讯未带日期字段）视为最新保留；有日期才按 90 天过滤。
    """
    events = []
    for it in res:
        t = re.sub(r"<[^>]+>", "", it.get("title", ""))
        d = (it.get("date") or "")[:10]
        if d and d < cutoff:
            continue
        if kw in t and re.search(INTENT, t):
            events.append((d, t[:50]))
    return events


def _label_for(t: dict, ret20: float | None) -> str:
    """主题涨价标签：量价齐升/持续涨价/价格见顶/启动/平静。"""
    if t["n90"] >= 3 and t["n30"] >= 1:
        label = "持续涨价"
    elif t["n90"] >= 3:
        label = "价格见顶"
    elif t["n90"] >= 1:
        label = "启动"
    else:
        label = "平静"
    if label == "持续涨价" and ret20 is not None and ret20 > 0:
        label = "量价齐升"
    return label


def run() -> str:
    t0 = time.time()
    today = pd.Timestamp.now()
    cutoff = (today - pd.DateOffset(days=DAYS)).strftime("%Y-%m-%d")
    cutoff30 = (today - pd.DateOffset(days=30)).strftime("%Y-%m-%d")

    print("搜索产业链涨价新闻（东财资讯，按产品名+涨价）...")
    themes = {}
    rows = []
    for name, theme in ITEMS:
        kw = name
        try:
            res = _search(f"{kw} 涨价")
            events = _filter_events(res, kw, cutoff, cutoff30)
            n90 = len(events)
            n30 = sum(1 for d, _ in events if not d or d >= cutoff30)
            last = max((d for d, _ in events), default="")
            rows.append({"name": name, "kw": kw, "theme": theme,
                         "n90": n90, "n30": n30, "last": last,
                         "events": events})
            print(f"  {name}: {n90} 条/90天（30天内 {n30}）")
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: 失败 {e}")
        time.sleep(0.3)

    # 主题聚合 + 标签
    theme_map = {}
    for r in rows:
        t = theme_map.setdefault(r["theme"], {"n90": 0, "n30": 0, "last": "",
                                              "items": []})
        t["n90"] += r["n90"]
        t["n30"] += r["n30"]
        t["items"].append(f"{r['name']}{r['n90']}")
        if r["last"] > t["last"]:
            t["last"] = r["last"]
    for theme, t in theme_map.items():
        r20 = _topic_ret20(theme)
        t["ret20"] = round(r20, 1) if r20 is not None else None
        t["label"] = _label_for(t, r20)
        t["detail"] = "、".join(t["items"])

    L: list = []
    dstr = pd.Timestamp.now().strftime("%Y-%m-%d")
    L.append(f"# 产业链涨价事件追踪（{dstr}）")
    L.append("")
    L.append(f"> 东财资讯近 {DAYS} 天涨价新闻（标题含品种词+涨价意图词）；"
             "标签：持续涨价（90天≥3且30天有新）/价格见顶（90天≥3但30天无）/"
             "量价齐升（持续涨价+主题指数20日>0）/启动/平静。")
    L.append("")

    L.append("## 一、分产品事件")
    L.append("")
    L.append("| 产品 | 主题 | 90天 | 30天 | 最近事件 | 样例标题 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in sorted(rows, key=lambda x: -x["n90"]):
        sample = r["events"][-1][1] if r["events"] else "—"
        L.append(f"| {r['name']} | {r['theme']} | {r['n90']} | {r['n30']} | "
                 f"{r['last']} | {sample} |")
    L.append("")

    L.append("## 二、主题涨价标签")
    L.append("")
    L.append("| 主题 | 事件数 | 30天 | 20日指数 | 标签 |")
    L.append("| --- | --- | --- | --- | --- |")
    for theme in ("存储", "AI硬件", "半导体"):
        t = theme_map.get(theme)
        if not t:
            continue
        r20 = f"{t['ret20']:+.0f}%" if t["ret20"] is not None else "—"
        L.append(f"| {theme} | {t['n90']}（{t['detail']}） | {t['n30']} | "
                 f"{r20} | **{t['label']}** |")
    L.append("")
    L.append("> 研究用途，不构成投资建议。")
    L.append("")

    paths.ensure_dirs()
    md_path = paths.report_dir() / f"产业链涨价追踪_{dstr}.md"
    md_path.write_text("\n".join(L), encoding="utf-8")
    j = {"date": today.strftime("%Y-%m-%d"), "themes": {
        th: {"label": t["label"], "n90": t["n90"], "n30": t["n30"],
             "detail": t["detail"], "ret20": t["ret20"]}
        for th, t in theme_map.items()}}
    (paths.state_dir() / "price_events.json").write_text(
        json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成（{time.time()-t0:.0f}s）：{md_path}")
    return str(md_path)


if __name__ == "__main__":
    run()
