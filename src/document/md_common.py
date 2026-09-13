"""
Markdown 输出 / 文本解码的公共工具（叶模块）

抽取动机（第三轮清理后仍存在的重复）：
    「表格拼装」与「文本解码」两条规则此前在多处各写一份，且已经漂移：

    | 位置 | 表格渲染 | 解码链 |
    |---|---|---|
    | `html_to_markdown._render_table`   | 完整（表头 + `\\| --- \\|` 分隔行） | utf-8 → gb18030 → latin-1 |
    | `to_markdown._docx_to_markdown`    | 完整（手写复制同一逻辑） | — |
    | `to_markdown._pptx_to_markdown`    | **缺分隔行** → 渲染不成表格 | — |
    | `parsers/pptx.PptxParser`          | **缺分隔行** + 丢空单元格 → 列错位 | — |
    | `to_markdown._decode`              | — | utf-8 → gb18030 |
    | `parsers/base.Parser._decode_text` | — | utf-8 → gb18030(errors=replace) |

    同一份排版规则散成多份，结果就是「同一张 PPTX 表格，走索引路径与走
    转换接口拿到的 Markdown 不一样」。收敛到这里，规则只有一处。

依赖方向：本模块是 document 包内的**叶节点**，只依赖标准库；被
`html_to_markdown` / `to_markdown` / `parsers` 依赖，自身不反向依赖任何转换器。
"""

from typing import List

# ----------------------------------------------------------------------
# 文本解码
# ----------------------------------------------------------------------

# 解码候选顺序（确定性，不依赖外部探测库）。任何文本格式（.txt/.md/.html/.epub
# 章节）都走这一条链，避免同一份文件经不同入口得到不同文本：
#   1. utf-8   —— 绝大多数情况；能解通基本可断定就是 UTF-8
#   2. gb18030 —— 覆盖 GBK/GB2312 的中文 Windows 文档
#   3. latin-1 —— 逐字节映射，永不失败，作最后兜底
#
# Big5 说明（R10-2 死分支已移除，行为与移除前完全一致）：
#   曾有 big5 排在 gb18030 之后，但 gb18030 的字节空间几乎覆盖任意双字节
#   序列，绝大多数 Big5 文本会被它「成功」解码成乱码——big5 分支**不可达**，
#   留在链里只会误导读者以为它能救繁体文本，故移除。纯靠试解码无法区分
#   GB18030 / Big5（两者都"解得出"），真正的解法是字符集探测（如
#   charset-normalizer）。此处**刻意不引入**：探测库是否可用取决于环境，
#   一旦按可用性切换逻辑，解码结果就会随环境漂移（生产与开发不一致），
#   这比确定性乱码更难排查（该取舍见 feature-map §6 第 14 条）。
_DECODE_CHAIN = ("utf-8", "gb18030", "latin-1")


def decode_bytes(data: bytes, source_name: str = "") -> str:
    """把字节解码为文本（按 _DECODE_CHAIN 逐个尝试）

    Args:
        data: 原始字节
        source_name: 来源名（仅用于日志定位）

    Returns:
        解码后的文本；链末的 latin-1 不会抛错，故必有返回值。
    """
    for enc in _DECODE_CHAIN:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # 理论不可达（latin-1 接受任意字节），保留以防链被改坏
    from src.logging_setup import get_logger

    get_logger(__name__).warning("文本解码全部失败，改用替换模式: %s", source_name)
    return data.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------
# Markdown 表格
# ----------------------------------------------------------------------


def escape_pipe(text: str) -> str:
    """转义表格单元格中的 `|` 与换行，避免破坏 Markdown 表格结构"""
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render_md_table(rows: List[List[str]]) -> str:
    """把二维单元格渲染成标准 Markdown 表格。

    规则（此前散落在多处、且已漂移）：
        - 首行作表头，紧随一行 `| --- |` 分隔行（**没有分隔行 Obsidian 不会
          当表格渲染**，pptx 的两条路径此前都漏了这一行）；
        - 各行列数不齐时按最大列数右侧补空，避免列塌陷错位；
        - 整行单元格全空的行丢弃（多为排版占位）；
        - 单元格内容应已过 `escape_pipe`；本函数只负责拼装。

    Args:
        rows: 二维单元格文本

    Returns:
        Markdown 表格文本；无有效行时返回空串。
    """
    cleaned: List[List[str]] = []
    for r in rows or []:
        if not r:
            continue
        cells = [("" if c is None else str(c)) for c in r]
        if any(c.strip() for c in cells):
            cleaned.append(cells)

    if not cleaned:
        return ""

    width = max(len(r) for r in cleaned)
    if width == 0:
        return ""
    padded = [r + [""] * (width - len(r)) for r in cleaned]

    lines = [
        "| " + " | ".join(padded[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for r in padded[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)
