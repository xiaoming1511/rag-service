"""
统一日志配置（决策 S1：logging 替换 print）

用法：
    from src.logging_setup import get_logger
    logger = get_logger(__name__)
    logger.info("...")

约定：
- 全局只配置一次（幂等，重复调用无副作用）
- 级别由环境变量 RAG_LOG_LEVEL 控制（DEBUG/INFO/WARNING/ERROR），默认 INFO
- 本项目为单进程服务（uvicorn 单 worker + watcher 线程），
  使用 StreamHandler 输出即可；stdout 多用于 CLI 工具（eval 等），
  故默认输出到 stderr，避免污染管道输出
"""

import logging
import os
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def _configure_root() -> None:
    """配置根日志器（进程内仅一次）"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("RAG_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    # 降低三方库噪音
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "chromadb"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取已配置的模块日志器"""
    _configure_root()
    return logging.getLogger(name)
