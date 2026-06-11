"""
统一日志配置：双输出（终端 + 按天轮转文件）。

使用独立 "tts" logger，不与 uvicorn 的日志配置冲突。

使用方式：
    from logger import get_logger
    log = get_logger(__name__)
    log.info("message")
    log.warning("message")
    log.debug("message")
    log.error("message", exc_info=True)  # 附带 traceback
"""
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ── 格式 ──────────────────────────────────────────────────────────────────────
# [2026-06-11 10:30:45] [INFO ] [worker] message
_FORMAT = logging.Formatter(
    "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_initialized = False


def _init():
    """初始化 "tts" 顶级 logger（仅首次调用生效）。"""
    global _initialized
    if _initialized:
        return

    tts_logger = logging.getLogger("tts")
    tts_logger.setLevel(_resolve_log_level())
    tts_logger.propagate = False  # 不向 root logger 传播，避免重复输出

    # ── 终端输出 ──────────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setFormatter(_FORMAT)
    tts_logger.addHandler(console)

    # ── 文件输出（按天轮转，保留 7 天）────────────────────────────────────
    log_dir = Path(os.environ.get("TTS_LOG_DIR", str(Path(__file__).parent / "logs")))
    log_dir.mkdir(exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        filename=str(log_dir / "tts.log"),
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(_FORMAT)
    tts_logger.addHandler(file_handler)

    _initialized = True


def _resolve_log_level() -> int:
    """解析 TTS_LOG_LEVEL 环境变量，默认 INFO。"""
    level = os.environ.get("TTS_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level, logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """
    获取指定模块的 logger（继承自 "tts" 顶级 logger）。

    参数：
      name — 通常传 __name__，自动提取模块短名

    返回：
      logging.Logger 实例
    """
    _init()

    # 提取模块短名：package.module → module
    short = name.rsplit(".", 1)[-1] if "." in name else name
    return logging.getLogger(f"tts.{short}")
