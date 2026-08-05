"""数据目录解析。

优先级:
  1. $MAINRISE_HOME 环境变量
  2. 项目模式：在项目根（含 pyproject.toml + data/zzshare_daily）运行时，
     复用项目内 data/ 与 output/reports、output/state
  3. 独立软件模式：~/mainrise（report 在 home/reports，state 在 home/state）
"""
from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path | None:
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists() and (cwd / "data" / "zzshare_daily").is_dir():
        return cwd
    return None


def _is_project_home(h: Path) -> bool:
    """判断给定目录是否为项目结构（含 data/zzshare_daily 或 output/reports）。"""
    return (h / "data" / "zzshare_daily").is_dir() or (h / "output" / "reports").is_dir()


def home() -> Path:
    env = os.environ.get("MAINRISE_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    pr = _project_root()
    if pr is not None:
        return pr
    return Path.home() / ".mainrise"


def data_dir() -> Path:
    return home() / "data"


def zzshare_dir() -> Path:
    return data_dir() / "zzshare_daily"


def report_dir() -> Path:
    env = os.environ.get("MAINRISE_HOME", "").strip()
    if env and _is_project_home(Path(env).expanduser()):
        return Path(env).expanduser() / "output" / "reports"
    if _project_root() is not None:
        return home() / "output" / "reports"
    return home() / "reports"


def state_dir() -> Path:
    env = os.environ.get("MAINRISE_HOME", "").strip()
    if env and _is_project_home(Path(env).expanduser()):
        return Path(env).expanduser() / "output" / "state"
    if _project_root() is not None:
        return home() / "output" / "state"
    return home() / "state"


def stock_list_path() -> Path:
    return data_dir() / "stock_list.csv"


def trade_dates_path() -> Path:
    return data_dir() / "trade_dates.csv"


def ensure_dirs() -> None:
    for p in (home(), data_dir(), zzshare_dir(), report_dir(), state_dir()):
        p.mkdir(parents=True, exist_ok=True)
