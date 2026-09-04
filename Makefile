# ============================================================
# RAG Service 便捷命令（无需上传 GitHub 也可本地用）
# ============================================================
.PHONY: install test build-plugin install-plugin export-demo clean

# 安装 Python 依赖（含测试用）
install:
	pip install -r requirements.txt pytest pytest-asyncio python-multipart

# 运行全部测试（无 oMLX 时集成测试自动跳过）
test:
	cd $$(dirname $(realpath $(firstword $(MAKEFILE_LIST)))) && .venv/bin/python -m pytest test/ -q

# 构建 Obsidian 插件
build-plugin:
	cd obsidian-plugin && npm install && npm run build

# 安装插件到 vault（默认 ~/projects/obsidian，可用 OBSIDIAN_VAULT 覆盖）
install-plugin:
	cd obsidian-plugin && OBSIDIAN_VAULT=$${OBSIDIAN_VAULT:-$$HOME/projects/obsidian} node scripts/install.mjs

# 一键：测试 + 构建插件 + 安装插件
all: test build-plugin install-plugin

# 清理运行产物（保留 .venv 与向量库）
clean:
	rm -rf data/exports data/tmp data/conversations .pytest_cache