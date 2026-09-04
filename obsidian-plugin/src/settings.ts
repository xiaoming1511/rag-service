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
  /** 保存笔记时自动触发增量索引 */
  autoIndexOnSave: boolean;
  /** 定时兜底增量索引间隔（秒；0 表示关闭定时） */
  autoIndexIntervalSec: number;
}

export const DEFAULT_SETTINGS: RAGSettings = {
  apiBase: "http://127.0.0.1:8080/v1",
  apiKey: "dummy",
  topK: 3,
  useRerank: true,
  autoIndexOnSave: true,
  autoIndexIntervalSec: 0,
};

import { App, Notice, PluginSettingTab, Setting } from "obsidian";
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

    new Setting(containerEl)
      .setName("检索结果数 (top_k)")
      .setDesc("每次问答返回的引用来源数量")
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
      .setDesc("是否对检索结果做相关性精排")
      .addToggle((toggle) =>
        toggle
          .setValue(this.plugin.settings.useRerank)
          .onChange(async (value) => {
            this.plugin.settings.useRerank = value;
            await this.plugin.saveSettings();
          })
      );

    new Setting(containerEl).setName("自动索引").setHeading();

    new Setting(containerEl)
      .setName("保存笔记时触发增量索引")
      .setDesc("在 Obsidian 中保存/重命名/删除笔记时，防抖调用增量索引接口")
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
      .setName("定时兜底索引间隔（秒）")
      .setDesc("定时调用增量索引作为兜底；0 表示关闭定时")
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

    containerEl.createEl("p", {
      text: "提示：若需手动触发增量索引，可使用命令面板中的「触发增量索引」。",
      cls: "rag-setting-tip",
    });

    // ================================================================
    //  服务端配置（RAG 服务）— 通过 GET/POST /v1/config 控制
    // ================================================================
    containerEl.createEl("h2", { text: "服务端配置（RAG 服务）" });
    containerEl.createEl("p", {
      text: `以下开关保存在 config/settings.yaml（服务端）并通过 /v1/config 热生效；模型名等字段需重启服务。`,
      cls: "rag-setting-tip",
    });

    const serverStates: Record<string, { cb?: (v: any) => void; get?: () => any }> = {};
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
  }

  // ================================================================
  //  服务端配置：控件构建 / 状态同步 / 保存
  // ================================================================

  private serverCfg: Record<string, any> = {};

  /** 把已加载的服务端配置回填到控件状态 */
  private applyServerCfgToState(states: Record<string, { cb?: (v: any) => void }>, cfg: any): void {
    const set = (key: string, val: any) => {
      const st = states[key];
      if (st && st.cb) st.cb(val);
    };
    set("strict_sources", cfg?.retrieval?.strict_sources);
    set("enable_rerank", cfg?.retrieval?.enable_rerank);
    set("similarity_threshold", cfg?.retrieval?.similarity_threshold);
    set("response_cache", cfg?.performance?.response_cache);
    set("response_cache_ttl", cfg?.performance?.response_cache_ttl);
    set("syntheses_enabled", cfg?.syntheses?.enabled);
    set("rewrite_query", cfg?.generation?.rewrite_query);
    set("max_history_rounds", cfg?.generation?.max_history_rounds);
    set("history_token_budget", cfg?.generation?.history_token_budget);
    set("chat_model", cfg?.routing?.chat ?? cfg?.omlx?.chat_model ?? "");
    set("rewrite_model", cfg?.routing?.rewrite ?? "");
    set("research_model", cfg?.routing?.research_subqueries ?? "");
  }

  /** 构建服务端配置控件（用户改动实时写入 state，保存时据此提交） */
  private buildServerControls(containerEl: HTMLElement, states: Record<string, any>): void {
    const stateOf = (key: string, init: any) => {
      const st: { value: any; cb?: (v: any) => void } = { value: init };
      states[key] = st;
      return st;
    };
    // 用户交互回写 state；加载时由 cb 回填控件
    const sync = (st: any) => (v: any) => { st.value = v; };

    const s1 = stateOf("strict_sources", false);
    new Setting(containerEl)
      .setName("严格来源模式")
      .setDesc("检索为空时不调用模型，诚实告知未找到")
      .addToggle((t) => {
        t.setValue(!!s1.value).onChange(sync(s1));
        s1.cb = (v: boolean) => t.setValue(!!v);
      });

    const s2 = stateOf("enable_rerank", true);
    new Setting(containerEl)
      .setName("重排序 (Rerank)")
      .setDesc("对检索结果做相关性精排")
      .addToggle((t) => {
        t.setValue(!!s2.value).onChange(sync(s2));
        s2.cb = (v: boolean) => t.setValue(!!v);
      });

    const s3 = stateOf("similarity_threshold", 0.5);
    new Setting(containerEl)
      .setName("相似度阈值")
      .setDesc("低于此相似度的检索结果被过滤（0~1）")
      .addSlider((sl) => {
        sl.setLimits(0.0, 1.0, 0.05).setValue(s3.value).setDynamicTooltip().onChange(sync(s3));
        s3.cb = (v: number) => sl.setValue(v);
      });

    const s4 = stateOf("response_cache", true);
    new Setting(containerEl)
      .setName("响应缓存")
      .setDesc("相同问题直接返回缓存结果（仅非流式、单轮）")
      .addToggle((t) => {
        t.setValue(!!s4.value).onChange(sync(s4));
        s4.cb = (v: boolean) => t.setValue(!!v);
      });

    const s5 = stateOf("response_cache_ttl", 3600);
    new Setting(containerEl)
      .setName("响应缓存有效期（秒）")
      .setDesc("缓存结果过期时间")
      .addText((text) => {
        text.setValue(String(s5.value)).onChange(sync(s5));
        s5.cb = (v: any) => text.setValue(String(v));
      });

    const s6 = stateOf("syntheses_enabled", true);
    new Setting(containerEl)
      .setName("问答沉淀（syntheses）")
      .setDesc("问答写为 vault 根 syntheses/，可被再次检索")
      .addToggle((t) => {
        t.setValue(!!s6.value).onChange(sync(s6));
        s6.cb = (v: boolean) => t.setValue(!!v);
      });

    const s7 = stateOf("rewrite_query", false);
    new Setting(containerEl)
      .setName("多轮追问改写")
      .setDesc("检索前用模型把追问改写为独立问题")
      .addToggle((t) => {
        t.setValue(!!s7.value).onChange(sync(s7));
        s7.cb = (v: boolean) => t.setValue(!!v);
      });

    const s8 = stateOf("max_history_rounds", 10);
    new Setting(containerEl)
      .setName("对话历史轮数")
      .setDesc("多轮对话保留的最大轮数")
      .addText((text) => {
        text.setValue(String(s8.value)).onChange(sync(s8));
        s8.cb = (v: any) => text.setValue(String(v));
      });

    const s9 = stateOf("history_token_budget", 2000);
    new Setting(containerEl)
      .setName("历史 token 预算")
      .setDesc("超出后从旧到新裁剪历史")
      .addText((text) => {
        text.setValue(String(s9.value)).onChange(sync(s9));
        s9.cb = (v: any) => text.setValue(String(v));
      });

    // ---- 模型路由 ----
    containerEl.createEl("h3", { text: "模型路由（不同任务使用不同模型）" });
    const textModel = (key: string, init: string, desc: string) => {
      const st = stateOf(key, init);
      new Setting(containerEl).setName(desc).addText((text) => {
        text.setValue(String(st.value)).setPlaceholder("留空 = 跟随默认聊天模型").onChange(sync(st));
        st.cb = (v: any) => text.setValue(String(v));
      });
      return st;
    };
    textModel("chat_model", "", "问答生成模型（空缺 = 默认聊天模型）");
    textModel("rewrite_model", "", "追问改写模型");
    textModel("research_model", "", "Deep Research 拆解模型");
  }

  /** 收集服务端控件状态并 POST 保存 */
  private async saveServerControls(states: Record<string, any>): Promise<void> {
    // states[key].value 由 cb 同步更新（控件 OnChange 时写入）
    const patch: Record<string, any> = {
      retrieval: {
        strict_sources: !!states["strict_sources"]?.value,
        enable_rerank: !!states["enable_rerank"]?.value,
        similarity_threshold: parseFloat(states["similarity_threshold"]?.value) || 0.5,
      },
      performance: {
        response_cache: !!states["response_cache"]?.value,
        response_cache_ttl: parseInt(states["response_cache_ttl"]?.value, 10) || 0,
      },
      syntheses: { enabled: !!states["syntheses_enabled"]?.value },
      generation: {
        rewrite_query: !!states["rewrite_query"]?.value,
        max_history_rounds: parseInt(states["max_history_rounds"]?.value, 10) || 10,
        history_token_budget: parseInt(states["history_token_budget"]?.value, 10) || 2000,
      },
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