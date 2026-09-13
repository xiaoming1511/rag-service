/**
 * 侧边栏聊天视图（对话式查询）
 *
 * 职责分工：
 * - 本文件（ChatView）：拥有全部业务逻辑（发送/流式接收/会话管理/编辑重发/
 *   导出/引用/耗时统计）与生命周期；UI 一律交给 React 渲染层（chat_app.tsx）。
 * - chat_app.tsx：纯渲染，通过 view 的快照 + notify() 驱动。
 *
 * React 安全约定：
 * - 视图打开时创建 1 个 React 根挂到 containerEl，onClose 时 unmount；
 * - 助手气泡外壳由 React 渲染一次（memo 冻结），流式内容由本类通过
 *   registerAssistantShell 拿到的 DOM 句柄命令式写入，React 永不回写；
 * - 每个 UI 区块都有错误边界，出错可见可重试，绝不整块空白。
 */
import { ItemView, MarkdownRenderer, Notice, setIcon, TFile, WorkspaceLeaf } from "obsidian";
import { createRoot, Root } from "react-dom/client";
import React from "react";
import type RAGServicePlugin from "./main";
import type { SourceInfo, TimingInfo } from "./api";
import { ChatApp, ViewMessage } from "./chat_app";

export const VIEW_TYPE_CHAT = "rag-service-chat";

/** 历史会话元信息（服务端 listSessions 返回项） */
export interface SessionMeta {
  id: string;
  title?: string;
  message_count?: number;
  created_at?: number;
  updated_at?: number;
}

/** 助手气泡的 DOM 句柄（流式/完成态命令式写入用） */
export interface BubbleHandles {
  key: number;
  wrap: HTMLElement;
  bubble: HTMLElement;
  agentActions: HTMLElement;
  agentBadge: HTMLElement;
  agentTimer: HTMLElement;
  timerValue: HTMLElement;
  progressEl: HTMLElement;
  progressBar: HTMLElement;
  progressPct: HTMLElement;
}

// 桌面端打开外部附件（多模态图片预览）
declare const require: (m: string) => any;
const electron = typeof require === "function" ? require("electron") : null;

export class ChatView extends ItemView {
  plugin: RAGServicePlugin;

  // ---- React 渲染层契约 ----
  sessionsSnapshot: SessionMeta[] = [];
  historyOpen = false;
  activeSessionId: string | null = null;
  streaming = false;
  connectionStatus: "connecting" | "connected" | "streaming" | "done" | "offline" = "connecting";
  /** ChatApp 注入：调用后触发一次安全重渲染 */
  uiNotifier: (() => void) | null = null;
  inputEl: HTMLTextAreaElement | null = null;

  private history: { role: "user" | "assistant"; content: string }[] = [];
  private activeController: AbortController | null = null;
  /** 按会话键存的「live 消息视图」：流式中途切换会话不丢失内容 */
  private liveBySession = new Map<string | null, ViewMessage[]>();
  /** 当前激活会话键（null = 未保存的新对话） */
  private activeKey: string | null = null;

  /** React 渲染层读取：当前激活会话的消息列表 */
  get messagesSnapshot(): ViewMessage[] {
    return this.liveBySession.get(this.activeKey) ?? [];
  }

  private ensureLive(key: string | null): ViewMessage[] {
    let arr = this.liveBySession.get(key);
    if (!arr) {
      arr = [];
      this.liveBySession.set(key, arr);
    }
    return arr;
  }
  private seq = 1;
  private bubbleRegistry = new Map<number, BubbleHandles>();
  private root: Root | null = null;

  constructor(leaf: WorkspaceLeaf, plugin: RAGServicePlugin) {
    super(leaf);
    this.plugin = plugin;
  }

  getViewType(): string {
    return VIEW_TYPE_CHAT;
  }

  getDisplayText(): string {
    return "对话式查询";
  }

  getIcon(): string {
    return "message-square";
  }

  // ------------------------------------------------------------
  //  生命周期
  // ------------------------------------------------------------

  async onOpen(): Promise<void> {
    this.containerEl.empty();
    this.containerEl.addClass("rag-chat-container");
    this.containerEl.addClass("rag-react-ui");

    // 探针：把任何错误画进视图，绝不空白
    const paint = (text: string) => {
      this.containerEl.empty();
      const d = this.containerEl.createDiv({ cls: "rag-fatal" });
      d.setText(text);
    };
    const guardVisibility = (): boolean => {
      if (!this.containerEl.isConnected) return false;
      if (this.containerEl.querySelector(".rag-fatal")) return false;
      return true;
    };

    try {
      this.root = createRoot(this.containerEl);
      this.root.render(React.createElement(ChatApp, { view: this }));
    } catch (e) {
      paint(`❌ React 挂载失败：${(e as Error)?.message ?? String(e)}`);
      console.error("[RAG] mount error:", e);
      return;
    }

    // 未捕获的运行期错误/未处理拒绝 → 视图内可见
    // 注意：window 级监听会捕获其他插件/系统的错误，故仅当面板可见且
    // 尚未渲染过错误时画一次（守卫返回 false 则跳过），避免反复污染视图。
    const onErr = (msg: string) => {
      if (!guardVisibility()) return;
      const d = this.containerEl.createDiv({ cls: "rag-fatal" });
      d.setText(`⚠️ [RAG] 运行期错误：${msg}`);
    };
    this.registerDomEvent(window, "error", (evt) => onErr(evt.message || String(evt.error)));
    this.registerDomEvent(window, "unhandledrejection", (evt) => {
      const r = (evt as PromiseRejectionEvent).reason;
      onErr(String((r as Error)?.message ?? r));
    });

    this.refreshConnectionStatus();
    void this.loadSessions();

    // 点击面板外关闭历史列表（容器内点击由 React 组件自行处理；面板内已 stopPropagation）
    this.registerDomEvent(document, "click", (evt) => {
      if (!this.historyOpen) return;
      if (this.containerEl.contains(evt.target as Node)) return;
      this.historyOpen = false;
      this.requestTick();
    });
  }

  /** 点击消息区关闭历史面板（面板内点击已 stopPropagation，不会误关） */
  closeHistory(): void {
    if (!this.historyOpen) return;
    this.historyOpen = false;
    this.requestTick();
  }

  async onClose(): Promise<void> {
    this.activeController?.abort();
    this.root?.unmount();
    this.root = null;
    this.bubbleRegistry.clear();
  }

  /** 请求 React 重渲染（所有状态变更后调用） */
  requestTick(): void {
    this.uiNotifier?.();
  }

  // ------------------------------------------------------------
  //  连接状态
  // ------------------------------------------------------------

  private setChatStatus(mode: "connecting" | "connected" | "streaming" | "done" | "offline"): void {
    this.connectionStatus = mode;
    this.requestTick();
  }

  private async refreshConnectionStatus(): Promise<void> {
    this.setChatStatus("connecting");
    try {
      await this.plugin.api.health();
      this.setChatStatus("connected");
    } catch {
      this.setChatStatus("offline");
    }
  }

  /** 增量索引（头部按钮 + 命令面板共用） */
  refreshIndex(): Promise<void> {
    return this.plugin.refreshIndex();
  }

  // ------------------------------------------------------------
  //  会话管理（历史面板操作）
  // ------------------------------------------------------------

  toggleHistory(): void {
    this.historyOpen = !this.historyOpen;
    if (this.historyOpen) void this.loadSessions();
    this.requestTick();
  }

  async selectSession(id: string): Promise<void> {
    this.historyOpen = false;
    this.requestTick();
    await this.switchSession(id);
  }

  private async loadSessions(): Promise<void> {
    try {
      const res = await this.plugin.api.listSessions();
      this.sessionsSnapshot = res.sessions || [];
      this.requestTick();
    } catch (e) {
      console.error("会话列表加载失败:", e);
    }
  }

  async newSession(): Promise<void> {
    this.historyOpen = false;
    this.activeSessionId = null;
    this.activeKey = null;
    this.history = [];
    this.liveBySession.set(null, []);
    this.cleanupStreaming();
    this.requestTick();
    this.inputEl?.focus();
    this.inputEl?.select();
  }

  private async switchSession(id: string): Promise<void> {
    if (id === this.activeSessionId) return;
    try {
      const data = await this.plugin.api.getSession(id);
      this.activeSessionId = id;
      this.activeKey = id;
      this.history = (data.messages || []).map((m: any) => ({ role: m.role, content: m.content }));
      // 会话键控的 live 存储：若该会话之前正在流式，这里保留其半成品消息；
      // 否则从服务端历史构建一次（以后沿用，切换回来不再丢失）
      if (!this.liveBySession.has(id)) {
        this.liveBySession.set(
          id,
          this.history.map((m, i) => ({ key: this.seq++, role: m.role, content: m.content, streaming: false }))
        );
      }
      this.requestTick();
      // 历史/半成品内容由消息组件按状态（content）驱动渲染，无需额外时序处理
    } catch (e) {
      console.error("切换会话失败:", e);
      new Notice(`❌ 读取会话失败: ${(e as Error).message}`);
    }
  }

  async deleteSession(id: string): Promise<void> {
    try {
      await this.plugin.api.deleteSession(id);
      if (id === this.activeSessionId) {
        this.activeSessionId = null;
        this.activeKey = null;
        this.history = [];
        this.liveBySession.delete(id);
        this.cleanupStreaming();
        this.requestTick();
      } else {
        this.liveBySession.delete(id);
      }
      new Notice("🗑 会话已删除");
      void this.loadSessions();
    } catch (e) {
      console.error("删除会话失败:", e);
      new Notice(`❌ 删除失败: ${(e as Error).message}`);
    }
  }

  promptRename(id: string): void {
    const s = this.sessionsSnapshot.find((x) => x.id === id);
    const old = s?.title || "";
    const title = window.prompt("重命名会话", old);
    if (title == null || title.trim() === "" || title.trim() === old) return;
    void this.plugin.api
      .renameSession(id, title.trim())
      .then(() => {
        new Notice("✅ 会话已重命名");
        void this.loadSessions();
      })
      .catch((e) => new Notice(`❌ 重命名失败: ${(e as Error).message}`));
  }

  async exportSession(id: string): Promise<void> {
    try {
      const data = await this.plugin.api.getSession(id);
      const title = ((data.title || "会话") as string).trim() || "会话";
      const lines: string[] = [`# ${title}`, "", `> 由 RAG Service 导出 · ${new Date().toLocaleString()}`, ""];
      for (const m of data.messages || []) {
        lines.push(`## ${m.role === "user" ? "🧑 用户" : "🤖 助手"}`, "", (m.content || "").trim(), "");
      }
      const safe = title.replace(/[\\/:*?"<>|]/g, "_").slice(0, 60) || "会话";
      const path = `RAG 导出/${safe}.md`;
      await this.app.vault.create(path, lines.join("\n"));
      new Notice(`✅ 已导出: ${path}`);
    } catch (e) {
      new Notice(`❌ 导出失败: ${(e as Error).message}`, 6000);
    }
  }

  /** 打开「RAG 服务开关与模式说明」笔记（避免写死路径：按固定清单+文件名查找） */
  async openDocsNote(): Promise<void> {
    const candidates = [
      "RAG 服务-开关与模式说明.md",
      "RAG 服务-开关与模式说明",
    ];
    let file: TFile | null = null;
    for (const c of candidates) {
      const f = this.app.vault.getAbstractFileByPath(c);
      if (f instanceof TFile) {
        file = f;
        break;
      }
    }
    if (!file) {
      // 兜底：按文件名在整个库中查找
      file =
        this.app.vault
          .getFiles()
          .find((f) => f.name === "RAG 服务-开关与模式说明.md") ?? null;
    }
    if (file) {
      const leaf = this.app.workspace.getLeaf(false);
      await leaf.openFile(file);
    } else {
      new Notice("说明笔记不存在：RAG 服务-开关与模式说明.md", 6000);
    }
  }

  // ------------------------------------------------------------
  //  助手气泡注册（React 外壳 → DOM 句柄）
  // ------------------------------------------------------------

  registerAssistantShell(key: number, wrap: HTMLElement): void {
    // React commit 时子元素可能尚未挂好，句柄统一延迟到下一帧构建（含重试）
    window.setTimeout(() => this.tryBuildHandles(key, wrap), 0);
  }

  /** 构建 DOM 句柄；子元素未就绪则返回 false（下次注册/轮询再试）。
      注意：会话切换回来外壳会重挂载，这里**总是**用当前 wrap 重建句柄（覆盖旧的已脱离节点）。 */
  private tryBuildHandles(key: number, wrap: HTMLElement): boolean {
    const q = (cls: string) => wrap.querySelector(cls) as HTMLElement | null;
    const actions = q(".rag-agent-actions");
    const bubble = q(".rag-msg-bubble");
    if (!actions || !bubble) return false;
    this.bubbleRegistry.set(key, {
      key,
      wrap,
      bubble,
      agentActions: actions,
      agentBadge: q(".rag-agent-badge") as HTMLElement,
      agentTimer: q(".rag-agent-timer") as HTMLElement,
      timerValue: q(".rag-timer-value") as HTMLElement,
      progressEl: q(".rag-progress") as HTMLElement,
      progressBar: q(".rag-progress-bar") as HTMLElement,
      progressPct: q(".rag-progress-pct") as HTMLElement,
    });
    if (wrap.isConnected) this.scrollToBottom();
    return true;
  }

  /** 轮询等待某助手气泡句柄就绪（流式入口用，最多 ~20 帧） */
  private waitForBubble(key: number, tries = 20): Promise<boolean> {
    return new Promise((resolve) => {
      let n = 0;
      const check = () => {
        if (this.bubbleRegistry.has(key)) return resolve(true);
        if (++n > tries) return resolve(false);
        window.setTimeout(check, 0);
      };
      check();
    });
  }

  // ------------------------------------------------------------
  //  发送与流式接收
  // ------------------------------------------------------------

  sendPreset(question: string): void {
    if (!this.inputEl) return;
    this.inputEl.value = question;
    void this.sendFromEl(this.inputEl);
  }

  async sendFromEl(el: HTMLTextAreaElement | null): Promise<void> {
    if (!el) return;
    const question = el.value.trim();
    if (!question || this.streaming) return;
    el.value = "";
    await this.send(question);
  }

  private async send(question: string): Promise<void> {
    this.streaming = true;
    this.setChatStatus("streaming");

    const sessionKey = this.activeKey; // 记录本次流式所属会话（可能为 null=新对话）
    const live = this.ensureLive(sessionKey);

    // 用户消息
    const userKey = this.seq++;
    live.push({ key: userKey, role: "user", content: question, streaming: false });
    this.history.push({ role: "user", content: question });
    this.requestTick();
    // P11-2：sessionIdPromise 局部持有最终会话 id——读 this.activeSessionId
    // 会在「流式中途切换会话」后把消息写进切换后的会话（服务端串号）。
    const sessionIdPromise = this.ensureSession(question);
    void sessionIdPromise.then((sid) => {
      if (sid) void this.plugin.api.appendMessage(sid, "user", question);
    });

    // 助手外壳（state-driven）
    const asstKey = this.seq++;
    live.push({ key: asstKey, role: "assistant", content: "", streaming: true });
    this.requestTick();

    // 等 React 渲染外壳、句柄构建好（含延迟构建与重试）再开始流式
    const ok = await this.waitForBubble(asstKey);
    if (!ok) {
      new Notice("⚠️ 助手气泡渲染未就绪，请重试");
      this.streaming = false;
      this.setChatStatus("connected");
      return;
    }
    const h = this.bubbleRegistry.get(asstKey);
    if (!h) {
      this.streaming = false;
      this.setChatStatus("connected");
      return;
    }
    await this.runStream(question, sessionKey, asstKey, h, sessionIdPromise);
  }

  private async runStream(
    question: string,
    sessionKey: string | null,
    asstKey: number,
    h: BubbleHandles,
    sessionIdPromise: Promise<string | null>
  ): Promise<void> {
    const { timerValue, progressBar, progressPct } = h;

    const controller = new AbortController();
    this.activeController = controller;
    const IDLE_MS = 120000;
    const HARD_MS = 600000;
    let timedOut = false;
    const abortByTimeout = () => {
      timedOut = true;
      controller.abort();
    };
    let idleTimer = window.setTimeout(abortByTimeout, IDLE_MS);
    const hardTimer = window.setTimeout(abortByTimeout, HARD_MS);
    const resetIdleTimer = () => {
      window.clearTimeout(idleTimer);
      idleTimer = window.setTimeout(abortByTimeout, IDLE_MS);
    };

    const startTime = Date.now();
    const timerInterval = window.setInterval(() => {
      timerValue?.setText(`${((Date.now() - startTime) / 1000).toFixed(1)}s`);
    }, 100);

    let lastPct = 0;
    const setProgress = (p: number) => {
      lastPct = Math.max(lastPct, Math.min(100, p));
      progressBar.style.width = `${lastPct}%`;
      progressPct?.setText(`${Math.round(lastPct)}%`);
    };
    setProgress(3);

    let fullAnswer = "";
    let serverTiming: TimingInfo | null = null;
    let stopped = false;
    let lastTick = 0;

    /** 更新状态驱动的内容 + 节流触发重渲染（切换会话后仍在后台累积，切换回来立即可见） */
    const updateContent = (content: string, streaming: boolean, stats?: string) => {
      const live = this.liveBySession.get(sessionKey);
      const m = live?.find((x) => x.key === asstKey);
      if (m) {
        m.content = content;
        m.streaming = streaming;
        if (stats !== undefined) m.statsText = stats;
      }
      if (this.activeKey !== sessionKey) return; // 不在当前会话：只写状态，不渲染
      const now = Date.now();
      if (now - lastTick >= 200) {
        lastTick = now;
        this.requestTick();
      }
    };

    const finalize = async () => {
      window.clearInterval(timerInterval);
      const stats = this.renderStatsText(serverTiming, fullAnswer, startTime);
      updateContent(fullAnswer || "（已停止生成）", false, stats);
      // P11-2：中途切换会话后 this.history 已属于别的会话，不得推送；
      // 服务端 append 仍按原会话写入，切回时 switchSession 从服务端重建，不丢内容
      if (this.activeKey === sessionKey) {
        this.history.push({ role: "assistant", content: fullAnswer });
      }
      const sid = await sessionIdPromise;
      if (sid) {
        void this.plugin.api.appendMessage(sid, "assistant", fullAnswer);
      }
      this.setChatStatus(stopped ? "connected" : "done");
    };

    try {
      await this.plugin.api.queryStream(
        question,
        this.history,
        this.plugin.settings.topK,
        this.plugin.settings.useRerank,
        {
          onSources: (src) => {
            // 声明式：来源写入该消息的 live 状态（跨切换存活），由 CitationsBlock 渲染
            const live = this.liveBySession.get(sessionKey);
            const m = live?.find((x) => x.key === asstKey);
            if (m) m.sources = src;
            if (this.activeKey === sessionKey) this.requestTick();
          },
          onPhase: () => {
            // 阶段徽标由 React 按 streaming 展示，服务端 phase 不再逐个改
          },
          onTiming: (timing) => {
            serverTiming = timing;
          },
          onChunk: (chunk) => {
            fullAnswer += chunk;
            resetIdleTimer();
            setProgress(100 * (1 - Math.exp(-fullAnswer.length / 600)));
            updateContent(fullAnswer, true);
          },
          onDone: (full) => {
            fullAnswer = full || fullAnswer;
            setProgress(100);
            updateContent(fullAnswer, true);
            void finalize();
          },
          onError: (msg) => {
            window.clearInterval(timerInterval);
            updateContent(`❌ ${msg}`, false);
            this.setChatStatus("connected");
          },
        },
        controller.signal
      );
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        if (timedOut) {
          fullAnswer += "\n\n> ⏱ 生成超时被截断（120s 无新内容，或总时长超 600s）";
          new Notice("⏱ 回答生成超时已截断，可检查模型服务是否卡慢或调大服务端 max_tokens");
        }
        stopped = true;
        void finalize();
      } else {
        console.error("RAG 流式请求失败:", err);
        window.clearInterval(timerInterval);
        updateContent(`❌ 网络错误: ${(err as Error).message}`, false);
        this.setChatStatus("connected");
      }
    } finally {
      window.clearTimeout(idleTimer);
      window.clearTimeout(hardTimer);
      this.activeController = null;
      this.streaming = false;
      this.requestTick();
    }
  }

  /** 停止生成（React 停止按钮直接调用 → 必须是 public） */
  stopGeneration(): void {
    this.activeController?.abort();
  }

  private cleanupStreaming(): void {
    this.activeController?.abort();
    this.activeController = null;
    this.streaming = false;
  }

  /** 首条消息时懒创建服务端会话（标题取问题前 20 字）；返回最终会话 id（P11-2） */
  private async ensureSession(question: string): Promise<string | null> {
    if (this.activeSessionId) return this.activeSessionId;
    try {
      const s = await this.plugin.api.createSession(question.slice(0, 20));
      this.activeSessionId = s.id;
      void this.loadSessions();
      return s.id;
    } catch (e) {
      console.error("创建会话失败（消息仍正常发送）:", e);
      return null;
    }
  }

  // ------------------------------------------------------------
  //  编辑重发
  // ------------------------------------------------------------

  editUserMessage(index: number): void {
    if (this.streaming) {
      new Notice("⏳ 正在生成中，请先停止生成再编辑");
      return;
    }
    const live = this.liveBySession.get(this.activeKey) ?? [];
    const msg = live[index];
    if (!msg || msg.role !== "user") return;
    const content = msg.content;

    // 截断本地（该用户消息及其后全部移除）
    this.liveBySession.set(this.activeKey, live.slice(0, index));
    this.history = this.history.slice(0, index);
    if (this.activeSessionId) {
      void this.plugin.api.truncateSession(this.activeSessionId, index).catch((e) => {
        console.error("服务端会话截断失败:", e);
        new Notice(`⚠️ 会话同步失败: ${(e as Error).message}`);
      });
    }
    this.requestTick();

    if (this.inputEl) {
      this.inputEl.value = content;
      this.inputEl.focus();
    }
  }

  // ------------------------------------------------------------
  //  渲染工具（状态驱动的消息组件调用）
  // ------------------------------------------------------------

  /** 把 Markdown 渲染进指定气泡（聊天面板 React 组件按内容状态调用） */
  renderMarkdownInto(bubble: HTMLElement, content: string, withCursor: boolean): void {
    bubble.empty();
    void MarkdownRenderer.render(this.app, content || "…", bubble, "", this);
    if (withCursor) {
      const cursor = document.createElement("span");
      cursor.className = "rag-cursor";
      const lastBlock = bubble.lastElementChild as HTMLElement | null;
      (lastBlock ?? bubble).appendChild(cursor);
    }
  }

  /** 打开引用来源笔记（React 引用卡片点击） */
  openSource(s: SourceInfo): void {
    void this.plugin.openSource(s);
  }

  /** 打开多模态附件图片（系统默认程序） */
  openImage(imgPath: string): void {
    if (electron?.shell) {
      void electron.shell.openPath(imgPath).catch(() => {});
    }
  }

  private fmtMs(ms?: number): string {
    if (ms == null || !isFinite(ms)) return "—";
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }

  /** 完成态统计文本：⏱ 耗时 + 分阶段 + tokens/toks/s（由消息组件按状态渲染，跨切换存活） */
  private renderStatsText(timing: TimingInfo | null, answer: string, startMs: number): string {
    const parts: string[] = [];
    const totalMs = timing?.total_ms ?? (Date.now() - startMs);
    parts.push(`⏱ ${this.fmtMs(totalMs)}`);
    if (timing) {
      const inner: string[] = [];
      const stageDetail: string[] = [];
      const rt = timing.retrieve;
      if (rt?.embed_ms != null) stageDetail.push(`嵌入 ${this.fmtMs(rt.embed_ms)}`);
      if (rt?.recall_ms != null) stageDetail.push(`召回 ${this.fmtMs(rt.recall_ms)}`);
      if (rt?.rerank_ms != null) stageDetail.push(`重排 ${this.fmtMs(rt.rerank_ms)}`);
      if (timing.retrieve_ms != null) {
        inner.push(`检索 ${this.fmtMs(timing.retrieve_ms)}${stageDetail.length ? `（${stageDetail.join("·")}）` : ""}`);
      }
      if (timing.generate_ms != null) inner.push(`生成 ${this.fmtMs(timing.generate_ms)}`);
      if (inner.length) parts.push(`（${inner.join("，")}）`);
    }
    const u = timing?.usage;
    const genSec = timing?.generate_ms ? timing.generate_ms / 1000 : (Date.now() - startMs) / 1000;
    if (u && u.completion_tokens != null) {
      parts.push(`${u.completion_tokens} tokens`);
      if (u.generation_tokens_per_second != null) {
        parts.push(`≈${Math.round(u.generation_tokens_per_second)} toks/s`);
      } else if (genSec > 0 && u.completion_tokens > 0) {
        parts.push(`≈${Math.round(u.completion_tokens / genSec)} toks/s`);
      }
    } else if (answer && genSec > 0) {
      const est = Math.max(1, Math.round(answer.length * 0.75));
      parts.push(`≈${est} tokens · ≈${Math.round(est / genSec)} toks/s（估算）`);
    }
    return parts.join(" · ");
  }

  /** 滚动消息区到底部 */
  private scrollToBottom(): void {
    const el = this.containerEl.querySelector(".rag-chat-messages");
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }
}