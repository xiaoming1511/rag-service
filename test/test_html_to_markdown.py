"""
HTML → Markdown 转换测试（离线，无外部依赖）

覆盖本次修复的核心回归点：
- `<pre>` 代码块逐字保留（含空白与换行），不得塌成一行
- 非白名单容器（如 `<div class="architecture">`）里的 ASCII 架构图不得丢失
- ASCII 图必须套 ``` 围栏（否则 Obsidian 折叠空格导致排版错乱）
- HTML 注释不得混入正文
- 表格 / 列表 / 标题 / 行内语法正确转换
- HTMLParser（索引链路）同样保住架构图
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.document.html_to_markdown import (  # noqa: E402
    HTMLToMarkdownConverter,
    html_to_markdown,
    _looks_like_ascii_art,
)
from src.document.parsers.html import HTMLParser  # noqa: E402


# ================================================================
# 样例：模拟真实导出文档的结构（含架构图 div + pre + 注释）
# ================================================================

ARCH_DIAGRAM = """┌─────────────────────────────────────────┐
│              接入层 (Interface)          │
│  ┌───────────┐   ┌───────────┐          │
│  │  Web UI   │   │ Obsidian  │          │
│  └───────────┘   └───────────┘          │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│              API 层 (FastAPI)            │
└─────────────────────────────────────────┘"""


def _make_html(inner: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>测试文档</title></head>
<body>
<div class="container">
{inner}
</div>
</body></html>"""


# ================================================================
# 核心回归：架构图
# ================================================================

def test_ascii_art_in_div_is_preserved():
    """架构图位于非白名单 div 中时不得丢失（本次问题根因）"""
    html = _make_html(
        '<h2>二、架构设计</h2>\n'
        f'<div class="architecture">\n{ARCH_DIAGRAM}\n</div>'
    )
    md = html_to_markdown(html)

    assert "接入层" in md, "架构图内容被丢弃"
    assert "API 层" in md, "架构图内容被丢弃"
    assert "Web UI" in md


def test_ascii_art_wrapped_in_fence():
    """ASCII 图必须套 ``` 围栏，否则 Obsidian 折叠空格造成排版错乱"""
    html = _make_html(f'<div class="architecture">\n{ARCH_DIAGRAM}\n</div>')
    md = html_to_markdown(html)

    assert "```" in md, "ASCII 图未套代码围栏"
    # 围栏内的图必须保留多行，不得塌成一行
    fenced = md.split("```")[1]
    assert len([l for l in fenced.splitlines() if l.strip()]) >= 8


def test_ascii_art_not_collapsed_to_single_line():
    """回归：架构图不得被压成单行（塌陷即用户遇到的排版错乱）"""
    html = _make_html(f'<div class="architecture">\n{ARCH_DIAGRAM}\n</div>')
    md = html_to_markdown(html)

    lines = md.splitlines()
    longest = max(len(l) for l in lines)
    # 若塌陷，最长行会接近整个图的总长度
    assert longest < len(ARCH_DIAGRAM), "架构图疑似塌陷为单行"


def test_pre_block_preserved_verbatim():
    """<pre> 内容逐字保留（空白与换行）"""
    tree = "rag-service/\n├── api/\n│   └── app.py\n└── run_api.py"
    html = _make_html(f"<pre>{tree}</pre>")
    md = html_to_markdown(html)

    assert "├── api/" in md
    assert "│   └── app.py" in md, "缩进被破坏"
    assert "```" in md


def test_pre_with_common_indent_dedented():
    """<pre> 的统一源码缩进应被去掉，但相对缩进保留"""
    html = _make_html(
        "<pre>\n"
        "        line1\n"
        "            line2\n"
        "        line3\n"
        "</pre>"
    )
    md = html_to_markdown(html)
    body = md.split("```")[1]

    assert body.startswith("\nline1"), f"公共缩进未去除: {body!r}"
    assert "\n    line2" in body, "相对缩进被破坏"


# ================================================================
# 注释与噪音
# ================================================================

def test_html_comment_not_rendered():
    """HTML 注释不得混入正文（会产出重复的伪标题）"""
    html = _make_html(
        "<!-- ============================================ -->\n"
        "<!--  一、系统概述                                 -->\n"
        "<!-- ============================================ -->\n"
        "<h2>一、系统概述</h2>\n<p>正文</p>"
    )
    md = html_to_markdown(html)

    assert md.count("一、系统概述") == 1, f"注释混入正文: {md!r}"


def test_script_style_dropped():
    html = _make_html(
        "<style>.a{color:red}</style>"
        "<script>var x=1;</script>"
        "<p>可见正文</p>"
    )
    md = html_to_markdown(html)

    assert "可见正文" in md
    assert "color:red" not in md
    assert "var x" not in md


# ================================================================
# 结构：标题 / 表格 / 列表 / 行内
# ================================================================

def test_headings():
    html = _make_html("<h1>一级</h1><h2>二级</h2><h3>三级</h3>")
    md = html_to_markdown(html)

    assert "# 一级" in md
    assert "## 二级" in md
    assert "### 三级" in md


def test_table_to_markdown():
    html = _make_html(
        "<table>"
        "<tr><th>组件</th><th>选型</th></tr>"
        "<tr><td>LLM</td><td>oMLX</td></tr>"
        "</table>"
    )
    md = html_to_markdown(html)

    assert "| 组件 | 选型 |" in md
    assert "| --- | --- |" in md
    assert "| LLM | oMLX |" in md


def test_table_cell_pipe_escaped():
    """单元格内的 | 必须转义，避免破坏表格结构"""
    html = _make_html("<table><tr><th>a</th></tr><tr><td>x | y</td></tr></table>")
    md = html_to_markdown(html)

    assert "x \\| y" in md


def test_unordered_and_ordered_list():
    html = _make_html("<ul><li>甲</li><li>乙</li></ul><ol><li>第一</li></ol>")
    md = html_to_markdown(html)

    assert "- 甲" in md
    assert "- 乙" in md
    assert "1. 第一" in md


def test_nested_list():
    html = _make_html("<ul><li>父<ul><li>子</li></ul></li></ul>")
    md = html_to_markdown(html)

    assert "- 父" in md
    assert "  - 子" in md, f"嵌套缩进丢失: {md!r}"


def test_inline_syntax():
    html = _make_html(
        '<p>用 <strong>粗体</strong> 和 <em>斜体</em> 与 <code>code()</code>，'
        '见 <a href="http://x.test">链接</a></p>'
    )
    md = html_to_markdown(html)

    assert "**粗体**" in md
    assert "*斜体*" in md
    assert "`code()`" in md
    assert "[链接](http://x.test)" in md


def test_paragraph_source_newlines_collapsed():
    """<p> 内的源码缩进换行应折叠为空格（HTML 语义如此）"""
    html = _make_html("<p>\n    第一句，\n    第二句\n</p>")
    md = html_to_markdown(html)

    assert "第一句， 第二句" in md, f"段落换行未按 HTML 语义折叠: {md!r}"


def test_br_creates_newline():
    """<br> 是显式换行，必须保留"""
    html = _make_html("<p>第一行<br>第二行</p>")
    md = html_to_markdown(html)

    assert "第一行\n第二行" in md, f"<br> 未保留: {md!r}"


def test_blockquote():
    html = _make_html("<blockquote>引用内容</blockquote>")
    md = html_to_markdown(html)

    assert "> 引用内容" in md


def test_images_kept_and_optionally_dropped():
    html = _make_html('<p><img src="a.png" alt="图"></p>')

    assert "![图](a.png)" in html_to_markdown(html)
    assert "a.png" not in html_to_markdown(html, keep_images=False)


# ================================================================
# 判定函数
# ================================================================

def test_looks_like_ascii_art_detection():
    assert _looks_like_ascii_art(ARCH_DIAGRAM) is True
    assert _looks_like_ascii_art("普通的一行文字") is False
    assert _looks_like_ascii_art("多行\n普通文本") is False


def test_normal_div_not_treated_as_art():
    """含正常块级结构的 div 不应被当 ASCII 图"""
    html = _make_html('<div class="note"><p>普通段落</p><p>第二段</p></div>')
    md = html_to_markdown(html)

    assert "普通段落" in md
    assert "第二段" in md


# ================================================================
# 索引链路：HTMLParser
# ================================================================

def test_parser_preserves_diagram():
    """HTMLParser（索引用）必须保住架构图（原实现会整段丢弃）"""
    html = _make_html(
        "<h2>二、架构设计</h2>\n"
        f'<div class="architecture">\n{ARCH_DIAGRAM}\n</div>'
    ).encode("utf-8")

    result = HTMLParser().parse_bytes(html, "test.html")

    assert "接入层" in result.content
    assert "API 层" in result.content
    assert "```" in result.content


def test_parser_title_from_title_tag():
    html = _make_html("<h1>正文标题</h1><p>x</p>").encode("utf-8")
    result = HTMLParser().parse_bytes(html, "t.html")

    assert result.title == "测试文档"


def test_parser_title_fallback_to_h1():
    html = "<html><body><h1>唯一标题</h1><p>x</p></body></html>".encode("utf-8")
    result = HTMLParser().parse_bytes(html, "t.html")

    assert result.title == "唯一标题"


def test_parser_gb18030_encoding():
    """中文 Windows 导出的 GB18030 编码文档"""
    html = _make_html("<p>中文内容测试</p>").encode("gb18030")
    result = HTMLParser().parse_bytes(html, "gb.html")

    assert "中文内容测试" in result.content


def test_parser_url_like_input():
    """URL 抓取路径（parse_bytes）同样适用"""
    html = _make_html("<h1>网页标题</h1><pre>a\nb</pre>").encode("utf-8")
    result = HTMLParser().parse_bytes(html, "http://example.test/page")

    assert "网页标题" in result.content
    assert "a\nb" in result.content


# ================================================================
# 真实文档（存在则跑，不存在跳过）
# ================================================================

def test_real_exported_document():
    """用仓库里真实的导出 HTML 验证端到端转换"""
    import pytest

    path = Path(__file__).parent.parent / "docs" / "deepseek_html_20260904_a59476.html"
    if not path.exists():
        pytest.skip("真实样例不存在")

    converter = HTMLToMarkdownConverter()
    md = converter.convert_bytes(path.read_bytes(), path.name)

    # 架构图六个层级关键词必须全部保留
    for kw in ("接入层", "API 层", "编排层", "文档处理层", "基础设施层"):
        assert kw in md, f"真实文档转换丢失: {kw}"

    # 表格转成了 Markdown 表格
    assert "| 组件 | 选型 | 作用 | 状态 |" in md

    # 无塌陷：最长行远小于整篇长度
    longest = max(len(l) for l in md.splitlines())
    assert longest < 300, f"存在塌陷长行: {longest}"

    # 围栏成对
    assert md.count("```") % 2 == 0


def test_template_convert_sample():
    """综合样例 HTML（标题/表格/列表/代码块/架构图/引用/行内样式）端到端转换"""
    import pytest

    path = Path(__file__).parent.parent / "docs" / "template_convert_test.html"
    if not path.exists():
        pytest.skip("样例不存在")

    from src.document.to_markdown import convert_to_markdown

    result = convert_to_markdown(path.read_bytes(), "html", path.name)
    md = result.markdown

    # 标题层级
    assert "# 🧠 RAG 知识库系统 — 转换测试" in md
    assert "## 📌 一、组件清单" in md
    assert "## 🏗️ 二、架构设计" in md

    # 表格
    assert "| 组件 | 选型 | 状态 |" in md
    assert "| LLM 推理 | oMLX 0.6.4 | ✅ 已部署 |" in md

    # 架构图保留 + 围栏
    assert "接入层" in md
    assert "API 层" in md
    assert md.count("```") % 2 == 0

    # 行内样式
    assert "**标题层级**" in md
    assert "*行内样式*" in md
    assert "`行内代码`" in md
    assert "[链接](http://127.0.0.1:8080)" in md

    # 列表（嵌套）
    assert "- 分块策略：按 Markdown 标题切分" in md
    assert "  - 子项：提升检索精度" in md
    assert "1. 第一步" in md

    # 引用块（尾部无孤立 >）
    assert "> 这是一个引用块。" in md
    assert "\n>\n" not in md, "引用块尾部出现孤立 >"

    # 无塌陷
    longest = max(len(l) for l in md.splitlines())
    assert longest < 300
