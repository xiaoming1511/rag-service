/**
 * 插件设置
 */

export interface RAGSettings {
  /** RAG 服务地址（默认本地服务端口 8080） */
  apiBase: string;
  /** API Key（本地服务通常为占位符） */
  apiKey: string;
  /** 检索返回结果数 */
  topK: number;
  /** 是否使用重排序 */
  useRerank: boolean;
  /** 使用模式（简洁/标准/详细/自定义）：一键预设一组检索参数 */
  mode: "simple" | "standard" | "deep" | "custom";
  /** 保存笔记时自动触发增量索引 */
  autoIndexOnSave: boolean;
  /** 自动索引完成后弹出提示 */
  autoIndexNotify: boolean;
  /** 定时兜底增量索引间隔（秒；0 表示关闭定时） */
  autoIndexIntervalSec: number;
  /** 自动把 vault 中的 .html/.htm 转换为同名 .md（调服务端转换器） */
  autoConvertHtml: boolean;
  /** 高级功能开关（后端已具备、此前插件未接通的能力；默认全关保持界面简洁） */
  features: RAGFeatureFlags;
}

/**
 * 高级功能开关
 *
 * 背景：这些能力在服务端早已实现（见 docs/feature-map-and-status.md 5.3 节），
 * 但插件侧此前**没有任何入口**——后端有、前端没接，等于用户用不到。
 *
 * 为什么用开关而不是直接铺开：聊天 + 自动索引是主干流程，这四项是低频的
 * 运维/研究类操作。默认全关，避免命令面板与设置页被低频项占满；需要时逐个打开。
 *
 * 为什么**不**接非流式 `/v1/query`：对话场景下流式严格优于"等完整响应再显示"，
 * 接了反而更差。该端点保留给脚本/程序化调用（curl、CI），不是插件该用的。
 */
export interface RAGFeatureFlags {
  /** 深度研究（/v1/research）：拆子查询并行检索，汇总成报告并落成笔记 */
  research: boolean;
  /** 索引工具：全量重建（/v1/index）与网页索引（/v1/index/url） */
  indexTools: boolean;
  /** 后台摄入队列（/v1/index/async + /v1/index/jobs）：长任务不阻塞、可查进度与取消 */
  asyncJobs: boolean;
  /** 项目归档（GET /v1/export、POST /v1/import）：整包备份与恢复 */
  archiveTools: boolean;
}

export const DEFAULT_FEATURES: RAGFeatureFlags = {
  research: false,
  indexTools: false,
  asyncJobs: false,
  archiveTools: false,
};

export const DEFAULT_SETTINGS: RAGSettings = {
  apiBase: "http://127.0.0.1:8080/v1",
  apiKey: "dummy",
  topK: 3,
  useRerank: true,
  mode: "standard",
  autoIndexOnSave: true,
  autoIndexNotify: true,
  autoIndexIntervalSec: 0,
  autoConvertHtml: false,
  features: { ...DEFAULT_FEATURES },
};

/** 模式预设：一键套用一组「插件查询参数 + 服务端检索/生成参数」 */
export interface ModePreset {
  label: string;
  /** 一句话说明 */
  desc: string;
  pluginTopK: number;
  useRerank: boolean;
  server: {
    top_k: number;
    rerank_top_k: number;
    recall_candidates: number;
    synthesis_weight: number;
    similarity_threshold: number;
    strict_sources: boolean;
    /** 上下文 token 预算（生成档位：越大回答越全越慢） */
    context_token_budget: number;
    /** 回答最大 token 数（生成档位：越小越快） */
    max_tokens: number;
    /** 回答风格（提示词层控制长短：brief | balanced | detailed） */
    answer_style: "brief" | "balanced" | "detailed";
  };
}

export const MODE_PRESETS: Record<"simple" | "standard" | "deep", ModePreset> = {
  simple: {
    label: "简洁",
    desc: "快问快答：引用少而准，回答简短，查询最快",
    pluginTopK: 3,
    useRerank: true,
    server: {
      top_k: 3,
      rerank_top_k: 3,
      recall_candidates: 12,
      synthesis_weight: 0.5,
      similarity_threshold: 0.4,
      strict_sources: true,
      context_token_budget: 4000,
      max_tokens: 512,
      answer_style: "brief",
    },
  },
  standard: {
    label: "标准",
    desc: "均衡默认：引用与耗时适中，覆盖大多数日常提问",
    pluginTopK: 5,
    useRerank: true,
    server: {
      top_k: 5,
      rerank_top_k: 5,
      recall_candidates: 24,
      synthesis_weight: 0.5,
      similarity_threshold: 0.4,
      strict_sources: false,
      context_token_budget: 6000,
      max_tokens: 1024,
      answer_style: "balanced",
    },
  },
  deep: {
    label: "详细",
    desc: "全引用深回答：召回更宽、引用更多、回答更完整（耗时最长）",
    pluginTopK: 8,
    useRerank: true,
    server: {
      top_k: 8,
      rerank_top_k: 8,
      recall_candidates: 40,
      synthesis_weight: 0.3,
      similarity_threshold: 0.3,
      strict_sources: false,
      context_token_budget: 8000,
      max_tokens: 2048,
      answer_style: "detailed",
    },
  },
};

import { App, DropdownComponent, Notice, PluginSettingTab, Setting } from "obsidian";
import type RAGServicePlugin from "./main";

export class RAGSettingTab extends PluginSettingTab {
  plugin: RAGServicePlugin;

  constructor(app: App, plugin: RAGServicePlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();

    containerEl.createEl("h2", { text: "RAG Service 设置" });

    new Setting(containerEl)
      .setName("API 服务地址")
      .setDesc("本地 RAG 服务的接口地址（默认 http://127.0.0.1:8080/v1）")
      .addText((text) =>
        text
          .setPlaceholder("http://127.0.0.1:8080/v1")
          .setValue(this.plugin.settings.apiBase)
          .onChange(async (value) => {
            this.plugin.settings.apiBase = value.trim();
            await this.plugin.saveSettings();
            this.plugin.updateApiClient();
          })
      );

    new Setting(containerEl)
      .setName("API Key")
      .setDesc("oMLX 本地服务通常跳过认证，保留占位符即可")
      .addText((text) =>
        text
          .setPlaceholder("dummy")
          .setValue(this.plugin.settings.apiKey)
          .onChange(async (value) => {
            this.plugin.settings.apiKey = value.trim() || "dummy";
            await this.plugin.saveSettings();
            this.plugin.updateApiClient();
          })
      );

    if (this.plugin.settings.mode === "custom") {
      new Setting(containerEl)
        .setName("检索结果数 (top_k)")
        .setDesc("每次问答从知识库检索的来源片段数量：越多引用越全面，但检索与生成耗时略增")
        .addSlider((slider) =>
          slider
            .setLimits(1, 10, 1)
            .setValue(this.plugin.settings.topK)
            .setDynamicTooltip()
            .onChange(async (value) => {
              this.plugin.settings.topK = value;
              await this.plugin.saveSettings();
            })
        );

      new Setting(containerEl)
        .setName("重排序 (Rerank)")
        .setDesc("开启后对检索结果做相关性精排，回答引用更精准（略增耗时）；关闭则按向量相似度直接返回")
        .addToggle((toggle) =>
          toggle
            .setValue(this.plugin.settings.useRerank)
            .onChange(async (value) => {
              this.plugin.settings.useRerank = value;
              await this.plugin.saveSettings();
            })
        );
    } else {
      // 本分支已排除 "custom"，TS 能收窄类型，原先的三元判断是死代码
      // （tsc 报 TS2367：'simple'|'standard'|'deep' 与 'custom' 无重叠）
      const preset = MODE_PRESETS[this.plugin.settings.mode as "simple" | "standard" | "deep"];
      containerEl.createEl("p", {
        text: `当前处于「${preset.label}」模式：检索参数（top_k / rerank 等）已由预设接管，下方开关已隐藏；选「🛠 自定义」后将展开全部单独开关。`,
        cls: "rag-setting-tip",
      });
    }

    // ================================================================
    //  使用模式：一键聚合预设（简洁 / 标准 / 详细 / 自定义）
    // ================================================================
    new Setting(containerEl).setName("使用模式").setHeading();

    const modeSetting = new Setting(containerEl)
      .setName("模式")
      .setDesc(this.modeDescribe())
      .addDropdown((dd) => {
        this.modeDropdown = dd;
        dd.addOption("simple", "😌 简洁：快问快答")
          .addOption("standard", "⚖️ 标准：均衡默认")
          .addOption("deep", "🔍 详细：全引用深回答")
          .addOption("custom", "🛠 自定义（手动微调）")
          .setValue(this.plugin.settings.mode)
          .onChange(async (v) => {
            if (this.applying) return; // 程序化 setValue 触发的 onChange：忽略
            if (v === "custom") {
              this.plugin.settings.mode = "custom";
              await this.plugin.saveSettings();
            } else {
              await this.applyMode(v as "simple" | "standard" | "deep");
            }
            // 模式变更后重渲染：非自定义隐藏单体开关，自定义展开
            this.refreshTab();
          });
      });

    new Setting(containerEl).setName("自动索引").setHeading();

    new Setting(containerEl)
      .setName("保存笔记时触发增量索引")
      .setDesc("开启后，笔记保存/重命名/删除时自动防抖（2 秒）调用服务端增量索引，保持向量库与笔记同步；关闭则只能手动触发")
      .addToggle((toggle) =>
        toggle
          .setValue(this.plugin.settings.autoIndexOnSave)
          .onChange(async (value) => {
            this.plugin.settings.autoIndexOnSave = value;
            await this.plugin.saveSettings();
            this.plugin.rebindAutoIndex();
          })
      );

    new Setting(containerEl)
      .setName("自动索引完成提示")
      .setDesc("开启后，自动增量索引完成时右下角弹出新增/更新/删除统计；关闭则静默后台同步（仅手动索引时仍会提示）")
      .addToggle((toggle) =>
        toggle.setValue(this.plugin.settings.autoIndexNotify).onChange(async (value) => {
          this.plugin.settings.autoIndexNotify = value;
          await this.plugin.saveSettings();
        })
      );

    new Setting(containerEl)
      .setName("定时兜底索引间隔（秒）")
      .setDesc("每隔 N 秒自动调用一次增量索引兜底（防止保存事件漏触发）；填 0 表示关闭定时索引")
      .addText((text) =>
        text
          .setPlaceholder("0")
          .setValue(String(this.plugin.settings.autoIndexIntervalSec))
          .onChange(async (value) => {
            const num = parseInt(value, 10) || 0;
            this.plugin.settings.autoIndexIntervalSec = Math.max(0, num);
            await this.plugin.saveSettings();
            this.plugin.rebindAutoIndex();
          })
      );

    new Setting(containerEl)
      .setName("自动转换文档 → Markdown")
      .setDesc("开启后，放入 vault 的 html/pdf/docx/pptx/epub/txt 文件会自动调用服务端转换器生成同名 .md 笔记（保留标题层级，ASCII 图/代码块以代码块保留，需 RAG 服务在线）")
      .addToggle((toggle) =>
        toggle
          .setValue(this.plugin.settings.autoConvertHtml)
          .onChange(async (value) => {
            this.plugin.settings.autoConvertHtml = value;
            await this.plugin.saveSettings();
            this.plugin.rebindAutoConvertHtml();
          })
      );

    containerEl.createEl("p", {
      text: "提示：若需手动触发增量索引，可使用命令面板中的「触发增量索引」。",
      cls: "rag-setting-tip",
    });

    // ================================================================
    //  高级功能（后端已具备、此前插件未接通 → 开关控制入口是否出现）
    // ================================================================
    new Setting(containerEl).setName("高级功能").setHeading();
    containerEl.createEl("p", {
      text: "以下能力服务端均已实现，此前插件没有入口。默认关闭以保持界面简洁；打开后可在命令面板与下方按钮中使用。",
      cls: "rag-setting-tip",
    });

    const featureToggle = (key: keyof RAGFeatureFlags, name: string, desc: string): void => {
      new Setting(containerEl)
        .setName(name)
        .setDesc(desc)
        .addToggle((t) =>
          t.setValue(this.plugin.settings.features[key]).onChange(async (v) => {
            this.plugin.settings.features[key] = v;
            await this.plugin.saveSettings();
            // 命令面板无需动态注册：高级命令用 checkCallback 依据开关自动显隐，
            // 这里只需重渲染设置页，让下方动作按钮跟着显隐。
            this.refreshTab();
          })
        );
    };

    featureToggle(
      "research",
      "深度研究（/v1/research）",
      "开启后命令面板出现「深度研究」：把问题拆成多个子查询分别检索，汇总成带引用的报告并落成笔记（耗时明显长于普通问答）"
    );
    featureToggle(
      "indexTools",
      "索引工具（全量重建 / 网页索引）",
      "开启后可用「全量重建索引」（/v1/index，清空后重建）与「索引网页到知识库」（/v1/index/url，把网页抓进向量库）"
    );
    featureToggle(
      "asyncJobs",
      "后台摄入队列（异步任务）",
      "开启后长索引任务可后台执行（/v1/index/async），并可查看进度与取消（/v1/index/jobs）；适合大库全量重建"
    );
    featureToggle(
      "archiveTools",
      "项目归档（导出 / 导入）",
      "开启后可用「导出项目归档」把配置+文档清单+向量库+会话打成 ZIP 存进 vault；把归档 ZIP 放进 vault 后右键可导入（导入后需重启服务）"
    );

    // ---- 按开关显隐的动作按钮（与命令面板入口等价，便于发现） ----
    if (this.plugin.settings.features.research) {
      new Setting(containerEl)
        .setName("立即执行一次深度研究")
        .setDesc("弹窗询问研究问题；结果写入「RAG 导出/深度研究-<日期>.md」并打开")
        .addButton((b) => b.setButtonText("开始研究").onClick(() => void this.plugin.runResearch()));
    }
    if (this.plugin.settings.features.indexTools) {
      new Setting(containerEl)
        .setName("全量重建索引")
        .setDesc("清空向量库后重新索引所有文档（耗时较长；开启后台队列时建议走异步任务）")
        .addButton((b) => b.setButtonText("全量重建").onClick(() => void this.plugin.runFullIndex()));
      new Setting(containerEl)
        .setName("索引网页")
        .setDesc("把一个 URL 抓取并写入知识库（仅 http/https，服务端会拒绝内网/回环地址）")
        .addButton((b) => b.setButtonText("输入网址").onClick(() => void this.plugin.runUrlIndex()));
    }
    if (this.plugin.settings.features.asyncJobs) {
      new Setting(containerEl)
        .setName("后台摄入任务")
        .setDesc("提交异步任务，并查看最近任务的进度 / 状态 / 取消")
        .addButton((b) => b.setButtonText("查看任务").onClick(() => void this.plugin.showJobs()))
        .addButton((b) =>
          b.setButtonText("提交后台全量重建").onClick(() => void this.plugin.submitBackgroundJob("full"))
        );
    }
    if (this.plugin.settings.features.archiveTools) {
      new Setting(containerEl)
        .setName("导出项目归档")
        .setDesc("把配置 + 文档清单 + 向量库 + 会话 + 附件打成 ZIP，存到「RAG 导出/归档-<日期>.zip」")
        .addButton((b) =>
          b.setButtonText("导出").setCta().onClick(() => void this.plugin.exportArchiveToVault())
        );
    }

    // ================================================================
    //  服务端配置（RAG 服务）— 通过 GET/POST /v1/config 控制
    //  仅在「自定义」模式展开；模式预设下由预设接管、隐藏全部单体开关
    // ================================================================
    if (this.plugin.settings.mode === "custom") {
      containerEl.createEl("h2", { text: "服务端配置（RAG 服务，自定义模式）" });
      containerEl.createEl("p", {
        text: `以下开关保存在 config/settings.yaml（服务端）并通过 /v1/config 热生效；模型名等字段需重启服务。`,
        cls: "rag-setting-tip",
      });

      const serverStates: Record<string, { cb?: (v: any) => void; get?: () => any }> = {};
      this.serverStates = serverStates as Record<string, { value: any; cb?: (v: any) => void }>;
      const loadBtn = new Setting(containerEl)
        .setName("加载服务端配置")
        .setDesc("从 RAG 服务读取当前配置（服务未启动时显示错误）")
        .addButton((b) =>
          b.setButtonText("加载").onClick(async () => {
            try {
              const cfg = await this.plugin.api.getServerConfig();
              this.serverCfg = cfg;
              this.applyServerCfgToState(serverStates, cfg);
              new Notice("✅ 服务端配置已加载");
            } catch (e) {
              new Notice(`❌ 加载失败: ${(e as Error).message}`, 6000);
            }
          })
        );

      // 服务端配置控件
      this.buildServerControls(containerEl, serverStates);

      // 保存按钮
      new Setting(containerEl).addButton((b) =>
        b.setButtonText("保存到服务端").setCta().onClick(() => void this.saveServerControls(serverStates))
      );
    } else {
      containerEl.createEl("p", {
        text: `当前模式已接管检索/回答预设（含服务端参数）。如需逐一调整（相似度阈值、召回窗、沉淀降权、缓存、模型路由等），请在上方选择「🛠 自定义」——展开后本区块会出现全部单独开关。`,
        cls: "rag-setting-tip",
      });
    }

    // ================================================================
    //  会话管理（D：导出全部 / 清理旧会话）
    // ================================================================
    containerEl.createEl("h2", { text: "会话管理" });
    new Setting(containerEl)
      .setName("导出全部会话")
      .setDesc("把全部会话（含消息）写入 vault：RAG 导出/全部会话-<日期>.json")
      .addButton((b) =>
        b.setButtonText("导出全部").setCta().onClick(() => void this.exportAllSessions())
      );
    new Setting(containerEl)
      .setName("清理旧会话")
      .setDesc("只保留最近更新的 100 条，删除更早（删除前建议先导出备份）")
      .addButton((b) =>
        b.setButtonText("清理（保留 100）").onClick(() => void this.cleanupOldSessions())
      );

    // ================================================================
    //  服务监控（A：只读仪表盘，读 /v1/status.metrics）
    // ================================================================
    containerEl.createEl("h2", { text: "服务监控" });
    const monWrap = containerEl.createDiv({ cls: "rag-monitor" });
    const renderMonitor = async () => {
      try {
        const st = await this.plugin.api.status();
        const m = st.metrics || {};
        monWrap.empty();
        monWrap.createDiv({
          cls: "rag-monitor-line",
          text: `状态 ${st.status} · 集合 ${st.collection_name} · ${st.vector_count} 向量`,
        });
        monWrap.createDiv({
          cls: "rag-monitor-line",
          text: `请求 共 ${m.total ?? 0} · 错误 ${m.errors ?? 0} · 平均 ${m.avg_latency_ms ?? 0}ms · 最大 ${m.max_latency_ms ?? 0}ms`,
        });
        for (const r of (m.recent || []).slice(0, 12)) {
          monWrap.createDiv({
            cls: "rag-monitor-line rag-monitor-req",
            text: `${r.method} ${r.path} → ${r.status}  ${r.latency_ms}ms`,
          });
        }
      } catch (e) {
        monWrap.empty();
        monWrap.createDiv({ cls: "rag-monitor-line", text: `❌ 服务不可达: ${(e as Error).message}` });
      }
    };
    new Setting(containerEl)
      .setName("刷新 / 自动轮询")
      .setDesc("每 5 秒自动刷新一次最近请求耗时（可关）")
      .addButton((b) => b.setButtonText("立即刷新").onClick(() => void renderMonitor()))
      .addToggle((t) =>
        t
          .setValue(this.monitorAuto)
          .setTooltip(this.monitorAuto ? "自动轮询开启" : "自动轮询关闭")
          .onChange(async (v) => {
            this.monitorAuto = v;
            if (v) {
              this.monitorTimer = window.setInterval(() => void renderMonitor(), 5000);
              void renderMonitor();
            } else if (this.monitorTimer !== null) {
              window.clearInterval(this.monitorTimer);
              this.monitorTimer = null;
            }
          })
      );
    void renderMonitor();
  }

  // ================================================================
  //  服务端配置：控件构建 / 状态同步 / 保存
  // ================================================================

  private serverCfg: Record<string, any> = {};
  /** 服务端配置控件状态（供模式切换后同步显示） */
  private serverStates: Record<string, { value: any; cb?: (v: any) => void }> = {};
  private modeDropdown: DropdownComponent | null = null;
  /** 程序化回填护栏：期间组件 setValue 触发的 onChange 不算用户手改（不标自定义） */
  private applying = false;
  /** 服务监控自动轮询（A） */
  private monitorAuto = false;
  private monitorTimer: number | null = null;

  /** 导出全部会话到 vault（D） */
  private async exportAllSessions(): Promise<void> {
    try {
      const data = await this.plugin.api.exportAllSessions();
      const name = `RAG 导出/全部会话-${new Date().toISOString().slice(0, 10)}.json`;
      await this.app.vault.create(name, JSON.stringify(data, null, 2));
      new Notice(`✅ 已导出 ${data.count ?? 0} 个会话: ${name}`);
    } catch (e) {
      new Notice(`❌ 导出失败: ${(e as Error).message}`, 6000);
    }
  }

  /** 清理旧会话（保留最新 100 条）（D） */
  private async cleanupOldSessions(): Promise<void> {
    if (!window.confirm("将删除除最新 100 条外的所有旧会话（建议先导出备份）。继续？")) return;
    try {
      const r = await this.plugin.api.cleanupSessions(100);
      new Notice(`✅ 已清理 ${r.deleted_count ?? 0} 个旧会话`);
    } catch (e) {
      new Notice(`❌ 清理失败: ${(e as Error).message}`, 6000);
    }
  }

  /** 模式说明文案（给「模式」下拉的描述行） */
  private modeDescribe(): string {
    const m = this.plugin.settings.mode;
    if (m === "custom") return "当前为手动微调状态：改动下方任意服务端开关后会自动切到这里";
    const p = MODE_PRESETS[m];
    return `${p.desc}。对应：top_k ${p.server.top_k} · rerank_top_k ${p.server.rerank_top_k} · 召回窗 ${p.server.recall_candidates} · 沉淀降权 ${p.server.synthesis_weight} · 上下文 ${p.server.context_token_budget} · 回答 ≤${p.server.max_tokens} tokens · 风格 ${p.server.answer_style}`;
  }

  /** 手动微调任一服务端开关 → 标记自定义（诚实标注偏离了预设）
      护栏：程序化回填（applyMode/加载服务端配置）期间不算用户改动 */
  private markCustom(): void {
    if (this.applying) return;
    if (this.plugin.settings.mode === "custom") return;
    this.plugin.settings.mode = "custom";
    void this.plugin.saveSettings();
    this.modeDropdown?.setValue("custom");
  }

  /** 重渲染设置页（模式切换后：非自定义隐藏单体开关，自定义展开） */
  private refreshTab(): void {
    this.containerEl.empty();
    this.display();
  }

  /** 应用模式预设：插件查询参数 + 服务端检索参数（/v1/config 热生效） */
  private async applyMode(mode: "simple" | "standard" | "deep"): Promise<void> {
    const p = MODE_PRESETS[mode];
    this.plugin.settings.mode = mode;
    this.plugin.settings.topK = p.pluginTopK;
    this.plugin.settings.useRerank = p.useRerank;
    await this.plugin.saveSettings();

    const patch = {
      retrieval: {
        ...p.server,
        context_token_budget: p.server.context_token_budget,
      },
      generation: { max_tokens: p.server.max_tokens, answer_style: p.server.answer_style },
    };
    try {
      await this.plugin.api.saveServerConfig(patch);
      new Notice(`✅ 已切换「${p.label}」模式`);
    } catch (e) {
      new Notice(`❌ 服务端应用失败（插件端已保存）：${(e as Error).message}`, 6000);
    }
    // 同步服务端配置控件显示（若已加载）
    this.applyServerCfgToState(this.serverStates, { retrieval: p.server });
  }

  /** 把已加载的服务端配置回填到控件状态（缺失字段保持控件原值，避免 undefined 写入）。
      回填期间置 applying 护栏：组件 setValue 触发的 onChange 不得被误判为用户手改（否则会被标成自定义）。 */
  private applyServerCfgToState(states: Record<string, { cb?: (v: any) => void }>, cfg: any): void {
    const prev = this.applying;
    this.applying = true;
    try {
      const set = (key: string, val: any) => {
        if (val === undefined || val === null) return;
        const st = states[key];
        if (st && st.cb) st.cb(val);
      };
      set("strict_sources", cfg?.retrieval?.strict_sources);
      set("enable_rerank", cfg?.retrieval?.enable_rerank);
      set("similarity_threshold", cfg?.retrieval?.similarity_threshold);
      set("rerank_top_k", cfg?.retrieval?.rerank_top_k);
      set("recall_candidates", cfg?.retrieval?.recall_candidates);
      set("synthesis_weight", cfg?.retrieval?.synthesis_weight);
      set("context_token_budget", cfg?.retrieval?.context_token_budget);
      set("max_tokens", cfg?.generation?.max_tokens);
      set("answer_style", cfg?.generation?.answer_style);
      set("response_cache", cfg?.performance?.response_cache);
      set("response_cache_ttl", cfg?.performance?.response_cache_ttl);
      set("syntheses_enabled", cfg?.syntheses?.enabled);
      set("rewrite_query", cfg?.generation?.rewrite_query);
      set("max_history_rounds", cfg?.generation?.max_history_rounds);
    set("history_token_budget", cfg?.generation?.history_token_budget);
    set("chat_model", cfg?.routing?.chat ?? cfg?.omlx?.chat_model ?? "");
    set("rewrite_model", cfg?.routing?.rewrite ?? "");
      set("research_model", cfg?.routing?.research_subqueries ?? "");
    } finally {
      this.applying = prev;
    }
  }

  /** 构建服务端配置控件（用户改动实时写入 state，保存时据此提交） */
  private buildServerControls(containerEl: HTMLElement, states: Record<string, any>): void {
    const stateOf = (key: string, init: any) => {
      const st: { value: any; cb?: (v: any) => void } = { value: init };
      states[key] = st;
      return st;
    };
    // 用户交互回写 state；加载时由 cb 回填控件。
    // 手动改动任意服务端开关 → 自动切换到「自定义」模式（诚实标注已偏离预设）
    const sync = (st: any) => (v: any) => { st.value = v; this.markCustom(); };

    const s1 = stateOf("strict_sources", false);
    new Setting(containerEl)
      .setName("严格来源模式")
      .setDesc("开启后检索不到相关内容时不调用模型，直接诚实告知未找到（避免模型编造）；关闭则仍会让模型凭通用知识作答")
      .addToggle((t) => {
        t.setValue(!!s1.value).onChange(sync(s1));
        s1.cb = (v: boolean) => t.setValue(!!v);
      });

    const s2 = stateOf("enable_rerank", true);
    new Setting(containerEl)
      .setName("重排序 (Rerank)")
      .setDesc("开启后对检索结果按相关性精排，答案引用更准（略增耗时）；关闭则按向量相似度顺序直接取前 N 条")
      .addToggle((t) => {
        t.setValue(!!s2.value).onChange(sync(s2));
        s2.cb = (v: boolean) => t.setValue(!!v);
      });

    const s3 = stateOf("similarity_threshold", 0.5);
    new Setting(containerEl)
      .setName("相似度阈值")
      .setDesc("低于此相似度（0~1）的检索结果会被过滤：调高引用更严格但可能漏召回，调低召回更多但可能不相关")
      .addSlider((sl) => {
        sl.setLimits(0.0, 1.0, 0.05).setValue(s3.value).setDynamicTooltip().onChange(sync(s3));
        s3.cb = (v: number) => sl.setValue(v);
      });

    const sRerankTopK = stateOf("rerank_top_k", 3);
    new Setting(containerEl)
      .setName("重排序结果数 (rerank_top_k)")
      .setDesc("重排序后保留的来源条数：越大引用越全面，越小越聚焦")
      .addSlider((sl) => {
        sl.setLimits(1, 10, 1).setValue(sRerankTopK.value).setDynamicTooltip().onChange(sync(sRerankTopK));
        sRerankTopK.cb = (v: number) => sl.setValue(v);
      });

    const sRecall = stateOf("recall_candidates", 30);
    new Setting(containerEl)
      .setName("召回候选窗 (recall_candidates)")
      .setDesc("rerank 前每路抓取的候选块数：越大真实高分块越不容易被漏掉，但重排输入越多、耗时越高")
      .addSlider((sl) => {
        sl.setLimits(5, 60, 1).setValue(sRecall.value).setDynamicTooltip().onChange(sync(sRecall));
        sRecall.cb = (v: number) => sl.setValue(v);
      });

    const sSynW = stateOf("synthesis_weight", 0.85);
    new Setting(containerEl)
      .setName("问答沉淀降权 (synthesis_weight)")
      .setDesc("历史问答沉淀块的相关性衰减（0~1）：越小越弱化“历史问答以问代答”对榜首的占领，0 = 完全排除；1 = 不过滤")
      .addSlider((sl) => {
        sl.setLimits(0.0, 1.0, 0.05).setValue(sSynW.value).setDynamicTooltip().onChange(sync(sSynW));
        sSynW.cb = (v: number) => sl.setValue(v);
      });

    const sMaxTokens = stateOf("max_tokens", 2048);
    new Setting(containerEl)
      .setName("回答长度上限 (max_tokens)")
      .setDesc("回答最大 token 数：调小 → 回答更短更快（生成耗时大头）")
      .addSlider((sl) => {
        sl.setLimits(64, 2048, 64).setValue(sMaxTokens.value).setDynamicTooltip().onChange(sync(sMaxTokens));
        sMaxTokens.cb = (v: number) => sl.setValue(v);
      });

    const sAnswerStyle = stateOf("answer_style", "balanced");
    new Setting(containerEl)
      .setName("回答风格")
      .setDesc("提示词层控制长短：简洁=结论与要点（最快）；标准=均衡；详细=完整展开")
      .addDropdown((dd) => {
        dd.addOption("brief", "简洁（要点，最快）")
          .addOption("balanced", "标准（均衡）")
          .addOption("detailed", "详细（完整展开）")
          .setValue(String(sAnswerStyle.value))
          .onChange(sync(sAnswerStyle));
        sAnswerStyle.cb = (v: string) => dd.setValue(v);
      });

    const sCtxBudget = stateOf("context_token_budget", 8000);
    new Setting(containerEl)
      .setName("上下文 token 预算")
      .setDesc("检索拼进提示的上下文上限：调小 → 生成更快但引用更少")
      .addSlider((sl) => {
        sl.setLimits(2000, 12000, 500).setValue(sCtxBudget.value).setDynamicTooltip().onChange(sync(sCtxBudget));
        sCtxBudget.cb = (v: number) => sl.setValue(v);
      });

    const s4 = stateOf("response_cache", true);
    new Setting(containerEl)
      .setName("响应缓存")
      .setDesc("开启后完全相同的问题直接返回缓存结果（仅非流式、单轮生效），响应更快；关闭则每次重新检索生成")
      .addToggle((t) => {
        t.setValue(!!s4.value).onChange(sync(s4));
        s4.cb = (v: boolean) => t.setValue(!!v);
      });

    const s5 = stateOf("response_cache_ttl", 3600);
    new Setting(containerEl)
      .setName("响应缓存有效期（秒）")
      .setDesc("缓存结果超过该时长后失效并重新生成；仅在「响应缓存」开启时生效")
      .addText((text) => {
        text.setValue(String(s5.value)).onChange(sync(s5));
        s5.cb = (v: any) => text.setValue(String(v));
      });

    const s6 = stateOf("syntheses_enabled", true);
    new Setting(containerEl)
      .setName("问答沉淀（syntheses）")
      .setDesc("开启后每轮问答会写成 vault 根目录 syntheses/ 下的笔记，并纳入索引可被再次检索；关闭则不做沉淀")
      .addToggle((t) => {
        t.setValue(!!s6.value).onChange(sync(s6));
        s6.cb = (v: boolean) => t.setValue(!!v);
      });

    const s7 = stateOf("rewrite_query", false);
    new Setting(containerEl)
      .setName("多轮追问改写")
      .setDesc("开启后多轮对话中的追问（如「那第二种呢」）会先由模型改写为独立完整问题再检索，提升召回；关闭则直接用原句检索（省一次调用）")
      .addToggle((t) => {
        t.setValue(!!s7.value).onChange(sync(s7));
        s7.cb = (v: boolean) => t.setValue(!!v);
      });

    const s8 = stateOf("max_history_rounds", 10);
    new Setting(containerEl)
      .setName("对话历史轮数")
      .setDesc("多轮对话时携带给模型的最大历史轮数：越大上下文越连贯，但 token 消耗越多")
      .addText((text) => {
        text.setValue(String(s8.value)).onChange(sync(s8));
        s8.cb = (v: any) => text.setValue(String(v));
      });

    const s9 = stateOf("history_token_budget", 2000);
    new Setting(containerEl)
      .setName("历史 token 预算")
      .setDesc("对话历史超过该 token 预算时从旧到新裁剪，防止超出模型上下文窗口")
      .addText((text) => {
        text.setValue(String(s9.value)).onChange(sync(s9));
        s9.cb = (v: any) => text.setValue(String(v));
      });

    // ---- 模型路由 ----
    containerEl.createEl("h3", { text: "模型路由（不同任务使用不同模型）" });
    const textModel = (key: string, init: string, name: string, desc: string) => {
      const st = stateOf(key, init);
      new Setting(containerEl)
        .setName(name)
        .setDesc(desc)
        .addText((text) => {
          text.setValue(String(st.value)).setPlaceholder("留空 = 跟随默认聊天模型").onChange(sync(st));
          st.cb = (v: any) => text.setValue(String(v));
        });
      return st;
    };
    textModel(
      "chat_model",
      "",
      "问答生成模型",
      "回答用户问题时使用的模型；留空则使用服务端默认聊天模型"
    );
    textModel(
      "rewrite_model",
      "",
      "追问改写模型",
      "多轮对话中把追问改写为独立问题时使用的模型；仅在「多轮追问改写」开启且留空时跟随默认聊天模型"
    );
    textModel(
      "research_model",
      "",
      "Deep Research 拆解模型",
      "深度研究（/v1/research）中把大问题拆解为子查询时使用的模型；留空则跟随默认聊天模型"
    );
  }

  /** 数值解析护栏：0 是合法值（相似度阈值=0 不过滤、TTL=0 不过期、轮数=0 不带历史），
      不能用 || 兜底——0 会被误当成「没填」替换成默认值。仅 NaN/undefined 走 fallback。 */
  private static finiteNum(v: number | undefined, fallback: number): number {
    return v != null && Number.isFinite(v) ? v : fallback;
  }

  /** 收集服务端控件状态并 POST 保存 */
  private async saveServerControls(states: Record<string, any>): Promise<void> {
    // states[key].value 由 cb 同步更新（控件 OnChange 时写入）
    const num = RAGSettingTab.finiteNum;
    const patch: Record<string, any> = {
      retrieval: {
        strict_sources: !!states["strict_sources"]?.value,
        enable_rerank: !!states["enable_rerank"]?.value,
        similarity_threshold: num(parseFloat(states["similarity_threshold"]?.value), 0.5),
        rerank_top_k: num(parseInt(states["rerank_top_k"]?.value, 10), 3),
        recall_candidates: num(parseInt(states["recall_candidates"]?.value, 10), 30),
        synthesis_weight: num(parseFloat(states["synthesis_weight"]?.value), 0.85),
        context_token_budget: num(parseInt(states["context_token_budget"]?.value, 10), 8000),
      },
      generation: {
        max_tokens: num(parseInt(states["max_tokens"]?.value, 10), 2048),
        answer_style: states["answer_style"]?.value || "balanced",
        rewrite_query: !!states["rewrite_query"]?.value,
        max_history_rounds: num(parseInt(states["max_history_rounds"]?.value, 10), 10),
        history_token_budget: num(parseInt(states["history_token_budget"]?.value, 10), 2000),
      },
      performance: {
        response_cache: !!states["response_cache"]?.value,
        response_cache_ttl: num(parseInt(states["response_cache_ttl"]?.value, 10), 0),
      },
      syntheses: { enabled: !!states["syntheses_enabled"]?.value },
      routing: {
        chat: (states["chat_model"]?.value || "").trim(),
        rewrite: (states["rewrite_model"]?.value || "").trim(),
        research_subqueries: (states["research_model"]?.value || "").trim(),
      },
    };

    try {
      await this.plugin.api.saveServerConfig(patch);
      new Notice("✅ 服务端配置已保存并热生效");
    } catch (e) {
      new Notice(`❌ 保存失败: ${(e as Error).message}`, 6000);
    }
  }
}