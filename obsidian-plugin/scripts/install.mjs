/*
 * 安装脚本：将构建产物复制到 Obsidian Vault 的插件目录
 * 用法：npm run install（会自动先 build）
 */
import { cpSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
// 插件根目录（scripts/ 的上一级）
const rootDir = join(__dirname, "..");

// 从 manifest.json 读取插件 id
const manifest = JSON.parse(
  readFileSync(join(rootDir, "manifest.json"), "utf-8")
);
const pluginId = manifest.id;

// 插件安装目标：Obsidian 社区插件目录
// 1. OBSIDIAN_VAULT 环境变量（如 OBSIDIAN_VAULT=/path/to/vault）
// 2. 默认 user home + 候选路径（macOS），按优先级取第一个「含 .obsidian 配置目录」的候选：
//    - ~/projects/obsidian/xu   真实活跃库（插件 rag-service / karpathywiki 所在）
//    - ~/projects/obsidian      外层容器（自身也有 .obsidian，但可能只是空壳，
//                                不一定是真正启用插件的库）——放在 xu 之后作兜底
//    - ~/Documents/Obsidian Vault 通用默认
const envVault = process.env.OBSIDIAN_VAULT;
const home = process.env.HOME || "";
const candidates = envVault
  ? [envVault]
  : [
      join(home, "projects/obsidian/xu"),
      join(home, "projects/obsidian"),
      join(home, "Documents/Obsidian Vault"),
    ];

// 必须存在 .obsidian 配置目录，才算是可安装插件的 vault 根
const vaultRoot = candidates.find((p) => existsSync(join(p, ".obsidian")));
if (!vaultRoot) {
  console.error(
    "❌ 未找到 Obsidian vault 根目录（需含 .obsidian 配置目录）。" +
      "请设置环境变量 OBSIDIAN_VAULT=/path/to/vault"
  );
  process.exit(1);
}

const destDir = join(vaultRoot, ".obsidian", "plugins", pluginId);
mkdirSync(destDir, { recursive: true });

const files = ["main.js", "manifest.json", "styles.css", "versions.json"];
for (const f of files) {
  cpSync(join(rootDir, f), join(destDir, f));
}

console.log(`✅ 插件已安装到: ${destDir}`);
console.log("   请在 Obsidian 设置 → 第三方插件 中启用「RAG Service」");