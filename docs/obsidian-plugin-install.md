# Obsidian 插件安装指引

「RAG Service」插件位于 `obsidian-plugin/`（TypeScript 源码），构建产物已随仓库提交，**无需 Node 环境即可安装**。安装后插件通过本地 REST 服务（`http://127.0.0.1:8080`）问答，与自建 vault 互补。

## 前置条件
1. 本地 RAG 服务已启动：`python run_api.py`（在项目根目录）
2. Obsidian 已安装并**打开过**你的 vault（`~/projects/obsidian`）

## 方式一：复制安装（推荐，无需命令行）

1. 打开你的 vault 文件夹 `~/projects/obsidian`
2. 进入 `.obsidian/plugins/`（若不存在则新建）：该目录可能需要先在 Obsidian 的“设置 → 第三方插件”中**关闭受限模式**才会生成
3. 新建子目录 `.obsidian/plugins/rag-service/`
4. 把下列 4 个文件**复制**进去：
   - `obsidian-plugin/main.js`
   - `obsidian-plugin/manifest.json`
   - `obsidian-plugin/styles.css`
   - `obsidian-plugin/versions.json`
5. Obsidian：设置 → 第三方插件 → **启用「RAG Service」**
6. 若“受限模式”挡住，先开启（信任该插件后关闭开关）

## 方式二：脚本安装（推荐，需 Node）

```bash
cd obsidian-plugin
npm install            # 第一次需要（拉取构建依赖）
npm run install        # = build + 复制到 ~/projects/obsidian/.obsidian/plugins/rag-service/
# 自定义 vault 路径：
OBSIDIAN_VAULT=/path/to/vault node scripts/install.mjs
```

## 使用
- 命令面板（`Cmd/Ctrl+P`）→ **“打开 RAG 聊天面板”** → 侧边栏出现聊天窗
- 提问后回答中的**来源引用**点击即可打开对应笔记（并定位到标题/命中行）
- 命令面板 → “触发增量索引”/“检查 RAG 服务状态”
- 设置 → “RAG Service” → 本地参数 + **服务端配置**（严格模式/重排序/阈值/缓存/沉淀/追问改写/历史/模型路由，经 `/v1/config` 热生效）

## 使用指引（新版 UI）

### 问答
- 底部输入框提问，`Enter` 发送、`Shift+Enter` 换行；回答以“气泡卡片”展示（用户=右侧高亮、助手=左侧卡片），支持 Markdown/代码块/引用
- 对话自动保留上下文（多轮追问，10 轮内历史）
- 面板顶部**会话栏**：`＋ 新建` 新会话、「下拉」切换历史会话（服务端保存）、`🗑 删除` 当前会话

### 来源引用（LLM Wiki 式来源卡片）
- 回答下方「📚 来源」卡片：文件名 + 相关度百分比 + 标题锚点 + 命中行
- **点击文件名 → 打开对应笔记并高亮命中行**（行级引文）；有 `📍 标题` 时无行号则定位标题
- 命中文档含提取的内嵌图片时，出现 `🖼 …` 链接，点击用系统看图程序打开

### 自动索引与提示
- 保存/重命名/删除笔记后 **约 2 秒自动增量索引**（防抖；设置里可关）
- 完成后右下角**弹出同步结果**（如 `♻️ 增量索引完成: 新增 0 · 更新 1 · 删除 0 · 未变 5`），状态栏短暂显示 `RAG: ✅ 已同步`；可在“自动索引完成提示”开关关闭弹窗
- 手动同步：命令面板 → “触发增量索引”（同样有结果提示）

### 状态与排错
- 状态栏 `RAG: 🟢 N 向量`（在线）/ `🔴 离线`（服务未启动）——点击可打开聊天面板
- 设置面板“服务端配置”区加载失败 = 服务未运行或地址不对

## 更新插件
```bash
cd obsidian-plugin && npm run install   # 会重新构建并覆盖安装
# 或手动用新 main.js/manifest.json 覆盖方式一的文件
```
Obsidian 内重启（或关闭重开 vault）后生效。

## 排错
| 现象 | 处理 |
|---|---|
| 状态栏显示“离线/🔴” | 确认 `python run_api.py` 在运行；检查插件设置里 API 地址是否为 `http://127.0.0.1:8080/v1` |
| 插件列表看不到 | `.obsidian/plugins/rag-service/` 是否创建在**正确 vault** 下；Obsidian 菜单 “重新加载 App” |
| 点击来源无反应 | 来源文件必须是当前 vault 内的笔记（附件/外部文件无法跳转） |
| 受限模式 | 设置 → 第三方插件 → 点击“打开社区插件”，对本插件点“启用” |