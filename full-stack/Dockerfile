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

# 代码在 /app，数据统一放 /data（部署时把持久卷挂到 /data，聊天记录才不会丢）
ENV AGENT_APP_ROOT=/data \
    MODELS_FILE=/app/models.json \
    SPLASH_LINES_FILE=/app/splash_lines.json \
    HOST=0.0.0.0 \
    PORT=8787

RUN mkdir -p /data

EXPOSE 8787
CMD ["bash", "./run-with-memory.sh"]
