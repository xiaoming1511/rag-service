/**
 * RAG Service API 客户端
 * 封装与本地 RAG 服务（FastAPI）的 HTTP 通信：
 * - 健康检查 /v1/health
 * - 流式问答 /v1/query/stream（SSE）
 * - 增量索引 /v1/index/refresh
 * - 状态 /v1/status
 */

export interface SourceInfo {
  file_name: string;
  content: string;
  score: number;
  file_path?: string;
  heading?: string;
  line_start?: number;   // 行级引文：命中片段起始行
  line_end?: number;     // 行级引文：命中片段结束行
  images?: { path: string; caption: string; source_file?: string }[]; // 多模态附件
}

export interface QueryResult {
  answer: string;
  sources: SourceInfo[];
  total_results: number;
}

/** 服务端分阶段耗时 + 用量（来自 SSE timing 事件 / 同步响应 timing_ms） */
export interface TimingInfo {
  total_ms?: number;
  rewrite_ms?: number;
  retrieve_ms?: number;
  retrieve?: {
    embed_ms?: number;
    recall_ms?: number;
    rerank_ms?: number;
    total_ms?: number;
  };
  generate_ms?: number;
  /** oMLX 真实 token 用量（流式 include_usage 末块 / 非流式 usage） */
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    generation_tokens_per_second?: number;
    time_to_first_token?: number;
    generation_duration?: number;
    total_time?: number;
  };
  cached?: boolean;
}

export interface StreamHandlers {
  onSources?: (sources: SourceInfo[]) => void;
  onChunk?: (text: string) => void;
  onDone?: (fullAnswer: string) => void;
  onError?: (message: string) => void;
  onPhase?: (phase: "检索中" | "生成中") => void;
  onTiming?: (timing: TimingInfo) => void;
}

// ================================================================
//  后端已具备、此前插件未接通的能力（第四轮接通）
//  这些能力在服务端早已实现（见 feature-map 报告 5.3 节），缺的只是插件入口。
//  是否在界面中出现由 settings.features.* 开关控制（默认关，保持界面简洁）。
// ================================================================

/** 后台摄入任务（POST /v1/index/async、GET /v1/index/jobs） */
export interface JobInfo {
  id: string;
  kind: "full" | "incremental" | "url" | string;
  status: "queued" | "running" | "done" | "failed" | "cancelled" | string;
  progress: string;
  progress_data?: {
    stage?: string;
    current?: number;
    total?: number;
    percent?: number;
    note?: string;
  } | null;
  error?: string | null;
  result?: Record<string, unknown> | null;
  created_at: number;
  started_at?: number | null;
  finished_at?: number | null;
}

/** 全量索引响应（POST /v1/index） */
export interface IndexAllResult {
  success: boolean;
  total_documents: number;
  total_chunks: number;
  vector_count: number;
  message: string;
}

/** 网页索引响应（POST /v1/index/url） */
export interface IndexUrlResult {
  success: boolean;
  url: string;
  title?: string | null;
  chunk_count: number;
  message: string;
}

/** 深度研究响应（POST /v1/research） */
export interface ResearchResult {
  report: string;
  sub_queries: string[];
  sources: SourceInfo[];
  total_results: number;
  /** 实际执行轮次：可能因"本轮无新信息"提前停止，小于请求的 max_rounds */
  rounds: number;
}

/** 错误详情统一转字符串（FastAPI 422 的 detail 可能是对象数组，直接拼接会显示 [object Object]） */
function detailToString(detail: unknown): string {
  if (typeof detail === "string") return detail;
  try {
    return JSON.stringify(detail, null, 0);
  } catch {
    return String(detail);
  }
}

/** 流式事件类型 */
type StreamEvent =
  | { type: "sources"; data: SourceInfo[] }
  | { type: "phase"; data: { phase: "检索中" | "生成中" } }
  | { type: "timing"; data: TimingInfo }
  | { type: "chunk"; data: string }
  | { type: "done"; data: string }
  | { type: "error"; data: unknown };

export class RAGApiClient {
  constructor(
    public baseUrl: string,
    public apiKey: string = "dummy"
  ) {}

  /** 拼接接口地址（容错：用户可能带/不带尾部斜杠） */
  private url(path: string): string {
    return `${this.baseUrl.replace(/\/+$/, "")}${path}`;
  }

  private async postJson(path: string, body: unknown): Promise<any> {
    const resp = await fetch(this.url(path), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.apiKey}` },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const data = await resp.json();
        detail = data.detail || detail;
      } catch {
        /* 忽略解析失败 */
      }
      throw new Error(detail);
    }
    return resp.json();
  }

  /** 健康检查 */
  async health(): Promise<{ status: string }> {
    const resp = await fetch(this.url("/health"), { headers: { Authorization: `Bearer ${this.apiKey}` } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 状态查询（含 metrics/index_job；服务端已扩展） */
  async status(): Promise<any> {
    const resp = await fetch(this.url("/status"), { headers: { Authorization: `Bearer ${this.apiKey}` } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 导出全部会话（JSON） */
  async exportAllSessions(): Promise<any> {
    const resp = await fetch(this.url("/sessions/export"), { headers: { Authorization: `Bearer ${this.apiKey}` } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 清理旧会话：保留最新 keep 条 */
  async cleanupSessions(keep = 100): Promise<any> {
    return this.postJson("/sessions/cleanup", { keep });
  }

  /** 触发增量索引 */
  async refreshIndex(): Promise<{ added: number; updated: number; removed: number; unchanged: number }> {
    return this.postJson("/index/refresh", {});
  }

  /** 任意格式 → Markdown（调用服务端 /v1/convert/to-md） */
  async documentToMarkdown(
    format: string,
    content: string,
    isBase64: boolean
  ): Promise<{ ok: boolean; markdown: string; title?: string; format?: string }> {
    const data = await this.postJson("/convert/to-md", {
      format,
      content,
      content_is_base64: isBase64,
    });
    if (!data.ok) throw new Error("转换返回异常");
    return data;
  }

  /** 读取服务端配置（GET /v1/config） */
  async getServerConfig(): Promise<any> {
    const resp = await fetch(this.url("/config"), { headers: { Authorization: `Bearer ${this.apiKey}` } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 更新服务端配置（POST /v1/config，热生效 + 写回配置文件） */
  async saveServerConfig(patch: Record<string, any>): Promise<any> {
    return this.postJson("/config", patch);
  }

  // ================================================================
  //  会话管理（/v1/sessions）
  // ================================================================

  async listSessions(): Promise<{ sessions: any[] }> {
    const resp = await fetch(this.url("/sessions"), { headers: { Authorization: `Bearer ${this.apiKey}` } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  async createSession(title?: string): Promise<any> {
    return this.postJson("/sessions", { title: title ?? "" });
  }

  async getSession(id: string): Promise<any> {
    const resp = await fetch(this.url(`/sessions/${id}`), { headers: { Authorization: `Bearer ${this.apiKey}` } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  async appendMessage(id: string, role: string, content: string): Promise<any> {
    return this.postJson(`/sessions/${id}/messages`, { role, content });
  }

  async deleteSession(id: string): Promise<any> {
    const resp = await fetch(this.url(`/sessions/${id}`), {
      method: "DELETE",
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 重命名会话（POST /v1/sessions/{id}/rename） */
  async renameSession(id: string, title: string): Promise<any> {
    return this.postJson(`/sessions/${id}/rename`, { title });
  }

  /** 截断会话：仅保留前 keepCount 条消息（撤回/编辑重发） */
  async truncateSession(id: string, keepCount: number): Promise<any> {
    return this.postJson(`/sessions/${id}/truncate`, { keep_count: keepCount });
  }

  // ================================================================
  //  高级能力（入口是否出现由 settings.features.* 开关决定）
  //  这些端点在服务端早已实现，此前插件侧完全没有调用方。
  // ================================================================

  /** 全量索引（POST /v1/index）：rebuild=true 时先清空再重建 */
  async indexAll(rebuild = false): Promise<IndexAllResult> {
    return this.postJson("/index", { rebuild });
  }

  /** 索引单个网页（POST /v1/index/url）：抓取 → 解析 → 分块 → 入库 */
  async indexUrl(url: string, timeout = 30): Promise<IndexUrlResult> {
    return this.postJson("/index/url", { url, timeout });
  }

  /** 提交后台摄入任务（POST /v1/index/async）：full | incremental | url */
  async submitJob(
    kind: "full" | "incremental" | "url",
    opts: { rebuild?: boolean; url?: string; timeout?: number } = {}
  ): Promise<JobInfo> {
    return this.postJson("/index/async", { kind, ...opts });
  }

  /** 列出后台摄入任务（GET /v1/index/jobs，新 → 旧） */
  async listJobs(limit = 50): Promise<{ jobs: JobInfo[]; total: number }> {
    const resp = await fetch(this.url(`/index/jobs?limit=${encodeURIComponent(String(limit))}`), {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 查询单个任务状态（GET /v1/index/jobs/{id}） */
  async getJob(id: string): Promise<JobInfo> {
    const resp = await fetch(this.url(`/index/jobs/${encodeURIComponent(id)}`), {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 取消摄入任务（POST /v1/index/jobs/{id}/cancel） */
  async cancelJob(id: string): Promise<{ ok: boolean; job_id: string }> {
    return this.postJson(`/index/jobs/${encodeURIComponent(id)}/cancel`, {});
  }

  /** 深度研究（POST /v1/research）：问题 → 子查询 → 并行检索 → 汇总报告 */
  async research(question: string, maxRounds?: number): Promise<ResearchResult> {
    const body: Record<string, unknown> = { question };
    if (maxRounds != null) body.max_rounds = maxRounds;
    const data = await this.postJson("/research", body);
    return {
      report: data.report ?? "",
      sub_queries: data.sub_queries ?? [],
      sources: data.sources ?? [],
      total_results: data.total_results ?? 0,
      rounds: data.rounds ?? 1,
    };
  }

  /** 导出完整项目归档（GET /v1/export）→ ZIP 字节（配置+文档清单+向量库+会话+附件） */
  async exportArchive(): Promise<ArrayBuffer> {
    const resp = await fetch(this.url("/export"), {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });
    if (!resp.ok) {
      throw new Error(await this.readErrorDetail(resp));
    }
    return resp.arrayBuffer();
  }

  /** 导入项目归档（POST /v1/import，multipart）；mode = merge | replace */
  async importArchive(zip: ArrayBuffer, mode: "merge" | "replace" = "merge"): Promise<any> {
    const form = new FormData();
    form.append("file", new Blob([zip], { type: "application/zip" }), "archive.zip");
    const resp = await fetch(this.url(`/import?mode=${encodeURIComponent(mode)}`), {
      method: "POST",
      headers: { Authorization: `Bearer ${this.apiKey}` },
      body: form,
    });
    if (!resp.ok) {
      throw new Error(await this.readErrorDetail(resp));
    }
    return resp.json();
  }

  /** 从失败响应里取 detail（FastAPI 的 detail 可能是对象数组，需转字符串） */
  private async readErrorDetail(resp: Response): Promise<string> {
    let detail: unknown = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      detail = data.detail || detail;
    } catch {
      /* 忽略解析失败 */
    }
    return detailToString(detail);
  }

  /**
   * 流式问答（SSE）
   * @param question 问题
   * @param history  对话历史（多轮追问）
   * @param topK     返回结果数
   * @param useRerank 是否重排序
   * @param handlers 事件回调
   * @param signal   取消信号（AbortSignal）
   */
  async queryStream(
    question: string,
    history: { role: string; content: string }[],
    topK: number,
    useRerank: boolean,
    handlers: StreamHandlers,
    signal?: AbortSignal
  ): Promise<void> {
    const resp = await fetch(this.url("/query/stream"), {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.apiKey}` },
      body: JSON.stringify({ question, history: history.slice(-20), top_k: topK, use_rerank: useRerank }),
      signal,
    });

    if (!resp.ok || !resp.body) {
      let detail: unknown = `HTTP ${resp.status}`;
      try {
        const data = await resp.json();
        detail = data.detail || detail;
      } catch {
        /* 忽略解析失败 */
      }
      handlers.onError?.(detailToString(detail));
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const handleEvent = (evt: StreamEvent) => {
      switch (evt.type) {
        case "sources":
          handlers.onSources?.(evt.data);
          break;
        case "phase":
          handlers.onPhase?.(evt.data.phase);
          break;
        case "timing":
          handlers.onTiming?.(evt.data);
          break;
        case "chunk":
          handlers.onChunk?.(evt.data);
          break;
        case "done":
          handlers.onDone?.(evt.data);
          break;
        case "error":
          handlers.onError?.(detailToString(evt.data));
          break;
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // 按 "data: {json}\n\n" 拆分 SSE 事件
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = raw.trim();
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6);
        if (payload === "[DONE]") continue;
        try {
          handleEvent(JSON.parse(payload) as StreamEvent);
        } catch {
          // 忽略非 JSON 的行
        }
      }
    }
  }
}