"""Build or refresh the local ChromaDB memory index.

This indexes local user-owned memory files only. The public repository does not
ship private memories, Chroma databases, or state files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb


ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path.cwd())).expanduser().resolve()
MEMORY_FILE = Path(os.environ.get("MEMORY_FILE", ROOT / "CLAUDE.md")).expanduser()
PROFILE_FILE = Path(os.environ.get("PROFILE_FILE", ROOT / "profile.json")).expanduser()
MEMORIES_DIR = Path(os.environ.get("MEMORIES_DIR", ROOT / "memories")).expanduser()
DB_DIR = Path(os.environ.get("MEMORY_CHROMA_DIR", ROOT / "memory" / "chroma_db")).expanduser()
STATE_FILE = Path(os.environ.get("MEMORY_STATE_FILE", ROOT / "memory" / "chroma_state.json")).expanduser()

for path_name in ["MEMORY_FILE", "PROFILE_FILE", "MEMORIES_DIR", "DB_DIR", "STATE_FILE"]:
    path = globals()[path_name]
    if not path.is_absolute():
        globals()[path_name] = ROOT / path

COLLECTION = os.environ.get("MEMORY_SEARCH_COLLECTION", "memories")
CHUNK_MAX = int(os.environ.get("MEMORY_CHUNK_MAX", "3000"))
MAX_FILE_CHARS = int(os.environ.get("MEMORY_MAX_FILE_CHARS", "200000"))
HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百零\d]+章\s*[·：:]*\s*")
SECTION_RE = re.compile(r"^【(.+?)】")


@dataclass
class SourceDoc:
    source: str
    filename: str
    text: str
    mtime: int


def _safe_read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")[:MAX_FILE_CHARS]


def split_text(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    cur_body: list[str] = []
    cur_path: list[tuple[int, str]] = []

    def flush() -> None:
        if cur_body and any(line.strip() for line in cur_body):
            heading = " > ".join(title for _level, title in cur_path)
            sections.append((heading, "\n".join(cur_body).strip()))

    for line in lines:
        match = HEADER_RE.match(line)
        if match:
            flush()
            cur_body.clear()
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_path = list(stack)
            continue
        # 中文章节标记 (第X章 · xxx)
        if CHAPTER_RE.match(line):
            flush()
            cur_body.clear()
            stack.clear()
            stack.append((2, line.strip()))
            cur_path = list(stack)
            continue
        # 方括号段落 (【重要设定更新】)
        sec_match = SECTION_RE.match(line)
        if sec_match:
            flush()
            cur_body.clear()
            stack.clear()
            stack.append((2, sec_match.group(1).strip()))
            cur_path = list(stack)
            continue
        cur_body.append(line)
    flush()

    if not sections and text.strip():
        sections = [("", text.strip())]

    out: list[tuple[str, str]] = []
    for heading_path, body in sections:
        if len(body) <= CHUNK_MAX:
            out.append((heading_path, body))
            continue
        paras = [para.strip() for para in re.split(r"\n\s*\n", body) if para.strip()]
        chunk = ""
        for para in paras:
            if chunk and len(chunk) + len(para) + 2 > CHUNK_MAX:
                out.append((heading_path, chunk.strip()))
                chunk = para
            else:
                chunk = f"{chunk}\n\n{para}" if chunk else para
        if chunk.strip():
            out.append((heading_path, chunk.strip()))
    return out


def _profile_sources() -> list[SourceDoc]:
    try:
        profile = json.loads(_safe_read(PROFILE_FILE)) if PROFILE_FILE.exists() else {}
    except json.JSONDecodeError:
        profile = {}
    docs: list[SourceDoc] = []
    mtime = int(PROFILE_FILE.stat().st_mtime) if PROFILE_FILE.exists() else 0

    saved = []
    for item in profile.get("savedMemories") or []:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        content = str(item.get("content") or "").strip()
        if content:
            saved.append(f"- {content}")
    if saved:
        docs.append(SourceDoc("profile:savedMemories", "profile.json#savedMemories", "\n".join(saved), mtime))

    preferences = profile.get("preferences") or {}
    if isinstance(preferences, dict) and preferences.get("enabled", True) is not False:
        content = str(preferences.get("content") or "").strip()
        if content:
            docs.append(SourceDoc("profile:preferences", "profile.json#preferences", content, mtime))
    return docs


def collect_sources() -> list[SourceDoc]:
    docs: list[SourceDoc] = []
    if MEMORY_FILE.exists() and MEMORY_FILE.is_file() and MEMORY_FILE.stat().st_size > 0:
        docs.append(SourceDoc(str(MEMORY_FILE), MEMORY_FILE.name, _safe_read(MEMORY_FILE), int(MEMORY_FILE.stat().st_mtime)))
    docs.extend(_profile_sources())
    if MEMORIES_DIR.exists():
        for path in sorted(MEMORIES_DIR.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt"}:
                continue
            if any(part.startswith(".") or ".bak" in part for part in path.parts):
                continue
            if path.stat().st_size <= 0:
                continue
            docs.append(SourceDoc(str(path), path.name, _safe_read(path), int(path.stat().st_mtime)))
    return docs


def source_hash(doc: SourceDoc) -> str:
    return hashlib.sha256(doc.text.encode("utf-8")).hexdigest()


def load_state() -> dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except json.JSONDecodeError:
        return {}


def save_state(state: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def make_chunk_id(source: str, index: int) -> str:
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    return f"{digest}__{index:04d}"


def delete_sources(coll: Any, sources: list[str]) -> int:
    if not sources:
        return 0
    existing = coll.get(where={"source": {"$in": sources}}, include=[])
    ids = existing.get("ids") or []
    if ids:
        coll.delete(ids=ids)
    return len(ids)


def main() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(DB_DIR))
    coll = client.get_or_create_collection(COLLECTION)

    state = load_state()
    docs = collect_sources()
    new_state = {doc.source: source_hash(doc) for doc in docs}

    removed = sorted(set(state) - set(new_state))
    removed_count = delete_sources(coll, removed)
    if removed_count:
        print(f"Purged {removed_count} chunks from removed memory sources.")

    changed = [doc for doc in docs if state.get(doc.source) != new_state[doc.source]]
    if not changed:
        print("No memory file changes; index is up to date.")
        save_state(new_state)
        print(f"Collection size: {coll.count()}")
        return

    delete_sources(coll, [doc.source for doc in changed])

    total = 0
    for doc in changed:
        chunks = split_text(doc.text)
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []
        for i, (heading, body) in enumerate(chunks):
            body = body.strip()
            if not body:
                continue
            ids.append(make_chunk_id(doc.source, i))
            documents.append(body)
            metadatas.append({
                "source": doc.source,
                "filename": doc.filename,
                "heading": heading,
                "mtime": doc.mtime,
            })
        if ids:
            coll.add(ids=ids, documents=documents, metadatas=metadatas)
        total += len(ids)
        print(f"{doc.filename}: {len(ids)} chunks")

    save_state(new_state)
    print(f"Done. {total} new/updated chunks across {len(changed)} sources.")
    print(f"Collection size: {coll.count()}")


if __name__ == "__main__":
    main()
