# RAG 服务优化 · 设计文档（已确认版）

> 状态：已获用户确认，作为实施依据
> 日期：2026-09-04
> 范围：五项优化方向，分阶段实施，每阶段先确认细节再实施

---

## 一、确认的关键决策汇总

| 编号 | 决策点 | 结论 |
|---|---|---|
| D1 | 增量索引变更检测 | **两者结合**：先比较 mtime+size 快速筛选，可疑项再算内容 MD5 精确判定 |
| D2 | 增量索引监听形态 | **两者都要**：API 服务内置 watchdog 自动监听 + 提供 `POST /v1/index/refresh` 手动触发 + CLI `watch` 命令 |
| D3 | 多格式支持范围 | **全部**：PDF(.pdf)、Word(.docx)、本地网页(.html/.htm)、纯文本(.txt)、远程网页 URL（新增抓取接口） |
| D4 | 解析库选型 | **PyMuPDF + python-docx**（PDF 中文支持好、速度快；docx 用 python-docx） |
| D5 | 多轮对话追问策略 | **简单模式 + 可配置开关**：默认把历史直接传给 LLM，配置项可开启"LLM 问题改写"（为升级预留） |
| D6 | 对话历史长度 | **最近 10 轮 + token 预算裁剪**（约 2000 token，从旧到新裁超预算消息） |
| D7 | 性能优化范围 | **全部三项**：索引并行化 + 嵌入内存 LRU 缓存 + 相同问题响应缓存（含配置开关） |
| D8 | Obsidian 插件范围 | **完整版**：聊天面板 + 点击跳转 + 状态栏状态 + 设置面板 + 命令面板 + 自动索引开关 |
| D9 | vault 的 wiki/ 目录 | **继续排除**：加载器保持硬编码排除名为 wiki 的目录（用户确认），仅索引 xu/x 根笔记（当前 6 个），wiki/ 下的概念/实体页不纳入 |

> 实施进度：P0 ✅（2026-09-04）｜P1 ✅（2026-09-04，含离线单测 7 项）｜P2 ✅（2026-09-04，解析器注册表 + URL 端点，测试 5 项）｜P3 ✅（2026-09-04，历史裁剪 + 可选改写 + 前端回传，测试 9 项）｜P4 ✅（2026-09-04，并行分块 + 嵌入内存 LRU + 响应缓存，测试 12 项；实机验证缓存命中 6.1s → 0.002s）｜P5 ✅（2026-09-04，TypeScript+esbuild 完整版插件，已安装至 ~/projects/obsidian/.obsidian/plugins/rag-service/）
>
> **评审补全（LLM Wiki 对照，第二期）**：R1a ✅（严格来源模式 / 问答沉淀 syntheses/vault 根 / 后台摄入队列 单worker+持久化+jobs 端点）｜R1b ✅（EPUB/PPTX 解析 + Deep Research 简版 POST /v1/research）｜R2 ✅（AI 知识层生成 wiki-builder 写入既有 xu/wiki + 周期维护 + 知识图谱 /v1/graph + 网页力导向）。实机：知识层 6 摘要页+33 概念+15 实体，图谱 98 节点/116 边。测试共 **91 项**。
>
> **冗余删除（2026-09-04，用户逐项确认）**：① Web 聊天页（web/ 移除，根路径重定向 /docs，查询统一走 Obsidian 插件）② CLI（cli/ 移除，无人依赖）③ 知识图谱数据层（/v1/graph 路由 + graph.json 生成逻辑移除；wiki-builder 保留页面生成与互链，图谱展示交给 Obsidian Graph View）。测试 **90 项**。
>
> **第三期（2026-09-04）**：① 移除 AI 知识层生成 wiki-builder（模块/端点/自动串联/周期维护；**xu/wiki 交由外部 LLM Wiki 管理，本系统不处理该目录**，loader 继续排除 wiki）② 新增配置管理 GET/POST `/v1/config`（校验→写回 settings.yaml→热生效；检索/生成/沉淀/缓存/路由字段即时生效）③ **模型路由**：chat / rewrite / research_subqueries 三任务可分别配置模型（ModelRouter，空值回退默认聊天模型；含热更新与 API/插件控制）④ Obsidian 插件设置面板新增「服务端配置」区（严格模式/重排序/阈值/响应缓存/沉淀/追问改写/历史/模型路由 → /v1/config 热生效）。测试 **94 项**。
>
> **第四期（2026-09-04）**：⑤ 行级引文（chunker 记录原文行号 → sources 携带 line_start/end_line → 插件打开笔记选中高亮）⑥ 多模态附件（PDF/DOCX/PPTX 提取内嵌图片 → data/attachments + 上下文说明，随来源返回、插件可打开）⑦ 会话管理（服务端 JSON 持久化 + /v1/sessions CRUD + 插件会话栏）⑧ 项目导出/导入（ZIP 归档：配置/文档清单/向量库/变更清单/会话/附件；merge/replace）⑨ 递归 Deep Research（最大轮次可配 + 新信息增益停止）⑩ CI/发布（GitHub Actions + Makefile + 插件安装指引 docs/obsidian-plugin-install.md）。测试 **102 项**。

---

## 二、阶段计划

```
P0 前置基础   vector_store 按 doc_id 删除/查询 + 修正 index_single 判存在 bug
             + API sources 响应扩展 file_path/heading（向后兼容）
P1 增量索引   manifest(JSON) + 三态同步（新增/变更/删除）+ watchdog 自动监听
             + POST /v1/index/refresh + CLI watch
P2 多格式支持  loader 重构为解析器注册表 + PDF/DOCX/HTML/TXT + POST /v1/index/url
P3 多轮对话   前端维护历史并回传 + 后端历史裁剪工具 + 可选问题改写
P4 性能优化   索引并行 + 嵌入内存 LRU 缓存 + 相同问题响应缓存
P5 Obsidian 插件 TypeScript 插件（完整版：聊天面板/点击跳转/状态栏/设置/命令/自动索引）
```

依赖：P0 必须先于 P1；P1 完成后 P2–P5 可独立推进。

---

## 三、P0 关键设计

### 3.1 向量存储按文档操作（BaseVectorStore + ChromaStore）
- 新增 `get_by_doc_id(doc_id) -> List[Dict]`：按元数据 `doc_id` 查询某文档的全部块
  （chromadb 实现：`collection.get(where={"doc_id": doc_id})`）
- 新增 `delete_by_doc_id(doc_id) -> None`：删除某文档全部块
  （chromadb 实现：`collection.delete(where={"doc_id": doc_id})`）
- 用途：增量变更时"先删后加"；修正 `index_single` 判存在逻辑
  （现状 bug：库里 ID 是块 ID `{doc_id}_{idx}`，用 `get([document.id])` 永远判不存在）

### 3.2 API sources 响应扩展（向后兼容）
- `SourceInfo` 增加可选字段：`file_path: Optional[str]`、`heading: Optional[str]`
- `/v1/query`、`/v1/query/stream` 的 sources 中附带
  `r.metadata.file_path`（绝对路径，供 Obsidian 插件换算 vault 相对路径）
  与 `r.metadata.heading_path`（跳转锚点）
- 旧字段 `file_name` 保持不变，不破坏现有前端 / 插件消费方

---

## 四、P1 增量索引关键设计

### 4.1 变更清单（manifest）
- 文件：`{向量库持久化目录}/index_manifest.json`（与向量库一一对应；
  如 ./data/chroma_db/index_manifest.json，默认跟随 ChromaStore.persist_directory）
```json
{
  "version": 1,
  "docs": {
    "/abs/path/redis.md": {
      "doc_id": "md5(path)[:16]",
      "mtime": 1720000000.0,
      "size": 1024,
      "hash": "md5(文件内容)"
    }
  }
}
```

### 4.2 三态同步算法（IndexSync）
```
sync():
    1. 扫描当前文件（source_dirs + extensions）
    2. 对每个文件:
       旧记录 = manifest.get(path)
       - 无旧记录              → 新增：index_single + 写入清单
       - 旧记录 mtime/size 一致 → 未变：跳过
       - mtime/size 不一致     → 计算内容 MD5：
            与旧 hash 相同 → 仅更新时间戳（内容未变）
            不同          → 变更：delete_by_doc_id + index_single + 更新清单
    3. manifest 有但文件已消失 → 删除：delete_by_doc_id + 移除清单
    4. 保存清单
```

### 4.3 监听（watchdog）
- 依赖：`watchdog`（macOS 走 FSEvents，原生高效）
- `IndexWatcher(threading.Thread)`：Observer 递归监听所有 source_dirs，
  事件去抖（debounce 约 2s）后触发一次全量 `sync()`
- 全量 sync 对个人 vault 开销可忽略（stat 扫描 + manifest 比对；仅变更文件算 MD5 / 嵌入）
- 服务启动即开监听（守护线程，随进程退出）；CLI `watch` 子命令前台运行

### 4.4 接口
- `POST /v1/index/refresh`：手动触发增量同步（供 Obsidian 插件等外部调用）
- CLI：`python cli/main.py watch [--dirs ...]` 前台监听
- `RAGPipeline.index_incremental()` 包装 IndexSync

---

## 五、P2–P5 预留设计要点（实施前再细化确认）

- **P2**：`Parser` 抽象基类 + 扩展名→解析器注册表；URL 抓取用 bs4（已有依赖）；
  新端点 `POST /v1/index/url {url, format?}`；docs 类文档提取标题与正文
- **P3**：前端维护消息数组 → 请求带 history；后端 `trim_history(history, max_rounds=10, token_budget=2000)`；
  `config.generation.rewrite_query: bool = False` 启用 LLM 改写（改写后再检索）
- **P4**：索引并行用 `ThreadPoolExecutor`（加载/分块 CPU 密集，嵌入仍批量串行防爆）；
  内存 LRU 用 `functools.lru_cache` 包装磁盘缓存层；响应缓存 `src/cache/response_cache.py`
  （键=问题+top_k+rerank，TTL/开关进配置）
- **P5**：`obsidian-plugin/` 独立 TypeScript 工程（npm + esbuild）；
  `manifest.json` + `main.ts`：侧边栏视图（SSE 流式聊天）、`vault.getAbstractFileByPath`
  跳转（path = file_path 减 vault 根）、状态栏、设置面板、自动索引开关

---

## 六、扩容演进项备忘（2026-09-06，Wave 1-3 修复后新增）

> 以下为「暂不执行、达到触发条件后启动」的演进项，避免当前规模下的过度设计。
> 背景：评审发现 C1 并发风险后，已按批复采用乙方案（IndexSync 进程内互斥锁 + manifest 原子写，commit 118de22）。

### 6.1 索引写入队列收敛（原 C1-甲方案）

- **内容**：watcher 线程、`/v1/index/refresh`、IngestQueue worker 三路索引入口全部收敛到 IngestQueue 单 worker 串行执行，refresh 改「入队 + 回执」模式
- **暂缓原因**：插件 refresh 为同步等待统计返回；当前 8 文档 / 59 向量规模 sync <1s，互斥锁已消除并发风险
- **触发条件（满足任一）**：语料 ≥ 500 文档，或单次增量 sync > 10s，或出现多客户端同时触发索引
- **预估改动**：index_sync / ingest_queue / routes/index / 插件 api.ts，约 80-120 行

### 6.2 分块并行策略实测（原 S3）

- 当前保留并行分块（决策 D7）；wiki 扩容后用索引耗时对照（max_workers=1 vs 自动）实测再定

### 6.3 检索质量增强（B3 BM25 / B4 父子块）——已实现（2026-09-06，commit 319c301 / 00a7540）

- **B3 BM25 混合检索**：已实现（jieba + rank-bm25 + RRF 融合），实测 8 文档规模收益≈零，**默认关闭**（settings.yaml `hybrid: false`），扩容后翻开关并用 `make eval` 对照
- **B4 父子块召回**：已按方案 A 落地（检索时按 doc_id+heading_path 实时聚合兄弟块，零重建），当前语料 59 块仅 1 个多块章节、机制空转，**默认开启**，扩容后自动生效
- **度量基线**：`make eval`（32 条 QA：recall@5=0.9062 / mrr@5=0.7865 / hit@5=0.9375）；对照开关 `--no-hybrid` / `--no-parent`

### 6.4 公网访问加固（P0.5 全量）

- 预留接口已就绪：`config.auth.enabled` 默认 false（commit 118de22），插件已默认发送 Bearer 头，开启即生效
- 启用步骤：settings.yaml 打开 auth 并设 api_key → Tailscale（推荐，零公网暴露）或 Cloudflare Tunnel 组网 → 视暴露面补限流