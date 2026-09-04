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

import { App, PluginSettingTab, Setting } from "obsidian";
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
  }
}