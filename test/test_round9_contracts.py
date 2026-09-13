"""
Round 9 契约测试（D6-3 IngestQueue 持久化/上限/快照 + deep_research 截断口径）

- D6-3a 进度落盘限频：update_progress 每个 tick 不再全量重写 JSON
- D6-3b 终态任务保留上限：_jobs 与持久化文件不再无限累积
- D6-3c 快照读取：get/list 返回副本，API 线程不再读到 worker 撕裂中的状态
- R9-2 deep_research 上下文：按 token 预算装配（整块保留），不再按字符硬截
"""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.ingest_queue import IngestQueue
from src.vector_store.base import SearchResult


# ================================================================
# D6-3a 进度落盘限频
# ================================================================

class TestProgressPersistThrottle:
    def _blocked_queue(self, tmp_path):
        """run_fn 阻塞在事件上，worker 停在 running，不产生收尾 _save 干扰"""
        gate = threading.Event()

        def run(job):
            gate.wait(timeout=10)
            return {}

        q = IngestQueue(run_fn=run, store_path=str(tmp_path / "jobs.json"))
        return q, gate

    def test_first_tick_persists_second_rapid_tick_does_not(self, tmp_path):
        q, gate = self._blocked_queue(tmp_path)
        try:
            job = q.submit("full")
            deadline = time.time() + 5
            while time.time() < deadline and q.get(job.id).status != "running":
                time.sleep(0.02)

            store = tmp_path / "jobs.json"
            before = store.read_text(encoding="utf-8")

            q.update_progress(job, "嵌入", 30, 100)  # 第 1 次：应落盘
            mid = store.read_text(encoding="utf-8")
            assert json.loads(mid)["jobs"][0]["progress_data"]["percent"] == 30.0

            q.update_progress(job, "嵌入", 60, 100)  # 第 2 次（间隔 <1s）：不应落盘
            after = store.read_text(encoding="utf-8")
            assert json.loads(after)["jobs"][0]["progress_data"]["percent"] == 30.0, \
                "限频窗口内的进度 tick 不应触发全量重写"

            # 内存态始终最新（API 读的是内存）
            assert q.get(job.id).progress_data["percent"] == 60.0
        finally:
            gate.set()
            q.shutdown()

    def test_throttle_expires_after_interval(self, tmp_path):
        q, gate = self._blocked_queue(tmp_path)
        try:
            job = q.submit("full")
            deadline = time.time() + 5
            while time.time() < deadline and q.get(job.id).status != "running":
                time.sleep(0.02)

            store = tmp_path / "jobs.json"
            q.update_progress(job, "嵌入", 30, 100)
            time.sleep(1.1)  # 越过限频窗口
            q.update_progress(job, "嵌入", 90, 100)
            saved = json.loads(store.read_text(encoding="utf-8"))
            assert saved["jobs"][0]["progress_data"]["percent"] == 90.0
        finally:
            gate.set()
            q.shutdown()


# ================================================================
# D6-3b 终态任务保留上限
# ================================================================

class TestFinishedJobRetention:
    def test_finished_jobs_capped(self, tmp_path):
        q = IngestQueue(run_fn=lambda job: {"ok": True},
                        store_path=str(tmp_path / "jobs.json"),
                        max_finished_jobs=2)
        jobs = [q.submit("incremental") for _ in range(4)]

        def status_or_gone(jid):
            job = q.get(jid)
            return "gone" if job is None else job.status

        deadline = time.time() + 10
        while time.time() < deadline:
            if all(status_or_gone(j.id) in ("done", "gone") for j in jobs):
                break
            time.sleep(0.02)

        # 超上限的最旧任务被裁剪（get 返回 None 是预期行为）
        assert status_or_gone(jobs[0].id) == "gone"
        assert status_or_gone(jobs[1].id) == "gone"
        listed = q.list(limit=200)
        assert len(listed) == 2, "终态任务超过上限应裁剪最旧的"
        kept_ids = {j.id for j in listed}
        assert kept_ids == {jobs[-1].id, jobs[-2].id}, "必须保留最新的两个"

        saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
        assert len(saved["jobs"]) == 2, "持久化文件同样受上限约束"

    def test_running_and_queued_never_pruned(self, tmp_path):
        gate = threading.Event()

        def run(job):
            if job.kind == "url":
                gate.wait(timeout=10)
            return {}

        q = IngestQueue(run_fn=run, store_path=str(tmp_path / "jobs.json"),
                        max_finished_jobs=1)
        try:
            done = q.submit("full")              # 快速完成 → finished=[done]，恰达上限
            deadline = time.time() + 10
            while time.time() < deadline and q.get(done.id).status != "done":
                time.sleep(0.02)
            blocker = q.submit("url")            # 占住 worker（running）
            follower = q.submit("incremental")   # 排队（queued）
            deadline = time.time() + 5
            while time.time() < deadline and q.get(blocker.id).status != "running":
                time.sleep(0.02)
            # finished 已达上限，但 running/queued 任务绝不裁剪，done 仍可查
            assert q.get(done.id).status == "done"
            assert q.get(blocker.id).status == "running"
            assert q.get(follower.id).status == "queued"
        finally:
            gate.set()
            q.shutdown()


# ================================================================
# D6-3c 快照读取
# ================================================================

class TestSnapshotSemantics:
    def test_get_returns_snapshot_not_live_reference(self, tmp_path):
        q = IngestQueue(run_fn=lambda job: {"ok": True},
                        store_path=str(tmp_path / "jobs.json"))
        job = q.submit("full")
        deadline = time.time() + 10
        while time.time() < deadline and q.get(job.id).status != "done":
            time.sleep(0.02)

        snap = q.get(job.id)
        snap.status = "hacked"          # 改副本不应影响队列真实状态
        snap.result = {"evil": True}
        real = q.get(job.id)
        assert real.status == "done"
        assert real.result == {"ok": True}

    def test_list_returns_snapshots(self, tmp_path):
        q = IngestQueue(run_fn=lambda job: {"ok": True},
                        store_path=str(tmp_path / "jobs.json"))
        job = q.submit("full")
        deadline = time.time() + 10
        while time.time() < deadline and q.get(job.id).status != "done":
            time.sleep(0.02)

        listed = q.list(limit=10)
        assert len(listed) == 1
        listed[0].status = "hacked"
        assert q.list(limit=10)[0].status == "done"


# ================================================================
# R9-2 deep_research 上下文口径
# ================================================================

class _CtxStubRetriever:
    """返回指定内容的单结果检索桩"""

    def __init__(self, content: str, score: float = 0.9):
        self._content = content
        self._score = score

    def retrieve(self, query, top_k=3, use_rerank=False):
        return [SearchResult(
            id="c1", score=self._score, content=self._content,
            metadata={"file_name": "a.md", "heading_path": "H1"},
        )]


class _CtxStubGenerator:
    model = "stub"

    def generate(self, query, context, **kwargs):
        return f"报告({len(context)})"


class TestResearchContextBudget:
    def test_context_respects_token_budget(self):
        from src.pipeline.deep_research import DeepResearch
        from src.retrieval.context_builder import estimate_tokens

        dr = DeepResearch(retriever=_CtxStubRetriever("字" * 8000),
                          generator=_CtxStubGenerator(),
                          max_context_tokens=500)
        out = dr.research("问题")
        # 桩把 context 长度回显进报告：报告(N) → 用同内容反算 token 数
        ctx_len = int(out["report"].split("(")[1].split(")")[0])
        assert estimate_tokens("字" * ctx_len) <= 500 + 100, \
            f"上下文应按 token 预算装配，实际 {estimate_tokens('字' * ctx_len)}"

    def test_default_budget_falls_back_to_schema(self):
        from src.pipeline.deep_research import DeepResearch
        from src.config import RetrievalConfig

        dr = DeepResearch(retriever=_CtxStubRetriever("x"), generator=_CtxStubGenerator())
        assert dr.max_context_tokens == RetrievalConfig().context_token_budget

    def test_no_char_hard_truncation_marker(self):
        """预算内的小上下文不应出现任何截断；行为与主问答链路一致（整块保留）"""
        from src.pipeline.deep_research import DeepResearch

        dr = DeepResearch(retriever=_CtxStubRetriever("短内容"),
                          generator=_CtxStubGenerator(),
                          max_context_tokens=500)
        out = dr.research("问题")
        assert "已截断" not in out["report"]


# ================================================================
# R9-3 import stats["files"] 口径 + R9-4 syntheses 点前缀
# ================================================================

class TestImportStatsCaliber:
    def test_files_counts_imported_not_destination(self, tmp_path):
        """files 必须是「本次导入复制的文件数」——目标目录预置的无关文件不得计入"""
        import zipfile as zfmod
        from src.pipeline.export_import import export_archive, import_archive
        from src.vector_store.chroma_store import ChromaStore

        store = ChromaStore(collection_name="cal", persist_directory=str(tmp_path / "vs"),
                            embedding_dimension=1024)
        store.add(ids=["a1"], embeddings=[[0.1] * 1024],
                  documents=["口径测试"], metadatas=[{"file_name": "x.md"}])
        vs_dir = tmp_path / "vs"
        mf = vs_dir / "index_manifest.json"
        mf.write_text('{"version": 1, "docs": {}}', encoding="utf-8")
        conv = tmp_path / "conversations"
        conv.mkdir()
        (conv / "abc.json").write_text('{"id":"abc","title":"对话","messages":[]}', encoding="utf-8")

        zip_path = tmp_path / "out" / "export.zip"
        export_archive(dest=str(zip_path), vector_store_dir=str(vs_dir),
                       manifest_path=str(mf), documents=[],
                       conversations_dir=str(conv),
                       attachments_dir=str(tmp_path / "att"),
                       config_path=str(tmp_path / "cfg.yaml"))

        with zfmod.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            expected = (sum(1 for n in names if n.startswith("vector_store/")) +
                        sum(1 for n in names if n.startswith("conversations/")))

        # 目标目录预置无关文件：旧口径（对 dst 全量 rglob）会把它们计入
        dst_vs = tmp_path / "dst_vs"
        dst_vs.mkdir(parents=True)
        (dst_vs / "preexisting.bin").write_bytes(b"x")
        (dst_vs / "another.bin").write_bytes(b"y")

        stats = import_archive(src=str(zip_path), vector_store_dir=str(dst_vs),
                               manifest_path=str(tmp_path / "dst_mf.json"),
                               conversations_dir=str(tmp_path / "dst_conv"),
                               attachments_dir=str(tmp_path / "dst_att"))
        assert stats["files"] == expected, \
            f"files 应为导入量 {expected}，而不是目标目录全量 {stats['files']}"


class TestSynthesesTmpPrefix:
    def test_tmp_file_uses_dot_prefix_and_cleaned(self, tmp_path, monkeypatch):
        """写盘中断时留下的孤儿临时文件必须是点前缀（Obsidian 隐藏）且被清理"""
        import os as _os
        import src.pipeline.syntheses as syn

        captured = {}

        def fake_replace(src, dst):
            captured["tmp"] = Path(src)
            raise OSError("模拟写盘中断")

        monkeypatch.setattr(_os, "replace", fake_replace)
        out = tmp_path / "syntheses"
        result = syn.save_syntheses("问题", "回答", [], out)
        assert result is None  # 失败被吞掉（返回 None），不抛出
        assert captured["tmp"].name.startswith("."), "临时文件必须点前缀（Obsidian 隐藏）"
        assert not captured["tmp"].exists(), "异常路径必须清理临时文件"
