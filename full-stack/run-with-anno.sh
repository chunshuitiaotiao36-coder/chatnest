#!/usr/bin/env bash
# 小窝 + 一起看书（anno）。照着 run-with-memory.sh 的样子写。
# 先跑一次 ./anno-setup.sh，之后每次用这个起。
set -euo pipefail

cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
export AGENT_APP_ROOT="${AGENT_APP_ROOT:-$(pwd)}"

# anno 的 Node 服务。它自己 listen 在 127.0.0.1:3300（server.mjs 写死），
# 外面进不来，只能经小窝的 /marginalia 转发——鉴权就守在那一层。
export ANNO_ORIGIN="${ANNO_ORIGIN:-http://127.0.0.1:3300}"
# 梁忱那支笔：不设这个，「一起看书」就只剩她一个人看。见 app/claude.py。
# 🔴 是 /mcp/sse，不是 /mcp。anno 的 README 写的是「SSE (/mcp) 和 Streamable
#    HTTP (/mcp)」，**是错的**——server.mjs 里只注册了 /mcp/sse 和
#    /mcp/messages，裸 POST /mcp 直接 "Cannot POST /mcp"。填错了不会报错，
#    只是梁忱静默地一件工具都拿不到。
export ANNO_MCP_URL="${ANNO_MCP_URL:-http://127.0.0.1:3300/mcp/sse}"

# 🔴 一个变量管两头：小窝这边用 ANNO_MCP_TOKEN 去连，anno 那边认的环境变量
#    叫 MCP_AUTH_TOKEN，这儿把它传下去，省得两边配错还不报错。
#    留空 = anno 的 mcpAuthorized() 直接放行（server.mjs:648）。只听本机时
#    这是安全的；一旦开 ANNO_PUBLIC_MCP 挂到公网，app/anno.py 会拒绝在空令牌
#    的情况下挂载那组路由。
export MCP_AUTH_TOKEN="${ANNO_MCP_TOKEN:-}"

( cd anno/server && exec node server.mjs ) &
ANNO_PID=$!
trap 'kill "$ANNO_PID" 2>/dev/null || true' EXIT

# 等它起来再放小窝进来，免得她开屏那一下书房正好 502。
for _ in $(seq 1 40); do
  curl -sf "$ANNO_ORIGIN/health" >/dev/null 2>&1 && break
  kill -0 "$ANNO_PID" 2>/dev/null || { echo "✗ anno 起不来，看上面的报错"; exit 1; }
  sleep 0.25
done

./run.sh
