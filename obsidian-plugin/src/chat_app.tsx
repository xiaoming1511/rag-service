/**
 * RAG 聊天面板 UI（React 渲染层）
 *
 * 职责划分：
 * - 本文件：只做「渲染」——把 ChatView（逻辑持有者）的快照画出来
 * - ChatView：拥有全部业务逻辑（发送/流式/会话/导出/编辑重发…）
 *
 * React 安全约定（吸取此前“面板空白”教训）：
 * 1. 整个视图只有 1 个 React 根，onClose 时 unmount；
 * 2. 组件仅因 view.notify() 快照变化而重渲染；
 * 3. 助手气泡外壳用 React.memo 冻结：流式内容由 ChatView 通过 ref 注册表
 *    命令式写入（Markdown/计时/进度/停止按钮/统计行），React 永不回写；
 * 4. 每个区块（头部/消息区/历史面板/输入区/底部）都有错误边界，
 *    任何一处出错都显示可见错误 + 重试，绝不出现“整块空白”。
 */
import React, { Component, ReactNode, memo, useEffect, useMemo, useRef, useState } from "react";
import { Notice, setIcon } from "obsidian";
import type { ChatView } from "./chat_view";
import type { SessionMeta } from "./chat_view";
// 注意：SourceInfo 必须显式导入。此前漏了它，而构建只跑 esbuild（不做类型
// 检查），于是这个错误被完全掩盖 —— 打开 tsc 后它立刻报 TS2304。
import type { SourceInfo } from "./api";

// ================================================================
// 错误边界
// ================================================================

interface BoundaryState {
  failed: boolean;
  msg?: string;
}

class UiBoundary extends Component<
  { title: string; fallback?: (msg: string, reset: () => void) => ReactNode; children: ReactNode },
  BoundaryState
> {
  state: BoundaryState = { failed: false };

  static getDerivedStateFromError(err: unknown): BoundaryState {
    return { failed: true, msg: String((err as Error)?.message ?? err) };
  }

  componentDidCatch(err: unknown): void {
    console.error(`[RAG-UI] ${this.props.title} 渲染失败:`, err);
  }

  render(): ReactNode {
    if (this.state.failed) {
      if (this.props.fallback) return this.props.fallback(this.state.msg ?? "", () => this.setState({ failed: false, msg: undefined }));
      return (
        <div className="rag-ui-error">
          <div>「{this.props.title}」渲染出错：{this.state.msg}</div>
          <button className="rag-ui-retry" onClick={() => this.setState({ failed: false, msg: undefined })}>
            重试
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

function Icon({ name, cls }: { name: string; cls?: string }): React.JSX.Element {
  return (
    <span
      className={cls}
      ref={(el) => {
        if (el && el.childElementCount === 0) setIcon(el, name);
      }}
    />
  );
}

// ================================================================
// 顶部标题栏
// ================================================================

function Header({ view, historyOpen }: { view: ChatView; historyOpen: boolean }): React.JSX.Element {
  return (
    <div className="rag-header">
      <div className="rag-header-title">
        <span className="rag-header-icon">
          <Icon name="message-square" />
        </span>
        <span>对话式查询</span>
      </div>
      <div className="rag-header-actions">
        <div
          className="rag-header-btn"
          aria-label="增量索引：把笔记的新增/修改/删除同步到向量库"
          onClick={() => void view.refreshIndex()}
        >
          <Icon name="refresh-cw" />
        </div>
        <div
          className="rag-header-btn"
          aria-label="查看历史会话"
          onClick={(e) => {
            e.stopPropagation();
            view.toggleHistory();
          }}
        >
          <Icon name="history" />
          <span className="rag-header-btn-text">历史会话</span>
        </div>
        <div className="rag-header-btn rag-header-btn-accent" aria-label="新建对话" onClick={() => void view.newSession()}>
          <Icon name="plus" />
          <span className="rag-header-btn-text">新建对话</span>
        </div>
      </div>
    </div>
  );
}

// ================================================================
// 信息横幅
// ================================================================

function Banner(): React.JSX.Element {
  return (
    <div className="rag-info-banner">
      <span className="rag-info-icon">
        <Icon name="info" />
      </span>
      <span className="rag-info-text">回答基于知识库内容生成，下方附带引用来源，可追溯至原始笔记。</span>
    </div>
  );
}

// ================================================================
// 空状态（含建议问题 + 「查看开关说明」卡片）
// ================================================================

const SUGGESTED_QUESTIONS = ["总结我的 RAG 知识库技术栈", "文档分块策略是怎样的？", "如何提升检索准确率？"];

function EmptyState({ view }: { view: ChatView }): React.JSX.Element {
  return (
    <div className="rag-empty-state">
      <div className="rag-empty-icon">
        <Icon name="message-square" />
      </div>
      <div className="rag-empty-title">与知识库对话</div>
      <div className="rag-empty-desc">基于知识库内容提问，回答附带引用来源，可追溯至原始笔记。</div>
      <div className="rag-empty-hint">试试这样问</div>
      <div className="rag-empty-suggestions">
        {SUGGESTED_QUESTIONS.map((q) => (
          <div key={q} className="rag-suggestion-card" onClick={() => view.sendPreset(q)}>
            <span className="rag-suggestion-text">{q}</span>
            <span className="rag-suggestion-arrow">
              <Icon name="arrow-right" />
            </span>
          </div>
        ))}
        <div className="rag-suggestion-card" onClick={() => void view.openDocsNote()}>
          <span className="rag-suggestion-text">📖 查看 RAG 服务开关说明</span>
          <span className="rag-suggestion-arrow">
            <Icon name="arrow-right" />
          </span>
        </div>
      </div>
    </div>
  );
}

// ================================================================
// 消息列表（用户气泡 + 助手外壳）
// ================================================================

/** 用户消息：一次性渲染纯文本，编辑重发按钮挂在 wrap 上。memo：流式重渲染时不重复渲染用户气泡 */
function UserBubbleInner({ view, msg, index }: { view: ChatView; msg: ViewMessage; index: number }): React.JSX.Element {
  return (
    <div className="rag-msg rag-msg-user" data-msg-index={index}>
      <div className="rag-msg-bubble">{msg.content}</div>
      <div className="rag-user-label">
        <span>You</span>
        <span className="rag-user-label-icon">
          <Icon name="user" />
        </span>
        <span
          className="rag-user-edit"
          aria-label="编辑并重新发送（撤回该消息及之后的对话）"
          onClick={() => view.editUserMessage(index)}
        >
          <Icon name="pencil" />
        </span>
      </div>
    </div>
  );
}

const UserBubble = memo(UserBubbleInner);

/** 助手消息：外壳由 React 渲染一次；内容由「状态驱动」渲染——
    每次 content/streaming 变化都重新 render Markdown（流式增长/历史加载/会话切换回退都能正确显示）。
    徽章/计时/进度条/停止按钮/统计行仍由 ChatView 经句柄命令式更新（选中/停流式用）。 */
function AssistantShellInner({ view, msg }: { view: ChatView; msg: ViewMessage }): React.JSX.Element {
  const bubbleRef = useRef<HTMLDivElement>(null);

  // 渲染策略（流畅度关键）：
  // - 流式中：内容用「纯文本追加」更新（瞬时、零 Markdown 解析开销）——避免每个 chunk
  //   都全量 Markdown 重渲染导致的卡顿；仅有打字动画（无内容）或纯文本（有内容）。
  // - 非流式（完成/历史）：一次性 Markdown 渲染（Obsidian 阅读感）。
  useEffect(() => {
    const bubble = bubbleRef.current;
    if (!bubble) return;
    if (msg.streaming) {
      bubble.empty();
      if (msg.content === "") {
        const typing = bubble.createDiv({ cls: "rag-typing" });
        typing.createEl("i");
        typing.createEl("i");
        typing.createEl("i");
      } else {
        const plain = bubble.createSpan({ cls: "rag-plain-stream" });
        plain.setText(msg.content);
      }
      return;
    }
    view.renderMarkdownInto(bubble, msg.content || "…", false);
  }, [msg.content, msg.streaming, view]);

  return (
    <div
      className="rag-msg rag-msg-assistant"
      ref={(wrap) => {
        // 无条件注册：就绪与否由 ChatView.tryBuildHandles 内部处理（延迟到子元素挂好后）。
        if (wrap) view.registerAssistantShell(msg.key, wrap);
      }}
    >
      {msg.streaming ? (
        <div className="rag-agent-row">
          <div className="rag-agent-name">
            <span className="rag-agent-icon">
              <Icon name="bot" />
            </span>
            <span>Agent</span>
          </div>
          <span className="rag-agent-badge">生成中</span>
          <span className="rag-agent-timer">
            <span className="rag-timer-prefix">已用时 </span>
            <span className="rag-timer-value">0.0s</span>
          </span>
          <div className="rag-agent-actions">
            <div
              className="rag-agent-btn rag-agent-stop"
              aria-label="停止生成"
              onClick={() => view.stopGeneration()}
            >
              <span className="rag-stop-square" />
              <span className="rag-stop-text">停止</span>
              <span className="rag-stop-text-narrow">生成</span>
            </div>
          </div>
        </div>
      ) : null}
      {msg.streaming ? (
        <div className="rag-progress">
          <div className="rag-progress-track">
            <div className="rag-progress-bar" style={{ width: "0%" }} />
          </div>
          <span className="rag-progress-pct">0%</span>
        </div>
      ) : null}
      <div className="rag-msg-bubble" ref={bubbleRef} />
      {!msg.streaming && msg.content ? (
        <div className="rag-agent-actions">
          <div
            className="rag-agent-btn"
            aria-label="复制回答"
            onClick={() => {
              void navigator.clipboard
                .writeText(msg.content)
                .then(() => new Notice("✅ 已复制回答"))
                .catch(() => new Notice("❌ 复制失败"));
            }}
          >
            <Icon name="copy" />
            <span>复制</span>
          </div>
        </div>
      ) : null}
      {msg.statsText && !msg.streaming ? <div className="rag-msg-stats">{msg.statsText}</div> : null}
      <CitationsBlock view={view} sources={msg.sources} />
    </div>
  );
}

/** 引用来源（声明式、可展开、可点击跳转；状态驱动，跨会话切换存活） */
function CitationsBlock({ view, sources }: { view: ChatView; sources?: SourceInfo[] }): React.JSX.Element | null {
  const [open, setOpen] = useState(false);
  if (!sources || sources.length === 0) return null;
  const methodText = view.plugin?.settings?.useRerank ? "向量检索 + 重排序" : "向量检索";
  return (
    <div className={`rag-citations${open ? " is-open" : ""}`}>
      <div className="rag-citation-line" onClick={() => setOpen(!open)}>
        <span className="rag-citation-icon">
          <Icon name="search" />
        </span>
        <span>引用 {sources.length} 个页面</span>
        <span className="rag-citation-method"> · {methodText}</span>
        <span className="rag-citation-chevron">
          <Icon name={open ? "chevron-up" : "chevron-down"} />
        </span>
      </div>
      {open ? (
        <div className="rag-citation-list">
          {sources.map((s, i) => (
            <div key={i} className="rag-source">
              <div className="rag-source-head">
                <a className="rag-source-link" onClick={() => view.openSource(s)}>
                  {s.file_name}
                </a>
                <span className="rag-source-score"> {(s.score * 100).toFixed(1)}%</span>
              </div>
              {(s.heading || (s.line_start && s.line_end)) ? (
                <div className="rag-source-heading">
                  {[s.heading ? `📍 ${s.heading}` : null, s.line_start && s.line_end ? `第 ${s.line_start}-${s.line_end} 行` : null]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
              ) : null}
              {s.images && s.images.length > 0
                ? s.images.map((img, j) => (
                    <div key={j} className="rag-source-img">
                      <a onClick={() => view.openImage(img.path)}>🖼 {img.caption?.slice(0, 24) || "图片"}</a>
                    </div>
                  ))
                : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// 注意：不 memo —— 内容状态驱动，content/streaming 变化时必须重渲染
const AssistantShell = AssistantShellInner;

/** 消息列表 */
function Messages({ view }: { view: ChatView }): React.JSX.Element {
  const messages = view.messagesSnapshot;
  if (messages.length === 0) return <EmptyState view={view} />;
  return (
    <div className="rag-chat-messages" onClick={() => view.closeHistory()}>
      {messages.map((m, i) =>
        m.role === "user" ? (
          <UserBubble key={m.key} view={view} msg={m} index={i} />
        ) : (
          <AssistantShell key={m.key} view={view} msg={m} />
        )
      )}
    </div>
  );
}

// ================================================================
// 历史会话面板（React 列表：搜索 + 日期分组 + 导出/重命名/删除）
// ================================================================

function dateLabel(ts: number | undefined): string {
  if (!ts || !isFinite(ts)) return "未标注时间";
  const d = new Date(ts * 1000);
  const now = new Date();
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  if (diff === 0) return "今天";
  if (diff === 1) return "昨天";
  const mmdd = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return d.getFullYear() === now.getFullYear() ? mmdd : `${d.getFullYear()}-${mmdd}`;
}

function HistoryPanel({ view }: { view: ChatView }): React.JSX.Element | null {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const sessions = view.sessionsSnapshot;
  const list = useMemo(() => {
    const arr = sessions
      .filter((s) => !q || (s.title || "").toLowerCase().includes(q))
      .slice()
      .sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
    return arr;
  }, [sessions, q]);

  const groups = useMemo(() => {
    const map = new Map<string, SessionMeta[]>();
    for (const s of list) {
      const label = dateLabel(s.updated_at ?? s.created_at);
      const arr = map.get(label) ?? [];
      arr.push(s);
      map.set(label, arr);
    }
    return [...map.entries()];
  }, [list]);

  return (
    <UiBoundary title="历史会话">
      {view.historyOpen ? (
        <div
          className="rag-history-panel is-open"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="rag-history-search">
            <input
              type="text"
              placeholder={`搜索会话（${sessions.length} 个）…`}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          {groups.length === 0 ? (
            <div className="rag-history-empty">{q ? "没有匹配的会话" : "暂无历史会话"}</div>
          ) : (
            groups.map(([label, items]) => (
              <div key={label}>
                <div className="rag-history-group-label">{label}</div>
                {items.map((s) => (
                  <div key={s.id} className={`rag-history-item${s.id === view.activeSessionId ? " is-active" : ""}`}>
                    <div className="rag-history-main" onClick={() => view.selectSession(s.id)}>
                      <div className="rag-history-title">{s.title || "未命名会话"}</div>
                      <div className="rag-history-meta">{s.message_count ?? 0} 条消息</div>
                    </div>
                    <div className="rag-history-ops">
                      <span
                        className="rag-history-op"
                        aria-label="导出为 Markdown"
                        title="导出"
                        onClick={(e) => {
                          e.stopPropagation();
                          void view.exportSession(s.id);
                        }}
                      >
                        <Icon name="download" />
                      </span>
                      <span
                        className="rag-history-op"
                        aria-label="重命名"
                        title="重命名"
                        onClick={(e) => {
                          e.stopPropagation();
                          view.promptRename(s.id);
                        }}
                      >
                        <Icon name="pencil" />
                      </span>
                      <span
                        className="rag-history-op rag-history-op-danger"
                        aria-label="删除会话"
                        title="删除"
                        onClick={(e) => {
                          e.stopPropagation();
                          void view.deleteSession(s.id);
                        }}
                      >
                        <Icon name="trash-2" />
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      ) : null}
    </UiBoundary>
  );
}

// ================================================================
// 输入区
// ================================================================

function InputArea({ view }: { view: ChatView }): React.JSX.Element {
  const ref = useRef<HTMLTextAreaElement>(null);
  return (
    <div className="rag-chat-input-area">
      <textarea
        ref={(el) => {
          view.inputEl = el;
          (ref as React.MutableRefObject<HTMLTextAreaElement | null>).current = el;
        }}
        className="rag-chat-input"
        rows={2}
        placeholder="输入问题…（Enter 发送，Shift+Enter 换行）"
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void view.sendFromEl(ref.current);
          }
        }}
      />
      <div
        className={`rag-chat-send${view.streaming ? " is-disabled" : ""}`}
        role="button"
        aria-label="发送"
        onClick={() => void view.sendFromEl(ref.current)}
      >
        <Icon name="send-horizontal" />
      </div>
    </div>
  );
}

// ================================================================
// 底部状态栏
// ================================================================

function Footer({ view }: { view: ChatView }): React.JSX.Element {
  const [cls, text] = STATUS_MAP[view.connectionStatus] ?? ["is-muted", "连接中…"];
  return (
    <div className="rag-chat-footer">
      <span className="rag-footer-note">由本地 RAG 管线生成 · 内容仅供参考</span>
      <div className="rag-footer-status">
        <span className={`rag-status-dot ${cls}`} />
        <span className="rag-status-text">{text}</span>
      </div>
    </div>
  );
}

const STATUS_MAP: Record<string, [string, string]> = {
  connecting: ["is-muted", "连接中…"],
  connected: ["is-ok", "服务已连接"],
  streaming: ["is-streaming", "流式输出中"],
  done: ["is-muted", "回答完成"],
  offline: ["is-error", "服务离线"],
};

// ================================================================
// 应用根
// ================================================================

export interface ViewMessage {
  key: number;
  role: "user" | "assistant";
  content: string;
  /** 该消息是否正在流式生成中（用于气泡展示打字动画/持续渲染） */
  streaming?: boolean;
  /** 完成态统计文本（耗时/tokens/toks/s），由 ChatView 计算后写入状态 */
  statsText?: string;
  /** 引用来源（声明式渲染可展开、可点击跳转；状态驱动、跨切换存活） */
  sources?: SourceInfo[];
}

export function ChatApp({ view }: { view: ChatView }): React.JSX.Element {
  const [, setTick] = useState(0);
  useEffect(() => {
    view.uiNotifier = () => setTick((t) => t + 1);
    view.requestTick();
    return () => {
      view.uiNotifier = null;
    };
  }, [view]);

  return (
    <UiBoundary title="聊天面板">
      <Header view={view} historyOpen={view.historyOpen} />
      <Banner />
      <UiBoundary title="消息区">
        <Messages view={view} />
      </UiBoundary>
      <InputArea view={view} />
      <Footer view={view} />
      <HistoryPanel view={view} />
    </UiBoundary>
  );
}