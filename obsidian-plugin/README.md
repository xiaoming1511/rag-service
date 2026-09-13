# RAG Service · Obsidian 插件

原生 Obsidian 插件（TypeScript + React + esbuild），实现：

- **侧边栏聊天面板**：SSE 流式回答、Markdown 渲染、多轮对话追问、撤回重发
- **引用点击跳转**：点击来源直接在 Obsidian 中打开笔记（行级高亮 / 标题锚点）
- **状态栏**：实时显示 RAG 服务在线状态与向量数量
- **自动增量索引**：保存/重命名/删除笔记时防抖触发 + 可选定时兜底
- **自动文档转换**：放入 vault 的 html/pdf/docx/pptx/epub/txt 自动转成同名 `.md`
- **设置面板**：API 地址/Key、使用模式四档预设（简洁/标准/详细/自定义）、
  自动索引与自动转换开关、服务端配置热更新、会话管理、服务监控
- **高级功能（默认关闭，在设置里逐个开启）**：后端早已实现但此前插件无入口的能力
  - **深度研究**（`/v1/research`）：拆子查询并行检索，汇总成带引用的报告并落成笔记
  - **索引工具**：全量重建（`/v1/index`）、网页索引（`/v1/index/url`）
  - **后台摄入队列**（`/v1/index/async` + `/v1/index/jobs`）：长任务不阻塞，可看进度/取消
  - **项目归档**（`/v1/export`、`/v1/import`）：配置+文档清单+向量库+会话打包成 ZIP，
    导出到 vault；把归档 ZIP 放进 vault 后右键即可导入

> 非流式 `/v1/query` **刻意未接**：对话场景下流式严格优于"等完整响应再显示"，
> 该端点保留给脚本 / 程序化调用。

## 构建与安装

```bash
# 一次安装 dev 依赖（obsidian/esbuild/typescript/@types/react）
npm install

# 类型检查（React 层也参与，.tsx 不再有检查真空）
npm run typecheck

# 构建 + 安装到 vault（默认 ~/projects/obsidian，或用环境变量指定）
# build 会先跑 tsc --noEmit：类型错误会直接拦下构建
npm run install
# 或指定 vault：
OBSIDIAN_VAULT=/path/to/vault node scripts/install.mjs

# 仅构建（不安装）
npm run build

# 开发模式（监听改动实时重建；不做类型检查，请另行 npm run typecheck）
npm run dev
```

安装后：
1. 使用 Obsidian 打开 vault（`~/projects/obsidian`）
2. 设置 → 第三方插件 → 关闭"受限模式"并启用「RAG Service」
3. 命令面板（Cmd+P）→ "打开 RAG 聊天面板"

## 与后端配合

- 依赖本地 RAG 服务运行中：`python run_api.py`（默认 127.0.0.1:8080）
- 主干调用：`/v1/query/stream`、`/v1/index/refresh`、`/v1/health`、`/v1/status`、
  `/v1/sessions*`、`/v1/config`、`/v1/convert/to-md`
- 高级能力调用（开关开启后）：`/v1/research`、`/v1/index`、`/v1/index/url`、
  `/v1/index/async`、`/v1/index/jobs*`、`/v1/export`、`/v1/import`
- 点击跳转依赖 API `sources` 中的 `file_path`（绝对路径）、`heading`、
  `line_start`/`line_end` 字段

## 目录结构

```
obsidian-plugin/
├── manifest.json        # 插件清单（id: rag-service）
├── versions.json
├── tsconfig.json        # 含 .tsx + jsx，npm run typecheck 用
├── esbuild.config.mjs   # 构建配置（打包 React）
├── scripts/install.mjs  # 安装脚本（复制产物到 vault/.obsidian/plugins/）
├── src/
│   ├── main.ts          # 插件入口（命令/状态栏/自动索引/跳转/高级功能）
│   ├── api.ts           # RAG API 客户端（SSE 解析 + 全部端点封装）
│   ├── chat_view.tsx    # 侧边栏聊天视图：业务逻辑与生命周期
│   ├── chat_app.tsx     # 聊天面板的 React 渲染层（纯渲染 + 错误边界）
│   └── settings.ts      # 设置面板（含使用模式预设与高级功能开关）
└── styles.css           # 插件样式
```

> 注：视图层已拆分为 `chat_view.tsx`（逻辑）+ `chat_app.tsx`（渲染）。
> 旧版文档提到的 `chat_view.ts` 已不存在。
