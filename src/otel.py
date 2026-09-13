"""
OpenTelemetry 可选接入（混合方案 C 的 B 部分）

启用与依赖：
- 设置环境变量 OTEL_EXPORTER_OTLP_ENDPOINT（如 http://127.0.0.1:4318/v1/traces）
- 安装（可选，未安装时自动降级 no-op，不影响任何功能）：
    pip install opentelemetry-sdk \
               opentelemetry-exporter-otlp-proto-http \
               opentelemetry-instrumentation-fastapi \
               opentelemetry-instrumentation-httpx

行为：
- 未设置端点：maybe_init_otel() 直接返回 False，span()/get_tracer() 均为 no-op
- 设置端点且依赖齐全：FastAPI/httpx 自动埋点 + pipeline 手动 span
  （pipeline.retrieve / pipeline.generate / pipeline.generate_stream），
  日志与 trace 通过 request_id（日志字段）与 trace_id/span_id（OTel）各自关联
"""

import contextlib
import os
from typing import Any, Dict, Optional

from src.logging_setup import get_logger

logger = get_logger("rag.otel")

_tracer: Optional[Any] = None
_initialized = False


def maybe_init_otel(app=None) -> bool:
    """尝试初始化 OTel（幂等）。返回是否真正启用。"""
    global _tracer, _initialized
    if _initialized:
        return _tracer is not None
    _initialized = True

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "rag-service"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("rag-service")

        # FastAPI 自动埋点（可选依赖）
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            if app is not None:
                FastAPIInstrumentor.instrument_app(app)
        except Exception as e:
            logger.warning("OTel FastAPI 埋点不可用（跳过）: %s", e)
        # httpx（oMLX 调用）自动埋点
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor().instrument()
        except Exception as e:
            logger.warning("OTel httpx 埋点不可用（跳过）: %s", e)

        logger.info("OpenTelemetry 已启用（OTLP: %s）", endpoint)
        return True
    except Exception as e:
        logger.warning(
            "OpenTelemetry 初始化失败（保持 no-op）: %s — 如需启用请安装："
            "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http "
            "opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx",
            e,
        )
        return False


def get_tracer():
    return _tracer


@contextlib.contextmanager
def span(name: str, attrs: Optional[Dict[str, Any]] = None):
    """手动 span（OTel 未启用时为 no-op）——pipeline 阶段埋点用"""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as s:
        if attrs:
            s.set_attributes({k: str(v) for k, v in attrs.items() if v is not None})
        yield s