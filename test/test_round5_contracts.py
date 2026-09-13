"""
第五轮（Round 5）契约与健壮性回归

覆盖本轮修掉的缺陷：
1. 事件循环：阻塞端点不得写成 async def 直接跑同步代码（/v1/research 实测冻结）
2. deep_research.parse_sub_queries：不得吃掉合法子问题开头的数字
3. deep_research._retrieve_all：单个子查询检索失败不中断整体研究
4. /v1/research 响应携带实际执行轮次；max_rounds 上限与实现夹取一致
5. evaluation.compare_with_baseline：基线缺 meta 不再抛 AttributeError
6. EvalDataset.load：非 dict 项 / 非字符串 gold 给出明确 ValueError
7. export_import.import_archive：非法 mode 报错；缺 meta.json 的包先拒后解
8. syntheses：int 型分数也能渲染进来源行
9. 结构守卫：src 下除 logging_setup 外不得直接 logging.getLogger
"""

import asyncio
import inspect
import json
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from pydantic import ValidationError

from src.api.app import create_app
from src.api.schemas import ResearchRequest
from src.evaluation import run_eval
from src.evaluation.dataset import EvalDataset
from src.pipeline.deep_research import DeepResearch, parse_sub_queries
from src.pipeline.export_import import import_archive
from src.pipeline.syntheses import save_syntheses


# ======================================================================
# 1. 事件循环：阻塞端点
# ======================================================================

class _StubPipeline:
    """最小 pipeline 桩：research 阻塞指定时长；rounds=2 便于断言响应字段

    SLEEP=1.0：窗口要明显大于机器抖动——第八轮全量回归时（机器高负载）
    health 曾被调度延迟推过 0.35s 绝对上限造成假阳性；窗口拉大后，
    「事件循环被冻结」与「调度抖动」在时序上不再重叠。
    """

    SLEEP = 1.0

    def research(self, question, sub_queries=None, max_rounds=None):
        time.sleep(self.SLEEP)
        return {
            "report": "研究报告",
            "sub_queries": ["子问题"],
            "sources": [],
            "total_results": 0,
            "rounds": 2,
        }

    def get_stats(self):
        return {"vector_store": {"count": 0, "collection_name": "stub"}}


def _reset_pipeline_globals():
    """create_app(pipeline) 会写模块级 _pipeline 全局；用例结束必须复位，
    否则会污染后续用例（它们依赖"未注入 pipeline"的状态）"""
    from src.api.routes import archive, config, index, query, research, sessions, status
    for mod in (query, index, status, research, config, sessions, archive):
        mod.set_pipeline(None)


def test_research_route_does_not_block_event_loop():
    """/v1/research 阻塞期间，其它请求必须仍能推进（事件循环未被冻结）

    未修复前（async def 内直接 time.sleep）实测 /v1/health 会被拖到研究结束才返回。
    """
    import httpx

    app = create_app(_StubPipeline())

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t0 = time.perf_counter()

            async def slow():
                r = await c.post("/v1/research", json={"question": "q"})
                assert r.status_code == 200, r.text
                assert r.json()["rounds"] == 2
                return time.perf_counter() - t0

            async def quick():
                await asyncio.sleep(0.1)
                r = await c.get("/v1/health")
                assert r.status_code == 200, r.text
                return time.perf_counter() - t0

            return await asyncio.gather(slow(), quick())

    try:
        slow_done, quick_done = asyncio.run(scenario())
    finally:
        _reset_pipeline_globals()

    assert quick_done < 0.8, f"health 响应过慢（{quick_done:.3f}s），疑似事件循环被阻塞"
    assert quick_done < slow_done - 0.2, (
        f"事件循环疑似被阻塞：health 在 {quick_done:.3f}s 返回，"
        f"research 在 {slow_done:.3f}s 返回"
    )


_HEAVY_ENDPOINTS = [
    ("research", "POST", "/v1/research"),
    ("status", "GET", "/v1/status"),
    ("status", "GET", "/v1/health"),
    ("archive", "GET", "/v1/export"),
    ("convert", "POST", "/v1/convert/to-md"),
    ("convert", "POST", "/v1/convert/html2md"),
    # Round 8：/v1/index 队列四端点（同步 sqlite/文件 IO，曾为 async def）
    ("index", "POST", "/v1/index/async"),
    ("index", "GET", "/v1/index/jobs"),
    ("index", "GET", "/v1/index/jobs/{job_id}"),
    ("index", "POST", "/v1/index/jobs/{job_id}/cancel"),
]


@pytest.mark.parametrize("module_name,method,path", _HEAVY_ENDPOINTS)
def test_heavy_endpoints_are_not_async_handlers(module_name, method, path):
    """重阻塞端点必须是 def（Starlette 自动放入线程池），不能裸 async def 跑同步代码

    说明：当前 starlette 版本把 include_router 包成 _IncludedRouter（不暴露 .path），
    故改为直接检查各 router 的 routes 列表——比遍历 app.routes 更稳。
    """
    import importlib

    mod = importlib.import_module(f"src.api.routes.{module_name}")
    matched = [
        r for r in mod.router.routes
        if getattr(r, "path", None) == path
        and method in (getattr(r, "methods", None) or set())
    ]
    assert matched, f"{module_name}.router 中未找到 {method} {path}"
    assert not inspect.iscoroutinefunction(matched[0].endpoint), (
        f"{method} {path} 仍是 async def —— 阻塞调用会冻结整个事件循环"
    )


# ======================================================================
# 2. parse_sub_queries：行首数字
# ======================================================================

def test_parse_sub_queries_preserves_leading_digits():
    """旧实现 lstrip("0123456789...") 会把 "3D 渲染…" 剥成 "D 渲染…"，静默污染检索"""
    assert parse_sub_queries("3D 渲染管线如何工作？") == ["3D 渲染管线如何工作？"]
    assert parse_sub_queries("2025 年的新变化有哪些？") == ["2025 年的新变化有哪些？"]
    assert parse_sub_queries("5 个主要应用场景是什么？") == ["5 个主要应用场景是什么？"]
    assert parse_sub_queries("2 阶段提交如何处理故障？") == ["2 阶段提交如何处理故障？"]


def test_parse_sub_queries_still_strips_real_list_prefixes():
    """真正的列表前缀（编号 + 分隔符 / 项目符号）仍要剥掉"""
    assert parse_sub_queries("1. RAG 是什么\n2. 向量检索原理") == ["RAG 是什么", "向量检索原理"]
    assert parse_sub_queries("1、RAG 是什么") == ["RAG 是什么"]
    assert parse_sub_queries("2）RAG 是什么") == ["RAG 是什么"]
    assert parse_sub_queries("- 引用计数机制\n* 标记清除") == ["引用计数机制", "标记清除"]
    assert parse_sub_queries("# 标题式子问题") == ["标题式子问题"]


def test_parse_sub_queries_dedup_and_count():
    text = "1. A\n2. A\n3. B\n4. C"
    assert parse_sub_queries(text) == ["A", "B", "C"]
    assert parse_sub_queries(text, count=2) == ["A", "B"]


# ======================================================================
# 3. _retrieve_all 容错
# ======================================================================

class _FlakyRetriever:
    """指定子查询抛异常，模拟单路检索失败"""

    def __init__(self, boom_on="boom"):
        self.boom_on = boom_on
        self.seen = []

    def retrieve(self, q, top_k=3, use_rerank=True):
        self.seen.append(q)
        if q == self.boom_on:
            raise RuntimeError("模拟检索失败")
        return [q]


def test_retrieve_all_tolerates_single_query_failure():
    retriever = _FlakyRetriever()
    dr = DeepResearch(retriever=retriever, generator=object())

    out = dr._retrieve_all(["ok1", "boom", "ok2"])

    assert set(retriever.seen) == {"ok1", "boom", "ok2"}, "所有子查询都应被尝试"
    assert out == [["ok1"], [], ["ok2"]], f"失败路应为空列表，实际 {out}"


def test_retrieve_all_serial_path_tolerates_failure():
    """单条子查询时走串行分支，同样不能因一路失败而抛出"""
    retriever = _FlakyRetriever()
    dr = DeepResearch(retriever=retriever, generator=object())
    assert dr._retrieve_all(["boom"]) == [[]]


# ======================================================================
# 4. max_rounds 契约
# ======================================================================

def test_research_request_max_rounds_matches_implementation_clamp():
    """schema 声明 le=10 而 DeepResearch 夹到 5，会让调用方"要 8 轮得 5 轮"且无提示"""
    dr = DeepResearch(retriever=object(), generator=object(), max_rounds=99)
    assert dr.max_rounds == 5, "实现侧硬夹上限是 5"

    assert ResearchRequest(question="q", max_rounds=5).max_rounds == 5
    with pytest.raises(ValidationError):
        ResearchRequest(question="q", max_rounds=6)


# ======================================================================
# 5. compare_with_baseline 防御
# ======================================================================

def _write_baseline(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_compare_with_baseline_missing_meta_does_not_crash(tmp_path, monkeypatch):
    """基线有 metrics 无 meta：旧实现返回 baseline_meta=None，
    调用方 .get() 直接 AttributeError"""
    p = tmp_path / "baseline.json"
    _write_baseline(p, {"metrics": {"recall@5": 0.5}})
    monkeypatch.setattr(run_eval, "BASELINE_PATH", p)

    out = run_eval.compare_with_baseline({"recall@5": 0.6})

    assert out["baseline"] is None
    assert "error" in out
    # 关键：调用方（main）的打印路径不再炸
    assert out.get("baseline_meta") is None


def test_compare_with_baseline_valid(tmp_path, monkeypatch):
    p = tmp_path / "baseline.json"
    _write_baseline(p, {"meta": {"timestamp": "2026-09-11T00:00:00"}, "metrics": {"recall@5": 0.5}})
    monkeypatch.setattr(run_eval, "BASELINE_PATH", p)

    out = run_eval.compare_with_baseline({"recall@5": 0.6, "mrr@5": None})

    assert out["baseline_meta"]["timestamp"] == "2026-09-11T00:00:00"
    assert out["diff"] == {"recall@5": 0.1}, "None 指标不应进入 diff"


def test_compare_with_baseline_non_object_json(tmp_path, monkeypatch):
    p = tmp_path / "baseline.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(run_eval, "BASELINE_PATH", p)
    out = run_eval.compare_with_baseline({"recall@5": 0.6})
    assert out["baseline"] is None


# ======================================================================
# 6. EvalDataset 校验
# ======================================================================

def test_dataset_rejects_non_dict_item(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps(["not-an-object"]), encoding="utf-8")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        EvalDataset.load(str(p))


def test_dataset_rejects_non_string_gold_docs(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps([{"id": "a", "question": "q", "gold_docs": [{"x": 1}]}]),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="gold_docs 元素必须是非空字符串"):
        EvalDataset.load(str(p))


def test_dataset_accepts_valid_item(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps([{"id": "a", "question": "q", "gold_docs": ["x.md"]}]),
                 encoding="utf-8")
    ds = EvalDataset.load(str(p))
    assert len(ds) == 1
    assert ds.gold_set(ds.items[0]) == {"x.md"}


# ======================================================================
# 7. import_archive 守卫
# ======================================================================

def _write_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


def test_import_archive_rejects_unknown_mode(tmp_path):
    """拼错 mode 不能静默退化成 merge（否则用户以为已重建知识库）"""
    z = _write_zip(tmp_path / "a.zip", {"meta.json": "{}"})
    with pytest.raises(ValueError, match="mode 仅支持 merge / replace"):
        import_archive(
            src=str(z),
            vector_store_dir=str(tmp_path / "vs"),
            manifest_path=str(tmp_path / "mf.json"),
            mode="replcae",
        )


def test_import_archive_rejects_zip_without_meta(tmp_path):
    """缺 meta.json 的包应在解包前就被拒（不必先完整落盘）"""
    z = _write_zip(tmp_path / "b.zip", {"whatever.txt": "x"})
    with pytest.raises(ValueError, match="缺少 meta.json"):
        import_archive(
            src=str(z),
            vector_store_dir=str(tmp_path / "vs"),
            manifest_path=str(tmp_path / "mf.json"),
            mode="merge",
        )
    assert not (tmp_path / "vs").exists(), "被拒的归档不应产生向量库目录"


def test_import_archive_merge_still_works(tmp_path):
    """正向路径不能被守卫改坏"""
    z = _write_zip(tmp_path / "c.zip", {
        "meta.json": json.dumps({"tool": "rag-service"}),
        "index_manifest.json": "{}",
        "vector_store/chroma.sqlite3": "x",
        "conversations/s1.json": "{}",
    })
    stats = import_archive(
        src=str(z),
        vector_store_dir=str(tmp_path / "vs"),
        manifest_path=str(tmp_path / "mf.json"),
        conversations_dir=str(tmp_path / "conv"),
        mode="merge",
    )
    assert stats["mode"] == "merge"
    assert (tmp_path / "vs" / "chroma.sqlite3").exists()
    assert (tmp_path / "mf.json").exists()
    assert (tmp_path / "conv" / "s1.json").exists()


# ======================================================================
# 8. syntheses 分数渲染
# ======================================================================

def test_save_syntheses_renders_int_score(tmp_path):
    """旧实现 isinstance(score, float) 会把 int 分（1 / 0）判成"无分"，分数整段消失"""
    out = save_syntheses(
        question="问题",
        answer="回答",
        sources=[{"file_name": "f.md", "file_path": "/p/f.md", "score": 1}],
        out_dir=tmp_path,
    )
    assert out is not None and out.exists()
    content = out.read_text(encoding="utf-8")
    assert "分数 1.000" in content, content


def test_save_syntheses_omits_missing_score(tmp_path):
    out = save_syntheses(
        question="问题", answer="回答",
        sources=[{"file_name": "f.md"}], out_dir=tmp_path,
    )
    content = out.read_text(encoding="utf-8")
    # 模板为 `· 分数 {score} ·`，无分数时 score 为空串 → 出现连续两个空格
    assert "- [[f.md]] · 分数  ·" in content, content


# ======================================================================
# 9. 结构守卫：日志器
# ======================================================================

def test_no_direct_logging_getlogger_outside_logging_setup():
    """直接用 logging.getLogger 会绕过项目日志配置（无 request_id / 无 JSON / 可能不落盘）"""
    src_root = Path(__file__).parent.parent / "src"
    offenders = []
    for py in src_root.rglob("*.py"):
        if py.name == "logging_setup.py":
            continue
        if "logging.getLogger(" in py.read_text(encoding="utf-8"):
            offenders.append(str(py.relative_to(src_root)))
    assert not offenders, (
        f"应统一使用 src.logging_setup.get_logger，以下文件直接调用了 logging.getLogger: {offenders}"
    )


# ======================================================================
# 10. 上下文预算：单一真源
# ======================================================================

def test_retrieve_with_context_default_follows_config(monkeypatch):
    """max_context_tokens 默认值曾写死 4000，与 RetrievalConfig.context_token_budget
    形成第二份真源——改配置后直接调用本方法的地方会静默沿用旧值。"""
    import src.retrieval.retriever as retr_mod
    from src.retrieval.retriever import Retriever

    # 先构造（__init__ 内部也读 RetrievalConfig），再替换模块符号
    r = Retriever(vector_store=None, embedder=None, reranker=None, parent_expansion=False)
    monkeypatch.setattr(r, "retrieve", lambda *a, **kw: [])

    captured = []

    class _FakeConfig:
        context_token_budget = 12345

    monkeypatch.setattr(retr_mod, "RetrievalConfig", _FakeConfig)
    monkeypatch.setattr(retr_mod, "build_context",
                        lambda results, budget: captured.append(budget) or "ctx")

    r.retrieve_with_context("q")
    assert captured == [12345], "默认应回落 RetrievalConfig.context_token_budget"

    r.retrieve_with_context("q", max_context_tokens=999)
    assert captured[-1] == 999, "显式传参仍应优先"


def test_generation_eval_uses_configured_context_budget(monkeypatch):
    """run_generation_eval 曾写死 max_context_tokens=4000，评测与线上上下文不一致"""
    from types import SimpleNamespace

    import src.evaluation.run_eval as re_mod
    import src.generation.generator as gen_mod

    captured = {}

    class _RetrieverStub:
        def retrieve_with_context(self, query, top_k=None, use_rerank=True,
                                  max_context_tokens=None):
            captured["budget"] = max_context_tokens
            return "ctx", []

    class _JudgeStub:
        def evaluate_generation(self, question, context, answer, gold_keywords):
            return {"faithfulness": 1.0, "answer_relevance": 1.0}

    class _GeneratorStub:
        def __init__(self, **kwargs):
            pass

        def generate(self, query, context):
            return "answer"

    monkeypatch.setattr(gen_mod, "Generator", _GeneratorStub)

    config = SimpleNamespace(
        retrieval=SimpleNamespace(context_token_budget=7777),
        omlx=SimpleNamespace(chat_model="m"),
        generation=SimpleNamespace(max_tokens=16, temperature=0.0),
    )
    dataset = SimpleNamespace(items=[{"id": "a", "question": "q"}])

    out = re_mod.run_generation_eval(
        dataset, client=object(), retriever=_RetrieverStub(),
        config=config, judge=_JudgeStub(), top_k=5, use_rerank=False,
    )

    assert captured["budget"] == 7777, "应取 config.retrieval.context_token_budget"
    assert out["metrics"]["faithfulness"] == 1.0
