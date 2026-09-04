# RAG Service · Obsidian 插件

原生 Obsidian 插件（TypeScript + esbuild），实现：
- **侧边栏聊天面板**：SSE 流式回答、Markdown 渲染、多轮对话追问
- **引用点击跳转**：点击来源直接在 Obsidian 中打开笔记（并尽力定位标题锚点）
- **状态栏**：实时显示 RAG 服务在线状态与向量数量
- **自动增量索引**：保存/重命名/删除笔记时防抖触发 + 可选定时兜底
- **命令面板**：打开聊天面板 / 触发增量索引 / 检查服务状态
- **设置面板**：API 地址、API Key、top_k、重排序开关、自动索引开关

## 构建与安装

```bash
# 一次安装 dev 依赖（obsidian/esbuild/typescript）
npm install

# 构建 + 安装到 vault（默认 ~/projects/obsidian，或用环境变量指定）
npm run install
# 或指定 vault：
OBSIDIAN_VAULT=/path/to/vault node scripts/install.mjs

# 仅构建（不安装）
npm run build

# 开发模式（监听改动实时重建）
npm run dev
```

安装后：
1. 使用 Obsidian 打开 vault（`~/projects/obsidian`）
2. 设置 → 第三方插件 → 关闭"受限模式"并启用「RAG Service」
3. 命令面板（Cmd+P）→ "打开 RAG 聊天面板"

## 与后端配合

- 依赖本地 RAG 服务运行中：`python run_api.py`（默认 127.0.0.1:8080）
- 插件调用：`/v1/query/stream`、`/v1/index/refresh`、`/v1/health`、`/v1/status`
- 点击跳转依赖 API `sources` 中的 `file_path`（绝对路径）与 `heading` 字段
  （后端 P0 已扩展，向后兼容）

## 目录结构

```
obsidian-plugin/
├── manifest.json        # 插件清单（id: rag-service）
├── versions.json
├── esbuild.config.mjs   # 构建配置
├── scripts/install.mjs  # 安装脚本（复制产物到 vault/.obsidian/plugins/）
├── src/
│   ├── main.ts          # 插件入口（命令/状态栏/自动索引/跳转）
│   ├── api.ts           # RAG API 客户端（SSE 解析）
│   ├── chat_view.ts     # 侧边栏聊天视图
│   └── settings.ts      # 设置面板
└── styles.css           # 聊天视图样式
```