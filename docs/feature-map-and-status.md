# rag-service 功能全景与完成度评估

> 日期：2026-09-11 ｜ 代码基线：工作区未提交改动（54 文件 / +3467 −971），全量测试 **282 passed / 0 failed**
> 梳理方式：逐包静态阅读（AST 抽取全部模块 API 面 + 关键模块全文精读）+ 实测校验（向量库/评测集/配置实读）
> 本文档的"完成度"以**代码是否落地 + 是否被真实调用**双标准判定，不以注释或文档自述为准。

---

## 一、一句话定位

**一个面向个人知识库的本地 RAG 服务**：把本地 vault（Obsidian 笔记 + PDF/Word/PPT/EPUB/HTML/网页）索引成向量库，提供"检索 → 重排 → 生成"的问答能力，并通过一个 Obsidian 插件作为唯一前端。所有模型推理走本地 oMLX（OpenAI 兼容 API），**全程可离线、无外部依赖**。

| 维度 | 现状 |
|---|---|
| 代码规模 | Python 9642 行 / 67 个模块 / 14 个包；插件 TS 约 2770 行 / 5 个源文件 |
| 测试 | 源码 268 个 test 函数，pytest 收集 **282 项**，全绿 |
| 数据源 | `/Users/xuhuaming/projects/obsidian/xu`（加载器硬编码排除 `wiki/`） |
| 当前语料 | manifest **10 文档 / 65 向量** |
| 评测 | 32 条 QA 数据集；基线 recall@5=0.906 / mrr@5=0.786 / hit@5=0.938 / precision@5=0.583 |
| 前端 | Obsidian 插件（React 渲染 + SSE 流式），**Web 聊天页与 CLI 已移除** |
| 运维 | 结构化 JSON 日志 + 请求指标 + 每日备份 + 评测 CLI + GitHub Actions CI |

---

## 二、系统架构分层

```
┌─────────────────────────────────────────────────────────────┐
│  前端：Obsidian 插件（obsidian-plugin/）                       │
│  chat_view.tsx(逻辑) + chat_app.tsx(React 渲染) + api.ts(HTTP)│
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP/SSE  (Bearer 可选)
┌───────────────────────────▼─────────────────────────────────┐
│  接口层  src/api/                                             │
│  app.py（中间件: CORS/限流/请求日志 + 全局异常兜底 + 认证依赖）    │
│  routes/: query · index · research · convert · sessions ·     │
│           config · archive · status                           │
│  schemas.py(14 个 pydantic 模型)  metrics.py(请求指标环)        │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  编排层  src/pipeline/                                        │
│  rag_pipeline.py ← 主流程（同步 / SSE 流式 / 异步流式 三通道）    │
│  indexer(全量) · index_sync(增量) · ingest_queue(异步队列)      │
│  watcher(文件监听) · deep_research(递归研究) · syntheses(沉淀)   │
│  export_import(归档) · backup(备份)                            │
└──────┬────────────────┬─────────────────┬───────────────────┘
       │                │                 │
┌──────▼──────┐ ┌───────▼────────┐ ┌──────▼──────────────────┐
│ 文档层       │ │ 检索层          │ │ 生成层                   │
│ document/    │ │ retrieval/      │ │ generation/             │
│ loader       │ │ retriever       │ │ generator               │
│ parsers×8    │ │ ├ 稠密(Chroma)  │ │ model_router            │
│ chunker      │ │ ├ BM25(RRF融合) │ │ history(裁剪)            │
│ to_markdown  │ │ ├ reranker      │ │                         │
│ html_to_md   │ │ ├ parent_expand │ │                         │
└──────┬───────┘ │ └ context_builder│ └────────┬────────────────┘
       │         └───────┬─────────┘          │
┌──────▼─────────────────▼────────────────────▼────────────────┐
│  基础层                                                        │
│  embedding/(oMLX 客户端 + 双层缓存)  vector_store/(Chroma 抽象)   │
│  cache/(响应缓存)  session/(会话存储)  security/(SSRF)           │
│  config.py  logging_setup.py  otel.py                          │
└───────────────────────────────────────────────────────────────┘
```

**依赖方向**：接口层 → 编排层 → (文档|检索|生成) → 基础层。`rag_pipeline` 是唯一的聚合点，所有组件在 `run_api.py` 里一次性装配并注入；路由通过模块级 `set_pipeline()` 拿到同一个 pipeline 实例。**没有依赖注入框架，没有全局可变单例之外的服务定位**——pipeline 是事实上的"应用上下文"。

---

## 三、功能模块清单

### 3.1 接口层 `src/api/`

| 模块 | 作用 | 关键点 |
|---|---|---|
| `app.py` | 应用装配 | 两个中间件（限流在内、请求日志在外，保证 429 也进日志与指标）、`verify_bearer` 认证依赖（默认关）、全局异常兜底只回 `{"detail":"服务器内部错误"}` |
| `routes/query.py` | 问答 | `POST /v1/query`（同步，`asyncio.to_thread` 防阻塞事件循环，回 `X-RAG-*-Ms` 头）；`POST /v1/query/stream`（SSE） |
| `routes/index.py` | 索引 | 全量 `POST /v1/index`；网页 `POST /v1/index/url`（前置 SSRF 校验）；增量 `POST /v1/index/refresh`；异步队列 `POST /v1/index/async` + `GET /v1/index/jobs[/{id}]` + `POST .../cancel` |
| `routes/research.py` | 深度研究 | `POST /v1/research` |
| `routes/convert.py` | 文档转 MD | `POST /v1/convert/to-md`（统一入口）、`/convert/html2md`（兼容旧版，行为已对齐）、`GET /v1/convert/formats` |
| `routes/sessions.py` | 会话管理 | 完整 CRUD + `append` / `truncate`（撤回重发）/ `rename` / `cleanup` / `export`；role 白名单校验 |
| `routes/config.py` | 配置热更新 | `GET/POST /v1/config`；**白名单只允许改 `retrieval/generation/performance/syntheses/routing`**，`auth`/`security` 不可远程改；读-改-写全程持锁 |
| `routes/archive.py` | 导出导入 | `GET /v1/export`（ZIP 归档）、`POST /v1/import?mode=merge\|replace`；导出保留最近 5 份自动清理 |
| `routes/status.py` | 状态 | `GET /v1/status`（向量数 + 当前摄入任务 + 请求指标 + rerank 缓存命中率）、`GET /v1/health` |
| `metrics.py` | 请求指标 | 内存环形缓冲，供 `/v1/status` 展示最近请求与错误数 |

### 3.2 文档层 `src/document/`

| 模块 | 作用 |
|---|---|
| `loader.py` | 递归加载 + URL 抓取；**硬编码排除 `wiki/` 目录**（决策 D9）；提取内嵌图片落地 `data/attachments` |
| `parsers/` | 注册表模式，8 个解析器：`markdown`（含 frontmatter）、`pdf`（PyMuPDF）、`docx`、`pptx`、`epub`、`html`、`text` + 抽象基类 |
| `chunker.py` | 按 Markdown 标题层级切分（`heading` 策略）或定长切分（`fixed`）；**记录 `line_start/end_line` 与 `heading_path`** 供行级引文跳转 |
| `to_markdown.py` | 统一「任意格式 → Markdown」；`prefix_title(replace_existing)` 支持显式标题覆盖 |
| `html_to_markdown.py` | 高质量 HTML→MD：保留表格/列表/代码块，**把 ASCII 架构图套围栏保留**（这是本项目一个专门修过的痛点） |

### 3.3 检索层 `src/retrieval/` —— 本项目技术含量最高的部分

`retriever.retrieve()` 的完整链路：

1. **嵌入查询** → 2. **召回**（稠密 Chroma cosine，或 `hybrid=true` 时稠密+BM25 双路 **RRF 融合**）→ 3. **沉淀降权**（`synthesis_weight`：把 `syntheses/` 历史问答块乘系数，抑制"以问代答"霸榜）→ 4. **精排**（reranker 只处理候选窗 TopN，带 LRU+TTL 缓存）→ 5. **二次阈值过滤**（`rerank_threshold`，语义与 cosine 阈值分离）→ 6. **父子块扩展**（命中"小块"实时聚合同 `doc_id+heading_path` 的兄弟块喂上下文，来源列表仍展示小块）→ 7. **token 感知上下文装配**（`context_builder`，替代旧的字符硬截断）。

设计要点：`recall_candidates`（召回窗，默认 30）与 `rerank_candidates`（精排池，默认 8）分离——因为本地 rerank 一次 30 对耗时可达 ~6s，收窄精排池是延迟与保真的折中。

### 3.4 生成层 `src/generation/`

| 模块 | 作用 |
|---|---|
| `generator.py` | 同步 / 同步流式 / 异步 / 异步流式 四种生成；`answer_style` 三档；**追问改写**（`rewrite_query`，默认关） |
| `model_router.py` | 任务→模型路由：`chat` / `rewrite` / `research_subqueries` 可各配模型，空值回退默认 |
| `history.py` | 多轮历史：最近 N 轮 + token 预算从旧到新裁剪 |

### 3.5 编排层 `src/pipeline/`

| 模块 | 作用 | 状态 |
|---|---|---|
| `rag_pipeline.py` | 主流程编排；三通道查询；问答沉淀；分阶段耗时统计 | ✅ 完整 |
| `indexer.py` | 全量索引：加载→分块（可并行）→嵌入（分批）→入库；支持取消与进度回调 | ✅ 完整 |
| `index_sync.py` | 增量索引：manifest + **三态算法**（新增/变更/删除，mtime+size 快筛 → MD5 精判）；进程内互斥锁 + 原子写 | ✅ 完整 |
| `ingest_queue.py` | 后台摄入队列：单 worker 串行、磁盘 JSON 持久化（重启可恢复）、进度/取消 | ✅ 完整 |
| `watcher.py` | watchdog 文件监听（macOS FSEvents），去抖后触发增量同步 | ✅ 完整 |
| `deep_research.py` | 递归多轮研究：拆子查询 → 并行检索 → 汇总 → 生成追问，**新信息增益停止**，轮次上限 5 | ✅ 完整 |
| `syntheses.py` | 问答沉淀为 vault 根 `syntheses/*.md`，**该目录会被索引**（形成知识复利） | ✅ 完整 |
| `export_import.py` | ZIP 归档：配置+文档清单+向量库+变更清单，`merge`/`replace` 两种模式 | ✅ 完整 |
| `backup.py` | 每日自动备份会话为 JSON，保留最近 N 份，下界钳制 + 原子写 | ✅ 完整 |

### 3.6 基础层

| 模块 | 作用 |
|---|---|
| `embedding/client.py` | oMLX 客户端：OpenAI 兼容，同步+异步，**带重试**（可重试错误识别 + 退避） |
| `embedding/embedder.py` | **双层缓存**：内存 LRU（默认 4096）+ 磁盘缓存；批量嵌入 |
| `vector_store/` | `base.py` 抽象 + `chroma_store.py` 实现；支持按 `doc_id` 增删查（增量索引的基础） |
| `cache/response_cache.py` | 相同问题响应缓存（LRU+TTL），仅对无历史单轮查询生效 |
| `session/store.py` | 会话 JSON 持久化；`_mtime` 容错、`truncate`/`cleanup` 下界钳制 |
| `security/url_safety.py` | SSRF 防护：拒内网/回环/链路本地/云元数据/未指定地址；`allow_private_urls` 开关（默认拒） |
| `config.py` | pydantic 配置 + 单例 `ConfigManager`（原子写回） |
| `logging_setup.py` | 结构化 JSON 日志（上海时区）+ `request_id` 跨模块关联 + 文件轮转 |
| `otel.py` | 可选 OpenTelemetry，设 `OTEL_EXPORTER_OTLP_ENDPOINT` 即启用 |

---

## 四、核心业务流程

### 流程 1：索引（三个入口，殊途同归）

```
[入口A] watcher 检测文件变化 ──┐
[入口B] POST /v1/index/refresh ─┼──► IndexSync.sync()
[入口C] POST /v1/index/async ──┘      │  manifest 三态比对
   └─► IngestQueue(单 worker)          ├─ 新增 → index_single()
                                       ├─ 变更 → delete_by_doc_id() + index_single()
                                       └─ 删除 → delete_by_doc_id()
                                              │
                                   加载(loader) → 分块(chunker) → 嵌入(embedder) → 入库(Chroma)
                                              │
                                       bm25_index.invalidate()  ← 语料变了，下次查询重建稀疏索引
```
三个入口**共享同一个 `IndexSync` 实例**（在 `run_api.py` 里注入），保证 manifest 不分裂；并发安全由 `IndexSync` 内部互斥锁保证。

### 流程 2：问答（前端唯一链路是 SSE 流式）

```
插件 ChatView ──POST /v1/query/stream {question, history, top_k, use_rerank}──►
  pipeline.query_stream_async()
    ├─ [可选] 追问改写（LLM 把"那它呢"→独立问题）
    ├─ event: phase "检索中"
    ├─ retrieve_with_context()  → asyncio.to_thread（Chroma 是同步的，不阻塞事件循环）
    │      嵌入 → 召回(±BM25/RRF) → 沉淀降权 → 精排(±缓存) → 阈值过滤
    │      → 父子块扩展 → token 预算装配上下文
    ├─ event: sources  ← 先于正文发出，插件立即渲染引用
    ├─ event: phase "生成中"
    ├─ event: chunk ×N ← 逐块流式
    ├─ event: timing   ← 分阶段耗时（先于 done，供前端提前展示）
    ├─ event: done     ← 完整回答
    └─ 落盘沉淀 syntheses/*.md（仅当有来源）
```
缓存命中时（仅同步通道）直接返回，耗时从 ~6s 降到 ~0.002s。

### 流程 3：增量同步的三态判定（`index_sync`）

```
扫描当前文件 → 与 manifest 比对
  无记录            → 新增：索引 + 写清单
  mtime/size 一致   → 跳过（快筛，不读文件内容）
  mtime/size 不一致 → 算内容 MD5：
                        hash 相同 → 只更新时间戳（内容其实没变）
                        hash 不同 → 删旧块 + 重索引 + 更新清单
  manifest 有但文件没了 → 删块 + 移出清单
```

### 流程 4：导出 / 导入（灾难恢复）

导出：配置摘要 + 文档清单 + **整个向量库目录** + 变更清单 + 会话 + 附件 → 单个 ZIP。
导入：`merge`（合并）或 `replace`（重建），**导入后需重启服务**以重新加载配置与集合。

---

## 五、完成度评估

### 5.1 后端：功能层面基本完整

| 能力 | 状态 | 说明 |
|---|---|---|
| 多格式解析 | ✅ | 8 种格式 + URL 抓取，均有解析器与测试 |
| 全量 / 增量 / 异步索引 | ✅ | 三条链路齐全，含取消与进度 |
| 检索链路（混合+重排+父子块+降权） | ✅ | 均已实现并可配置 |
| 问答（同步+流式） | ✅ | SSE 事件协议完整（phase/sources/chunk/timing/done/error） |
| 多轮对话 | ✅ | 历史裁剪 + 可选改写 |
| 会话管理 | ✅ | 服务端 JSON CRUD + 撤回重发 |
| 配置热更新 | ✅ | 白名单 + 校验 + 持锁 + 原子写 |
| 文档转 Markdown | ✅ | 统一入口 + 兼容端点 |
| 深度研究 | ✅ | 递归多轮 + 增益停止（但前端未接，见 5.3） |
| 导出导入 | ✅ | ZIP 归档 + merge/replace |
| 认证 / 限流 | ⚠️ 预留 | 代码完整，**默认关闭**（`auth.enabled=false`、`rate_limit.enabled=false`） |
| 可观测性 | ✅ | JSON 日志 + request_id + 请求指标 + 可选 OTel |
| 备份 | ✅ | 每日备份**会话**（不含向量库，见 6.2） |
| 检索质量评测 | ✅ | 32 条 QA + 4 指标 + 基线对比 + LLM-as-judge |
| CI | ✅ | GitHub Actions 跑 pytest + 构建插件 |

### 5.2 Obsidian 插件：功能丰富（原"类型检查真空"第四轮已修）

> 第四轮已修：`tsconfig.json` 此前只 `include` 了 `.ts`、且未配 `jsx`，导致 `.tsx`（聊天面板主体）从未被类型检查，`npm run build` 只做 esbuild 打包不做类型校验——真实类型错误（如 `chat_app.tsx` 未导入 `SourceInfo`）被静默放过。现已纳入 `.tsx` + `jsx: react-jsx`，补装 `@types/react*`，`build` 前置 `tsc --noEmit`，当前 **tsc 0 error**。

已实现（用户可见）：

- **聊天面板**：SSE 流式、引用可展开并点击跳转笔记、行级高亮、用户消息编辑重发、助手消息复制/停止生成/进度条/已用时、空态建议问题、错误边界（任一块出错显示可见错误而非整块空白）
- **命令（4 个）**：打开聊天面板、触发增量索引、检查服务状态、转换当前文件为 Markdown
- **右键菜单**：对 html/htm/pdf/docx/pptx/epub/txt 文件加"转换为 Markdown (.x → .md)"
- **状态栏**：30s 轮询显示 `RAG: 🟢 N 向量 / 🔴 离线`，可点击开面板
- **设置页**：服务地址/API Key/检索参数/使用模式四档预设（simple/standard/deep/custom）/自动索引/自动转换/**服务端配置区（20+ 项热更新）**/会话管理（导出全部、清理保留 N 条）/服务监控

### 5.3 ✅ 后端已实现但插件未接通的能力（第四轮已接通，开关控制）

这是此前最值得注意的"完成度断层"——后端有、插件没接，等于用户用不到。
**第四轮已全部接通**，入口由设置里的「高级功能」开关控制（默认关，保持界面简洁）：

| 后端能力 | 端点 | 插件现状（第四轮后） | 开关 |
|---|---|---|---|
| 深度研究 | `POST /v1/research` | ✅ 命令「深度研究」+ 设置页按钮；报告写入 `RAG 导出/深度研究-<日期>.md` 并打开 | `features.research` |
| 异步摄入队列 + 进度/取消 | `POST /v1/index/async`、`GET /v1/index/jobs`、`POST .../cancel` | ✅ 任务面板（Modal）：列表 + 进度 + 取消 + 提交 | `features.asyncJobs` |
| 全量索引 | `POST /v1/index` | ✅ 命令「全量重建索引」（`rebuild=true`） | `features.indexTools` |
| 网页索引 | `POST /v1/index/url` | ✅ 命令「索引网页到知识库」 | `features.indexTools` |
| 项目导出/导入 | `GET /v1/export`、`POST /v1/import` | ✅ 导出 ZIP 到 `RAG 导出/归档-<日期>.zip`；vault 内右键 `.zip` 可导入 | `features.archiveTools` |
| 同步问答（非流式） | `POST /v1/query` | ⛔ **有意不接**：对话场景流式严格更优，该端点留给脚本/程序化调用 | — |

设计取舍：
- **为什么用开关而不是直接铺开**：聊天 + 自动索引是主干，这四项是低频运维/研究操作。
  全铺开会把命令面板与设置页淹掉。
- **为什么命令用 `checkCallback` 而不是动态注册/注销**：开关关闭时直接返回 `false`，
  命令在命令面板里就不出现，等价于"按开关显隐"，且不必管理注册生命周期。
- **为什么归档导入走"vault 内右键 .zip"**：Obsidian 没有原生文件选择器；而导出恰好
  也写进 vault，两者形成闭环。

### 5.4 明确的代码缺陷与待办

**A. 本次梳理中顺手修掉的 3 处遗留缺陷**（均为第三轮漏网）：

| 位置 | 问题 | 修复 |
|---|---|---|
| `routes/index.py:63` | `index_documents` 的 `detail=str(e)` 泄露内部错误（同文件另两处已修，此处被回滚漏掉） | 改 `logger.exception` + 通用 detail |
| `routes/research.py:58` | `detail=str(e)` 泄露——**该路由第三轮完全未覆盖** | 补 logger + 通用 detail |
| `routes/convert.py:151` | `/convert/html2md` 的 title 覆盖缺 `replace_existing=True`，与 `/convert/to-md` 口径不一致，显式 title 静默失效 | 补 `replace_existing=True` |

（已跑受影响测试：**86 passed**）

**B. 检索参数默认值存在"多份真理"**（✅ 第四轮已修）：

原先 `Retriever.__init__` 自带一份字面量默认值，与 `RetrievalConfig` 的字段默认值**相反**：

| 参数 | `RetrievalConfig` 字段默认值 | 旧的 `Retriever.__init__` 字面量 | 部署覆盖（`settings.yaml`） |
|---|---|---|---|
| `hybrid` | `False` | **`True`** | `True` |
| `parent_expansion` | `True` | **`False`** | `True` |
| `rerank_candidates` | `8` | **`12`** | `8` |
| `synthesis_weight` | `0.85` | **`1.0`** | `0.5` |

修法：全部构造参数默认 `None` → 回落 `RetrievalConfig()` 的**字段默认值**。
刻意回落到 schema 默认值而非 `config_manager` 已加载的配置——后者会让构造结果
取决于"此刻配置是否已加载"，同一段代码在服务内与单测/脚本里行为不同。

修正后的参数来源只有两层（不再有三份）：
1. **代码默认** = `RetrievalConfig` 字段默认值（唯一来源）；
2. **部署覆盖** = `config/settings.yaml`（`run_api` / `run_eval` 显式读取并传入）。

> ⚠️ **修这个改动立刻暴露了一个被旧默认值掩盖的真 bug**：`retrieve()` 里第三处
> syntheses 判定 `if not (_is_syntheses_path(_result_path(rr)) and self.synthesis_weight <= 0)`
> 少了另两处的 `fp and` 守卫。由于 `and` 先求值左侧，只要有一个检索结果的
> metadata 不带 `file_path`，且走了 rerank 分支，`retrieve()` 就整条抛
> `AttributeError`。旧默认 `synthesis_weight=1.0` 让整个降权分支短路、永不进入，
> 因此线上（`synthesis_weight=0.5` 但 metadata 总带路径）恰好没炸。
> 已在 `_is_syntheses_path` 内统一兜底空路径。

**C. 插件侧确定性问题**（✅ 第四轮已修）：

| 问题 | 证据 | 状态 |
|---|---|---|
| `SourceInfo` 未导入却使用 | `chat_app.tsx` 用 `SourceInfo`（286、539 行），import 只到 19 行 | ✅ 补 `import type { SourceInfo }` |
| `.tsx` 完全不参与类型检查 | `tsconfig.json` `include` 只含 `*.ts`、无 `jsx`；build 无 `tsc` | ✅ 加 `jsx: react-jsx` + `include: **/*.tsx`；`build` 前置 `tsc --noEmit` |
| **React 类型包根本没装**（更深一层） | `react` 在 dependencies，但 `@types/react` 不在 devDependencies | ✅ 补 `@types/react`/`@types/react-dom`；此前即便打开 tsc 也有 234 条噪音错误 |
| `stopGeneration` 是 `private` 却被 React 调用 | `chat_app.tsx:244` 调 `view.stopGeneration()` | ✅ 改为 public（类型检查才看得见的问题） |
| CSS 类缺失 | `rag-plain-stream`/`rag-react-ui`/`rag-ui-error`/`rag-ui-retry` 在 `styles.css` 0 命中 | ✅ 补齐定义（`rag-plain-stream` 缺 `pre-wrap` 会让流式换行被折叠） |
| 死方法 | `api.ts` 的 `htmlToMarkdown` 无调用方 | ✅ 删除 |
| `citationsDock` 残留 | `chat_view.tsx` 保留句柄，实际已被 React `CitationsBlock` 取代 | ✅ 删除（含 `BubbleHandles` 字段与构建处） |
| `settings.ts` 死分支 | `mode !== "custom"` 在 else 分支恒真（tsc TS2367） | ✅ 去掉三元判断 |
| `onPhase` 空实现 | `chat_view.tsx:530-532` 主动忽略服务端 `phase` 事件 | ⏸ **有意为之**，非缺陷；服务端事件未被利用，属可选优化 |
| README 文档滞后 | `obsidian-plugin/README.md` 仍写 `chat_view.ts` | ✅ 重写（含新目录结构、高级功能、typecheck 说明） |

类型检查结果：修复前 **22 条错误**（含 `@types/react` 缺装导致的 234 条噪音）→ 修复后 **0 条**。

**D. 第三轮报告中已记录、仍待决策的 8 项**（方案已就绪，等拍口径）：
DNS rebinding TOCTOU、`rebuild=True` 空目录不清空、全局 `print()`→logging 收尾、两套 token 估算口径、ZIP 解压体积上限（zip bomb）、全局 body 限制、备份口径（"会话" vs "归档"）、未评审面。

**E. 未评审面（第四轮范围）**：`src/evaluation/*`、`src/pipeline/deep_research.py`、`obsidian-plugin` TS 侧。本文档 5.3/C 两节即为 TS 侧的初步结论。

**F. 第五轮（收口轮）修掉的缺陷**（✅ 全部已修 + 回归测试）：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| R5-1 | **高** | 阻塞端点写成 `async def` 直接跑同步代码 → **冻结整个事件循环**。实证：stub 内阻塞 2s，`/v1/health` 的发起被从 0.3s 拖到 2.077s | ✅ 9 个端点改 `def`（Starlette 线程池）；`/v1/import` 必须 `await file.read()` 故显式 `asyncio.to_thread` |
| R5-2 | 中 | `parse_sub_queries` 用字符集 `lstrip` → 吃掉合法子问题开头的数字（`3D 渲染管线…` → `D 渲染管线…`；`2025 年的新变化` → `年的新变化`），静默污染检索 | ✅ 改 `_LIST_PREFIX_RE`：数字须紧跟分隔符才算编号 |
| R5-3 | 中 | `_retrieve_all` 用 `ex.map` 无逐条容错 → 任一子查询检索失败，整次深度研究 500 | ✅ 内部 `_one()` 包裹，失败记 warning 返回 `[]` |
| R5-4 | 中 | `run_generation_eval` 的检索裸调用（检索版有 `try`）→ 生成评测一条失败即整体中断 | ✅ 补齐同口径 `try/except + continue` |
| R5-5 | 中 | `ResearchRequest.max_rounds` 声明 `le=10`，`DeepResearch` 硬夹到 5 → "要 8 轮得 5 轮"且无提示 | ✅ schema 对齐 `le=5` |
| R5-6 | 中 | `evaluation/judge.py`、`retrieval/bm25_index.py` 用 `logging.getLogger` → 绕过项目日志配置（无 request_id / 无 JSON / 可能不落盘） | ✅ 统一 `get_logger` + 结构守卫测试防回退 |
| R5-7 | 中 | 上下文预算第二份真源：`retrieve_with_context` / `run_eval` 写死 `4000`，与 `RetrievalConfig.context_token_budget` 并存 | ✅ 回落配置字段（同 Round 4 的构造参数策略） |
| R5-8 | 低 | `compare_with_baseline`：基线有 `metrics` 无 `meta` → 调用方 `.get()` 抛 `AttributeError` | ✅ 结构防御 + `baseline_meta` 恒为 dict |
| R5-9 | 低 | `import_archive` 的 `mode` 未校验 → `"replcae"` 静默按 merge 走（用户以为已重建） | ✅ 库层补守卫 |
| R5-10 | 低 | `import_archive` 先 `extractall` 再校验 `meta.json` → 无效包也完整落盘 | ✅ 先查 `namelist` 再解包 |
| R5-11 | 低 | `/v1/research` 丢弃管道算出的 `rounds` | ✅ 响应新增 `rounds` 并在插件报告中展示 |
| R5-12 | 低 | `EvalDataset.load` 非 dict 项抛 `AttributeError`；`gold_docs` 元素类型未校验 | ✅ 明确 `ValueError` |
| R5-13 | 低 | `syntheses`：`_short_hash` 死代码；`isinstance(score, float)` 使 int 分数整段消失 | ✅ 删死代码；接受 `(int,float)` 且排除 `bool` |

> **D5-1 已于第七轮关闭**（原问题：`precision@k` 块级不去重、分母 k，与 `recall@k` 文档级去重、分母 gold 文档数**混名并列**，两级口径不同却看不出哪级在退化）。处置见 H 节与第六节第 11 条。
> 其余记录项：zip 解包体积上限、`stats["files"]` 口径、`index.py` 队列端点毫秒级阻塞、`RAGPipeline.__init__` 字面量默认、`syntheses` 的 `.tmp` 落在被监听目录、`deep_research` 按字符截断——详见 `.workbuddy/reports/2026-09-11-repair-plan-v5.md` 第六节。

**G. 第六轮（并发与收尾）修掉的缺陷**（✅ 全部已修 + 回归测试；详见 `2026-09-11-repair-plan-v6.md`）：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| R6-1 | **中** | `Retriever.last_timings` 存实例属性，而检索经 `asyncio.to_thread` 多线程并发 → **A 请求读到 B 请求的分阶段耗时**（实证 2 并发稳定 1/2 串号），污染 `X-RAG-Retrieve-Ms` 与 `request_timing` 日志 | ✅ 改 `threading.local()`；异步路径把「检索+读耗时」放进同一个 `to_thread` 闭包 |
| R6-2 | **中** | 流式收尾副作用（问答沉淀 / 耗时日志）位于**最后一次 `yield done` 之后** → 客户端收到 done 即断开时 Starlette `aclose()` 生成器，`GeneratorExit` 使其永不执行（实证复现，两者双失） | ✅ 收尾上移到最后一次 yield 之前（`query_stream` + `query_stream_async`） |
| R6-3 | 低 | 嵌入缓存磁盘读写 `open()` 缺 `encoding` → 非 UTF-8 locale 下异常被 `except Exception: pass` 吞掉，**缓存静默全 miss**（每次都打嵌入 API），零日志 | ✅ 补 `encoding="utf-8"` + `ensure_ascii=False` |
| R6-4 | 低 | `RAGPipeline._index_sync` 惰性创建**无锁** → 并发入口各建一个 `IndexSync`，而其互斥锁是**实例级**的，两个实例互不排除，manifest 竞争照旧（生产由 `run_api.py` 预注入故不触发） | ✅ 双检加锁 |
| R6-5 | **中** | **D5-5 关闭**：`RAGPipeline.__init__` 的 `max_context_tokens` / `strict_sources` / `save_syntheses` 是字面量默认值，与配置模型构成第二份真源（数值当前恰好一致纯属巧合） | ✅ `None` → 回落 schema 字段默认（与 Round 4/5 同策略） |
| R6-6 | 低 | `/v1/sessions/cleanup` 路由 `keep` 下界钳 0，store 内部钳 1 → 传 0 时**实际留 1 条却回报 `keep: 0`** | ✅ 路由下界对齐为 1 |
| R6-7 | 低 | **D5-8 关闭** + 过期注释：`sessions.py` 未用导入 `Any`/`Dict`；docstring 称"会话存储是 sqlite 阻塞 I/O"，实为 `data/conversations/*.json` 文件 I/O | ✅ 一并修正 |

> **第六轮未改（需决策 / 记录）**：
> - **D6-1**：~~异步流式路径 usage 竞态~~ → **✅ 第八轮已修**（A 方案：加法式 `usage_out` 契约，见下文 I 节与第六节第 12 条）。
> - **D6-2（安全）**：CORS `allow_origins=["*"]` + 默认 `auth.enabled=false` → 用户浏览器中**任意网页**可 `fetch('http://127.0.0.1:8000/v1/query')` 向知识库提问并读回答案，也可读 `/v1/status`、`/v1/sessions`。
> - **D6-3**：~~`IngestQueue` 进度每次 tick 全量重写 jobs JSON、`_jobs` 无上限、`get()/list()` 返回活对象致路由锁外读可能撕裂快照~~ → **✅ 第九轮已修**（限频落盘 / 终态裁剪 / 快照副本 / 状态迁移全入锁，见下文 J 节与第六节第 13 条）。

> **第六轮确认"无问题"**：`security/url_safety.py` 的 SSRF 防护实测扎实（含 `http://[::ffff:169.254.169.254]/` 等 IPv4-mapped IPv6 绕过形态，6/6 拦截）；`api/app.py` 两个中间件、`cache/response_cache.py`、`otel.py`、`config.py`、`metrics.py`、`session/store.py` 逐项检查无缺陷；全仓静态扫描无 TODO/FIXME、无可变默认参数、无裸 `except: pass`、无 async 内 `time.sleep`。

**H. 第七轮（指标口径 + 分块/装配/加载）修掉的缺陷**（✅ 全部已修 + 回归测试；详见 `2026-09-11-repair-plan-v7.md`）：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| D5-1 | **中** | `precision@k`（块级、分母 k）与 `recall@k`（文档级、分母 gold 文档数）**混名并列** → 同一结果集上两级可给出互斥印象，无法判断哪级在退化。实测 32 条中 **31 条**的 top-5 含同文档重复块（`redis-001~003` 五个块全来自同一文档） | ✅ 拆为 `doc_*` / `chunk_*` 两套键名；新增 `doc_precision@k`（文档集合纯度）与 `chunk_recall@k`（gold 块覆盖率，分母取向量库真实块数）；`chunk_recall` 分母不可得时返回 **None 而非 0.0**；基线**已真跑重建**（oMLX 实测 1m14s），旧基线归档为 `baseline-v1-recall-only.json` |
| R7-1 | 中 | `chunker._split_long_content` 的**最后一个子块**漏传 `start_line`/`end_line` → 落默认 0 → 下游 `start_line or None` 转成 None，该块**行级引文静默消失**（同章节其余子块均有行号） | ✅ 补传行号，全部子块一致 |
| R7-2 | 低 | `parent_expander` 兄弟块排序键只有 `chunk_index`，而长章节的全部子块**共享同一个 `chunk_index`** → 子块先后取决于 `vector_store` 返回顺序（非任何契约） | ✅ 排序键补 `sub_index`（缺省 0，与未切分块等价） |
| R7-3 | 低 | `loader` 的 `metadata.update(parsed.metadata)` 排在结构性字段**之后** → 笔记 frontmatter 可覆盖 `file_path`/`file_name`（实测 frontmatter 写 `file_path: /etc/passwd` 即污染 metadata） | ✅ 改为解析器元数据先行、结构性字段后置覆盖；当前唯一消费方是 `images`/`title`，故**无现存影响**，属防御性加固 |

> **第七轮确认"无问题"**：`chunker.py` 的围栏代码块状态机、表格转散文、heading 栈维护；`loader.py` 的 SSRF 前置校验、附件落盘、隐藏文件/目录过滤；`parent_expander` 的预算裁窗与 `hit_pos < 0` 降级 —— 逐项核对无缺陷。
>
> **第七轮观察（非缺陷，仅记录）**：top-5 同文档重复率 31/32；但实测 `redis-001` 的 5 个结果分属 **5 个不同 `heading_path`**，父块扩展后来源数仍为 5、上下文 587 tokens —— 查询本身即 Redis 专题，命中正确文档的不同小节属正常行为。**是否引入按文档去重 / MMR 提多样性属产品取舍，本轮未改。**


**I. 第八轮（D6-1 usage 竞态 + 队列端点 + 解包防护）修掉的缺陷**（✅ 全部已修 + 回归测试；详见 `2026-09-11-repair-plan-v8.md`）：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| D6-1 | 中 | `OMLXClient.last_chat_usage` / `Generator.last_usage` 共享单例属性：异步流式路径 client 的 usage 写点（流末块）与读取点之间存在挂起窗口，并发请求覆盖共享属性 → **`timing.usage` 报成别的请求的 token 数**（门控桩实证稳定复现；并发越多越明显，永不报错） | ✅ 加法式 `usage_out` 契约：client(4)/generator(4) 透传，pipeline(3) 消费点改读局部 holder；共享属性保留但降级为诊断用途；门控桩竞态用例转回归 |
| R8-2 | 低 | `/v1/index` 队列四端点（submit/list/get/cancel）为 `async def` 直跑同步 sqlite/文件 IO（毫秒级），违反 Round 5 路由规则 | ✅ 改 `def`（Starlette 线程池）；守卫测试并入 `_HEAVY_ENDPOINTS` 参数化清单 |
| R8-3 | 低 | `import_archive` 对 zip 无解包上限 → 恶意/损坏归档可借解压炸弹耗尽磁盘 | ✅ 解包前按清单头校验解压总量（2 GiB）与条目数（20 万）上限，超限拒绝；上限为模块级常量可调 |

> **第八轮复查记录**：`deep_research.py`（轮次停止 / 子查询去重 / 按字符截断为已记录项）与 `model_router.py`（原子替换 / 回退）核对无新缺陷。`history.py` 一个低危观察（**未改**）：token 预算裁剪逐条 `pop(0)`，可能把一轮从中间裁开，使历史以 assistant 消息开头——本地 oMLX 容忍该形态，且"至少保留最近 1 轮"的兜底保证最后一问完整；若将来接入对消息交替有严格要求的上游，应改为按轮（2 条）对齐裁剪。


**J. 第九轮（D6-3 队列治理 + 截断口径 + 统计口径）修掉的缺陷**（✅ 全部已修 + 回归测试；详见 `2026-09-11-repair-plan-v9.md`）：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| D6-3a | 中 | `IngestQueue.update_progress` 每个进度 tick 全量重写 jobs JSON——全量索引时每文档一次磁盘重写，任务越大磁盘越热 | ✅ 限频落盘（`PROGRESS_PERSIST_INTERVAL=1.0s`）：内存态每次都更新（API 读内存，零滞后感知），磁盘最多滞后 1s；任务收尾必落盘 |
| D6-3b | 中 | `_jobs` 无上限：终态任务跨重启无限累积，持久化文件单调膨胀 | ✅ `max_finished_jobs`（默认 100）：提交/完成/加载三个时机裁剪最旧终态任务，running/queued 永不裁剪 |
| D6-3c | 中 | worker 在锁外改 `job.status/result/finished_at`，且 `get`/`list` 返回活引用——API 线程可读到撕裂中间态，调用方可误改内部对象 | ✅ 双管齐下：worker 状态迁移全部入锁；公开查询返回快照副本（`cancel` 改内部持锁查找） |
| R9-2 | 中 | `deep_research._build_context` 对拼接整串按**字符**硬截——拦腰截断最后一条来源，且与主问答链路的 token 预算口径不一致 | ✅ 删除劣化复刻，改调检索层 `build_context`（token 感知、整块保留、行边界截断）；参数 `max_context_length`（字符）→ `max_context_tokens`（None 回落 `RetrievalConfig().context_token_budget` 单一真源） |
| R9-3 | 低 | `import_archive` 的 `stats["files"]` 统计**目标目录全量**文件数而非导入量——merge 模式下导入量被放大 | ✅ `_merge_copy` 返回实际复制数，向量库 + 会话 + 附件三处累加（键名不变，语义修正） |
| R9-4 | 低 | syntheses 临时文件落在被监听目录且非点前缀——写盘中断的孤儿 `.tmp` 会出现在 Obsidian 文件列表 | ✅ 临时文件加 `.` 前缀（Obsidian 隐藏点文件）；索引层面本就无害（加载器扩展名白名单 + watcher 2s 去抖 ≫ 毫秒级写入） |

> **第九轮确认"无害"**：syntheses `.tmp` 落在被监听目录——核实加载器按解析器扩展名白名单过滤（`.tmp` 不索引）且 watcher 去抖 2s 远大于毫秒级写入，索引层面无风险，仅孤儿文件观感问题（R9-4 顺带解决）。

**K. 第十轮（低危记录项清零）修掉的缺陷**（✅ 全部已修 + 回归测试；详见 `2026-09-11-repair-plan-v10.md`）：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| R10-1 | 低 | `trim_history` token 裁剪逐条 `pop(0)`，可裁出「以 assistant 开头」的轮中孤儿（Round 8 记录项） | ✅ 轮对齐：丢 user 时连同同轮 assistant 成对丢（剩余 ≥2 条且下一条恰为 assistant 时）；奇数畸形历史退化为旧行为；「至少保底最近 1 轮」不变 |
| R10-2 | 低 | 解码链 `big5` 分支不可达（gb18030 字节空间覆盖任意双字节序列，Round 4 记录项），留在链里误导读者 | ✅ 移除死分支（`utf-8 → gb18030 → latin-1`），**行为完全不变**；「不引入探测库」的取舍升格为 §6 第 14 条；round4 锁定用例注释同步 |
| R10-3 | 低 | 转换接口不回传图片元数据：pptx 图片形状被**静默丢弃**，epub 内嵌图片无法枚举（Round 4 记录项） | ✅ `ConvertResult.images`（pptx：{slide,caption,ext,data_base64}，caption 语义与索引路径一致；epub：{src,ext,data_base64}，src 对应正文引用）；`/v1/convert/to-md` 响应加 `images` 字段 + 8MiB 聚合护栏（超限去 data 保元数据）；加法式，旧消费方无感 |

> **第十轮口径说明**：R10-3 只做「元数据 + 数据回传」，**不改 markdown 正文**（pptx 正文仍无图片语法，与索引路径口径一致：正文不含图片内容、元数据单列）。插件侧消费（另存附件 / 改写 `![](src)` 引用）留待插件实测轮按需接入。

**L. 第十一轮（插件静态审查 + E2E 契约实测）修掉的缺陷**（✅ 全部已修；详见 `2026-09-11-repair-plan-v11.md`）：

| 编号 | 级别 | 问题 | 处置 |
|---|---|---|---|
| P11-1 | 中 | 插件 `autoIndexOnSave` 关闭后不生效：`vault.on(...)` 处理器无法单独注销，关闭只翻标志位，保存笔记仍触发增量索引直到插件重载 | `debouncedRefresh` 入口自查开关 |
| P11-2 | 中 | 插件流式收尾跨会话串号：`finalize` 读 `activeSessionId`/`history` **当前值**，流式中途切换会话会把回答写进别的会话（服务端+本地双污染） | `ensureSession` 返回 id、`sessionIdPromise` 局部持有贯穿收尾；本地 history 推送加会话守卫 |
| P11-3 | 低 | 插件 `saveServerControls` 用 `\|\|` 兜底吞掉合法 0 值（阈值 0/TTL 0/轮数 0） | `finiteNum` 护栏（仅 NaN/undefined 走 fallback），E2E 含回归断言 |

> **第十一轮运行期验证（可自动化部分）**：新增 E2E 契约测试（`obsidian-plugin/scripts/e2e_server.py` + `e2e-contract.mjs`）：插件**真实 api.ts** 打包后对真实 FastAPI 服务（stub pipeline + 真实会话/队列 + 配置副本防污染）跑全部 HTTP 调用面，**30 项断言全绿**——含 SSE 全事件序列、队列取消、归档往返、`timing.usage` 透传（Round8 usage_out 契约的端到端验证）。`tsc --noEmit` 0 error。**Obsidian GUI 内的人工验证**（装包/问答/引用跳转/进度条）仍需用户按报告第五节清单执行。


---

## 六、关键设计取舍备忘（决策记录 / 本节怎么用）

**这一节是干什么的**：把「这样实现是**有意为之**，不是待修的 bug」这类判断固化下来，
并写明**什么条件下该推翻它**。它解决三个具体问题：

1. **防止把设计当缺陷改回去** —— 例如看到 `rerank_candidates=8`（而召回窗是 30），
   不知情的人会当成"漏块"去调大，结果延迟从秒级涨到数秒。有这一节，改动前先看到代价。
2. **区分"设计"与"退化"** —— 第四轮的限流中间件顺序就是典型：备忘里记了*原因*，
   才能判断"429 不进日志"是设计缺陷还是有意取舍（结论：是缺陷，已修）。
3. **给后续评审提供输入** —— 可以逐条对照，不必每次重新推一遍。

**维护要求**：每条含「结论 / 理由 / 何时该推翻 / 状态」。
⚠️ **过期的备忘比没有更糟**——它会主动误导。本节第 3、5 两条在第四轮就被发现
分别是"表述不准"与"把缺陷写成了设计"（已订正）。

---

1. **为什么 `rag_pipeline` 是唯一聚合点**
   - 结论：路由层通过模块级 `set_pipeline()` 拿实例，不引入 DI 框架。
   - 理由：简单直观、改动面小。
   - 何时该推翻：出现"多个 pipeline 实例并存"（多租户、A/B 两套索引）时。
   - 状态：✅ 有效。代价是隐式契约——谁 `set_pipeline` 晚了就 503。

2. **为什么精排池要收窄**
   - 结论：`recall_candidates=30`（保证不漏）与 `rerank_candidates=8`（保证不快死）分离。
   - 理由：本地 rerank 一次 30 对耗时可达 ~6s；收窄到 8 对在保住高分块的同时大幅降延迟。
   - 何时该推翻：换用更快的 reranker（GPU / 服务端 rerank）后可放宽。
   - 状态：✅ 有效。

3. **为什么沉淀要降权**
   - 结论：把 `syntheses/` 目录块的相关性乘 `synthesis_weight`，压制"历史问答以问代答"霸榜。
   - 理由：历史问答落进 `syntheses/` 后会被再次检索到。
   - ⚠️ **第四轮订正**：原文写作"`synthesis_weight=0.5`（当前配置）"，容易被读成代码默认值。
     实际是**两层**：代码默认 `RetrievalConfig.synthesis_weight = 0.85`；
     部署覆盖 `config/settings.yaml = 0.5`。
   - 何时该推翻：沉淀目录被移出索引范围时（该参数即失效）。
   - 状态：✅ 有效（表述已订正）。

4. **为什么索引三入口共享一个 `IndexSync`**
   - 结论：文件监听 / `/v1/index/refresh` / 异步队列共用同一实例，靠 manifest 做三态判定。
   - 理由：manifest 是单一真相源；分裂会导致重复索引或漏删。
   - 何时该推翻：引入多写者（多进程/多机）时需改为外部锁或集中式索引服务。
   - 状态：✅ 有效。

5. **限流中间件与日志中间件的注册顺序**
   - ⚠️ **第四轮订正——原文把缺陷写成了"设计取舍"**。原文说"限流注册在日志之前…
     429 会绕过日志与指标 → 观测盲区"，读起来像是知情选择；实际那只是 L11 缺陷的
     **现象描述**。
   - 结论（已修）：`add_middleware` **后注册者处于更外层**。现顺序为
     `CORS → RateLimit → RequestLogging`，即**请求日志在最外层**，429 因此也被记录、
     也计入 metrics。实测：`max_per_minute=2` 连发 5 次 → `[200,200,429,429,429]`，
     `metrics.errors=3`，429 日志带 `request_id`。
   - 理由：观测盲区不是可接受的取舍——限流恰恰是最需要被观测的行为之一。
   - 何时该推翻：无（该顺序是唯一正确解）。
   - 状态：✅ 已修；本条改写为"为什么必须是这个顺序"。

6. **为什么 `wiki/` 被硬编码排除**
   - 结论：该目录由外部 LLM Wiki 管理，不纳入索引。
   - 理由：纳入会造成循环索引与语义污染（决策 D9）。
   - 何时该推翻：LLM Wiki 不再写回 vault 时。
   - 状态：✅ 有效。

7. **为什么 `parsers/*`（索引路径）与 `to_markdown.*`（转换接口）没有合并**（第四轮新增）
   - 结论：保留两条路径，各自独立；但把**排版规则**（表格拼装、文本解码）收敛到
     `src/document/md_common.py`。
   - 理由：两者的输出契约不同——索引路径要 `ParsedContent`（含 images/metadata，
     且标题单独作为字段），转换接口要 `ConvertResult`（标题已前缀进正文）。
     强行合并会让其中一个迁就另一个。
     但"表格怎么拼、编码怎么回退"是**纯排版规则**，与输出契约无关，必须只有一份。
   - 何时该推翻：若将来索引路径也需要 Markdown 输出（而非纯文本 + 结构字段），
     可考虑合并为单一格式注册表。
   - 状态：✅ 有效。已知残余差异见第七节"待完善"。

8. **为什么路由端点有的写 `async def`、有的写 `def`**（第五轮新增）
   - 结论：**按端点内是否有阻塞调用决定**。
     - 端点内**无** `await` 且要跑同步逻辑（检索/生成/打包/解析/sqlite）→ 写 `def`。
       Starlette 会把 `def` 端点丢进线程池，事件循环不受影响。
     - 端点内**必须** `await`（`UploadFile.read()`、SSE 流式）→ 保持 `async def`，
       但把其中的同步重活显式包进 `await asyncio.to_thread(...)`。
   - 理由：`async def` 端点直接跑在事件循环上，任何同步阻塞都会**冻结整个进程**
     （不只是这个请求，而是所有请求 + 插件 30s 状态轮询 + 正在进行的 SSE）。
     实测：`/v1/research` 阻塞期间 `/v1/health` 的发起被推迟 1.78s。
   - 为什么不用统一风格（全部 `async def` + `to_thread`）：`def` 改动更小、
     没有额外的缩进与异常处理扰动，且这正是 FastAPI 的推荐做法。
   - 何时该推翻：若将来给端点接入需要 `await` 的前置依赖（如异步鉴权、异步限流），
     则这些端点必须改回 `async def`，并把同步体全部改为 `to_thread`。
   - 状态：✅ 有效（第八轮订正）。原文写"`index.py` 的 4 个队列端点仍为 `async def`"，
     实际第八轮 R8-2 已把 `submit/list/get/cancel_index_job` 四个队列端点改为 `def`
     （同步 sqlite 与 JSON 落盘直跑事件循环，守卫已并入 `_HEAVY_ENDPOINTS`）。
     同文件仍为 `async def` 的是 `index_documents` / `index_url` / `refresh_index` 三个
     ——它们内部确实 `await asyncio.to_thread(...)`，符合规则而非例外。

9. **为什么 per-request 数据不能存在长生命周期共享对象上**（第六轮新增）
   - 结论：**逐请求产生的数据（分阶段耗时、token usage）必须随返回值传递，或以线程局部存放；
     禁止挂在 `Retriever` / `Generator` / `OMLXClient` 这类进程级单例的实例属性上。**
     并在读取侧守一条硬纪律：**`asyncio.to_thread` 场景下，读取必须与写入在同一线程内完成**
     （把「调用 + 读取」包进同一个闭包整体交给 `to_thread`）。
   - 理由：单例被多线程/多协程共享，而 `retrieve()` 经 `asyncio.to_thread` 并发执行。
     A 写完、B 覆盖、A 再读——数据静默张冠李戴。失效的恰恰是观测类数据
     （响应头 `X-RAG-Retrieve-Ms`、日志 `request_timing`、插件状态栏），
     而观测数据错了比崩溃更难发现。
     实证：2 并发下稳定 1/2 请求读到别人的检索耗时；修复前 3/3 复现、修复后 3/3 干净。
   - 为什么不全用 `contextvars`：`asyncio.to_thread` 会把 context **复制**进 worker 线程，
     线程内的写入不会回流到调用方 context，跨线程读取拿不到值——对本场景无效。
   - 已知例外：异步流式路径（`generate_stream_async`）全程在同一事件循环线程，
     线程局部无法隔离两个交错协程，故 `usage` 的彻底修法是改返回契约
     （D6-1 已修，见第 12 条 `usage_out`）。
   - 何时该推翻：若将来引入"请求上下文对象"（每请求一个显式 context 实例贯穿调用链），
     则可统一由该对象承载，无需 thread-local 与返回值双轨。

10. **为什么 CORS 保持 `allow_origins=["*"]`**（第七轮新增，D6-2）
    - 结论：**保持通配，仅标注风险**（代码内注释与本节同步）。
    - 已知风险：服务默认监听 `127.0.0.1` 且 `auth.enabled=false`，此时用户浏览器中**任意网页**
      都能 `fetch('http://127.0.0.1:8000/v1/query')` 向本机知识库提问并读回答案，也能读
      `/v1/status`、`/v1/sessions`。
    - 为什么"允许 Obsidian 调用"不构成理由：Obsidian 的常规请求不受同源策略约束
      （不经浏览器 CORS 检查），通配实际是为其他本地工具留的。
    - 何时该推翻（满足任一条即应收敛为显式白名单 `app://obsidian.md` / `null` /
      `127.0.0.1:*` / `localhost:*`，并先在真实 Obsidian 中确认 SSE 流式仍可用）：
      ① 服务暴露到非 `127.0.0.1`（局域网 / 反代 / 容器端口映射）；
      ② 用户常态性"浏览器挂着任意标签页同时跑本服务"；
      ③ 上线任何按 cookie 或浏览器自动携带凭据鉴权的形态。
    - 状态：✅ 有效（D6-2 已决策）。

11. **为什么检索指标必须分「文档级 / 块级」两套**（第七轮新增，D5-1）
    - 结论：**gold 标注是文档级的、检索返回是块级的，两者不能共用一个指标名。**
      `doc_*` 回答"该找的文档找到没有"，`chunk_*` 回答"塞进上下文的那 k 个块里有多少有用"。
    - 定义取舍（**可复议**）：`doc_precision@k` = 前 k 个块折叠出的**不同文档**中 gold 占比
      （分母 = 不同文档数，可能小于 k）。曾考虑"分母取 k"的写法（于是"1 篇 gold 的 5 个块
      占满前 5 名 → precision = 1/5 = 0.2"）——**已否决**：它用块计数作分母、文档计数作分子，
      正是本次要消除的层级混用。
    - 折叠顺序约定：**先截断前 k 个块，再对文档去重**，保证两级观察同一段 top-k，数字可直接对照。
    - 何时该推翻：若引入**块级 gold 标注**（标出哪些块相关），`chunk_*` 可换成真正的块级 qrels，
      届时 `doc_precision` 的定义可重新审议。
    - 基线影响：本次**重建了 `baseline.json`**。同一批检索结果上新旧口径对照 ——
      `recall@5 → doc_recall@5` **0.9062**（同义，数字不变）、`mrr@5 → doc_mrr@5` **0.8719**、
      `hit@5 → doc_hit@5` **0.9375**、`precision@5 → chunk_precision@5` **0.5750**；
      新增 `doc_precision@5` **0.5349**、`chunk_recall@5` **0.3164**。

12. **为什么 usage 用「加法式 `usage_out`」传递而不是共享属性**（第八轮新增，D6-1）
    - 结论：**per-request 数据必须随调用传递**。client 四个 chat 方法新增可选
      `usage_out: dict` 参数，usage 同时写入共享属性（仅诊断）与 `usage_out`
      （功能消费路径）；generator 四个方法透传；pipeline 三处消费点读自己的
      局部 holder，不再读 `generator.last_usage`。
    - 为什么线程局部救不了：异步流式路径全程在**同一事件循环线程**，两个交错
      协程共享同一线程；且 client 的 usage 写点（流末块）与 generator 的读取点
      之间存在挂起窗口（等待流 EOF），并发请求恰好在该窗口内覆盖共享属性。
      实证：门控桩让 B 的写点严格落在 A 的写→读窗口内，修复前 A 稳定拿到 B 的
      token 数。
    - 为什么选加法式而不是删共享属性：`last_chat_usage` / `last_usage` 保留更新，
      不破坏既有调用方与测试桩；新契约对不传 `usage_out` 的调用方完全无感。
    - 何时该推翻：若将来引入"每请求一个显式 context 对象贯穿调用链"，usage 可
      由该对象承载，`usage_out` 参数可并入其中。

13. **为什么队列公开查询返回快照副本而 cancel 用内部查找**（第九轮新增，D6-3c）
    - 结论：`get`/`list` 返回 `IndexJob(**j.to_dict())` 副本。API 线程在锁外逐字段
      读取，若拿到活引用，worker 的锁外写（现已全部入锁）仍可能让一次响应呈现
      「status=done 但 result=None」类撕裂态；副本让一次读要么全旧要么全新。
    - `cancel` 不能用 `get`（改副本无效），改为锁内直接查找内部对象——
      "查询给快照、变更走内部"是这条边界的不变式。
    - 浅拷贝安全的前提：`params` 提交后不再变、`progress_data`/`result` 均为
      **整体替换**而非原地修改。若将来引入原地 mutate 这些 dict 的代码，快照
      需升级为深拷贝。
    - 何时该推翻：若查询成为热点（副本开销可测），可改返回不可变视图/版本号
      校验，当前规模无必要。

14. **为什么解码链刻意不做 Big5 探测**（第十轮升格，R10-2）
    - 现状：纯试解码无法区分 GB18030 / Big5（gb18030 字节空间覆盖任意双字节
      序列，两者都"解得出"），Big5 文本走 gb18030 得到**确定性乱码**。
    - 真正的解法是字符集探测（charset-normalizer 已是传递依赖且实测能正确
      区分），但**刻意不引入**：探测库是否可用取决于环境，一旦按可用性切换
      逻辑，解码结果就会随环境漂移（生产与开发不一致），比确定性乱码更难排查。
    - R10-2 只移除了链中不可达的 big5 死分支（行为不变，代码诚实），未改变
      上述取舍。锁定用例：`test_round4_convergence.py::test_big5_limitation_...`
      ——若将来引入探测，该用例失败即提示同步更新结论。
    - 何时该推翻：若真实用户 vault 中繁体文档占比显著、且乱码投诉可复现，
      应把探测做成**显式配置项**（`decoding.detector: none|charset-normalizer`）
      而非隐式按可用性切换——漂移问题由此消解。

---

## 七、一页速览

```
已完成（可直接用）
├─ 多格式索引（8 格式 + URL）· 全量/增量/异步三入口 · 文件监听自动同步
├─ 混合检索 + 重排 + 父子块 + 沉淀降权 + token 预算上下文
├─ 流式问答(SSE) + 多轮对话 + 追问改写 + 响应缓存
├─ 会话 CRUD/撤回重发 · 配置热更新 · 文档转 MD · 导出导入
├─ 日志/指标/备份/评测/CI 全套运维设施
└─ Obsidian 插件：聊天面板 + 跳转 + 命令 + 右键菜单 + 状态栏 + 设置页

第四轮已修（原"待完善"前三项 + 末项）
├─ 插件已接通：深度研究 / 异步队列 / 全量索引 / 网页索引 / 项目导出导入 → 设置页开关控制
├─ 插件类型检查真空 → tsconfig 纳入 .tsx + jsx 配置；SourceInfo 补导入；tsc 0 error
├─ 检索参数默认值两份不一致 → Retriever 回落到 RetrievalConfig 单一真源；顺带修出空路径 AttributeError
└─ 插件死代码与样式缺失 → 删 htmlToMarkdown/citationsDock，补齐 7 个 CSS 类

第五轮已修（收口轮：补齐前四轮留下的未评审面）
├─ 【高】阻塞端点写成 async def → 冻结整个事件循环（/v1/research 实测 health 被拖到 2.08s）
│    → 9 个端点改 def（Starlette 线程池）；/v1/import 必须 async 故显式 to_thread
├─ 【中】deep_research：parse_sub_queries 吃掉行首数字（3D 渲染 → D 渲染）· _retrieve_all 无逐条容错
├─ 【中】evaluation：生成评测检索未受保护 · 基线缺 meta 崩溃 · 数据集校验报错不清
├─ 【中】max_rounds 契约不一致（schema le=10 vs 实现夹 5）· judge/bm25 绕过项目日志器
├─ 【中】上下文预算第二份真源（retrieve_with_context / run_eval 写死 4000）
└─ 【低】import_archive mode 静默降级 · 先解包后校验 · 丢弃 rounds · syntheses 死代码与 int 分数

第六轮已修（并发与收尾：深审前五轮未覆盖的模块）
├─ 【中】检索分阶段耗时跨请求串号（实例属性 vs 多线程并发，实证 1/2 稳定串号）→ 改线程局部 + 同线程捕获
├─ 【中】流式收尾副作用在最后一次 yield 之后 → 断连即静默丢失沉淀与耗时日志 → 上移到 yield 之前
├─ 【中】RAGPipeline 构造默认值第二真源（D5-5）→ None 回落 schema 字段默认
├─ 【低】嵌入缓存读写缺 encoding → 非 UTF-8 locale 下缓存静默失效
├─ 【低】_index_sync 惰性创建无锁 → 多个 IndexSync 实例互不互斥
└─ 【低】会话清理 keep 下界路由/store 不一致 · sessions.py 过期注释与未用导入（D5-8）

第七轮已决策 / 已修（原"待决策"项的归宿）
├─ D6-2 CORS 通配 → **已决策：保持现状 + 标注风险**（第六节第 10 条 + `app.py` 内注释）
└─ D5-1 指标口径 → **已修：拆 doc_*/chunk_* 两套 + 真跑重建基线**（H 节 + 第六节第 11 条）

第八轮已修（usage 契约 + 队列端点 + 解包防护）
├─ 【中】D6-1 异步流式 usage 竞态 → 加法式 `usage_out` 局部 holder（client 4 方法 / generator 4 方法 / pipeline 3 消费点）
├─ 【低】`/v1/index` 队列四端点 `async def` → `def`（同步 sqlite 直跑事件循环），并入 `_HEAVY_ENDPOINTS`
├─ 【低】`import_archive` 解包上限（2GiB / 20 万条目，清单头先验后解）
└─ 【加固】事件循环守卫测试抗负载（SLEEP 0.4→1.0，上限 0.35→0.8s，全量高负载下曾假阳性）

第九轮已修（队列治理 + 预算收口）
├─ 【低】D6-3 摄入队列：进度落盘限频 1Hz · `max_finished_jobs=100` 裁剪 · 查询返回快照副本 · 状态迁移全入锁
├─ 【中】`deep_research` 按字符截断 → 改用 `context_builder.build_context` token 预算单一真源
├─ 【低】导出导入 `stats["files"]` 口径（改回真实复制计数）
└─ 【低】`syntheses` 的 `.tmp` 落在被监听目录 → dot-prefix 隐藏避免触发索引

第十轮已修（历史裁剪 + 解码诚实 + 图片元数据）
├─ 【低】对话历史按 token 裁剪会切断 user/assistant 配对 → 改为整轮对齐裁剪
├─ 【低】解码链中不可达的 big5 死分支移除（行为不变，第六节第 14 条说明为何不做探测）
└─ 【低】pptx/epub 图片与元数据在转换接口未回传 → `ConvertResult.images` + 8MiB 载荷上限

第十一轮已修（插件运行期 + E2E 契约）
├─ 【中】P11-2 会话切换竞态（共享 `activeSessionId` 跨 await 被覆写）→ 局部 Promise 持有 + `activeKey` 守卫
├─ 【低】P11-1 自动索引防抖回调未自检开关 → 回调内复查 `autoIndexOnSave`
├─ 【低】P11-3 设置项 `|| 默认值` 无法表达 0 → `finiteNum()` 显式有限数校验
└─ 【验证】E2E 契约测试（真实插件 `api.ts` bundle ↔ 真实 FastAPI，30 断言全绿，含 R8 的 `usage_out` 端到端）

待完善（剩余 —— 全部为"已决策不修 / 有意为之"，非未处理缺陷）

├─ **D6-2 CORS 通配** → 保持 `allow_origins=["*"]` + 标注风险（第六节第 10 条；三条推翻判据）
├─ **解码不做 Big5 探测** → 只移除死分支，刻意不引入环境相关探测（第六节第 14 条）
├─ **/v1/query 不接子查询拆分** → 有意设计；跨文档/多主题问题走 `/v1/research`（第四轮决策）
├─ **检索多样性保持方案 A** → 不按文档去重、不加 MMR；top-5 同文档重复率 31/32 属单文档专题正常表现
│   （推翻判据：跨文档查询成为主要用法、且重复挤占上下文成为可复现痛点 → 单独立项做 A/B）
├─ **插件 `onPhase` 空实现** → `chat_view.tsx:530-532` 主动忽略服务端 `phase` 事件，属可选优化
└─ **唯一未做的验证** → Obsidian GUI 人工验证 7 项清单（自动化测试无法覆盖真实 GUI 环境）
```
