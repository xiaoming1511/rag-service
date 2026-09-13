"""
统一日志配置（结构化 JSON 行 + 请求关联 + 文件轮转；混合方案 C 的 A 部分）

用法：
    from src.logging_setup import get_logger, set_request_id
    logger = get_logger(__name__)
    logger.info("事件", extra={...})   # extra 键并入 JSON 行
    logger.error("出错", exc_info=e, extra={...})  # 自动附带 {error:{type,message,stack}}

特性：
- 默认输出「单行 JSON」到 stderr + 轮转文件 logs/rag-service.log（10MB × 5 份）
- request_id：任意线程/协程内由中间件 set_request_id() 写入 contextvar，
  后续该请求的所有日志（pipeline/retrieval/generation/异常）自动携带，可跨模块关联定位
- 环境变量：
  RAG_LOG_LEVEL    DEBUG/INFO/WARNING/ERROR，默认 INFO
  RAG_LOG_FORMAT   json（默认） | text
  RAG_LOG_DIR      JSON/日志目录（默认 ./logs，相对服务运行目录）
- OTel 可选（混合方案 C 的 B 部分）：见 src.otel.maybe_init_otel()
"""

import contextvars
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

_CONFIGURED = False

# 请求 ID（中间件置入；日志 Formatter 读出并写入每条记录）
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# logging.LogRecord 保留字段（extra 传入这些键会被忽略，避免污染）
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "asctime", "message",
}


def set_request_id(rid: str) -> contextvars.Token:
    """为该请求上下文设置 request_id，返回 Token 供复位"""
    return request_id_var.set(rid)


def reset_request_id(token: contextvars.Token) -> None:
    try:
        request_id_var.reset(token)
    except ValueError:
        pass


def get_request_id() -> str:
    return request_id_var.get("-")


def _now_local() -> str:
    """上海时区（Asia/Shanghai）ISO 时间；系统无该 tz 数据时回退本地时间"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="milliseconds")
    except Exception:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    """单行 JSON 格式化器：ts(上海时区)/level/logger/msg/request_id + extra 字段 + 错误堆栈"""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": _now_local(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = get_request_id()
        if rid and rid != "-":
            payload["request_id"] = rid
        # 合并 extra（跳过保留字段）
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            payload[k] = v if isinstance(v, (dict, list, str, int, float, bool)) or v is None else str(v)
        # 异常堆栈（错误日志可定位）
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "stack": "".join(traceback.format_exception(*record.exc_info)).strip(),
            }
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps(
                {"ts": payload["ts"], "level": payload["level"], "logger": payload["logger"], "msg": str(payload["msg"])},
                ensure_ascii=False,
            )


class RequestIdFilter(logging.Filter):
    """把当前上下文 request_id 附加到记录（text 格式用 %(request_id)s）"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s [rid=%(request_id)s]"


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.getenv("RAG_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.getenv("RAG_LOG_FORMAT", "json").lower()
    log_dir = os.getenv("RAG_LOG_DIR", "./logs")

    root = logging.getLogger()
    root.setLevel(level)

    if fmt == "text":
        formatter: logging.Formatter = logging.Formatter(_TEXT_FORMAT, datefmt="%H:%M:%S")
    else:
        formatter = JsonFormatter()

    # 注意：RequestIdFilter 必须挂在 handler 上，不能挂在 root logger 上。
    # logger 级别的 filter 只对「该 logger 直接产生」的记录生效，子 logger
    # 传播上来的记录不会经过它——而业务日志几乎全部来自子 logger
    # （rag.pipeline / rag.api.convert ...）。挂 root 时 text 格式的
    # %(request_id)s 会抛 ValueError，该条记录被 logging 内部错误吞掉，
    # 表现为「切到 text 格式后日志大面积消失」。
    rid_filter = RequestIdFilter()

    # stderr（服务控制台/重定向日志）
    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(formatter)
    stderr_h.addFilter(rid_filter)
    root.addHandler(stderr_h)

    # 轮转文件（10MB × 5）
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_h = RotatingFileHandler(
            os.path.join(log_dir, "rag-service.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_h.setFormatter(formatter)
        file_h.addFilter(rid_filter)
        root.addHandler(file_h)
    except Exception as e:  # 文件不可写时不阻断服务
        logging.getLogger(__name__).warning("日志文件初始化失败（仅 stderr）: %s", e)

    # 降低三方库噪音
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "chromadb"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取已配置的模块日志器"""
    _configure_root()
    return logging.getLogger(name)