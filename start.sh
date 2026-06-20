#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
API_PORT="${API_PORT:-}"
UI_PORT="${UI_PORT:-}"
LOG_DIR="$ROOT/.logs"
mkdir -p "$LOG_DIR" "$ROOT/.pycache_tmp"

export AGENT_ENABLE_SHELL="${AGENT_ENABLE_SHELL:-1}"
export AGENT_ENABLE_BROWSER="${AGENT_ENABLE_BROWSER:-1}"
export AGENT_ENABLE_COMPUTER="${AGENT_ENABLE_COMPUTER:-1}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "缺少命令: $1"
    exit 1
  fi
}

find_free_port() {
  python3 -c 'import socket,sys
start=int(sys.argv[1])
for port in range(start, start + 100):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:
    raise SystemExit("没有找到可用端口")' "$1"
}

wait_http() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 50); do
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS --max-time 1 "$url" >/dev/null 2>&1; then
        return 0
      fi
    elif python3 -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1], timeout=1).read(1)' "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "$label 启动失败，日志：$LOG_DIR"
  return 1
}

cleanup() {
  if [[ -n "${API_PID:-}" ]]; then kill "$API_PID" >/dev/null 2>&1 || true; fi
  if [[ -n "${UI_PID:-}" ]]; then kill "$UI_PID" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT INT TERM

require_cmd python3

if [[ -z "$API_PORT" ]]; then API_PORT="$(find_free_port 9100)"; fi
if [[ -z "$UI_PORT" ]]; then
  UI_START=9101
  if [[ "$UI_START" -le "$API_PORT" ]]; then UI_START=$((API_PORT + 1)); fi
  UI_PORT="$(find_free_port "$UI_START")"
fi
if [[ "$API_PORT" == "$UI_PORT" ]]; then
  echo "API_PORT 和 UI_PORT 不能相同: $API_PORT"
  exit 1
fi

echo "启动虚拟员工平台..."
echo "项目目录: $ROOT"
echo "API 端口: $API_PORT"
echo "页面端口: $UI_PORT"
echo "Agent 工具: shell=$AGENT_ENABLE_SHELL browser=$AGENT_ENABLE_BROWSER computer=$AGENT_ENABLE_COMPUTER"

PORT="$API_PORT" PYTHONPYCACHEPREFIX="$ROOT/.pycache_tmp" python3 "$ROOT/agent-server.py" >"$LOG_DIR/agent-server.log" 2>&1 &
API_PID="$!"

python3 -m http.server "$UI_PORT" --bind 127.0.0.1 --directory "$ROOT" >"$LOG_DIR/ui-server.log" 2>&1 &
UI_PID="$!"

wait_http "http://127.0.0.1:$API_PORT/teams" "智能体 API"
wait_http "http://127.0.0.1:$UI_PORT/agency-workspace.html" "前端页面"

URL="http://127.0.0.1:$UI_PORT/agency-workspace.html?agent_port=$API_PORT"
FT_DEBUG_URL="http://127.0.0.1:$UI_PORT/foreign-trade-workspace.html?agent_port=$API_PORT"
echo
echo "已启动。打开这个地址："
echo "$URL"
echo "外贸模块调试页（可选）："
echo "$FT_DEBUG_URL"
echo
echo "在平台内新建项目，选择「AI外贸获客小队」，然后在群聊里填写厂家资料并启动获客流程。"
echo "外贸5人小队会接力：主管定ICP → 获客找客户 → 背调评分 → 开发内容 → 跟进交付。右上角「🌐 资料包」只是辅助工具。"
echo "通用员工平台里可点击右上角 ⚙️ 填写 DeepSeek API Key。"
echo "每个虚拟员工都是可执行任务的 Agent，可调用文件、网页、Shell、浏览器和电脑操作工具。"
echo "如需点击/输入/控制应用，请在 macOS 系统设置里给终端或 Codex 辅助功能权限。"
echo "按 Ctrl+C 停止服务。日志目录: $LOG_DIR"

if [[ "${NO_OPEN:-0}" != "1" ]] && command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || true
fi

wait
