"""5 分钟 K 线归档：腾讯 m5 每日拉取增量存储，供盘中点火检测与月度买卖点优化。

数据源：腾讯 ifzq.gtimg.cn（服务器/本机均可用，东财 klt=5 对服务器 IP 风控）。
腾讯 m5 返回 320 根（约 6.7 个交易日，滚动窗口）——每日 17:30 拉一次，
按 (code, 日期) 增量合并落盘 data/m5daily/<code>.csv，逐日积累即历史。

腾讯 5 分钟字段：[时间 YYYYMMDDHHMM, 开, 收, 高, 低, 量(手), {}, 额]。
落盘 CSV 列：datetime(YYYY-MM-DD HH:MM), open, close, high, low, volume(股,×100)。

用法:
    python3 -m mainrise.m5data archive            # 归档卡点企业全部 5 分钟（17:30 每日）
    python3 -m mainrise.m5data archive --codes 600519,000938   # 指定代码
"""
from __future__ import annotations

import argparse
import time

import pandas as pd
import requests

from mainrise import paths
from mainrise.report import load_chokepoint_codes

TX_M5 = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}
# 腾讯 m5 单次最多返回的根数（滚动窗口，够最近 ~6.7 交易日）
TX_MAX = 320


def _secid(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def fetch_m5(code: str, tries: int = 4) -> list[list]:
    """腾讯 m5 拉取，返回 [[YYYYMMDDHHMM, o, c, h, l, vol_hand, {}, amt], ...] 升序。"""
    u = f"{TX_M5}?param={_secid(code)},m5,,{TX_MAX}"
    for i in range(tries):
        try:
            r = requests.get(u, headers=_HDR, timeout=10)
            d = r.json()
            node = (d.get("data") or {}).get(_secid(code)) or {}
            mk = node.get("m5") or []
            if mk:
                return mk
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5 + i * 1.5)
    return []


def archive(codes: list[str] | None = None) -> dict:
    """归档指定代码（默认全部卡点企业）的 5 分钟线到 data/m5daily/<code>.csv。"""
    codes = codes or sorted(load_chokepoint_codes())
    out_dir = paths.data_dir() / "m5daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"ok": 0, "empty": 0, "codes": []}
    for code in codes:
        mk = fetch_m5(code)
        if not mk:
            stats["empty"] += 1
            print(f"  {code}: 无数据（腾讯限流/无5分钟）")
            continue
        rows = []
        for x in mk:
            ts = x[0]  # YYYYMMDDHHMM
            dt = (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}")
            try:
                rows.append({"datetime": dt, "open": float(x[1]),
                             "close": float(x[2]), "high": float(x[3]),
                             "low": float(x[4]),
                             "volume": float(x[5]) * 100})  # 手→股
            except (ValueError, IndexError):
                continue
        if not rows:
            stats["empty"] += 1
            continue
        df = pd.DataFrame(rows).drop_duplicates("datetime", keep="last")
        df = df.sort_values("datetime")
        p = out_dir / f"{code}.csv"
        if p.exists():
            old = pd.read_csv(p, dtype={"datetime": str})
            df = pd.concat([old, df]).drop_duplicates("datetime", keep="last")
            df = df.sort_values("datetime")
        df.to_csv(p, index=False, encoding="utf-8-sig")
        stats["ok"] += 1
        stats["codes"].append(code)
        print(f"  {code}: {len(df)} 根（{df['datetime'].iloc[0]} ~ "
              f"{df['datetime'].iloc[-1]}）")
        time.sleep(0.8)  # 腾讯限频保护
    print(f"归档完成：{stats['ok']} 只有数据 / {stats['empty']} 只空")
    return stats


def load_m5(code: str) -> pd.DataFrame:
    """读取已归档 5 分钟线（无则空表）。"""
    p = paths.data_dir() / "m5daily" / f"{code}.csv"
    if not p.exists():
        return pd.DataFrame(columns=["datetime", "open", "close", "high",
                                     "low", "volume"])
    df = pd.read_csv(p, dtype={"datetime": str})
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="5分钟K线归档（腾讯m5）")
    ap.add_argument("action", choices=["archive"], help="archive=归档")
    ap.add_argument("--codes", default="", help="逗号分隔代码，默认全部卡点企业")
    args = ap.parse_args()
    if args.codes:
        archive([c.strip() for c in args.codes.split(",") if c.strip()])
    else:
        archive()


if __name__ == "__main__":
    main()
