FROM python:3.12-slim

# 🔴 下面到 `pip install -r requirements.txt` 为止的四条指令**一个字都不要改**。
#    Docker 的层缓存是按「指令原文 + 上游层」算的，改动其中任何一条，
#    后面**全部**层作废——包括那条几十分钟的 pip（chromadb 拖着 onnxruntime、
#    librosa 拖着 numba/llvmlite、还有 matplotlib）。
#    08-31 我为了合并 ffmpeg 动了第一条 apt，等于把她服务器上那份已经建好的
#    pip 缓存扔了。她当时正卡在 Coolify 一小时的部署超时上，那一下会让情况更糟。
#    要加新东西，往下面加新层，别动上面这四条。

# claude-agent-sdk 需要 Node 运行时来执行捆绑的 CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 下面几层都是新加的，但**必须待在 `COPY . .` 前面** ────────────────
# 🔴 08-31 之前 ffmpeg 和 anno 的 npm 写在 `COPY . .` 后面，后果不是
#    「第一次装得久」，是**每推一次代码这两样都从头重装一遍**：
#    ffmpeg 一个人要拉一百多个包、好几百 MB，层缓存一次都没接住过。
#    规矩：跟代码无关的东西一律排在 COPY 之前。

# 🔴 ffmpeg 是 hervoice 的硬依赖：把手机录的 webm 转成 16k 单声道 wav。
#    librosa/numpy 小窝本来就有（琴房频谱在用）。
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# 🔴 pymupdf / ebooklib 是「一起看书」要的：anno 的 PDF/EPUB 提取是
#    execFileSync('python3', ...) 起子进程跑的，所以必须在镜像里，
#    不能等运行时装。单独一层，不要并进上面那条 requirements——
#    并进去就把那层的缓存打掉了。
RUN pip install --no-cache-dir pymupdf ebooklib

# ── anno 的 Node 依赖 ────────────────────────────────────────────────
# 🔴 只 COPY 两个 package 文件再 npm install：这样只有依赖真的变了才重装。
#    改一行 Python 不该把 npm install 也拖下水。
#    下面那句 `COPY . .` 不会覆盖 node_modules——构建上下文里根本没有它
#    （.gitignore 和 .dockerignore 都排除了），而 COPY 只添不删。
COPY anno/server/package.json anno/server/package-lock.json ./anno/server/
RUN cd anno/server && npm install --omit=dev

# 🔴 潮汐（Murmur）要的 pyyaml。**单独一层，而且必须排在这儿**——
#    照上面 pymupdf 那条的规矩：并进 requirements.txt 会把第 19 行那层
#    （anthropic / claude-agent-sdk / librosa / matplotlib…）的缓存整个打掉，
#    连带下面 ffmpeg、pymupdf、npm install 全部重跑。08-31 就是这么把她的
#    部署拖进一小时超时的，不能再来第二次。
#    放在 `COPY . .` 正上方 = 只有它自己这一层是新的，下面本来每次推代码都要重跑。
#    （uvicorn[standard] 的 extra 里其实带着 PyYAML，但那是传递依赖——
#      跟 httpx 一个道理，我们自己 import 的东西要自己写出来。）
RUN pip install --no-cache-dir pyyaml

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
    TELEMOOD_STATE_DIR=/data/telemood \
    MURMUR_ORIGIN=http://127.0.0.1:8020 \
    MURMUR_DATA=/data/murmur

RUN mkdir -p /data

EXPOSE 8787
# 🔴 不能直接 CMD uvicorn 了：一起看书要 anno 那个 Node 服务在旁边跑着，
#    否则书房第五栏点开是 502。docker-entrypoint.sh 把两个都拉起来。
CMD ["./docker-entrypoint.sh"]
