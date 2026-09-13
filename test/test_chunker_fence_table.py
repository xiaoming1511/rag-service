"""
分块器修复回归测试：围栏代码块感知 + Markdown 表格转散文（检索保真）

背景：
- 旧分块器把围栏代码块内的 "# 注释" 行误判为 Markdown 标题，导致 heading 栈
  崩坏（如 个人 RAG 知识库系统.md 中「调用示例」子节碎片化、后继章节被错误
  嵌套）。
- bge 模型对管道符表格语义打分趋零，表格改散文后真实信息块才可被检索到。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.document.loader import Document
from src.document.chunker import Chunker, _tables_to_prose


def _make_doc(content: str, file_name: str = "test.md") -> Document:
    return Document(
        id=f"doc_{file_name}",
        file_path=f"/tmp/{file_name}",
        file_name=file_name,
        content=content,
    )


class TestFenceAwareHeading:
    def test_fence_comment_lines_not_headings(self):
        """围栏内的 '# 注释' 行不得成为标题，也不得破坏 heading 栈"""
        doc = _make_doc(
            "# 文档\n\n"
            "## 甲章节\n\n正文甲。\n\n"
            "### 调用示例\n"
            "```\n"
            "# 1. 健康检查\n"
            "curl http://127.0.0.1:8080/v1/health\n"
            "# 2. 建立索引\n"
            "curl -X POST http://127.0.0.1:8080/v1/index\n"
            "```\n\n"
            "## 乙章节\n\n正文乙。\n"
        )
        chunks = Chunker().chunk_document(doc)
        # 不应出现把 "# 1. 健康检查" 当作标题的块
        headings = [c.metadata.get("heading") for c in chunks]
        assert "1. 健康检查" not in headings, f"注释行被误判为标题: {headings}"
        # 注释行与 curl 内容应留在「调用示例」块内
        ex = [c for c in chunks if c.metadata.get("heading") == "调用示例"]
        assert ex and "curl http://127.0.0.1:8080/v1/health" in ex[0].content
        assert "1. 健康检查" in ex[0].content
        # 围栏边界 ``` 不入内容
        assert all("```" not in c.content for c in chunks)
        # 后继章节不被错误嵌套在调用示例之下（仅挂真实根标题 文档）
        yi = [c for c in chunks if c.metadata.get("heading") == "乙章节"]
        assert yi and yi[0].metadata.get("heading_path") == "文档 > 乙章节"

    def test_heading_stack_keeps_nesting(self):
        """围栏前后标题层级栈保持正确：子节路径仍完整"""
        doc = _make_doc(
            "# 文档\n\n"
            "#### 调用示例\n"
            "```\n"
            "# 1. 健康检查\n"
            "curl ...\n"
            "# 4. 非流式问答\n"
            "curl ...\n"
            "```\n\n"
            "## ✅ 五、技术决策\n\n决策内容。\n"
        )
        chunks = Chunker().chunk_document(doc)
        subs = [c for c in chunks if c.metadata.get("heading") == "1. 健康检查"]
        # 注释行作为内容归入调用示例，heading 无"1. 健康检查"时路径本应为空；
        # 这里用内容检查：调用示例块包含全部注释行
        ex = [c for c in chunks if c.metadata.get("heading") == "调用示例"]
        assert ex, "调用示例块应存在"
        assert "1. 健康检查" in ex[0].content and "4. 非流式问答" in ex[0].content
        # 后继章节路径 = 真实根标题 + 自身标题（不再被嵌套在假标题下）
        decision = [c for c in chunks if c.metadata.get("heading") == "✅ 五、技术决策"]
        assert decision and decision[0].metadata.get("heading_path") == "文档 > ✅ 五、技术决策"


class TestTablesToProse:
    def test_table_converted_to_prose(self):
        """三四列表格转为含引导句的散文，管道符消失"""
        md = (
            "## 四、API 接口\n\n"
            "服务地址: `http://127.0.0.1:8080`\n\n"
            "| 接口 | 方法 | 说明 |\n"
            "| --- | --- | --- |\n"
            "| /v1/query | POST | 非流式问答 |\n"
            "| /v1/query/stream | POST | 流式问答（SSE） |\n"
        )
        chunks = Chunker().chunk_document(_make_doc(md))
        # 引导句 = 文件名（去后缀） + 全部 + 小节名 + 如下：
        api = [c for c in chunks if "四、API 接口" in c.metadata.get("heading_path", "")]
        assert api, "四、API 接口 块应存在"
        content = api[0].content
        assert "test的全部API 接口如下：" in content
        assert "接口 /v1/query" in content or "/v1/query POST 非流式问答" in content
        # 不再残留管道表格行
        assert not any(line.lstrip().startswith("|") for line in content.split("\n"))

    def test_prose_only_unchanged(self):
        """无表格的普通文本原样保留"""
        text = "第一行。\n第二行。\n"
        assert _tables_to_prose(text) == text

    def test_non_table_pipe_lines_keep(self):
        """非表格的 | 装饰行不被误转"""
        text = "|----|\n普通文本\n"
        assert _tables_to_prose(text) == text


class TestRealDocShape:
    def test_api_section_survives_as_prose(self):
        """模拟 个人 RAG 知识库系统.md 的 API 段：表格散文化 + 子节归位"""
        md = (
            "## 🌐 四、API 接口\n\n"
            "服务地址: `http://127.0.0.1:8080`\n\n"
            "| 接口 | 方法 | 说明 |\n"
            "| --- | --- | --- |\n"
            "| /v1/health | GET | 健康检查 |\n\n"
            "#### 📌 调用示例\n"
            "```\n"
            "# 1. 健康检查\n"
            "curl http://127.0.0.1:8080/v1/health\n"
            "# 3. 流式问答\n"
            "curl -X POST http://127.0.0.1:8080/v1/query/stream\n"
            "```\n"
        )
        chunks = Chunker().chunk_document(_make_doc(md))
        joined = "\n".join(c.content for c in chunks)
        assert "test的全部API 接口如下：" in joined and "/v1/health GET 健康检查" in joined
        assert "3. 流式问答" in joined and "curl -X POST" in joined
        # 真实标题路径保留
        api = [c for c in chunks if "四、API 接口" in c.metadata.get("heading_path", "")]
        assert api, "四、API 接口 块应存在"