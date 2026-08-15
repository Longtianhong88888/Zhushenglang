"""动态热主题（全A行业指数版·同花顺 API）：行业指数趋势判定（无前视）。

方向（用户 2026-08-14 三次指正）：
  ① 不看模型池（97 只不全面）→ 看全 A 行业指数；
  ② 不看太细分的行业 → 同花顺粗粒度行业指数（320 个行业中的粗粒度选择，
     每个主题 1~3 个，如 半导体/通信设备/计算机设备/光学光电子/专用设备/
     航天装备/军工装备/汽车整车/汽车零部件/化学制药/生物制品/工业金属/小金属）；
  ③ 用 API 接口 → 同花顺 hithink REST（fuyao.aicubes.cn，统一 Key）。

判定：主题映射行业指数（归一化后等权均值）收盘 > MA{short} 且 MA{short} > MA{long}
即"行业指数上升趋势"= 热；可选 slope（MA短均线上行）/ min_days（趋势连续天数）。

Key 读取：环境变量 HITHINK_FINANCE_API_KEY → settings.json api_key →
         ~/.hithink_key（均不进 git）。

缓存：output/state/ths_kline/{thscode}.csv（K线 TTL 1 天）、
      output/state/ths_industry_list.json（行业清单 TTL 7 天）；
      拉取失败回退旧缓存，无缓存则该主题当日无数据（不误判热）。

用法:
    python3 -m mainrise.themeindex            # 刷新/读取缓存并打印当日热主题
    python3 -m mainrise.themeindex --hot      # 只打印当日热主题
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from mainrise import bigtrend, paths

THEME_ORDER = [t for t in bigtrend.THEMES if t != "其他"]

# 8 主题 → 同花顺行业指数（粗粒度，1~3 个；多个取归一化等权均值）
THEME_INDEXES = {
    "半导体": [("881121.TI", "半导体")],
    "存储": [("881121.TI", "半导体")],                 # 无独立"存储"行业 → 半导体
    "AI硬件": [("881129.TI", "通信设备"), ("881130.TI", "计算机设备"),
               ("881122.TI", "光学光电子")],
    "机器人": [("884218.TI", "机器人"), ("881118.TI", "专用设备")],
    "商业航天": [("884180.TI", "航天装备"), ("881166.TI", "军工装备")],
    "自动驾驶": [("881125.TI", "汽车整车"), ("881126.TI", "汽车零部件")],
    "创新药": [("881140.TI", "化学制药"), ("881142.TI", "生物制品")],
    "有色": [("881168.TI", "工业金属"), ("881170.TI", "小金属")],
}

API = "https://fuyao.aicubes.cn"
CN_TZ = timezone(timedelta(hours=8))
KLINE_TTL = 86400          # 板块K线缓存 1 天
LIST_TTL = 7 * 86400       # 行业清单缓存 7 天


def get_key() -> str:
    """同花顺统一 Key：环境变量 → settings.json api_key → ~/.hithink_key。"""
    env = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
    if env:
        return env
    for sf in (Path(paths.home()) / "settings.json",
               Path.home() / ".mainrise" / "settings.json"):
        try:
            if sf.exists():
                cfg = json.loads(sf.read_text(encoding="utf-8"))
                k = str(cfg.get("api_key") or "").strip()
                if k:
                    return k
        except Exception:  # noqa: BLE001
            pass
    for p in (Path.home() / ".hithink_key",
              Path(paths.home()) / ".hithink_key"):
        try:
            if p.exists():
                k = p.read_text(encoding="utf-8").strip()
                if k:
                    return k
        except Exception:  # noqa: BLE001
            pass
    return ""


def _api_get(path: str, params: dict, key: str, tries: int = 3) -> dict | None:
    for k in range(tries):
        try:
            r = requests.get(API + path, params=params,
                             headers={"X-api-key": key}, timeout=25)
            j = r.json()
            if j.get("code") == 0:
                return j
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1 + k)
    return None


def load_industry_list(key: str, refresh: bool = False) -> list[dict]:
    """同花顺行业指数清单（tag=industry，缓存 7 天）。"""
    p = paths.state_dir() / "ths_industry_list.json"
    if not refresh and p.exists():
        age = time.time() - p.stat().st_mtime
        if age < LIST_TTL:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    j = _api_get("/api/a-share-index/catalog/ths-index-list",
                 {"tag": "industry"}, key)
    items = ((j or {}).get("data") or {}).get("item") or []
    items = [{"thscode": it["thscode"], "name": it["name"]} for it in items]
    if items:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return items


def fetch_kline(thscode: str, key: str) -> pd.DataFrame | None:
    """行业指数日 K → DataFrame(date, close)；失败返回 None。

    实测同花顺行业指数（.TI）历史自 2022-01-04 起（start=2021 返回空），
    start 定为 2022-01-01。
    """
    start = int(datetime(2022, 1, 1, tzinfo=CN_TZ).timestamp() * 1000)
    end = int(datetime.now(CN_TZ).timestamp() * 1000)
    j = _api_get("/api/a-share-index/prices/historical",
                 {"thscode": thscode, "interval": "1d",
                  "start": start, "end": end}, key)
    items = ((j or {}).get("data") or {}).get("item") or []
    if not items:
        return None
    rows = []
    for it in items:
        d = datetime.fromtimestamp(int(it["date_ms"]) / 1000, tz=CN_TZ)
        rows.append({"date": d.strftime("%Y-%m-%d"),
                     "close": float(it["close_price"])})
    return pd.DataFrame(rows)


def _cache_dir() -> Path:
    d = paths.state_dir() / "ths_kline"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_kline(thscode: str, key: str, refresh: bool = False,
               ttl: int = KLINE_TTL) -> pd.DataFrame | None:
    """读/刷新单行业指数 K 线缓存（csv；超 TTL 或缺省才拉取）。"""
    p = _cache_dir() / f"{thscode}.csv"
    if not refresh and p.exists():
        if time.time() - p.stat().st_mtime < ttl:
            try:
                return pd.read_csv(p)
            except Exception:  # noqa: BLE001
                pass
    df = fetch_kline(thscode, key)
    if df is not None and len(df):
        df.to_csv(p, index=False)
        return df
    if p.exists():
        try:
            return pd.read_csv(p)          # 拉取失败 → 旧缓存兜底
        except Exception:  # noqa: BLE001
            return None
    return None


def theme_indices(key: str, refresh: bool = False) -> tuple[pd.DataFrame, dict]:
    """date × theme → 主题行业指数（归一化等权均值）；返回 (指数表, 缺失诊断)。"""
    lst = load_industry_list(key)
    have = {it["thscode"] for it in lst}
    idxs, missing = {}, {}
    for theme, boards in THEME_INDEXES.items():
        cols = []
        for code, name in boards:
            if code not in have:
                missing[code] = f"{name}(清单无此指数)"
                continue
            df = load_kline(code, key, refresh=refresh)
            if df is None or len(df) < 30:
                missing[code] = name
                continue
            s = df.set_index("date")["close"]
            cols.append(s / float(s.iloc[0]))
        if cols:
            idxs[theme] = pd.concat(cols, axis=1).mean(axis=1)
        else:
            missing[theme] = "无可用行业指数K线"
    if idxs:
        return pd.DataFrame(idxs), missing
    return pd.DataFrame(), missing


def hot_themes_series(refresh: bool = False, ma_short: int = 20,
                      ma_long: int = 60, slope: bool = False,
                      min_days: int = 0,
                      rank_top: int | None = None,
                      lookback_days: int = 60) -> tuple[dict, dict]:
    """date -> 热主题列表 + 缺失诊断（无前视）。

    rank_top 非 None：月线级排名——按主题行业指数 lookback_days（默认 60 日≈
    一季度，月线级别）涨幅对主题池排名，取前 rank_top 为热主题（用户要求
    "至少排名前 10"，不再用二元趋势过滤出 0-1 个）。
    rank_top 为 None：均线趋势模式（index>MA{short} 且 MA{short}>MA{long}，
    可加 slope/min_days）——保留作对比研究用。
    """
    key = get_key()
    if not key:
        print("⚠ 未配置同花顺 Key（HITHINK_FINANCE_API_KEY / settings.json api_key）")
        return {}, {"key": "未配置"}
    idx, missing = theme_indices(key, refresh)
    if len(idx) == 0:
        return {}, missing
    out = {}
    if rank_top:
        mom = idx / idx.shift(lookback_days) - 1
        # M1 修复：主题池仅 8 个，rank_top=10 会全选=放开门槛（已被证伪的最差
        # 方案 +102%/-80%）；限制最多取 len-1（至少留 1 个非热，避免自毁）
        cap = max(1, min(rank_top, len(idx.columns) - 1))
        for d in idx.index:
            row = mom.loc[d].dropna().sort_values(ascending=False)
            out[str(d)] = list(row.index[:cap])
        return out, missing
    trend = {}
    for theme in idx.columns:
        s = idx[theme]
        maS = s.rolling(ma_short).mean()
        maL = s.rolling(ma_long).mean()
        t = (s > maS) & (maS > maL)
        if slope:
            t = t & (maS > maS.shift(1))
        if min_days > 0:
            t = t.rolling(min_days).min().fillna(0).astype(bool)
        trend[theme] = t.fillna(False)
    for d in idx.index:
        out[str(d)] = [th for th in THEME_ORDER
                       if th in trend and bool(trend[th].get(d, False))]
    return out, missing


def hot_codes_by_date(hot_series: dict, theme_map: dict) -> dict:
    """date -> set[code]：theme_map 中主题命中当日热主题的代码。"""
    out = {}
    for d, ths in hot_series.items():
        out[d] = {c for c, t in theme_map.items() if t in ths}
    return out


def today_hot(ma_short: int = 20, ma_long: int = 60,
              slope: bool = False, min_days: int = 0,
              rank_top: int | None = None,
              lookback_days: int = 60) -> list:
    hs, _ = hot_themes_series(False, ma_short, ma_long, slope, min_days,
                              rank_top, lookback_days)
    if hs:
        return hs[max(hs)]
    return []


def main() -> None:
    ap = argparse.ArgumentParser(description="动态热主题（同花顺全A行业指数）")
    ap.add_argument("--refresh", action="store_true", help="强制刷新缓存")
    ap.add_argument("--ma-short", type=int, default=20)
    ap.add_argument("--ma-long", type=int, default=60)
    ap.add_argument("--slope", action="store_true")
    ap.add_argument("--min-days", type=int, default=0)
    ap.add_argument("--rank-top", type=int, default=None,
                    help="月线级排名取前 N 为热主题（如 10）")
    ap.add_argument("--lookback", type=int, default=60,
                    help="排名用的涨幅回看天数（月线级别，默认 60）")
    ap.add_argument("--hot", action="store_true", help="只打印当日热主题")
    args = ap.parse_args()
    if args.hot:
        print("当日热主题:", today_hot(args.ma_short, args.ma_long,
                                        args.slope, args.min_days,
                                        args.rank_top, args.lookback))
        return
    hs, missing = hot_themes_series(args.refresh, args.ma_short, args.ma_long,
                                    args.slope, args.min_days,
                                    args.rank_top, args.lookback)
    if hs:
        last = max(hs)
        print(f"截止 {last} 热主题: {hs[last]}")
        print(f"最近 10 个交易日热主题切换: {dict(list(hs.items())[-10:])}")
    if missing:
        print("缺失板块:", missing)


if __name__ == "__main__":
    main()
