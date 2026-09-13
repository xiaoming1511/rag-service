#!/usr/bin/env bash
# ============================================================
# RAG Service 维护脚本（1）
# 用法:
#   scripts/maint.sh            # 等同 all
#   scripts/maint.sh export     # 导出全部会话 JSON → data/backups/
#   scripts/maint.sh backup     # 同上（别名）
#   scripts/maint.sh status     # 打印 /v1/status（metrics / rerank 缓存 / 索引任务）
#   scripts/maint.sh cleanup N  # 清理旧会话，保留最新 N 条（默认 100，删除前请先 export）
#   scripts/maint.sh all        # export + status
# 环境变量: RAG_BASE（默认 http://127.0.0.1:8080/v1）
# ============================================================
set -euo pipefail

RAG_BASE="${RAG_BASE:-http://127.0.0.1:8080/v1}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${ROOT_DIR}/data/backups"
TS="$(date +%Y%m%d-%H%M%S)"

say() { printf "\033[1;36m[RAG] %s\033[0m\n" "$*"; }

require_service() {
  if ! curl -s -m 3 "${RAG_BASE}/health" >/dev/null 2>&1; then
    echo "❌ RAG 服务未启动（${RAG_BASE}）。先: cd ${ROOT_DIR} && .venv/bin/python run_api.py" >&2
    exit 1
  fi
}

do_export() {
  mkdir -p "${BACKUP_DIR}"
  OUT="${BACKUP_DIR}/sessions-${TS}.json"
  curl -s -m 30 "${RAG_BASE}/sessions/export" > "${OUT}"
  say "已导出会话 → ${OUT}（$(python3 -c "import json,sys;print(json.load(open('${OUT}')).get('count',0))" 2>/dev/null || echo '?') 条）"
}

do_status() {
  curl -s -m 5 "${RAG_BASE}/status" | python3 -m json.tool
}

do_cleanup() {
  KEEP="${1:-100}"
  say "清理旧会话：保留最新 ${KEEP} 条…"
  curl -s -m 15 -X POST "${RAG_BASE}/sessions/cleanup" \
    -H 'Content-Type: application/json' -d "{\"keep\": ${KEEP}}" | python3 -m json.tool
}

case "${1:-all}" in
  export|backup) require_service; do_export ;;
  status)        require_service; do_status ;;
  cleanup)       require_service; do_cleanup "${2:-100}" ;;
  all|*)         require_service; do_export; do_status ;;
esac