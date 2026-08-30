#!/usr/bin/env bash
# 一起看书（anno）一次性安装。装完跑 ./run-with-anno.sh 就行。
#
# 🔴 anno 的 server.mjs 里有四个写死的绝对路径（第 19-22 行）：
#       /opt/marginalia/data          书的 JSON
#       /opt/marginalia/uploads       上传的 PDF / EPUB 原件
#       /opt/marginalia/extract_pdf.py
#       /opt/marginalia/extract_epub.py
#    它 README 里说这些能用环境变量改，**是假的**，代码里是 const。
#    我们不许改 anno 的源码（要保持跟上游一个字节不差，将来能直接拉更新），
#    所以由这个脚本把这四个位置备齐。
#
# 🔴 data 和 uploads 是她的书和批注，必须放持久卷。容器重建就没了的目录
#    别往里放——用 ANNO_HOME 指到持久卷上，这儿会做软链。
set -euo pipefail

cd "$(dirname "$0")"
[ -f .env ] && { set -a; source .env; set +a; }

ANNO_HOME="${ANNO_HOME:-/opt/marginalia}"
HERE="$(pwd)/anno"

echo "==> 装 Node 依赖"
command -v node >/dev/null || { echo "✗ 没有 node。anno 是 Node 服务，先装 Node 18+"; exit 1; }
( cd anno/server && npm install --omit=dev )

echo "==> 装 Python 提取依赖（anno 用它们读 PDF / EPUB）"
"${PYTHON:-python3}" -m pip install --quiet pymupdf ebooklib

echo "==> 备齐 $ANNO_HOME"
mkdir -p "$ANNO_HOME/data" "$ANNO_HOME/uploads"
# 提取脚本跟着仓库走：软链而不是复制，将来 git pull 更新了脚本自动生效。
ln -sf "$HERE/server/extract_pdf.py"  "$ANNO_HOME/extract_pdf.py"
ln -sf "$HERE/server/extract_epub.py" "$ANNO_HOME/extract_epub.py"

# server.mjs 认死 /opt/marginalia。如果 ANNO_HOME 指到别处（持久卷），
# 就在 /opt 下做一条软链把它接过去。
if [ "$ANNO_HOME" != "/opt/marginalia" ]; then
  echo "==> /opt/marginalia -> $ANNO_HOME"
  mkdir -p /opt
  ln -sfn "$ANNO_HOME" /opt/marginalia
fi

# 示例书：书架空着的时候放一本进去，省得她第一次打开是白的。
if [ -z "$(ls -A "$ANNO_HOME/data" 2>/dev/null)" ]; then
  echo "==> 书架是空的，放一本示例书进去"
  cp anno/data.example/*.json "$ANNO_HOME/data/"
fi

echo
echo "✓ 装好了。跑 ./run-with-anno.sh"
echo "  书和批注存在：$ANNO_HOME/data"
