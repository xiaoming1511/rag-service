/**
 * 侧边栏聊天视图
 * 支持 SSE 流式输出、来源引用点击跳转笔记、多轮对话历史
 */

import { ItemView, MarkdownRenderer, WorkspaceLeaf } from "obsidian";
import type RAGServicePlugin from "./main";
import type { SourceInfo } from "./api";

export const VIEW_TYPE_CHAT = "rag-service-chat";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

// 桌面端打开外部附件（多模态图片预览）
declare const require: (m: string) => any;
const electron = typeof require === "function" ? require("electron") : null;

export class ChatView extends ItemView {
  plugin: RAGServicePlugin;

  private history: ChatMessage[] = [];
  private messagesEl!: HTMLElement;
  private inputEl!: HTMLTextAreaElement;
  private sendBtn!: HTMLButtonElement;
  private sessionSelect!: HTMLSelectElement;
  private activeSessionId: string | null = null;
  private sessions: any[] = [];
  private streaming = false;

  constructor(leaf: WorkspaceLeaf, plugin: RAGServicePlugin) {
    super(leaf);
    this.plugin = plugin;
  }

  getViewType(): string {
    return VIEW_TYPE_CHAT;
  }

  getDisplayText(): string {
    return "RAG 问答";
  }

  getIcon(): string {
    return "message-square";
  }

  async onOpen(): Promise<void> {
    this.containerEl.empty();
    this.containerEl.addClass("rag-chat-container");

    // 会话栏（多会话管理）
    const sessionBar = this.containerEl.createDiv({ cls: "rag-session-bar" });
    this.sessionSelect = sessionBar.createEl("select", { cls: "rag-session-select" });
    this.sessionSelect.createEl("option", { value: "", text: "— 会话 —" });
    const newBtn = sessionBar.createEl("button", { text: "＋ 新建", cls: "rag-session-btn" });
    const delBtn = sessionBar.createEl("button", { text: "🗑 删除", cls: "rag-session-btn rag-session-del" });
    newBtn.addEventListener("click", () => void this.newSession());
    delBtn.addEventListener("click", () => void this.deleteActiveSession());
    this.sessionSelect.addEventListener("change", () => void this.switchSession());

    // 消息区
    this.messagesEl = this.containerEl.createDiv({ cls: "rag-chat-messages" });
    const placeholder = this.messagesEl.createDiv({ cls: "rag-chat-placeholder" });
    placeholder.setText("输入问题开始对话\n基于你的 Obsidian 知识库");

    // 输入区
    const inputArea = this.containerEl.createDiv({ cls: "rag-chat-input-area" });
    this.inputEl = inputArea.createEl("textarea", {
      cls: "rag-chat-input",
      attr: { placeholder: "输入问题 (Enter 发送, Shift+Enter 换行)", rows: "2" },
    });
    this.sendBtn = inputArea.createEl("button", { cls: "rag-chat-send", text: "发送" });

    this.inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void this.send();
      }
    });
    this.sendBtn.addEventListener("click", () => void this.send());

    void this.loadSessions();
  }

  async onClose(): Promise<void> {
    // 无额外清理
  }

  // ================================================================
  //  会话管理
  // ================================================================

  private async loadSessions(): Promise<void> {
    try {
      const res = await this.plugin.api.listSessions();
      this.sessions = res.sessions || [];
      this.sessionSelect.empty();
      this.sessionSelect.createEl("option", { value: "", text: "— 会话 —" });
      for (const s of this.sessions) {
        const label = `${s.title || "未命名"} (${s.message_count ?? 0})`;
        this.sessionSelect.createEl("option", { value: s.id, text: label });
      }
      if (this.activeSessionId) this.sessionSelect.value = this.activeSessionId;
    } catch (e) {
      console.error("会话列表加载失败:", e);
    }
  }

  private async newSession(): Promise<void> {
    try {
      const s = await this.plugin.api.createSession();
      this.activeSessionId = s.id;
      this.clearChatLocal();
      await this.loadSessions();
      this.sessionSelect.value = s.id;
    } catch (e) {
      console.error("新建会话失败:", e);
    }
  }

  private async switchSession(): Promise<void> {
    const id = this.sessionSelect.value;
    if (!id) return;
    try {
      const data = await this.plugin.api.getSession(id);
      this.activeSessionId = id;
      this.clearChatLocal();
      const msgs = (data.messages || []) as ChatMessage[];
      this.history = msgs.slice(-20);
      for (const m of msgs) {
        this.addMessage(m.role, m.content);
      }
      this.messagesEl.scrollTo({ top: this.messagesEl.scrollHeight });
    } catch (e) {
      console.error("切换会话失败:", e);
    }
  }

  private async deleteActiveSession(): Promise<void> {
    if (!this.activeSessionId) return;
    try {
      await this.plugin.api.deleteSession(this.activeSessionId);
      this.activeSessionId = null;
      this.clearChatLocal();
      await this.loadSessions();
    } catch (e) {
      console.error("删除会话失败:", e);
    }
  }

  /** 清空本地聊天区（保留历史数组同步） */
  private clearChatLocal(): void {
    this.history = [];
    this.messagesEl.empty();
    const ph = this.messagesEl.createDiv({ cls: "rag-chat-placeholder" });
    ph.setText("输入问题开始对话\n基于你的 Obsidian 知识库");
  }

  /** 清空对话 */
  clearChat(): void {
    this.history = [];
    this.messagesEl.empty();
    const ph = this.messagesEl.createDiv({ cls: "rag-chat-placeholder" });
    ph.setText("输入问题开始对话\n基于你的 Obsidian 知识库");
  }

  // ================================================================
  //  消息渲染
  // ================================================================

  private addMessage(role: "user" | "assistant", content: string): HTMLElement {
    const wrap = this.messagesEl.createDiv({ cls: `rag-msg rag-msg-${role}` });
    const bubble = wrap.createDiv({ cls: "rag-msg-bubble" });

    if (role === "user") {
      bubble.setText(content);
    } else {
      // 渲染 Markdown（Obsidian 内置渲染器）
      void MarkdownRenderer.render(
        this.app,
        content || "（空回答）",
        bubble,
        "",
        this
      );
    }
    this.messagesEl.scrollTo({ top: this.messagesEl.scrollHeight, behavior: "smooth" });
    return bubble;
  }

  private renderSources(sources: SourceInfo[]): HTMLElement {
    const box = this.messagesEl.createDiv({ cls: "rag-sources" });
    box.createDiv({ cls: "rag-sources-title", text: `📚 来源 (${sources.length})` });

    for (const s of sources) {
      const row = box.createDiv({ cls: "rag-source" });
      const link = row.createEl("a", { cls: "rag-source-link", text: s.file_name });
      const score = row.createSpan({ cls: "rag-source-score", text: ` ${(s.score * 100).toFixed(1)}%` });
      const info = [];
      if (s.heading) info.push(`📍 ${s.heading}`);
      if (s.line_start && s.line_end) info.push(`第 ${s.line_start}-${s.line_end} 行`);
      if (info.length) row.createDiv({ cls: "rag-source-heading", text: info.join(" · ") });
      // 多模态附件：点击用系统默认程序打开图片
      if (s.images && s.images.length > 0) {
        for (const img of s.images) {
          const imgRow = row.createDiv({ cls: "rag-source-img" });
          const imgLink = imgRow.createEl("a", { text: `🖼 ${img.caption?.slice(0, 24) || "图片"}` });
          imgLink.addEventListener("click", () => {
            if (electron?.shell) {
              void electron.shell.openPath(img.path).catch(() => { /* 无法打开时忽略 */ });
            }
          });
        }
      }
      link.addEventListener("click", () => {
        void this.plugin.openSource(s);
      });
      void score;
    }
    return box;
  }

  // ================================================================
  //  发送与流式接收
  // ================================================================

  private async send(): Promise<void> {
    const question = this.inputEl.value.trim();
    if (!question || this.streaming) return;

    this.inputEl.value = "";
    this.streaming = true;
    this.sendBtn.setAttr("disabled", "true");

    this.addMessage("user", question);
    this.history.push({ role: "user", content: question });
    // 同步到服务端会话
    if (this.activeSessionId) {
      void this.plugin.api.appendMessage(this.activeSessionId, "user", question);
    }

    const assistantBubble = this.addMessage("assistant", "…");

    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 120000);

    let fullAnswer = "";
    let sources: SourceInfo[] = [];

    // 流式渲染：保存原始文本，逐块重渲染
    const render = () => {
      assistantBubble.empty();
      void MarkdownRenderer.render(this.app, fullAnswer || "…", assistantBubble, "", this);
      this.messagesEl.scrollTo({ top: this.messagesEl.scrollHeight });
    };

    try {
      await this.plugin.api.queryStream(
        question,
        this.history,
        this.plugin.settings.topK,
        this.plugin.settings.useRerank,
        {
          onSources: (src) => {
            sources = src;
          },
          onChunk: (chunk) => {
            fullAnswer += chunk;
            render();
          },
          onDone: (full) => {
            fullAnswer = full || fullAnswer;
            render();
            if (sources.length > 0) {
              this.renderSources(sources);
            }
            // 记录助手回答，供追问使用，并同步到服务端会话
            this.history.push({ role: "assistant", content: fullAnswer });
            if (this.activeSessionId) {
              void this.plugin.api.appendMessage(this.activeSessionId, "assistant", fullAnswer);
            }
          },
          onError: (msg) => {
            assistantBubble.empty();
            assistantBubble.setText(`❌ ${msg}`);
          },
        },
        controller.signal
      );
    } catch (err) {
      console.error("RAG 流式请求失败:", err);
      assistantBubble.empty();
      assistantBubble.setText(`❌ 网络错误: ${(err as Error).message}`);
    } finally {
      window.clearTimeout(timeout);
      this.streaming = false;
      this.sendBtn.removeAttribute("disabled");
    }
  }
}