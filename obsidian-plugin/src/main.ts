/**
 * RAG Service Obsidian 插件主入口
 *
 * 功能（决策 D8 完整版）：
 * - 侧边栏聊天面板（SSE 流式 + 多轮追问）
 * - 来源引用点击跳转笔记（vault.getAbstractFileByPath）
 * - 状态栏实时状态
 * - 设置面板（API 地址 / 模型参数 / 自动索引开关）
 * - 命令面板命令
 * - 自动增量索引：保存笔记防抖触发 + 定时兜底（决策已确认"两者都要"）
 */

import {
  App,
  MarkdownView,
  Modal,
  Notice,
  Plugin,
  Setting,
  TAbstractFile,
  TFile,
  WorkspaceLeaf,
} from "obsidian";
import { DEFAULT_SETTINGS, RAGSettingTab, RAGFeatureFlags, RAGSettings } from "./settings";
import { JobInfo, RAGApiClient, SourceInfo } from "./api";
import { ChatView, VIEW_TYPE_CHAT } from "./chat_view";

export default class RAGServicePlugin extends Plugin {
  settings: RAGSettings;
  api: RAGApiClient;

  private statusBarItem!: HTMLElement;
  private refreshTimer: number | null = null;
  private debounceTimer: number | null = null;
  private busy = false;
  /** 自动 HTML→MD 转换：待处理文件 → 防抖定时器 */
  private htmlConvertTimer: number | null = null;
  private htmlConvertBound = false;
  /** 防抖窗口内累积的待转换文件路径（Set 去重，避免多文件互相覆盖） */
  private pendingConvertPaths = new Set<string>();

  async onload(): Promise<void> {
    await this.loadSettings();
    this.api = new RAGApiClient(this.settings.apiBase, this.settings.apiKey);

    // ---- 聊天视图 ----
    this.registerView(VIEW_TYPE_CHAT, (leaf: WorkspaceLeaf) => new ChatView(leaf, this));

    // ---- 命令面板 ----
    this.addCommand({
      id: "open-chat",
      name: "打开 RAG 聊天面板",
      callback: () => void this.openChat(),
    });
    this.addCommand({
      id: "refresh-index",
      name: "触发增量索引",
      callback: () => void this.refreshIndex(),
    });
    this.addCommand({
      id: "check-status",
      name: "检查 RAG 服务状态",
      callback: () => void this.checkStatus(),
    });
    this.addCommand({
      id: "convert-active-file",
      name: "转换当前文件为 Markdown",
      callback: () => void this.convertActiveFile(),
    });

    // ---- 高级功能命令 ----
    // 用 checkCallback 而不是动态注册/注销：返回值 false 时命令直接不出现在
    // 命令面板里，等价于「按开关显隐」，且不必操心注册生命周期。
    this.addAdvancedCommand("research", {
      id: "rag-research",
      name: "深度研究（多子查询汇总成报告）",
      run: () => void this.runResearch(),
    });
    this.addAdvancedCommand("indexTools", {
      id: "rag-index-all",
      name: "全量重建索引（清空后重建）",
      run: () => void this.runFullIndex(),
    });
    this.addAdvancedCommand("indexTools", {
      id: "rag-index-url",
      name: "索引网页到知识库",
      run: () => void this.runUrlIndex(),
    });
    this.addAdvancedCommand("asyncJobs", {
      id: "rag-jobs",
      name: "查看后台摄入任务（进度 / 取消）",
      run: () => void this.showJobs(),
    });
    this.addAdvancedCommand("archiveTools", {
      id: "rag-export-archive",
      name: "导出项目归档（配置+向量库+会话）",
      run: () => void this.exportArchiveToVault(),
    });

    // ---- 文件右键菜单：转换任意支持格式为 Markdown ----
    this.registerEvent(
      this.app.workspace.on("file-menu", (menu, file) => {
        if (!(file instanceof TFile)) return;
        const ext = file.extension.toLowerCase();
        if (!RAGServicePlugin.CONVERT_FORMATS.has(ext)) return;
        menu.addItem((item) => {
          item
            .setTitle(`转换为 Markdown (.${ext} → .md)`)
            .setIcon("file-text")
            .onClick(() => void this.convertFile(file.path, true));
        });
      })
    );

    // ---- 文件右键菜单：把归档 ZIP 导入 RAG 项目（受存档开关控制） ----
    // Obsidian 没有原生文件选择器，用「vault 内右键 .zip」代替，正好与
    // 「导出项目归档写进 vault」形成闭环。
    this.registerEvent(
      this.app.workspace.on("file-menu", (menu, file) => {
        if (!this.settings.features?.archiveTools) return;
        if (!(file instanceof TFile)) return;
        if (file.extension.toLowerCase() !== "zip") return;
        menu.addItem((item) => {
          item
            .setTitle("导入到 RAG 知识库（项目归档）")
            .setIcon("package")
            .onClick(() => void this.importArchiveFromVault(file));
        });
      })
    );

    // ---- 设置面板 ----
    this.addSettingTab(new RAGSettingTab(this.app, this));

    // ---- 状态栏 ----
    this.statusBarItem = this.addStatusBarItem();
    this.statusBarItem.classList.add("rag-status");
    this.statusBarItem.setText("RAG: 检测中…");
    this.statusBarItem.addEventListener("click", () => void this.openChat());
    this.registerInterval(window.setInterval(() => void this.updateStatus(), 30000));

    // ---- 自动索引 ----
    this.rebindAutoIndex();

    // ---- 自动 HTML→MD 转换 ----
    this.rebindAutoConvertHtml();

    // 启动时立即检查一次状态
    void this.updateStatus();

    // 每次手动触发索引
    this.app.workspace.onLayoutReady(() => {
      void this.refreshIndexQuiet(true);
    });
  }

  onunload(): void {
    if (this.refreshTimer !== null) {
      window.clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    if (this.debounceTimer !== null) {
      window.clearTimeout(this.debounceTimer);
      this.debounceTimer = null;
    }
    if (this.htmlConvertTimer !== null) {
      window.clearTimeout(this.htmlConvertTimer);
      this.htmlConvertTimer = null;
    }
  }

  /** 设置变更后重建 API 客户端 */
  updateApiClient(): void {
    this.api = new RAGApiClient(this.settings.apiBase, this.settings.apiKey);
    void this.updateStatus();
  }

  /** 按设置重建自动索引监听（保存触发 + 定时兜底） */
  rebindAutoIndex(): void {
    // 防抖保存回调：registerEvent 注册后无法单独注销，关闭开关只翻标志位，
    // 实际拦截靠 debouncedRefresh 内部判断（P11-1）
    if (!this.settings.autoIndexOnSave && this.refreshDebounceBound) {
      this.refreshDebounceBound = false;
    }
    if (this.settings.autoIndexOnSave && !this.refreshDebounceBound) {
      this.refreshDebounceBound = true;
      const handler = () => this.debouncedRefresh();
      this.registerEvent(this.app.vault.on("modify", handler));
      this.registerEvent(this.app.vault.on("rename", handler));
      this.registerEvent(this.app.vault.on("delete", handler));
    }

    // 定时兜底
    if (this.refreshTimer !== null) {
      window.clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    if (this.settings.autoIndexIntervalSec > 0) {
      this.refreshTimer = window.setInterval(
        () => void this.refreshIndexQuiet(true),
        this.settings.autoIndexIntervalSec * 1000
      );
    }
  }

  private refreshDebounceBound = false;

  /** 保存事件去抖（2 秒内连续保存只触发一次）
   * P11-1：vault.on(...) 注册的处理器在插件生命周期内无法单独注销
   * （rebindAutoIndex 关闭开关时只翻标志位），因此这里必须自查开关——
   * 否则「保存时索引」关掉后仍会继续触发，直到插件重载。 */
  debouncedRefresh(): void {
    if (!this.settings.autoIndexOnSave) return;
    if (this.debounceTimer !== null) {
      window.clearTimeout(this.debounceTimer);
    }
    this.debounceTimer = window.setTimeout(() => {
      this.debounceTimer = null;
      void this.refreshIndexQuiet(true);
    }, 2000);
  }

  // ================================================================
  //  自动 文档 → Markdown 转换（html/pdf/docx/pptx/epub/txt/md）
  // ================================================================

  /** 需要走 base64（二进制）的格式 */
  private static readonly BINARY_FORMATS = new Set(["pdf", "docx", "pptx", "epub"]);

  /** 支持的转换格式（文本格式直接传原文，二进制传 base64）。
   * 注意：不含 .md/.markdown —— 它们本身就是 Markdown，转换会与自身路径冲突 */
  private static readonly CONVERT_FORMATS = new Set([
    "html", "htm", "pdf", "docx", "pptx", "epub", "txt",
  ]);

  /** 按设置重建「自动转换」监听 */
  rebindAutoConvertHtml(): void {
    if (this.settings.autoConvertHtml && !this.htmlConvertBound) {
      this.htmlConvertBound = true;
      this.registerEvent(this.app.vault.on("create", (file) => this.maybeScheduleHtmlConvert(file)));
      this.registerEvent(this.app.vault.on("modify", (file) => this.maybeScheduleHtmlConvert(file)));
      this.registerEvent(this.app.vault.on("rename", (file) => this.maybeScheduleHtmlConvert(file)));
    }
    // 注意：关闭开关时无法撤销 create/modify 监听（registerEvent 已注册），
    // 由 maybeScheduleHtmlConvert 内部判断开关，确保关闭后不再触发。
  }

  /** 若文件是支持格式且开关开启，则防抖调度转换（用 Set 累积，避免多文件互相覆盖定时器） */
  private maybeScheduleHtmlConvert(file: TAbstractFile): void {
    if (!this.settings.autoConvertHtml) return;
    if (!(file instanceof TFile)) return;
    const ext = file.extension.toLowerCase();
    if (!RAGServicePlugin.CONVERT_FORMATS.has(ext)) return;

    this.pendingConvertPaths.add(file.path);

    if (this.htmlConvertTimer !== null) {
      window.clearTimeout(this.htmlConvertTimer);
    }
    this.htmlConvertTimer = window.setTimeout(async () => {
      this.htmlConvertTimer = null;
      const paths = Array.from(this.pendingConvertPaths);
      this.pendingConvertPaths.clear();
      for (const p of paths) {
        await this.convertFile(p);
      }
    }, 1500);
  }

  /** 读取文件内容 → 调服务端统一转换 → 写同名 .md
   * @param force 手动触发时传 true，绕过 autoConvertHtml 开关 */
  async convertFile(filePath: string, force = false): Promise<void> {
    if (!force && !this.settings.autoConvertHtml) return;

    const file = this.app.vault.getAbstractFileByPath(filePath);
    if (!(file instanceof TFile)) return;

    const ext = file.extension.toLowerCase();
    if (!RAGServicePlugin.CONVERT_FORMATS.has(ext)) return;

    // 用字符串拼接而非正则，避免 ext 中的元字符造成注入/误匹配
    const mdPath = filePath.slice(0, filePath.length - (ext.length + 1)) + ".md";

    try {
      let markdown: string;
      if (RAGServicePlugin.BINARY_FORMATS.has(ext)) {
        // 二进制：读字节 → base64 → 服务端解码转换
        const bytes = await this.app.vault.readBinary(file);
        const b64 = this.bytesToBase64(bytes);
        const result = await this.api.documentToMarkdown(ext, b64, true);
        markdown = result.markdown ?? "";
      } else {
        // 文本格式：直接传原文
        const content = await this.app.vault.cachedRead(file);
        if (!content.trim()) return;
        const result = await this.api.documentToMarkdown(ext, content, false);
        markdown = result.markdown ?? content;
      }

      if (!markdown) return;
      await this.app.vault.create(mdPath, markdown);
      new Notice(`✅ 已转换 ${ext.toUpperCase()} → Markdown: ${mdPath}`, 3000);
    } catch (err) {
      const msg = (err as Error).message;
      if (msg.includes("File already exists")) {
        // 已存在同名 .md：静默跳过（避免反复覆盖或刷通知）
        return;
      }
      new Notice(`❌ ${ext.toUpperCase()} 转换失败 (${filePath}): ${msg}`, 6000);
    }
  }

  /** Uint8Array → base64（分块避免调用栈溢出；不用 ... 展开，超大数组会栈溢出） */
  private bytesToBase64(bytes: ArrayBuffer): string {
    const arr = new Uint8Array(bytes);
    let binary = "";
    const chunkSize = 0x8000;
    for (let i = 0; i < arr.length; i += chunkSize) {
      const chunk = arr.subarray(i, i + chunkSize);
      const chunkStr = String.fromCharCode.apply(null, chunk as unknown as number[]);
      binary += chunkStr;
    }
    return btoa(binary);
  }

  /** 命令：转换当前活动文件为 Markdown（手动触发，绕过 autoConvertHtml 开关） */
  async convertActiveFile(): Promise<void> {
    const file = this.app.workspace.getActiveFile();
    if (!file) {
      new Notice("请先打开一个要转换的文件（html/pdf/docx/pptx/epub/txt）", 4000);
      return;
    }
    const ext = file.extension.toLowerCase();
    if (!RAGServicePlugin.CONVERT_FORMATS.has(ext)) {
      new Notice(`不支持的文件类型: .${ext}（支持 html/pdf/docx/pptx/epub/txt/md）`, 5000);
      return;
    }
    new Notice(`开始转换 ${file.name} …`, 2000);
    await this.convertFile(file.path, true);
  }

  // ================================================================
  //  聊天面板
  // ================================================================

  async openChat(): Promise<void> {
    const { workspace } = this.app;
    let leaf: WorkspaceLeaf | null = workspace.getLeavesOfType(VIEW_TYPE_CHAT)[0] ?? null;
    if (!leaf) {
      leaf = workspace.getRightLeaf(false);
      if (!leaf) return;
      await leaf.setViewState({ type: VIEW_TYPE_CHAT, active: true });
    }
    workspace.revealLeaf(leaf);
  }

  // ================================================================
  //  索引与状态
  // ================================================================

  /** 手动触发增量索引（带提示） */
  async refreshIndex(): Promise<void> {
    await this.refreshIndexQuiet(false);
  }

  /** 执行增量索引；quiet=true 时失败只更新状态栏（用于自动触发场景） */
  async refreshIndexQuiet(quiet: boolean): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      const res = await this.api.refreshIndex();
      const parts = [
        `新增 ${res.added}`,
        `更新 ${res.updated}`,
        `删除 ${res.removed}`,
        `未变 ${res.unchanged}`,
      ].join(" · ");
      // 手动触发（quiet=false）或开启"自动索引完成提示"时弹出通知
      if (!quiet || this.settings.autoIndexNotify) {
        new Notice(`♻️ 增量索引完成: ${parts}`, 2500);
      }
      this.statusBarItem.setText("RAG: ✅ 已同步");
    } catch (err) {
      const msg = (err as Error).message;
      if (!quiet) {
        new Notice(`❌ 增量索引失败: ${msg}`, 6000);
      }
      this.statusBarItem.setText("RAG: ❌ 离线");
    } finally {
      this.busy = false;
    }
  }

  /** 状态检查：更新状态栏（只打 /v1/status 一次，省一半探针请求；失败视为离线） */
  async updateStatus(): Promise<void> {
    try {
      const st = await this.api.status();
      this.statusBarItem.setText(`RAG: 🟢 ${st.vector_count} 向量`);
      this.statusBarItem.setAttribute("aria-label", `集合「${st.collection_name}」 · ${st.vector_count} 向量`);
    } catch {
      this.statusBarItem.setText("RAG: 🔴 离线");
    }
  }

  async checkStatus(): Promise<void> {
    try {
      const st = await this.api.status();
      new Notice(
        `RAG 服务在线：集合「${st.collection_name}」，${st.vector_count} 个向量`,
        5000
      );
      this.statusBarItem.setText(`RAG: 🟢 ${st.vector_count} 向量`);
    } catch (err) {
      new Notice(`❌ RAG 服务不可达: ${(err as Error).message}`, 6000);
      this.statusBarItem.setText("RAG: 🔴 离线");
    }
  }

  // ================================================================
  //  点击跳转笔记（核心：绕过 WebView，直接在 Obsidian 中打开）
  // ================================================================

  /**
   * 打开来源笔记
   * @param source 来源信息（file_path 为绝对路径，heading 为标题路径，尽力定位）
   */
  async openSource(source: SourceInfo): Promise<void> {
    const filePath = source.file_path || "";
    const heading = source.heading || "";
    const lineStart = source.line_start || 0;
    const lineEnd = source.line_end || 0;

    if (!filePath) {
      new Notice(`缺少来源路径: ${source.file_name}`);
      return;
    }

    // 绝对路径 → vault 相对路径
    let relative = filePath;
    try {
      // getBasePath 为桌面端 FileSystemAdapter 的运行时方法，类型上未声明，做安全转换
      const adapter = this.app.vault.adapter as { getBasePath?: () => string };
      const vaultBase = adapter.getBasePath?.();
      if (vaultBase && filePath.startsWith(vaultBase)) {
        relative = filePath.slice(vaultBase.length).replace(/^[\\/]+/, "");
      }
    } catch {
      // 无法获取根路径时直接尝试打开
    }

    const file = this.app.vault.getAbstractFileByPath(relative) as TFile | null;
    if (!file) {
      new Notice(`笔记不存在: ${relative}`);
      return;
    }

    // 打开笔记（active leaf）
    const leaf = this.app.workspace.getLeaf(false);
    await leaf.openFile(file);

    // 定位：优先按行号选中命中片段（行级引文高亮），否则按标题锚点
    if (lineStart > 0) {
      this.locateLines(lineStart, Math.max(lineStart, lineEnd));
    } else if (heading) {
      this.locateHeading(heading);
    }
  }

  /** 在已打开的笔记中选中指定行区间（行级引文高亮） */
  private locateLines(startLine: number, endLine: number): void {
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (!view) return;
    const editor = view.editor;
    const from = { line: Math.max(0, startLine - 1), ch: 0 };
    const lastLine = Math.min(editor.lineCount() - 1, Math.max(from.line, endLine - 1));
    const to = { line: lastLine, ch: editor.getLine(lastLine).length };
    editor.setSelection(from, to);
    editor.scrollIntoView({ from, to }, true);
    editor.focus();
  }

  /** 在已打开的笔记中定位标题（尽力而为，找不到则忽略） */
  private locateHeading(heading: string): void {
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (!view) return;
    const file = view.file;
    if (!file) return;

    void this.app.vault.cachedRead(file).then((content) => {
      // heading_path 形如 "基础语法 > 变量"，取最后一级标题
      const last = heading.split(">").pop()?.trim() ?? heading;
      const escaped = last.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const pattern = new RegExp(`^#{1,6}\\s+${escaped}\\s*$`, "m");
      const match = content.match(pattern);
      if (!match) return;

      const lineIndex = content.slice(0, match.index).split("\n").length - 1;
      const editor = view.editor;
      editor.setCursor({ line: lineIndex, ch: 0 });
      editor.scrollIntoView(
        { from: { line: lineIndex, ch: 0 }, to: { line: lineIndex, ch: 0 } },
        true
      );
      view.editor.focus();
    });
  }

  // ================================================================
  //  高级功能（后端已具备、此前插件未接通；由 settings.features 控制入口）
  // ================================================================

  /** 注册一条「受开关控制」的命令：开关关闭时命令不出现在命令面板 */
  private addAdvancedCommand(
    flag: keyof RAGFeatureFlags,
    spec: { id: string; name: string; run: () => void }
  ): void {
    this.addCommand({
      id: spec.id,
      name: spec.name,
      checkCallback: (checking: boolean) => {
        if (!this.settings.features?.[flag]) return false;
        if (!checking) spec.run();
        return true;
      },
    });
  }

  /** yyyyMMdd-HHmm（笔记 / 归档文件名后缀） */
  private stamp(): string {
    const d = new Date();
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  }

  /** 确保 vault 内目录存在（已存在则忽略） */
  private async ensureFolder(path: string): Promise<void> {
    if (this.app.vault.getAbstractFileByPath(path)) return;
    try {
      await this.app.vault.createFolder(path);
    } catch {
      /* 并发创建 / 已存在：忽略 */
    }
  }

  /** 深度研究：/v1/research → 报告写入笔记并打开 */
  async runResearch(): Promise<void> {
    const raw = window.prompt("研究问题（会拆成多个子查询分别检索后汇总）：");
    const question = (raw ?? "").trim();
    if (!question) return;

    const progress = new Notice(`🔬 深度研究中…（${question.slice(0, 20)}）`, 0);
    try {
      const res = await this.api.research(question);
      const lines: string[] = [
        `# 深度研究：${question}`,
        "",
        `> 由 RAG Service 生成 · ${new Date().toLocaleString()} · 共 ${res.rounds} 轮 · ${res.sources.length} 条来源`,
        "",
        res.report.trim(),
        "",
      ];
      if (res.sub_queries.length) {
        lines.push("## 子查询", "", ...res.sub_queries.map((q) => `- ${q}`), "");
      }
      if (res.sources.length) {
        lines.push("## 引用来源", "");
        for (const s of res.sources) {
          const loc = [
            s.heading,
            s.line_start && s.line_end ? `第 ${s.line_start}-${s.line_end} 行` : "",
          ]
            .filter(Boolean)
            .join(" · ");
          lines.push(`- ${s.file_name}${loc ? `（${loc}）` : ""}`);
        }
        lines.push("");
      }

      await this.ensureFolder("RAG 导出");
      const path = `RAG 导出/深度研究-${this.stamp()}.md`;
      const file = await this.app.vault.create(path, lines.join("\n"));
      new Notice(`✅ 深度研究完成：${path}`, 6000);
      await this.app.workspace.getLeaf(false).openFile(file);
    } catch (e) {
      new Notice(`❌ 深度研究失败: ${(e as Error).message}`, 8000);
    } finally {
      progress.hide();
    }
  }

  /** 全量索引：/v1/index（rebuild=true 先清空再重建） */
  async runFullIndex(): Promise<void> {
    const ok = window.confirm(
      "全量重建会先清空向量库再重新索引所有文档，期间检索结果会暂时缺失。\n\n继续？"
    );
    if (!ok) return;

    const progress = new Notice("🧹 全量重建中…（大库建议改用后台任务）", 0);
    try {
      const res = await this.api.indexAll(true);
      new Notice(
        `✅ 全量重建完成：${res.total_documents} 文档 / ${res.total_chunks} 块 / ${res.vector_count} 向量`,
        6000
      );
      void this.updateStatus();
    } catch (e) {
      new Notice(`❌ 全量重建失败: ${(e as Error).message}`, 8000);
    } finally {
      progress.hide();
    }
  }

  /** 网页索引：/v1/index/url */
  async runUrlIndex(): Promise<void> {
    const raw = window.prompt("要索引的网页地址（仅 http/https，内网地址会被服务端拒绝）：");
    const url = (raw ?? "").trim();
    if (!url) return;

    const progress = new Notice(`🌐 正在抓取并索引… ${url}`, 0);
    try {
      const res = await this.api.indexUrl(url);
      new Notice(`✅ 网页已索引：${res.title || res.url}（${res.chunk_count} 块）`, 6000);
      void this.updateStatus();
    } catch (e) {
      new Notice(`❌ 网页索引失败: ${(e as Error).message}`, 8000);
    } finally {
      progress.hide();
    }
  }

  /** 提交后台摄入任务：/v1/index/async */
  async submitBackgroundJob(kind: "full" | "incremental" | "url"): Promise<void> {
    try {
      const opts: { rebuild?: boolean; url?: string } = {};
      if (kind === "full") {
        if (!window.confirm("提交后台全量重建任务（服务端排队执行，清空后重建）？")) return;
        opts.rebuild = true;
      } else if (kind === "url") {
        const raw = window.prompt("要索引的网页地址（仅 http/https）：");
        const url = (raw ?? "").trim();
        if (!url) return;
        opts.url = url;
      }
      const job = await this.api.submitJob(kind, opts);
      new Notice(
        `✅ 已提交后台任务（${job.kind}，id ${job.id.slice(0, 8)}）；可用「查看后台摄入任务」跟踪`,
        6000
      );
    } catch (e) {
      new Notice(`❌ 提交后台任务失败: ${(e as Error).message}`, 6000);
    }
  }

  /** 后台任务面板：/v1/index/jobs */
  async showJobs(): Promise<void> {
    try {
      const { jobs } = await this.api.listJobs(20);
      new JobsModal(this.app, this, jobs).open();
    } catch (e) {
      new Notice(`❌ 读取后台任务失败: ${(e as Error).message}`, 6000);
    }
  }

  /** 导出项目归档：/v1/export → 以 ZIP 字节写进 vault */
  async exportArchiveToVault(): Promise<void> {
    const progress = new Notice("📦 正在打包项目归档…", 0);
    try {
      const buf = await this.api.exportArchive();
      await this.ensureFolder("RAG 导出");
      const path = `RAG 导出/归档-${this.stamp()}.zip`;
      await this.app.vault.createBinary(path, buf);
      new Notice(
        `✅ 已导出归档：${path}（${(buf.byteLength / 1024 / 1024).toFixed(1)} MB）`,
        7000
      );
    } catch (e) {
      new Notice(`❌ 导出归档失败: ${(e as Error).message}`, 8000);
    } finally {
      progress.hide();
    }
  }

  /** 导入项目归档：/v1/import（从 vault 内的 .zip 读入） */
  async importArchiveFromVault(file: TFile): Promise<void> {
    const mode = window.confirm(
      "导入模式：\n\n【确定】合并（merge）：保留现有数据后叠加\n【取消】重建（replace）：覆盖现有向量库\n\n导入完成后需重启 RAG 服务使配置/集合生效。"
    )
      ? "merge"
      : "replace";
    const progress = new Notice(`📥 正在导入 ${file.name}（${mode}）…`, 0);
    try {
      const buf = await this.app.vault.readBinary(file);
      const r = await this.api.importArchive(buf, mode);
      new Notice(`✅ 归档已导入：${r?.note ?? "请重启服务使配置/集合生效"}`, 8000);
      void this.updateStatus();
    } catch (e) {
      new Notice(`❌ 导入归档失败: ${(e as Error).message}`, 8000);
    } finally {
      progress.hide();
    }
  }

  // ================================================================
  //  设置持久化
  // ================================================================

  async loadSettings(): Promise<void> {
    const saved = (await this.loadData()) as Partial<RAGSettings> | null;
    this.settings = Object.assign({}, DEFAULT_SETTINGS, saved ?? {});
    // features 是嵌套对象：Object.assign 只做浅合并，一旦存档里含 features
    // （或将来新增子开关），默认值会被整块替换、缺失键变成 undefined。
    // 这里单独深合并一层，保证新增开关的默认值仍是 false。
    this.settings.features = Object.assign({}, DEFAULT_SETTINGS.features, saved?.features ?? {});
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
  }
}

// ================================================================
//  后台摄入任务面板（异步队列 /v1/index/jobs）
// ================================================================

/**
 * 列出最近的后台摄入任务，支持刷新与取消。
 *
 * 为什么用 Modal 而不是 Notice：Notice 无法承载"列表 + 每行一个操作按钮"，
 * 而长任务（全量重建）恰恰需要看进度、必要时取消。
 */
class JobsModal extends Modal {
  private plugin: RAGServicePlugin;
  private jobs: JobInfo[];

  constructor(app: App, plugin: RAGServicePlugin, jobs: JobInfo[]) {
    super(app);
    this.plugin = plugin;
    this.jobs = jobs;
  }

  onOpen(): void {
    this.titleEl.setText("RAG 后台摄入任务");
    this.render();
  }

  onClose(): void {
    this.contentEl.empty();
  }

  private async refresh(): Promise<void> {
    try {
      this.jobs = (await this.plugin.api.listJobs(20)).jobs;
    } catch (e) {
      new Notice(`❌ 刷新失败: ${(e as Error).message}`, 5000);
    }
    this.render();
  }

  private render(): void {
    const el = this.contentEl;
    el.empty();

    new Setting(el)
      .setName("操作")
      .setDesc("任务在服务端单 worker 串行执行")
      .addButton((b) => b.setButtonText("刷新").onClick(() => void this.refresh()))
      .addButton((b) =>
        b.setButtonText("提交后台全量重建").onClick(async () => {
          await this.plugin.submitBackgroundJob("full");
          await this.refresh();
        })
      );

    if (this.jobs.length === 0) {
      el.createEl("p", { text: "暂无任务。" });
      return;
    }

    for (const j of this.jobs) {
      const row = el.createDiv({ cls: "rag-job-row" });
      const pct = j.progress_data?.percent;
      const bits = [
        j.kind,
        j.status,
        pct != null ? `${Math.round(pct)}%` : "",
        j.progress || "",
      ].filter(Boolean);
      row.createDiv({ cls: "rag-job-line", text: bits.join(" · ") });

      if (j.error) {
        row.createDiv({ cls: "rag-job-error", text: `❌ ${j.error}` });
      }

      if (j.status === "queued" || j.status === "running") {
        new Setting(row).addButton((b) =>
          b.setButtonText("取消").onClick(async () => {
            try {
              await this.plugin.api.cancelJob(j.id);
              new Notice("已请求取消任务");
            } catch (e) {
              new Notice(`❌ 取消失败: ${(e as Error).message}`, 5000);
            }
            await this.refresh();
          })
        );
      }
    }
  }
}