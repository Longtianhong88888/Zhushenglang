"""构建内置数据包 build/bundled_data.zip。

包含：全市场行情 + 股票名册 + 交易日历 + 观察池/持仓账本 + 主升浪报告。
新电脑解压后开箱即用（观察池、买点、报告全部就绪）。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT_DIR = ROOT / "output"
OUT = ROOT / "build" / "bundled_data.zip"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((DATA / "zzshare_daily").glob("*.csv")):
            z.write(f, f"data/zzshare_daily/{f.name}")
            n += 1
        for name in ("stock_list.csv", "trade_dates.csv"):
            z.write(DATA / name, f"data/{name}")
            n += 1
        if (OUT_DIR / "state").is_dir():
            for f in sorted((OUT_DIR / "state").glob("*")):
                z.write(f, f"state/{f.name}")
                n += 1
        for pat in ("主升浪*.md", "信号评估*.md", "主升浪*.xlsx", "主升浪*.csv"):
            for f in sorted((OUT_DIR / "reports").glob(pat)):
                z.write(f, f"reports/{f.name}")
                n += 1
    print(f"内置数据包: {OUT} {OUT.stat().st_size / 1e6:.0f} MB, {n} 个文件")


if __name__ == "__main__":
    main()
