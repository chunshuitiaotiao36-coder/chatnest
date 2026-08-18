"""Embedded BM25 memory search — no separate service needed.

Reads CLAUDE.md, profile.json, and memories/*.md directly, builds an
in-process BM25 index with jieba tokenization, and auto-refreshes when
files change. Replaces the previous architecture that required a separate
memory_search_service process on port 3900.

If the environment variable MEMORY_SEARCH_URL is set to an HTTP URL,
falls back to the old HTTP-client behavior for backward compatibility
with the standalone vector+BM25 service.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 路径 ──────────────────────────────────────────────────
ROOT = Path(
    os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)
).expanduser().resolve()
MEMORY_FILE = Path(
    os.environ.get("MEMORY_FILE", ROOT / "CLAUDE.md")
).expanduser().resolve()
PROFILE_FILE = Path(
    os.environ.get("PROFILE_FILE", ROOT / "profile.json")
).expanduser().resolve()
MEMORIES_DIR = Path(
    os.environ.get("MEMORIES_DIR", ROOT / "memories")
).expanduser().resolve()

# ── 调参 ──────────────────────────────────────────────────
CHUNK_MAX = int(os.environ.get("MEMORY_CHUNK_MAX", "3000"))
MAX_FILE_CHARS = int(os.environ.get("MEMORY_MAX_FILE_CHARS", "200000"))
BM25_POOL = int(os.environ.get("MEMORY_BM25_POOL", "16"))
DEFAULT_TOP_K = int(os.environ.get("MEMORY_SEARCH_TOP_K", "8"))
DEFAULT_BUDGET = int(os.environ.get("MEMORY_SEARCH_BUDGET_CHARS", "2000"))
REFRESH_INTERVAL = float(os.environ.get("MEMORY_SEARCH_REFRESH_SECONDS", "60"))
MIN_QUERY_CHARS = 2  # 中文两个字就是完整词（寸止、断联、猫咪……）

# ── 外部服务回退 ──────────────────────────────────────────
_EXTERNAL_URL = os.environ.get("MEMORY_SEARCH_URL", "").strip()
_EXTERNAL_TIMEOUT = 1.5

# ── 分段正则 ──────────────────────────────────────────────
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_CHAPTER_RE = re.compile(
    r"^第[一二三四五六七八九十百零\d]+章\s*[·：:]*\s*"
)
_SECTION_RE = re.compile(r"^【(.+?)】")
_TOKEN_OK = re.compile(r"[一-鿿A-Za-z0-9]")


# =====================================================================
#  文本分段
# =====================================================================

def _is_section_boundary(line: str) -> str | None:
    """如果这一行是章节/段落的开头，返回标题；否则 None。"""
    stripped = line.strip()
    if not stripped:
        return None
    m = _HEADER_RE.match(stripped)
    if m:
        return m.group(2).strip()
    if _CHAPTER_RE.match(stripped):
        return stripped
    m = _SECTION_RE.match(stripped)
    if m:
        return m.group(1).strip()
    return None


def _split_text(text: str) -> list[tuple[str, str]]:
    """把一段文本按章节标记切成 (heading, body) 列表。

    识别三种分界线：
    - Markdown 标题 (## xxx)
    - 中文章节标记 (第X章 · xxx)
    - 方括号段落 (【xxx】)

    超过 CHUNK_MAX 的段落按空行再切一刀，标题保留。
    """
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []
    cur_heading = ""
    cur_body: list[str] = []

    def flush() -> None:
        body = "\n".join(cur_body).strip()
        if body:
            sections.append((cur_heading, body))

    for line in lines:
        heading = _is_section_boundary(line)
        if heading is not None:
            flush()
            cur_body = []
            cur_heading = heading
        else:
            cur_body.append(line)
    flush()

    if not sections and text.strip():
        sections = [("", text.strip())]

    # 超长段落按空行拆分，保持同一个 heading
    out: list[tuple[str, str]] = []
    for heading, body in sections:
        if len(body) <= CHUNK_MAX:
            out.append((heading, body))
            continue
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        chunk = ""
        for para in paras:
            if chunk and len(chunk) + len(para) + 2 > CHUNK_MAX:
                out.append((heading, chunk.strip()))
                chunk = para
            else:
                chunk = f"{chunk}\n\n{para}" if chunk else para
        if chunk.strip():
            out.append((heading, chunk.strip()))
    return out


# =====================================================================
#  BM25 索引
# =====================================================================

def _tokenize(text: str) -> list[str]:
    import jieba
    return [t for t in jieba.lcut(text) if _TOKEN_OK.search(t)]


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:MAX_FILE_CHARS]
    except OSError:
        return ""


def _file_signature() -> str:
    """文件修改时间拼成的签名，变了就重建索引。"""
    parts: list[str] = []
    for p in [MEMORY_FILE, PROFILE_FILE]:
        try:
            parts.append(f"{p.name}:{p.stat().st_mtime:.0f}")
        except OSError:
            parts.append(f"{p.name}:0")
    if MEMORIES_DIR.exists():
        for p in sorted(MEMORIES_DIR.rglob("*")):
            if p.suffix.lower() in {".md", ".txt"} and p.is_file():
                try:
                    parts.append(f"{p.name}:{p.stat().st_mtime:.0f}")
                except OSError:
                    pass
    return "|".join(parts)


class _MemoryIndex:
    """进程内的 BM25 记忆索引。"""

    def __init__(self) -> None:
        self.chunks: list[dict[str, str]] = []
        self.bm25: Any = None
        self._sig = ""
        self._last_check = 0.0
        self._build()

    # ── 收集所有记忆源 ────────────────────────────────────
    def _collect(self) -> list[dict[str, str]]:
        chunks: list[dict[str, str]] = []

        # 1) CLAUDE.md / MEMORY_FILE
        if MEMORY_FILE.exists() and MEMORY_FILE.is_file():
            text = _safe_read(MEMORY_FILE)
            if text.strip():
                for heading, body in _split_text(text):
                    chunks.append({
                        "text": body,
                        "heading": heading,
                        "source": MEMORY_FILE.name,
                    })

        # 2) profile.json → savedMemories
        if PROFILE_FILE.exists():
            try:
                profile = json.loads(_safe_read(PROFILE_FILE))
            except (json.JSONDecodeError, ValueError):
                profile = {}
            for item in profile.get("savedMemories") or []:
                if not isinstance(item, dict):
                    continue
                if not item.get("enabled", True):
                    continue
                content = str(item.get("content") or "").strip()
                if content:
                    chunks.append({
                        "text": content,
                        "heading": "",
                        "source": "savedMemories",
                    })

        # 3) memories/ 目录
        if MEMORIES_DIR.exists():
            for path in sorted(MEMORIES_DIR.rglob("*")):
                if path.suffix.lower() not in {".md", ".txt"}:
                    continue
                if any(part.startswith(".") for part in path.parts):
                    continue
                if not path.is_file():
                    continue
                text = _safe_read(path)
                if not text.strip():
                    continue
                for heading, body in _split_text(text):
                    chunks.append({
                        "text": body,
                        "heading": heading,
                        "source": path.name,
                    })

        return chunks

    # ── 建索引 ────────────────────────────────────────────
    def _build(self) -> None:
        from rank_bm25 import BM25Okapi

        self.chunks = self._collect()
        if self.chunks:
            # 标题也参与检索，"断联""海景房"这些关键词经常只出现在标题里
            tokens = [_tokenize(f'{c["heading"]} {c["text"]}') for c in self.chunks]
            self.bm25 = BM25Okapi(tokens)
        else:
            self.bm25 = None
        self._sig = _file_signature()
        self._last_check = time.monotonic()
        logger.info("[memory] BM25 index built: %d chunks", len(self.chunks))

    # ── 自动刷新 ──────────────────────────────────────────
    def _maybe_refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_check < REFRESH_INTERVAL:
            return
        self._last_check = now
        sig = _file_signature()
        if sig != self._sig:
            logger.info("[memory] files changed, rebuilding index")
            self._build()

    # ── 搜索 ──────────────────────────────────────────────
    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        budget_chars: int = DEFAULT_BUDGET,
    ) -> str:
        self._maybe_refresh()
        if not self.bm25 or not query.strip():
            return ""
        toks = _tokenize(query)
        if not toks:
            return ""
        scores = self.bm25.get_scores(toks)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        ranked = ranked[:BM25_POOL]

        parts: list[str] = []
        used = 0
        for i, score in ranked:
            if score <= 0:
                break
            chunk = self.chunks[i]
            text = chunk["text"]
            heading = chunk.get("heading", "")
            source = chunk.get("source", "")
            size = len(text) + len(heading) + 32
            if used + size > budget_chars and parts:
                break
            label = f"[{source}"
            if heading:
                label += f" · {heading}"
            label += "]"
            parts.append(f"{label}\n{text}")
            used += size
            if len(parts) >= top_k:
                break
        return "\n\n".join(parts)


# =====================================================================
#  公共接口
# =====================================================================

_INDEX: _MemoryIndex | None = None


def _recall_local(query: str, budget_chars: int, top_k: int) -> str:
    global _INDEX
    if _INDEX is None:
        _INDEX = _MemoryIndex()
    return _INDEX.search(query, top_k, budget_chars)


def _recall_remote(query: str, budget_chars: int, top_k: int) -> str:
    """兼容旧架构：向独立的 memory_search_service 发 HTTP 请求。"""
    payload = json.dumps(
        {"query": query, "top_k": top_k, "budget_chars": budget_chars},
    ).encode("utf-8")
    req = urllib.request.Request(
        _EXTERNAL_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_EXTERNAL_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("joined", "")


def recall(query: str, budget_chars: int = 2000, top_k: int = 8) -> str:
    """检索记忆。静默失败——记忆是锦上添花，绝不能拖垮聊天。"""
    q = (query or "").strip()
    if len(q) < MIN_QUERY_CHARS:
        return ""
    try:
        if _EXTERNAL_URL:
            try:
                return _recall_remote(q, budget_chars, top_k)
            except (OSError, urllib.error.URLError):
                # 外部服务连不上（Coolify 没跑 memory_search_service），
                # 降级到进程内 BM25，不浪费这次查询。
                logger.debug("external memory service unreachable, falling back to local BM25")
        return _recall_local(q, budget_chars, top_k)
    except Exception:
        logger.warning("memory recall failed", exc_info=True)
        return ""
