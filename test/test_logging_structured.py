"""
结构化日志（方案 C）离线测试：JSON 行格式、request_id 关联、错误堆栈
"""

import io
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.logging_setup import JsonFormatter, RequestIdFilter, set_request_id


def _emit(level: int, msg: str, extra=None, exc=None) -> str:
    """用 JsonFormatter 格式化一条记录并返回 JSON 行"""
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc,
    )
    for k, v in (extra or {}).items():
        setattr(record, k, v)
    formatter = JsonFormatter()
    return formatter.format(record)


class TestJsonFormatter:
    def test_basic_fields(self):
        line = _emit(logging.INFO, "hello")
        d = json.loads(line)
        assert d["level"] == "INFO"
        assert d["logger"] == "test.logger"
        assert d["msg"] == "hello"
        assert "ts" in d
        # 无上下文 request_id 时省略该键（默认 "-"）
        assert d.get("request_id", "-") == "-"

    def test_request_id_attached_from_context(self):
        from src.logging_setup import reset_request_id
        tok = set_request_id("abc123")
        try:
            line = _emit(logging.INFO, "with rid")
        finally:
            reset_request_id(tok)
        d = json.loads(line)
        assert d["request_id"] == "abc123"

    def test_extra_fields_merged_and_json_safe(self):
        line = _emit(
            logging.INFO,
            "request_timing",
            extra={"method": "POST", "path": "/v1/query", "latency_ms": 12.3, "timing": {"total_ms": 25.0, "stages": {"a": 1}}},
        )
        d = json.loads(line)
        assert d["method"] == "POST"
        assert d["timing"]["total_ms"] == 25.0
        # 非 JSON 安全值也应被序列化（default=str）
        line2 = _emit(logging.INFO, "x", extra={"obj": object()})
        assert json.loads(line2)["obj"].startswith("<")

    def test_error_stack_included(self):
        try:
            raise ValueError("boom")
        except ValueError as e:
            line = _emit(logging.ERROR, "failed", exc=(type(e), e, e.__traceback__))
        d = json.loads(line)
        assert d["error"]["type"] == "ValueError"
        assert "boom" in d["error"]["message"]
        assert "ValueError" in d["error"]["stack"]
        assert "test_logging_structured.py" in d["error"]["stack"]

    def test_request_id_filter_sets_record_field(self):
        rec = logging.LogRecord("n", logging.INFO, __file__, 1, "m", (), None)
        f = RequestIdFilter()
        assert f.filter(rec) is True
        assert hasattr(rec, "request_id")


def test_middleware_logs_request_with_id():
    """端到端：中间件 + 管道日志都带上同一 request_id（走 TestClient 触发中间件）"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.app import create_app

    app = create_app(None)  # 无 pipeline：/v1/health 依然可走中间件
    client = TestClient(app)

    # 捕获 JSON 行
    import logging as _l
    from src.logging_setup import get_logger

    buf = io.StringIO()
    h = _l.Handler()
    h.setLevel(_l.DEBUG)
    h.setFormatter(_l.Formatter("%(message)s"))
    # 用 StreamHandler 捕获本测试的 logger 输出（含 request 事件）
    from src.logging_setup import _configure_root
    _configure_root()
    capture = _l.StreamHandler(buf)
    capture.setLevel(_l.DEBUG)
    from src.logging_setup import JsonFormatter as JF
    capture.setFormatter(JF())
    rag_logger = get_logger("rag.api")
    rag_logger.addHandler(capture)
    old_level = rag_logger.level
    rag_logger.setLevel(_l.DEBUG)
    try:
        r = client.get("/v1/health")
        assert r.status_code == 200
        rid = r.headers.get("X-Request-Id")
        assert rid, "响应应带 X-Request-Id"
        lines = [json.loads(l) for l in buf.getvalue().strip().splitlines() if l.strip()]
        req_line = next((x for x in lines if x.get("msg") == "request"), None)
        assert req_line is not None, f"应记录 request 日志，实际: {lines}"
        assert req_line["request_id"] == rid
        assert req_line["path"] == "/v1/health" and req_line["status"] == 200
    finally:
        rag_logger.setLevel(old_level)
        rag_logger.removeHandler(capture)