#!/bin/zsh
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
set -a
source "$project_root/.env"
set +a

: "${WORKBENCH_MCP_TOKEN:?缺少 WORKBENCH_MCP_TOKEN}"
export WORKBENCH_BASE_URL="${WORKBENCH_BASE_URL:-http://127.0.0.1:8000}"
export WORKBENCH_MCP_CACHE_DIR="${WORKBENCH_MCP_CACHE_DIR:-$project_root/data/mcp-cache}"

exec "$project_root/.venv/bin/python" "$project_root/mcp_server.py"
