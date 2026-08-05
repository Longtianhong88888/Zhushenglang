"""项目全局配置。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
POOL_DIR = DATA_DIR / "pools"          # 每日股池原始缓存 (csv)
BARS_DIR = DATA_DIR / "bars"           # 全市场个股日线缓存 (csv, 每股一个文件)
ZZSHARE_DIR = DATA_DIR / "zzshare_daily"  # zzshare 全市场日线（按交易日缓存）
DATASET_DIR = DATA_DIR / "dataset"     # 构建好的样本集
MODEL_DIR = ROOT / "output" / "models"
REPORT_DIR = ROOT / "output" / "reports"
FIGURE_DIR = ROOT / "output" / "figures"

for _d in (POOL_DIR, BARS_DIR, ZZSHARE_DIR, DATASET_DIR, MODEL_DIR, REPORT_DIR, FIGURE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 研究区间（默认近两年，可按需扩大）
START_DATE = "2024-01-01"
END_DATE = None  # None 表示最新交易日

# 全市场日线重建（全量历史方案）
BARS_START = "2021-01-01"   # 抓取起点（提供足够的前置交易日）
RESEARCH_START = "2022-01-01"  # 样本研究起点（BARS_START 之前的行情仅用于计算前收/连板）
BARS_SLEEP = 0.25           # 每股抓取间隔（秒）

# zzshare 全量历史方案（官方涨停价，按交易日拉全市场）
ZZSHARE_START = "2021-01-01"  # 2020-08 创业板注册制 20cm 之后，口径统一

# 拉取哪些股池：zt=涨停池, zt_prev=昨日涨停池(次日表现), zb=炸板池, dt=跌停池
POOL_TYPES = ("zt", "zt_prev", "zb", "dt")

# 抓取限速（秒），避免被数据源限流
FETCH_SLEEP = 0.35
FETCH_RETRIES = 4

# LightGBM 默认参数
MODEL_PARAMS = dict(
    n_estimators=600,
    learning_rate=0.05,
    num_leaves=31,
    max_depth=6,
    min_child_samples=30,
    subsample=0.9,
    subsample_freq=1,
    colsample_bytree=0.9,
    reg_alpha=0.1,
    reg_lambda=1.0,
    random_state=42,
    n_jobs=-1,
    verbose=-1,
)

MODEL_FILENAME = "erban_model.joblib"
FEATURE_FILENAME = "features.txt"
