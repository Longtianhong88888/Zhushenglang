"""主升浪信号跟踪模型（可打包独立软件）。

子命令: init / update / backtest / evaluate / report / track / snapshot
数据目录: $MAINRISE_HOME 或 ~/.mainrise（自动创建）
"""

__version__ = "0.1.0"


# 过滤 urllib3 在 macOS 系统 Python 下的 NotOpenSSLWarning（无害，纯噪音）
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import urllib3.exceptions  # noqa: F401

warnings.filterwarnings("ignore", category=urllib3.exceptions.NotOpenSSLWarning)
