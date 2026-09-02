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

# 他的声音的缓存。🔴 跟 anno 一个道理，必须在**启动时**建：
# 构建期在 /data 下建的会被挂上来的持久卷盖掉。
mkdir -p "${VOICE_CACHE_DIR:-/data/voice}"

# telemood（TG 那条线的气泡 / 表情 / 贴纸 / 按钮）的两个 SQLite：
# 贴纸 catalog 和 callback store。
# 🔴 必须落在持久卷上，而且**必须在启动时建**——上游 SETUP 的示例写的是
#    `state/*.sqlite3` 这种相对路径，那会落在 /app（镜像层），容器一重建
#    她收藏的贴纸和还没过期的按钮全没。跟 anno / voice 同一个坑。
mkdir -p "${TELEMOOD_STATE_DIR:-/data/telemood}"

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

# ── 听她说话（hervoice）。🔴 只在开启时才起：它一跑起来就要装着 librosa，
#    那是几十 MB 常驻。不用这个功能的时候一个字节都不该占。 ──
if [ "${HERVOICE_ENABLED:-0}" = "1" ]; then
  mkdir -p "${HERVOICE_DATA:-/data/hervoice}"
  echo "[entrypoint] 起 hervoice（听她说话）…"
  ( cd hervoice && exec python3 -m uvicorn hervoice:app --host 127.0.0.1 --port 8010 ) &
  HV_PID=$!
  # 跟 anno 一个规矩：它挂了不拖垮容器，只是麦克风那个键不好使。
  ( while kill -0 "$HV_PID" 2>/dev/null; do sleep 30; done
    echo "[entrypoint] ⚠️ hervoice 退出了，按住说话会用不了；其余功能不受影响" ) &
else
  echo "[entrypoint] hervoice 未开启（HERVOICE_ENABLED=0），麦克风键会提示没开"
fi

# ── 潮汐（Murmur 情绪引擎）────────────────────────────────────────────
# 🔴 Murmur 把数据目录写死成 `BASE / "data"`（engine.py 第 19 行），
#    也就是 /app/murmur/data——那是**镜像层**，容器一重建，梁忱的心就清零。
#    没有环境变量可以覆盖它，所以只能软链出去。跟 anno 那个
#    /opt/marginalia 是同一个坑，处理方式也照抄那边的 link_opt()：
#    ln -sfn A B 在 B 已经是**真目录**时，会在 B **里面**建一个嵌套软链
#    而不是替换它，后果是服务继续读老目录、她的数据看起来「消失」，
#    而且一个字的报错都没有。
if [ "${MURMUR_ENABLED:-0}" = "1" ]; then
  MURMUR_DATA="${MURMUR_DATA:-/data/murmur}"
  mkdir -p "$MURMUR_DATA/snapshots"
  if [ -L murmur/data ] || [ ! -e murmur/data ]; then
    ln -sfn "$MURMUR_DATA" murmur/data
  elif [ -d murmur/data ] && [ -z "$(ls -A murmur/data 2>/dev/null)" ]; then
    rmdir murmur/data && ln -sfn "$MURMUR_DATA" murmur/data
  else
    echo "[entrypoint] ⚠️ murmur/data 是个非空真目录，不是软链——" \
         "情绪状态会写进镜像层，重部署就没了。手动检查一下。"
  fi
  echo "[entrypoint] 起 murmur（潮汐）…"
  ( cd murmur && exec python3 -m uvicorn api:app --host 127.0.0.1 --port "${MURMUR_PORT:-8020}" ) &
  MM_PID=$!
  # 跟 anno / hervoice 一个规矩：它挂了不拖垮容器，只是潮汐那一页看不了。
  ( while kill -0 "$MM_PID" 2>/dev/null; do sleep 30; done
    echo "[entrypoint] ⚠️ murmur 退出了，潮汐那一页会打不开；其余功能不受影响" ) &
else
  echo "[entrypoint] murmur 未开启（MURMUR_ENABLED=0），潮汐那一页会说没开启"
fi

echo "[entrypoint] 起小窝…"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8787}"
