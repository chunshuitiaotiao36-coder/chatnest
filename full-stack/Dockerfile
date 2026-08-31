FROM python:3.12-slim

# claude-agent-sdk 需要 Node 运行时来执行捆绑的 CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x *.sh

# ── 一起看书（书房第五栏，anno）──────────────────────────────────────
# Node 上面已经装了（claude-agent-sdk 要）。这儿只补 anno 自己的两份依赖。
# 🔴 anno 的 PDF/EPUB 提取是 execFileSync('python3', ...) 起子进程跑的，
#    所以 pymupdf / ebooklib 必须在镜像里，不能等运行时装。
RUN cd anno/server && npm install --omit=dev \
 && pip install --no-cache-dir pymupdf ebooklib

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
    VOICE_CACHE_DIR=/data/voice

RUN mkdir -p /data

EXPOSE 8787
# 🔴 不能直接 CMD uvicorn 了：一起看书要 anno 那个 Node 服务在旁边跑着，
#    否则书房第五栏点开是 502。docker-entrypoint.sh 把两个都拉起来。
CMD ["./docker-entrypoint.sh"]
