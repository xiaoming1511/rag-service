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
// 2. 默认 user home + 硬编码候选路径（macOS）
const envVault = process.env.OBSIDIAN_VAULT;
const candidates = envVault
  ? [envVault]
  : [
      join(process.env.HOME || "", "projects/obsidian"),
      join(process.env.HOME || "", "Documents/Obsidian Vault"),
    ];

const vaultRoot = candidates.find((p) => existsSync(p));
if (!vaultRoot) {
  console.error(
    "❌ 未找到 Obsidian vault 根目录。请设置环境变量 OBSIDIAN_VAULT=/path/to/vault"
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