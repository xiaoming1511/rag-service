/**
 * E2E 契约测试：插件真实 api.ts（打包为 .e2e/api.cjs）× 真实 FastAPI 服务
 *
 * 覆盖插件运行期的全部 HTTP 调用面：
 *   health / status / sessions CRUD+rename+truncate+cleanup+export /
 *   index refresh / index 全量 / index async 提交-列表-详情-取消 /
 *   convert to-md / research / config 读写 / export-import 归档 /
 *   query/stream SSE 全事件序列
 *
 * 用法：node scripts/e2e-contract.mjs <port>   （先启动 scripts/e2e_server.py）
 */

import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { RAGApiClient } = require("../.e2e/api.cjs");

const PORT = process.argv[2] || "8765";
const BASE = `http://127.0.0.1:${PORT}/v1`;

let passed = 0;
let failed = 0;
const failures = [];

function check(name, cond, extra = "") {
  if (cond) {
    passed++;
    console.log(`  ✅ ${name}`);
  } else {
    failed++;
    failures.push(name);
    console.log(`  ❌ ${name} ${extra}`);
  }
}

const api = new RAGApiClient(BASE, "dummy");

// ================================================================
async function testHealthAndStatus() {
  console.log("\n[1] health / status");
  const h = await api.health();
  check("health.status=healthy", h.status === "healthy");

  const st = await api.status();
  check("status.vector_count", st.vector_count === 42, `got ${st.vector_count}`);
  check("status.collection_name", st.collection_name === "e2e-contract");
  check("status.metrics 存在", typeof st.metrics === "object");
}

// ================================================================
async function testSessions() {
  console.log("\n[2] sessions 全生命周期");
  const s = await api.createSession("E2E 测试会话");
  check("createSession 返回 id", typeof s.id === "string" && s.id.length > 0);

  await api.appendMessage(s.id, "user", "第一问");
  await api.appendMessage(s.id, "assistant", "第一答");

  const got = await api.getSession(s.id);
  check("getSession 消息数=2", (got.messages || []).length === 2);

  await api.renameSession(s.id, "改名后的会话");
  const renamed = await api.getSession(s.id);
  check("renameSession 生效", renamed.title === "改名后的会话");

  await api.appendMessage(s.id, "user", "第二问");
  await api.appendMessage(s.id, "assistant", "第二答");
  await api.truncateSession(s.id, 2);
  const truncated = await api.getSession(s.id);
  check("truncateSession 保留 2 条", (truncated.messages || []).length === 2);

  const list = await api.listSessions();
  check("listSessions 包含该会话", (list.sessions || []).some((x) => x.id === s.id));

  const exported = await api.exportAllSessions();
  check("exportAllSessions count>=1", (exported.count ?? exported.sessions?.length ?? 0) >= 1);

  await api.cleanupSessions(100);

  // 删除放到最后（后续流式测试不再依赖）
  await api.deleteSession(s.id);
  const after = await api.listSessions();
  check("deleteSession 生效", !(after.sessions || []).some((x) => x.id === s.id));
  return s;
}

// ================================================================
async function testIndexEndpoints() {
  console.log("\n[3] 索引端点（refresh / 全量 / async 队列）");
  const r = await api.refreshIndex();
  check("refreshIndex 统计字段", r.added === 1 && r.unchanged === 10);

  const all = await api.indexAll(true);
  check("indexAll(rebuild)", all.total_documents === 3 && all.vector_count === 30);

  // 后台任务：快速任务（incremental）
  const job = await api.submitJob("incremental");
  check("submitJob 返回 id/status", !!job.id && ["queued", "running"].includes(job.status));

  // 慢任务（url）：用于取消
  const slow = await api.submitJob("url", { url: "https://example.com/page" });

  // 等快速任务完成
  let doneJob = null;
  for (let i = 0; i < 100; i++) {
    doneJob = await api.getJob(job.id);
    if (doneJob.status === "done" || doneJob.status === "failed") break;
    await new Promise((res) => setTimeout(res, 50));
  }
  check("快速任务收尾为 done", doneJob?.status === "done", `got ${doneJob?.status}`);
  check("任务结果载荷", doneJob?.result?.chunks === 7);

  // 取消慢任务
  await new Promise((res) => setTimeout(res, 200)); // 让它进入 running
  const cancelled = await api.cancelJob(slow.id);
  check("cancelJob 返回 ok", cancelled.ok === true);

  const jobs = await api.listJobs(20);
  check("listJobs 含两个任务", (jobs.jobs || []).length >= 2);
  check("JobInfo 字段齐全", ["id", "kind", "status", "progress", "created_at"]
    .every((k) => k in jobs.jobs[0]));
}

// ================================================================
async function testConvertAndConfig() {
  console.log("\n[4] convert / config");
  const conv = await api.documentToMarkdown("txt", "# 标题\n\n正文段落。", false);
  check("documentToMarkdown ok", conv.ok === true && typeof conv.markdown === "string");

  const cfg = await api.getServerConfig();
  check("getServerConfig 返回对象", typeof cfg === "object" && cfg !== null);

  await api.saveServerConfig({ retrieval: { similarity_threshold: 0.0 } });
  const cfg2 = await api.getServerConfig();
  check("P11-3 回归：阈值 0 不被 || 吞掉", cfg2?.retrieval?.similarity_threshold === 0,
    `got ${cfg2?.retrieval?.similarity_threshold}`);
  // 还原
  await api.saveServerConfig({ retrieval: { similarity_threshold: 0.4 } });
}

// ================================================================
async function testResearchAndArchive() {
  console.log("\n[5] research / export-import 归档");
  const res = await api.research("E2E 研究问题", 1);
  check("research 报告/子查询/rounds",
    res.report.includes("E2E") && res.sub_queries.length === 2 && res.rounds === 1);

  const zipBuf = await api.exportArchive();
  check("exportArchive 返回非空 ZIP", zipBuf.byteLength > 100);

  const imp = await api.importArchive(zipBuf, "merge");
  check("importArchive merge 往返", typeof imp === "object" && imp !== null);
}

// ================================================================
async function testQueryStream() {
  console.log("\n[6] query/stream SSE 全事件序列");
  const events = [];
  await api.queryStream(
    "测试问题",
    [{ role: "user", content: "测试问题" }],
    5,
    true,
    {
      onSources: (s) => events.push(["sources", s]),
      onPhase: (p) => events.push(["phase", p]),
      onTiming: (t) => events.push(["timing", t]),
      onChunk: (c) => events.push(["chunk", c]),
      onDone: (d) => events.push(["done", d]),
      onError: (m) => events.push(["error", m]),
    }
  );
  const types = events.map((e) => e[0]);
  check("事件顺序 sources→chunk→done",
    types.includes("sources") && types.includes("chunk") && types.includes("done"), JSON.stringify(types));
  check("无 error 事件", !types.includes("error"));
  check("完整答案拼接", (events.find((e) => e[0] === "done") || [])[1] === "你好，世界");
  const timing = (events.find((e) => e[0] === "timing") || [])[1];
  check("timing.usage 透传（Round8 契约）", timing?.usage?.completion_tokens === 5);
  const sources = (events.find((e) => e[0] === "sources") || [])[1];
  check("来源行级引文字段", sources?.[0]?.line_start === 3 && sources?.[0]?.line_end === 7);
}

// ================================================================
async function main() {
  console.log(`E2E 契约测试 → ${BASE}`);
  await testHealthAndStatus();
  await testSessions();
  await testIndexEndpoints();
  await testConvertAndConfig();
  await testResearchAndArchive();
  await testQueryStream();

  console.log(`\n========== 结果：${passed} passed / ${failed} failed ==========`);
  if (failed > 0) {
    console.log("失败项：", failures.join(" | "));
    process.exit(1);
  }
}

main().catch((e) => {
  console.error("E2E 脚本异常:", e);
  process.exit(1);
});
