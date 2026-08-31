FROM python:3.12-slim

# ── 系统依赖：Node + ffmpeg，一层装完 ────────────────────────────────
# Node：claude-agent-sdk 要它跑捆绑的 CLI。
# 🔴 ffmpeg 是 hervoice 的硬依赖：把手机录的 webm 转成 16k 单声道 wav。
#    librosa/numpy 小窝本来就有（琴房频谱在用）。
#
# 🔴 这一层必须待在 `COPY . .` **前面**。08-31 之前 ffmpeg 那句写在 COPY
#    后面，后果是**每推一次代码就重装一次 ffmpeg**（好几百 MB 的下载 +
#    一百多个包）。她第一次部署跑了一个小时还失败了。
#    规矩：跟代码无关的东西，一律排在 COPY 之前，让 Docker 的层缓存接住。
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python 依赖 ──────────────────────────────────────────────────────
# 🔴 pymupdf / ebooklib 是「一起看书」要的：anno 的 PDF/EPUB 提取是
#    execFileSync('python3', ...) 起子进程跑的，所以必须在镜像里，
#    不能等运行时装。跟主依赖同一层，同样在 COPY 之前。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir pymupdf ebooklib

# ── anno 的 Node 依赖 ────────────────────────────────────────────────
# 🔴 只 COPY 两个 package 文件再 npm install：这样只有依赖真的变了才重装。
#    改一行 Python 不该把 npm install 也拖下水。
#    下面那句 `COPY . .` 不会覆盖 node_modules——构建上下文里根本没有它
#    （.gitignore 和 .dockerignore 都排除了），而 COPY 只添不删。
COPY anno/server/package.json anno/server/package-lock.json ./anno/server/
RUN cd anno/server && npm install --omit=dev

# ── 代码 ─────────────────────────────────────────────────────────────
COPY . .
RUN chmod +x *.sh

# 🔴 anno 的 server.mjs 把四个路径写死成常量（第 19-22 行），改不了：
#      /opt/marginalia/{data,uploads,extract_pdf.py,extract_epub.py}
#    而这个镜像的持久卷是 /data。这儿只做 /opt 里那条软链——它在镜像层里，
#    不会被卷盖住。
#    🔴 /data 下面的目录和脚本软链**必须放到容器启动时做**，不能写在这儿：
#       构建期在 /data 下建的东西，运行时会被挂上来的持久卷整个盖掉。
#       见 docker-entrypoint.sh。
RUN ln -sfn /data/marginalia /opt/marginalia

# 代码在 /app，数据统一放 /data（部署时把持久卷挂到 /data，聊天记录才不会丢）
ENV AGENT_APP_ROOT=/data \
    MODELS_FILE=/app/models.json \
    HOST=0.0.0.0 \
    PORT=8787 \
    ANNO_HOME=/data/marginalia \
    ANNO_ORIGIN=http://127.0.0.1:3300 \
    ANNO_MCP_URL=http://127.0.0.1:3300/mcp/sse \
    VOICE_CACHE_DIR=/data/voice \
    HERVOICE_ORIGIN=http://127.0.0.1:8010 \
    HERVOICE_DATA=/data/hervoice \
    TELEMOOD_STATE_DIR=/data/telemood

RUN mkdir -p /data

EXPOSE 8787
# 🔴 不能直接 CMD uvicorn 了：一起看书要 anno 那个 Node 服务在旁边跑着，
#    否则书房第五栏点开是 502。docker-entrypoint.sh 把两个都拉起来。
CMD ["./docker-entrypoint.sh"]
