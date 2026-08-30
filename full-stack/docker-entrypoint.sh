#!/usr/bin/env bash
# 容器入口：把 anno（一起看书）和小窝一起拉起来。
#
# 🔴 为什么不能直接 CMD uvicorn：书房第五栏「一起看书」是 anno 那个 Node 服务
#    在 127.0.0.1:3300 上提供的，小窝只是转发。Node 不起，那一栏点开就是 502。
set -euo pipefail
cd /app

ANNO_HOME="${ANNO_HOME:-/data/marginalia}"

# 🔴 这几步必须在**运行时**做：/data 是持久卷，构建期在它下面建的东西
#    会被挂载整个盖掉。
mkdir -p "$ANNO_HOME/data" "$ANNO_HOME/uploads"
# 提取脚本软链回 /app，跟代码一起更新，不用每次改都重建卷里的副本。
ln -sf /app/anno/server/extract_pdf.py  "$ANNO_HOME/extract_pdf.py"
ln -sf /app/anno/server/extract_epub.py "$ANNO_HOME/extract_epub.py"
# server.mjs 认死 /opt/marginalia，所以它必须指到 ANNO_HOME。
# 🔴 不能只写 `ln -sfn`：如果 /opt/marginalia 已经是个**真目录**，
#    ln -sfn 会在它**里面**建一个嵌套软链（/opt/marginalia/marginalia），
#    而不是替换它——anno 于是继续读老目录，她的书看起来就「消失」了，
#    而且一个字的报错都没有。实跑踩到过，必须分情况处理。
link_opt() {
  local target="$1"
  if [ -L /opt/marginalia ] || [ ! -e /opt/marginalia ]; then
    ln -sfn "$target" /opt/marginalia
  elif [ -d /opt/marginalia ] && [ -z "$(ls -A /opt/marginalia 2>/dev/null)" ]; then
    rmdir /opt/marginalia && ln -sfn "$target" /opt/marginalia
  else
    # 非空真目录：里面很可能是上一版留下的书和批注。绝不动它。
    echo "[entrypoint] ⚠️ /opt/marginalia 是个非空真目录，不是软链。"
    echo "[entrypoint]    anno 会继续用它，但它**不在持久卷上，容器重建会丢**。"
    echo "[entrypoint]    要迁移：把里面的东西挪到 $target，删掉 /opt/marginalia，再重启。"
    return
  fi
  echo "[entrypoint] /opt/marginalia -> $target"
}
link_opt "$ANNO_HOME"

# 书架空着就放一本示例书，省得她第一次打开是白的。
if [ -z "$(ls -A "$ANNO_HOME/data" 2>/dev/null)" ]; then
  cp -n anno/data.example/*.json "$ANNO_HOME/data/" 2>/dev/null || true
fi

# 一个变量管两头：小窝用 ANNO_MCP_TOKEN 去连，anno 认的叫 MCP_AUTH_TOKEN。
export MCP_AUTH_TOKEN="${ANNO_MCP_TOKEN:-}"

echo "[entrypoint] 起 anno（一起看书）…"
( cd anno/server && exec node server.mjs ) &
ANNO_PID=$!

# 🔴 anno 挂了不能把整个容器带走——一起看书是书房里的一栏，
#    聊天、推送、寄相思都不依赖它。它死了就那一栏 502，别的照常。
( while true; do
    if ! kill -0 "$ANNO_PID" 2>/dev/null; then
      echo "[entrypoint] ⚠️ anno 退出了，一起看书那一栏会 502；其余功能不受影响"
      break
    fi
    sleep 30
  done ) &

for _ in $(seq 1 40); do
  curl -sf "${ANNO_ORIGIN:-http://127.0.0.1:3300}/health" >/dev/null 2>&1 && { echo "[entrypoint] anno 就绪"; break; }
  kill -0 "$ANNO_PID" 2>/dev/null || { echo "[entrypoint] ⚠️ anno 没起来，继续起小窝"; break; }
  sleep 0.25
done

echo "[entrypoint] 起小窝…"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8787}"
