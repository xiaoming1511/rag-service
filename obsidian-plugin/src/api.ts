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
}

export interface QueryResult {
  answer: string;
  sources: SourceInfo[];
  total_results: number;
}

export interface StreamHandlers {
  onSources?: (sources: SourceInfo[]) => void;
  onChunk?: (text: string) => void;
  onDone?: (fullAnswer: string) => void;
  onError?: (message: string) => void;
}

/** 流式事件类型 */
type StreamEvent =
  | { type: "sources"; data: SourceInfo[] }
  | { type: "chunk"; data: string }
  | { type: "done"; data: string }
  | { type: "error"; data: string };

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

  /** 状态查询 */
  async status(): Promise<{ vector_count: number; collection_name: string; status: string }> {
    const resp = await fetch(this.url("/status"), { headers: { Authorization: `Bearer ${this.apiKey}` } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  /** 触发增量索引 */
  async refreshIndex(): Promise<{ added: number; updated: number; removed: number; unchanged: number }> {
    return this.postJson("/index/refresh", {});
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
      let detail = `HTTP ${resp.status}`;
      try {
        const data = await resp.json();
        detail = data.detail || detail;
      } catch {
        /* 忽略解析失败 */
      }
      handlers.onError?.(detail);
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
        case "chunk":
          handlers.onChunk?.(evt.data);
          break;
        case "done":
          handlers.onDone?.(evt.data);
          break;
        case "error":
          handlers.onError?.(evt.data);
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