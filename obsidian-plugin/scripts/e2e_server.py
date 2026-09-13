"""E2E 服务端 runner：启动带 stub pipeline 的真实 FastAPI 服务

供 obsidian-plugin/scripts/e2e-contract.mjs 使用——把插件真实 api.ts 打包后
对着真实服务跑全部调用面（health/status/sessions/index/convert/research/
config/export/import/query stream SSE）。

用法：.venv/bin/python obsidian-plugin/scripts/e2e_server.py <port> <data_dir>
数据目录必须指向项目内路径（沙箱下系统 /tmp 不可用），由调用方创建。
"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

import uvicorn  # noqa: E402

from src.api.app import create_app  # noqa: E402
from src.api.routes import query, index, research, status  # noqa: E402
from src.pipeline.ingest_queue import IngestQueue  # noqa: E402
from src.session.store import ConversationStore  # noqa: E402


class StubPipeline:
    """覆盖插件全部调用面的最小 pipeline 桩（会话/队列用真实实现）"""

    def __init__(self, data_dir: Path):
        self.conversation_store = ConversationStore(str(data_dir / "conversations"))
        self.ingest_queue = IngestQueue(
            run_fn=self._run_job,
            store_path=str(data_dir / "index_jobs.json"),
        )
        self._gate = None  # url 任务用：延迟执行以便测试取消

    # ---- /v1/status ----
    def get_stats(self):
        return {
            "vector_store": {"count": 42, "collection_name": "e2e-contract"},
            "retriever": {"rerank_cache": None},
        }

    # ---- /v1/query/stream（SSE）----
    async def query_stream_async(self, question, top_k=5, use_rerank=True, history=None):
        yield (
            'data: {"type": "phase", "data": {"phase": "检索中"}}\n\n'
        )
        yield (
            'data: {"type": "sources", "data": [{"file_name": "e2e-note.md",'
            ' "content": "片段", "score": 0.87, "file_path": "/tmp/x/e2e-note.md",'
            ' "heading": "设置 > 基础", "line_start": 3, "line_end": 7}]}\n\n'
        )
        yield (
            'data: {"type": "timing", "data": {"total_ms": 12.3, "retrieve_ms": 4.1,'
            ' "generate_ms": 6.2, "usage": {"prompt_tokens": 10, "completion_tokens": 5,'
            ' "total_tokens": 15, "generation_tokens_per_second": 42.0}}}\n\n'
        )
        yield 'data: {"type": "chunk", "data": "你好"}\n\n'
        await asyncio.sleep(0.05)
        yield 'data: {"type": "chunk", "data": "，世界"}\n\n'
        yield 'data: {"type": "done", "data": "你好，世界"}\n\n'

    # ---- /v1/research ----
    def research(self, question, sub_queries=None, max_rounds=2):
        return {
            "report": f"关于「{question}」的研究报告",
            "sub_queries": ["子查询 A", "子查询 B"],
            "sources": [
                {"file_name": "e2e-note.md", "score": 0.9, "file_path": "/tmp/x/e2e-note.md",
                 "heading": "设置", "line_start": 1, "line_end": 2}
            ],
            "total_results": 1,
            "rounds": 1,
        }

    # ---- /v1/index、/v1/index/refresh、/v1/index/url ----
    def index(self, source_dirs=None, rebuild=False):
        return {"total_documents": 3, "total_chunks": 30, "total_vectors": 30}

    def index_incremental(self):
        return {"added": ["a.md"], "updated": [], "removed": [], "unchanged": 10}

    def index_url(self, url, timeout=30):
        return {"success": True, "title": "示例页面", "chunks": 4}

    # ---- /v1/index/async 队列 run_fn ----
    def _run_job(self, job):
        if job.kind == "url":
            # 慢任务：给 cancel 测试留窗口；取消时 run_fn 收尾仍走 cancelled 分支
            deadline = time.time() + 5
            while time.time() < deadline:
                if self.ingest_queue.is_cancelled(job.id):
                    return {"cancelled": True}
                time.sleep(0.05)
        return {"ok": True, "chunks": 7}


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    data_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else PROJECT / ".pytest_tmp" / "e2e-data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 配置必须先加载（真实启动流程由 __main__.py 完成）；且必须用副本——
    # POST /v1/config 会写回 yaml，直接指项目真配置会被 E2E 污染
    import shutil
    from src.config import config_manager
    cfg_copy = data_dir / "settings.yaml"
    shutil.copy(PROJECT / "config" / "settings.yaml", cfg_copy)
    config_manager.load(str(cfg_copy))

    app = create_app(StubPipeline(data_dir))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
