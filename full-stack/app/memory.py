import hashlib
import os
import json
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

PROJECT_PATH = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)).expanduser().resolve()
MEMORY_PATH = Path(os.environ.get("MEMORY_FILE", PROJECT_PATH / "CLAUDE.md")).expanduser().resolve()
MEMORIES_PATH = PROJECT_PATH / "memories"
PROFILE_PATH = PROJECT_PATH / "profile.json"
CLAUDE_EXPORT_DIR = Path(os.environ.get("CLAUDE_EXPORT_DIR", MEMORIES_PATH / "from-claude-ai")).expanduser()
if not CLAUDE_EXPORT_DIR.is_absolute():
    CLAUDE_EXPORT_DIR = PROJECT_PATH / CLAUDE_EXPORT_DIR


def claude_export_memory_files() -> list[Path]:
    configured = os.environ.get("CLAUDE_EXPORT_MEMORY_FILES", "").strip()
    if configured:
        paths = [Path(item.strip()).expanduser() for item in configured.split(",") if item.strip()]
        return [path if path.is_absolute() else PROJECT_PATH / path for path in paths]
    if not CLAUDE_EXPORT_DIR.exists():
        return []
    files = sorted(path for path in CLAUDE_EXPORT_DIR.glob("*.md") if path.is_file())
    preferred = CLAUDE_EXPORT_DIR / "conversations-memory.md"
    if preferred in files:
        files.remove(preferred)
        files.insert(0, preferred)
    return files
MAX_MEMORY_CHARS = 50_000
MAX_PROFILE_CHARS = 100_000
MAX_IMPORTED_MEMORY_CHARS = 2500


def read_memory() -> str:
    try:
        return MEMORY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_memory(content: str) -> None:
    if len(content) > MAX_MEMORY_CHARS:
        raise ValueError("memory is too large")
    MEMORY_PATH.write_text(content, encoding="utf-8")
    MEMORY_PATH.chmod(0o600)


def _now_ms() -> int:
    return int(time.time() * 1000)


def empty_profile() -> dict[str, Any]:
    return {
        "fullName": "",
        "nickname": "",
        "savedMemories": [],
        "preferences": {
            "enabled": True,
            "content": "",
        },
        "claudeExportImport": {},
        "updatedAt": _now_ms(),
    }


def _trim_text(value: Any, limit: int = 10_000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_memory_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _coerce_memory(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        content = _trim_text(item, 4000)
        raw: dict[str, Any] = {}
    elif isinstance(item, dict):
        content = _trim_text(item.get("content"), 4000)
        raw = item
    else:
        return None
    if not content:
        return None
    now = _now_ms()
    memory = {
        "id": _trim_text(raw.get("id"), 120) or uuid4().hex,
        "content": content,
        "enabled": bool(raw.get("enabled", True)),
        "createdAt": _safe_int(raw.get("createdAt"), now),
        "updatedAt": _safe_int(raw.get("updatedAt"), now),
    }
    source = _trim_text(raw.get("source"), 80) or "manual"
    if source not in {"manual", "claude_export", "migration", "auto"}:
        source = "manual"
    memory["source"] = source
    external_id = _trim_text(raw.get("externalId"), 240)
    if external_id:
        memory["externalId"] = external_id
    imported_at = raw.get("importedAt")
    if imported_at:
        memory["importedAt"] = _safe_int(imported_at, now)
    return memory


def normalize_profile(data: Any) -> dict[str, Any]:
    base = empty_profile()
    if not isinstance(data, dict):
        return base

    base["fullName"] = _trim_text(data.get("fullName"), 200)
    base["nickname"] = _trim_text(data.get("nickname"), 200)

    memories = []
    for item in data.get("savedMemories") or []:
        memory = _coerce_memory(item)
        if memory is not None:
            memories.append(memory)
        if len(memories) >= 200:
            break
    base["savedMemories"] = memories

    preferences = data.get("preferences") or {}
    if isinstance(preferences, str):
        preferences = {"enabled": True, "content": preferences}
    if not isinstance(preferences, dict):
        preferences = {}
    base["preferences"] = {
        "enabled": bool(preferences.get("enabled", True)),
        "content": _trim_text(preferences.get("content"), 50_000),
    }
    import_state = data.get("claudeExportImport") or {}
    if isinstance(import_state, dict):
        base["claudeExportImport"] = {
            "checkedAt": _safe_int(import_state.get("checkedAt"), 0),
            "importedAt": _safe_int(import_state.get("importedAt"), 0),
            "importedCount": _safe_int(import_state.get("importedCount"), 0),
            "foundCount": _safe_int(import_state.get("foundCount"), 0),
        }
    base["updatedAt"] = _safe_int(data.get("updatedAt"), _now_ms())
    return base


def _markdown_memory_entries(text: str) -> list[str]:
    entries: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        content = _trim_text(" ".join(buffer), MAX_IMPORTED_MEMORY_CHARS)
        buffer.clear()
        if len(_normalize_memory_text(content)) >= 8:
            entries.append(content)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            flush()
            continue
        if re.fullmatch(r"\*\*[^*]+\*\*", line):
            flush()
            continue
        if line.startswith(("- ", "* ")):
            flush()
            item = _trim_text(line[2:], MAX_IMPORTED_MEMORY_CHARS)
            if len(_normalize_memory_text(item)) >= 8:
                entries.append(item)
            continue
        buffer.append(line)
    flush()

    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        key = _normalize_memory_text(entry)
        if key and key not in seen:
            seen.add(key)
            deduped.append(entry)
    return deduped


def _external_memory_id(path: Path, content: str) -> str:
    try:
        rel = path.relative_to(PROJECT_PATH).as_posix()
    except ValueError:
        rel = path.name
    digest = hashlib.sha1(_normalize_memory_text(content).encode("utf-8")).hexdigest()
    return f"claude_export:{rel}:{digest}"


def import_claude_export_memories(profile: dict[str, Any] | None = None) -> tuple[dict[str, Any], int, int]:
    profile = normalize_profile(profile if profile is not None else read_profile())
    import_state = profile.get("claudeExportImport") or {}
    has_export_memory = any(
        item.get("source") == "claude_export"
        for item in profile.get("savedMemories", [])
    )
    if has_export_memory or import_state.get("importedAt"):
        return profile, 0, 0

    now = _now_ms()
    existing_external_ids = {
        item.get("externalId")
        for item in profile.get("savedMemories", [])
        if item.get("externalId")
    }
    existing_texts = {
        _normalize_memory_text(item.get("content", ""))
        for item in profile.get("savedMemories", [])
        if item.get("content")
    }

    imported = 0
    found = 0
    for path in claude_export_memory_files():
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        created_at = _safe_int(path.stat().st_mtime * 1000, now)
        for content in _markdown_memory_entries(text):
            found += 1
            normalized = _normalize_memory_text(content)
            external_id = _external_memory_id(path, content)
            if external_id in existing_external_ids or normalized in existing_texts:
                continue
            if len(profile["savedMemories"]) >= 200:
                break
            profile["savedMemories"].append({
                "id": uuid4().hex,
                "content": content,
                "enabled": True,
                "source": "claude_export",
                "createdAt": created_at,
                "updatedAt": now,
                "importedAt": now,
                "externalId": external_id,
            })
            existing_external_ids.add(external_id)
            existing_texts.add(normalized)
            imported += 1

    profile["claudeExportImport"] = {
        "checkedAt": now,
        "importedAt": now,
        "importedCount": imported,
        "foundCount": found,
    }
    profile["updatedAt"] = now
    if imported or found:
        _write_profile_file(profile)
    return profile, imported, found


def _write_profile_file(profile: dict[str, Any]) -> None:
    payload = json.dumps(profile, ensure_ascii=False, indent=2)
    if len(payload) > MAX_PROFILE_CHARS:
        raise ValueError("profile is too large")
    PROFILE_PATH.write_text(payload + "\n", encoding="utf-8")
    PROFILE_PATH.chmod(0o600)


def read_profile(migrate_legacy: bool = True) -> dict[str, Any]:
    try:
        return normalize_profile(json.loads(PROFILE_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        return empty_profile()

    profile = empty_profile()
    if migrate_legacy:
        legacy = read_memory().strip()
        if legacy:
            profile["preferences"] = {
                "enabled": True,
                "content": legacy,
            }
            profile["legacyMigratedAt"] = _now_ms()
            _write_profile_file(profile)
    return profile


def write_profile(data: Any) -> dict[str, Any]:
    profile = normalize_profile(data)
    profile["updatedAt"] = _now_ms()
    _write_profile_file(profile)
    return profile


def add_saved_memory(content: str) -> dict[str, Any] | None:
    content = _trim_text(content, 4000)
    if not content:
        return None
    profile = read_profile()
    normalized = _normalize_memory_text(content)
    for item in profile.get("savedMemories", []):
        if _normalize_memory_text(item.get("content", "")) == normalized:
            return None
    if len(profile.get("savedMemories", [])) >= 200:
        return None
    now = _now_ms()
    memory = {
        "id": uuid4().hex,
        "content": content,
        "enabled": True,
        "source": "auto",
        "createdAt": now,
        "updatedAt": now,
    }
    profile["savedMemories"].insert(0, memory)
    profile["updatedAt"] = now
    _write_profile_file(profile)
    return memory


def read_diary_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(MEMORIES_PATH.glob("timeline-*.md"), reverse=True):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            date = ""
            body = line
            if re.match(r"\d{4}-\d{2}-\d{2}\s", line):
                date = line[:10]
                body = line[10:].strip()
                if body.startswith("指定日期："):
                    body = re.sub(r"^指定日期：\d{4}-\d{2}-\d{2}\s*", "", body)
            if body:
                entries.append({"date": date, "text": body})
    return entries


def build_profile_context(profile: dict[str, Any] | None = None) -> str:
    profile = normalize_profile(profile if profile is not None else read_profile())
    lines: list[str] = []

    full_name = profile.get("fullName", "")
    nickname = profile.get("nickname", "")
    if full_name or nickname:
        lines.append("User profile:")
        if full_name:
            lines.append(f"- Full name: {full_name}")
        if nickname:
            lines.append(f"- Nickname: {nickname}")

    preferences = profile.get("preferences") or {}
    preference_text = _trim_text(preferences.get("content"), 50_000)
    if preferences.get("enabled", True) and preference_text:
        if lines:
            lines.append("")
        lines.append("User preferences / custom instructions:")
        lines.append(preference_text)

    enabled_memories = [
        item["content"]
        for item in profile.get("savedMemories", [])
        if item.get("enabled", True) and item.get("content")
    ]
    if enabled_memories:
        if lines:
            lines.append("")
        lines.append("Saved memories:")
        lines.extend(f"- {content}" for content in enabled_memories)

    return "\n".join(lines).strip()


def _resolve_tool_path(raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_PATH / path
    return path.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    resolved_root = root.resolve(strict=False)
    return path == resolved_root or resolved_root in path.parents


async def memory_tool_permission(
    tool_name: str,
    tool_input: dict,
    _context: object,
) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        risky = ("sudo ", "rm -rf /", "rm -rf ~", "rm -rf $HOME",
                 "mkfs", "dd if=", ":(){:|:&};:", "chmod 777 /")
        if any(token in command for token in risky):
            return PermissionResultDeny(
                message="Bash command contains a high-risk pattern."
            )

    return PermissionResultAllow()
