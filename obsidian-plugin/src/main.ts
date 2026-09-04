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
  Notice,
  Plugin,
  TFile,
  WorkspaceLeaf,
} from "obsidian";
import { DEFAULT_SETTINGS, RAGSettingTab, RAGSettings } from "./settings";
import { RAGApiClient, SourceInfo } from "./api";
import { ChatView, VIEW_TYPE_CHAT } from "./chat_view";

export default class RAGServicePlugin extends Plugin {
  settings: RAGSettings;
  api: RAGApiClient;

  private statusBarItem!: HTMLElement;
  private refreshTimer: number | null = null;
  private debounceTimer: number | null = null;
  private busy = false;

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
  }

  /** 设置变更后重建 API 客户端 */
  updateApiClient(): void {
    this.api = new RAGApiClient(this.settings.apiBase, this.settings.apiKey);
    void this.updateStatus();
  }

  /** 按设置重建自动索引监听（保存触发 + 定时兜底） */
  rebindAutoIndex(): void {
    // 防抖保存回调（由 registerEvent 注册一次，内部判断开关）
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

  /** 保存事件去抖（2 秒内连续保存只触发一次） */
  debouncedRefresh(): void {
    if (this.debounceTimer !== null) {
      window.clearTimeout(this.debounceTimer);
    }
    this.debounceTimer = window.setTimeout(() => {
      this.debounceTimer = null;
      void this.refreshIndexQuiet(true);
    }, 2000);
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
      if (!quiet) {
        const parts = [
          `新增 ${res.added}`,
          `更新 ${res.updated}`,
          `删除 ${res.removed}`,
          `未变 ${res.unchanged}`,
        ].join(" · ");
        new Notice(`♻️ 增量索引完成: ${parts}`, 4000);
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

  /** 状态检查：更新状态栏 */
  async updateStatus(): Promise<void> {
    try {
      const h = await this.api.health();
      if (h.status === "healthy") {
        try {
          const st = await this.api.status();
          this.statusBarItem.setText(`RAG: 🟢 ${st.vector_count} 向量`);
        } catch {
          this.statusBarItem.setText("RAG: 🟢 在线");
        }
      } else {
        this.statusBarItem.setText("RAG: ⚪ 未初始化");
      }
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

    // 尽力定位标题锚点：在编辑器中查找标题行并移动光标
    if (heading) {
      this.locateHeading(heading);
    }
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
  //  设置持久化
  // ================================================================

  async loadSettings(): Promise<void> {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
  }
}