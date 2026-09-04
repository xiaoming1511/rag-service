"""
AI 知识层生成器（wiki-builder · Karpathy LLM Wiki 补齐）

核心思想：传统 wiki 维护成本太高（交叉引用/一致性/更新），而 LLM 擅长这类
"记账"工作。本模块在增量索引之后自动：
1. 为每篇新文档生成「来源摘要页」（sources/）——LLM 概括并提取关键要点；
2. 抽取「概念」与「实体」，自动创建/更新对应页面（concepts/ 、entities/），
   并互相 [[链接]]（互链）；
3. 维护 index.md（全库目录）与 log.md（操作日志）。

注意：知识层目录（默认 vault 的 wiki/）按约定不参与向量检索，仅供人工查阅。
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.document.loader import Document, DocumentLoader
from src.generation.generator import Generator


class WikiBuilder:
    """AI 知识层生成器"""

    SUMMARY_PROMPT = (
        "你是知识库整理助手。请阅读下面的原始文档，输出它的「来源摘要」，格式为：\n"
        "## 概述\n（2-3 句话概括文档主题与核心内容）\n"
        "## 关键要点\n（3-6 条要点，每条一行，以 - 开头）\n"
        "只输出上述 Markdown 内容，不要多余说明。"
    )

    EXTRACT_PROMPT = (
        "你是知识图谱构建助手。请阅读下面的原始文档，提取其中出现的「概念」"
        "（方法、理论、框架、术语）与「实体」（人物、项目、组织、产品）。\n"
        "只输出 JSON，格式：{\"concepts\": [\"概念1\", \"概念2\"], \"entities\": [\"实体1\"]}\n"
        "每个列表 2-6 项，无则输出空列表。"
    )

    def __init__(
            self,
            loader: DocumentLoader,
            generator: Generator,
            wiki_dir: str,
            enabled: bool = True,
    ):
        """
        初始化构建器

        Args:
            loader: 文档加载器（用于扫描原始文档）
            generator: 生成器（LLM 调用）
            wiki_dir: 知识层目录（写入 sources/concepts/entities/index/log）
            enabled: 是否启用（False 时 build_pending 直接返回空统计）
        """
        self.loader = loader
        self.generator = generator
        self.wiki_dir = Path(wiki_dir)
        self.enabled = enabled

    # ================================================================
    # 对外入口
    # ================================================================

    def build_pending(self, force: bool = False) -> Dict[str, Any]:
        """
        扫描原始文档，为"缺少页面或页面过期"的文档生成知识层页面

        Args:
            force: True 时强制重建全部页面（覆盖已有）

        Returns:
            Dict: {built, updated, skipped, concepts, entities}
        """
        if not self.enabled:
            return {"enabled": False, "built": [], "skipped": []}

        stats = {"built": [], "skipped": [], "concepts": 0, "entities": 0}
        docs = self.loader.load()

        for doc in docs:
            summary_path = self._summary_path(doc)
            if not force and summary_path.exists():
                # 页面存在且文档未更新 → 跳过
                if self._page_is_fresh(summary_path, doc):
                    stats["skipped"].append(doc.file_name)
                    continue
                stats["updated"] = stats.get("updated", [])
                stats["updated"].append(doc.file_name)
            stats = self.build_for_document(doc, extra_stats=stats)
            stats["built"].append(doc.file_name)

        self._update_index()
        if stats["built"] or stats.get("updated"):
            self._append_log(
                f"构建知识层：新增 {len(stats['built'])} / 更新 {len(stats.get('updated', []))} / "
                f"概念 {stats['concepts']} / 实体 {stats['entities']}"
            )
        return stats

    def build_for_document(self, doc: Document, extra_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """为单个文档构建全部知识层页面（摘要 + 概念 + 实体 + 日志）"""
        stats = extra_stats if extra_stats is not None else {}
        stats.setdefault("concepts", 0)
        stats.setdefault("entities", 0)

        # 1. LLM 摘要
        summary_body = self._llm_summary(doc)

        # 2. LLM 抽取概念/实体
        concepts, entities, defs = self._llm_extract(doc)

        # 3. 写入来源摘要页（含 [[概念]] 互链）
        links = ", ".join(f"[[{c}]]" for c in concepts[:8])
        page_front = (
            "---\n"
            f"type: source\n"
            f"source_file: \"{doc.file_name}\"\n"
            f"created: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n"
        )
        body = (
            f"# {doc.file_name}\n\n"
            f"{summary_body}\n"
        )
        if links:
            body += f"\n## 相关概念\n{links}\n"
        self._write_page(self._summary_path(doc), page_front + body)

        # 4. 概念页（互链回来源）
        for c in concepts:
            name = self._sanitize(c)
            if not name:
                continue
            definition = defs.get(c, "")
            self._upsert_topic_page("concepts", name, definition, doc.file_name)
            stats["concepts"] += 1

        # 5. 实体页
        for e in entities:
            name = self._sanitize(e)
            if not name:
                continue
            definition = defs.get(e, "")
            self._upsert_topic_page("entities", name, definition, doc.file_name)
            stats["entities"] += 1

        return stats

    # ================================================================
    # 页面工具
    # ================================================================

    def _summary_path(self, doc: Document) -> Path:
        """来源摘要页路径（文件名=原始文件名，冲突时加序号）"""
        out = self.wiki_dir / "sources"
        base = Path(doc.file_name).stem
        candidate = out / f"{base}.md"
        suffix = 2
        while candidate.exists():
            # 已存在但属于其他来源 → 加序号避免覆盖
            if self._page_source_file(candidate) == doc.file_name:
                break
            candidate = out / f"{base}-{suffix}.md"
            suffix += 1
        return candidate

    def _page_source_file(self, path: Path) -> Optional[str]:
        """读取页面 frontmatter 中的 source_file"""
        try:
            content = path.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.startswith("source_file:"):
                            return line.split(":", 1)[1].strip().strip('"')
        except Exception:
            pass
        return None

    def _page_is_fresh(self, path: Path, doc: Document) -> bool:
        """页面是否最新：比较文档修改时间与页面创建时间"""
        try:
            page_mtime = path.stat().st_mtime
            doc_mtime = Path(doc.file_path).stat().st_mtime
            return page_mtime >= doc_mtime - 1
        except OSError:
            return True

    def _write_page(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _upsert_topic_page(self, folder: str, name: str, definition: str, source_file: str):
        """创建或追加概念/实体页"""
        path = self.wiki_dir / folder / f"{name}.md"
        source_link = f"[[{Path(source_file).stem}]]"
        if path.exists():
            # 追加来源（去重）
            content = path.read_text(encoding="utf-8")
            if source_link in content or source_file in content:
                return
            content = content.rstrip() + f"\n- {source_link}\n"
            path.write_text(content, encoding="utf-8")
            return

        front = (
            "---\n"
            f"type: {folder.rstrip('s') if folder.endswith('s') else folder}\n"
            f"created: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n"
        )
        body = (
            f"# {name}\n\n"
            f"{definition.strip() or '（暂无定义，来自 LLM 抽取）'}\n\n"
            "## 相关来源\n"
            f"- {source_link}\n"
        )
        self._write_page(path, front + body)

    # ================================================================
    # LLM 调用
    # ================================================================

    def _chat(self, system: str, user: str, max_tokens: int) -> str:
        """调用聊天模型；失败返回空串"""
        client = getattr(self.generator, "client", None)
        if client is None:
            return ""
        try:
            return (client.chat_sync(
                model=self.generator.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=0.3,
            ) or "").strip()
        except Exception as e:
            print(f"⚠️ wiki-builder LLM 调用失败: {e}")
            return ""

    def _llm_summary(self, doc: Document) -> str:
        """生成来源摘要；LLM 失败时降级为原文截断"""
        user = f"文档内容：\n{doc.content[:4000]}"
        summary = self._chat(self.SUMMARY_PROMPT, user, max_tokens=800)
        if not summary:
            summary = f"## 概述\n（文档摘要生成失败，展示原文开头）\n{doc.content[:300]}"
        return summary

    def _llm_extract(self, doc: Document) -> Tuple[List[str], List[str], Dict[str, str]]:
        """抽取概念与实体；返回 (concepts, entities, 名称->一句话定义)"""
        user = f"文档内容：\n{doc.content[:4000]}"
        raw = self._chat(self.EXTRACT_PROMPT, user, max_tokens=400)
        concepts, entities, defs = [], [], {}
        if not raw:
            return concepts, entities, defs

        try:
            # 提取 JSON 子串（容错多余文字）
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
            concepts = [str(x).strip() for x in data.get("concepts", []) if str(x).strip()]
            entities = [str(x).strip() for x in data.get("entities", []) if str(x).strip()]
            for item in data.get("definitions", []):
                if isinstance(item, dict) and item.get("name"):
                    defs[str(item["name"])] = item.get("definition", "")
        except Exception as e:
            print(f"⚠️ wiki-builder 概念抽取解析失败: {e}")
        return concepts, entities, defs

    @staticmethod
    def _sanitize(name: str) -> str:
        """文件名安全化：去路径分隔与非法字符"""
        cleaned = re.sub(r'[\\/:*?"<>|#\[\]]', "", name).strip(" .")
        return cleaned[:60] or ""

    # ================================================================
    # index / log / graph
    # ================================================================

    def _update_index(self):
        """重写 index.md（按子目录分类列出全部页面）"""
        lines = ["# Wiki 索引", "", f"更新于 {datetime.now().isoformat(timespec='seconds')}", ""]
        for folder in ("sources", "concepts", "entities"):
            folder_path = self.wiki_dir / folder
            entries = sorted(p.stem for p in folder_path.glob("*.md")) if folder_path.exists() else []
            if entries:
                lines.append(f"## {folder}")
                for e in entries:
                    lines.append(f"- [[{e}]]")
                lines.append("")
        self._write_page(self.wiki_dir / "index.md", "\n".join(lines) + "\n")

    def _append_log(self, message: str):
        """追加操作日志"""
        log_path = self.wiki_dir / "log.md"
        line = f"- {datetime.now().isoformat(timespec='seconds')} {message}"
        if log_path.exists():
            log_path.write_text(log_path.read_text(encoding="utf-8").rstrip() + "\n" + line + "\n", encoding="utf-8")
        else:
            self._write_page(log_path, f"# 操作日志\n\n{line}\n")