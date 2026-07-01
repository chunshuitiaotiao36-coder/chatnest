import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import delete_session, list_sessions


ROOT = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)).expanduser().resolve()
PROJECT_DIR = str(ROOT)
DB_PATH = Path(os.environ.get("CONVERSATION_DB", ROOT / "conversations.db")).expanduser().resolve()
SESSION_DIR = Path(os.environ.get("CLAUDE_SESSION_DIR", Path.home() / ".claude" / "projects")).expanduser()
SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ConversationNotFound(LookupError):
    pass


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_store() -> None:
    with _connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conv_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                starred INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                latest_session_id TEXT
            );
            CREATE TABLE IF NOT EXISTS session_aliases (
                session_id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL REFERENCES conversations(conv_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL REFERENCES conversations(conv_id)
                    ON DELETE CASCADE,
                source_id TEXT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                text TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                traces_json TEXT NOT NULL DEFAULT '[]',
                edited INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conv_id
                ON messages(conv_id, id);
            CREATE UNIQUE INDEX IF NOT EXISTS messages_source_id
                ON messages(conv_id, source_id)
                WHERE source_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS message_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL REFERENCES conversations(conv_id)
                    ON DELETE CASCADE,
                base_message_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('edit', 'retry')),
                tail_json TEXT NOT NULL,
                latest_session_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS message_branches_conv_base
                ON message_branches(conv_id, base_message_id);
            """
        )
        message_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "attachments_json" not in message_columns:
            db.execute(
                """
                ALTER TABLE messages
                ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'
                """
            )
        if "traces_json" not in message_columns:
            db.execute(
                """
                ALTER TABLE messages
                ADD COLUMN traces_json TEXT NOT NULL DEFAULT '[]'
                """
            )
        if "edited" not in message_columns:
            db.execute(
                """
                ALTER TABLE messages
                ADD COLUMN edited INTEGER NOT NULL DEFAULT 0
                """
            )
        branch_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(message_branches)").fetchall()
        }
        if "latest_session_id" not in branch_columns:
            db.execute(
                """
                ALTER TABLE message_branches
                ADD COLUMN latest_session_id TEXT
                """
            )
        migrated = db.execute(
            "SELECT value FROM store_meta WHERE key = 'legacy_migrated'"
        ).fetchone()
    if not migrated:
        migrate_legacy_sessions()
        with _connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO store_meta(key, value)
                VALUES ('legacy_migrated', ?)
                """,
                (_now(),),
            )


def _physical_messages(session_id: str) -> list[dict[str, Any]]:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        return []
    path = SESSION_DIR / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    assistants: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = item.get("type")
            message = item.get("message", {})
            timestamp = item.get("timestamp") or _now()
            if role == "user" and isinstance(message.get("content"), str):
                result.append(
                    {
                        "source_id": item.get("uuid"),
                        "role": "user",
                        "text": message["content"],
                        "thinking": "",
                        "timestamp": timestamp,
                    }
                )
            elif role == "assistant":
                source_id = message.get("id") or item.get("uuid")
                entry = assistants.get(source_id)
                if entry is None:
                    entry = {
                        "source_id": source_id,
                        "role": "assistant",
                        "text": "",
                        "thinking": "",
                        "timestamp": timestamp,
                    }
                    assistants[source_id] = entry
                    result.append(entry)
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        entry["text"] += block.get("text", "")
                    elif block.get("type") == "thinking":
                        entry["thinking"] += block.get("thinking", "")
    return [
        item
        for item in result
        if item["role"] == "user" or item["text"] or item["thinking"]
    ]


def migrate_legacy_sessions() -> None:
    sessions = list_sessions(directory=PROJECT_DIR, limit=500)
    with _connect() as db:
        for session in sessions:
            known = db.execute(
                "SELECT conv_id FROM session_aliases WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if known:
                continue
            conv_id = str(uuid.uuid4())
            created = session.created_at or session.last_modified or _now()
            updated = session.last_modified or session.created_at or created
            title = (
                session.custom_title
                or session.summary
                or session.first_prompt
                or "新对话"
            )
            db.execute(
                """
                INSERT INTO conversations
                    (conv_id, title, starred, created_at, updated_at,
                     latest_session_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conv_id,
                    title,
                    int(session.tag == "starred"),
                    str(created),
                    str(updated),
                    session.session_id,
                ),
            )
            db.execute(
                "INSERT INTO session_aliases(session_id, conv_id) VALUES (?, ?)",
                (session.session_id, conv_id),
            )
            for message in _physical_messages(session.session_id):
                db.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (conv_id, source_id, role, text, thinking, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conv_id,
                        message["source_id"],
                        message["role"],
                        message["text"],
                        message["thinking"],
                        message["timestamp"],
                    ),
                )


def conversation_list() -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """
            SELECT conv_id, title, starred, created_at, updated_at,
                   latest_session_id
            FROM conversations
            ORDER BY starred DESC, updated_at DESC
            """
        ).fetchall()
    def api_time(value: str) -> str | int:
        return int(value) if value and value.isdigit() else value

    return [
        {
            "conv_id": row["conv_id"],
            "session_id": row["conv_id"],
            "title": row["title"],
            "starred": bool(row["starred"]),
            "created_at": api_time(row["created_at"]),
            "last_modified": api_time(row["updated_at"]),
            "updated_at": api_time(row["updated_at"]),
            "latest_session_id": row["latest_session_id"],
        }
        for row in rows
    ]


def resolve_conversation(identifier: str | None) -> str | None:
    if not identifier:
        return None
    with _connect() as db:
        row = db.execute(
            "SELECT conv_id FROM conversations WHERE conv_id = ?",
            (identifier,),
        ).fetchone()
        if row:
            return row["conv_id"]
        row = db.execute(
            "SELECT conv_id FROM session_aliases WHERE session_id = ?",
            (identifier,),
        ).fetchone()
        return row["conv_id"] if row else None


def ensure_conversation(
    conversation_id: str | None = None,
    title: str = "新对话",
) -> str:
    conv_id = resolve_conversation(conversation_id)
    if conversation_id and not conv_id:
        raise ConversationNotFound("conversation not found")
    if conv_id:
        return conv_id
    conv_id = str(uuid.uuid4())
    now = _now()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO conversations
                (conv_id, title, starred, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (conv_id, title[:120] or "新对话", now, now),
        )
    return conv_id


def _message_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["attachments"] = json.loads(item.pop("attachments_json"))
    except (json.JSONDecodeError, TypeError):
        item["attachments"] = []
    try:
        item["traces"] = json.loads(item.pop("traces_json"))
    except (json.JSONDecodeError, TypeError):
        item["traces"] = []
    item["edited"] = bool(item.get("edited", 0))
    item["branch_count"] = int(item.get("branch_count", 0) or 0)
    return item


def _rows_to_messages(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [_message_from_row(row) for row in rows]


def begin_turn(
    message: str,
    conversation_id: str | None = None,
    legacy_session_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[str, str | None, int]:
    conv_id = resolve_conversation(conversation_id or legacy_session_id)
    now = _now()
    with _connect() as db:
        if conv_id:
            row = db.execute(
                "SELECT latest_session_id FROM conversations WHERE conv_id = ?",
                (conv_id,),
            ).fetchone()
            if not row:
                raise ConversationNotFound("conversation not found")
            resume_id = row["latest_session_id"]
        else:
            conv_id = str(uuid.uuid4())
            resume_id = None
            db.execute(
                """
                INSERT INTO conversations
                    (conv_id, title, starred, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (conv_id, message.strip()[:120] or "新对话", now, now),
            )
        title_row = db.execute(
            "SELECT title FROM conversations WHERE conv_id = ?",
            (conv_id,),
        ).fetchone()
        if title_row and title_row["title"] == "新对话":
            title = message.strip()[:120]
            if not title and attachments:
                title = attachments[0].get("name", "附件")
            db.execute(
                "UPDATE conversations SET title = ? WHERE conv_id = ?",
                (title or "新对话", conv_id),
            )
        cursor = db.execute(
            """
            INSERT INTO messages(
                conv_id, role, text, thinking, attachments_json, timestamp
            )
            VALUES (?, 'user', ?, '', ?, ?)
            """,
            (
                conv_id,
                message,
                json.dumps(attachments or [], ensure_ascii=False),
                now,
            ),
        )
        user_message_id = int(cursor.lastrowid)
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE conv_id = ?",
            (now, conv_id),
        )
    return conv_id, resume_id, user_message_id


def complete_turn(
    conv_id: str,
    session_id: str,
    text: str,
    thinking: str,
    traces: list | None = None,
) -> int:
    now = _now()
    with _connect() as db:
        if not db.execute(
            "SELECT 1 FROM conversations WHERE conv_id = ?",
            (conv_id,),
        ).fetchone():
            raise ConversationNotFound("conversation not found")
        db.execute(
            """
            UPDATE conversations
            SET latest_session_id = ?, updated_at = ?
            WHERE conv_id = ?
            """,
            (session_id, now, conv_id),
        )
        db.execute(
            """
            INSERT INTO session_aliases(session_id, conv_id)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET conv_id = excluded.conv_id
            """,
            (session_id, conv_id),
        )
        traces_str = json.dumps(traces or [], ensure_ascii=False)
        cursor = db.execute(
            """
            INSERT INTO messages(
                conv_id, role, text, thinking, attachments_json, traces_json, timestamp
            )
            VALUES (?, 'assistant', ?, ?, '[]', ?, ?)
            """,
            (conv_id, text, thinking, traces_str, now),
        )
        return int(cursor.lastrowid)


def _select_context_messages(
    db: sqlite3.Connection,
    conv_id: str,
    through_id: int,
) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT m.id, m.source_id, m.role, m.text, m.thinking, m.attachments_json,
               m.traces_json, m.edited, m.timestamp,
               (
                   SELECT COUNT(*)
                   FROM message_branches b
                   WHERE b.conv_id = m.conv_id AND b.base_message_id = m.id
               ) AS branch_count
        FROM messages m
        WHERE m.conv_id = ? AND m.id <= ?
        ORDER BY m.id
        """,
        (conv_id, through_id),
    ).fetchall()
    return _rows_to_messages(rows)


def _tail_snapshot(rows: list[sqlite3.Row]) -> str:
    return json.dumps(_rows_to_messages(rows), ensure_ascii=False)


def prepare_edit_turn(
    conversation_id: str,
    message_id: int,
    content: str,
) -> dict[str, Any]:
    resolved = resolve_conversation(conversation_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    content = content.strip()
    if not content:
        raise ValueError("message cannot be empty")
    now = _now()
    with _connect() as db:
        row = db.execute(
            """
            SELECT id, source_id, role, text, thinking, attachments_json,
                   traces_json, edited, timestamp
            FROM messages
            WHERE conv_id = ? AND id = ?
            """,
            (resolved, message_id),
        ).fetchone()
        if not row:
            raise ConversationNotFound("message not found")
        if row["role"] != "user":
            raise ValueError("only user messages can be edited")
        tail_rows = db.execute(
            """
            SELECT id, source_id, role, text, thinking, attachments_json,
                   traces_json, edited, timestamp, 0 AS branch_count
            FROM messages
            WHERE conv_id = ? AND id >= ?
            ORDER BY id
            """,
            (resolved, message_id),
        ).fetchall()
        if tail_rows:
            old_conv = db.execute(
                "SELECT latest_session_id FROM conversations WHERE conv_id = ?",
                (resolved,),
            ).fetchone()
            cursor = db.execute(
                """
                INSERT INTO message_branches(
                    conv_id, base_message_id, kind, tail_json,
                    latest_session_id, created_at
                )
                VALUES (?, ?, 'edit', ?, ?, ?)
                """,
                (
                    resolved,
                    message_id,
                    _tail_snapshot(tail_rows),
                    old_conv["latest_session_id"] if old_conv else None,
                    now,
                ),
            )
            branch_id = int(cursor.lastrowid)
        else:
            branch_id = None
        db.execute(
            "DELETE FROM messages WHERE conv_id = ? AND id > ?",
            (resolved, message_id),
        )
        db.execute(
            """
            UPDATE messages
            SET text = ?, edited = 1
            WHERE conv_id = ? AND id = ?
            """,
            (content, resolved, message_id),
        )
        db.execute(
            """
            UPDATE conversations
            SET latest_session_id = NULL, updated_at = ?
            WHERE conv_id = ?
            """,
            (now, resolved),
        )
        context = _select_context_messages(db, resolved, message_id)
        edited = context[-1]
    return {
        "conv_id": resolved,
        "resume_id": None,
        "user_message_id": message_id,
        "message": content,
        "attachments": edited.get("attachments", []),
        "context_messages": context,
        "branch_id": branch_id,
    }


def prepare_retry_turn(
    conversation_id: str,
    assistant_message_id: int,
) -> dict[str, Any]:
    resolved = resolve_conversation(conversation_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    now = _now()
    with _connect() as db:
        row = db.execute(
            """
            SELECT id, role
            FROM messages
            WHERE conv_id = ? AND id = ?
            """,
            (resolved, assistant_message_id),
        ).fetchone()
        if not row:
            raise ConversationNotFound("message not found")
        if row["role"] != "assistant":
            raise ValueError("only assistant messages can be regenerated")
        user_row = db.execute(
            """
            SELECT id, source_id, role, text, thinking, attachments_json,
                   traces_json, edited, timestamp
            FROM messages
            WHERE conv_id = ? AND id < ? AND role = 'user'
            ORDER BY id DESC
            LIMIT 1
            """,
            (resolved, assistant_message_id),
        ).fetchone()
        if not user_row:
            raise ConversationNotFound("source user message not found")
        tail_rows = db.execute(
            """
            SELECT id, source_id, role, text, thinking, attachments_json,
                   traces_json, edited, timestamp, 0 AS branch_count
            FROM messages
            WHERE conv_id = ? AND id >= ?
            ORDER BY id
            """,
            (resolved, assistant_message_id),
        ).fetchall()
        if tail_rows:
            old_conv = db.execute(
                "SELECT latest_session_id FROM conversations WHERE conv_id = ?",
                (resolved,),
            ).fetchone()
            cursor = db.execute(
                """
                INSERT INTO message_branches(
                    conv_id, base_message_id, kind, tail_json,
                    latest_session_id, created_at
                )
                VALUES (?, ?, 'retry', ?, ?, ?)
                """,
                (
                    resolved,
                    assistant_message_id,
                    _tail_snapshot(tail_rows),
                    old_conv["latest_session_id"] if old_conv else None,
                    now,
                ),
            )
            branch_id = int(cursor.lastrowid)
        else:
            branch_id = None
        db.execute(
            "DELETE FROM messages WHERE conv_id = ? AND id >= ?",
            (resolved, assistant_message_id),
        )
        db.execute(
            """
            UPDATE conversations
            SET latest_session_id = NULL, updated_at = ?
            WHERE conv_id = ?
            """,
            (now, resolved),
        )
        context = _select_context_messages(db, resolved, int(user_row["id"]))
        user = _message_from_row(user_row)
    return {
        "conv_id": resolved,
        "resume_id": None,
        "user_message_id": int(user_row["id"]),
        "message": user["text"],
        "attachments": user.get("attachments", []),
        "context_messages": context,
        "branch_id": branch_id,
    }


def restore_branch(branch_id: int | None) -> None:
    if not branch_id:
        return
    with _connect() as db:
        branch = db.execute(
            """
            SELECT conv_id, tail_json, latest_session_id
            FROM message_branches
            WHERE id = ?
            """,
            (branch_id,),
        ).fetchone()
        if not branch:
            return
        try:
            tail = json.loads(branch["tail_json"])
        except (json.JSONDecodeError, TypeError):
            return
        if not tail:
            return
        first_id = min(int(item["id"]) for item in tail if item.get("id"))
        now = _now()
        db.execute(
            "DELETE FROM messages WHERE conv_id = ? AND id >= ?",
            (branch["conv_id"], first_id),
        )
        for item in tail:
            db.execute(
                """
                INSERT OR REPLACE INTO messages(
                    id, conv_id, source_id, role, text, thinking,
                    attachments_json, traces_json, edited, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(item["id"]),
                    branch["conv_id"],
                    item.get("source_id"),
                    item.get("role"),
                    item.get("text", ""),
                    item.get("thinking", ""),
                    json.dumps(item.get("attachments", []), ensure_ascii=False),
                    json.dumps(item.get("traces", []), ensure_ascii=False),
                    int(bool(item.get("edited", False))),
                    item.get("timestamp") or now,
                ),
            )
        db.execute(
            """
            UPDATE conversations
            SET latest_session_id = ?, updated_at = ?
            WHERE conv_id = ?
            """,
            (branch["latest_session_id"], now, branch["conv_id"]),
        )


def conversation_messages(
    conv_id: str,
    before_id: int | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    page_size = max(1, min(limit or 0, 200)) if limit else None
    with _connect() as db:
        if page_size:
            params: list[Any] = [resolved]
            before_clause = ""
            if before_id is not None:
                before_clause = "AND m.id < ?"
                params.append(before_id)
            params.append(page_size + 1)
            rows = db.execute(
                f"""
                SELECT m.id, m.role, m.text, m.thinking, m.attachments_json,
                       m.traces_json, m.edited, m.timestamp,
                       (
                           SELECT COUNT(*)
                           FROM message_branches b
                           WHERE b.conv_id = m.conv_id
                             AND b.base_message_id = m.id
                       ) AS branch_count
                FROM messages m
                WHERE m.conv_id = ?
                {before_clause}
                ORDER BY m.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            has_more = len(rows) > page_size
            rows = rows[:page_size]
            rows = list(reversed(rows))
        else:
            rows = db.execute(
                """
                SELECT m.id, m.role, m.text, m.thinking, m.attachments_json,
                       m.traces_json, m.edited, m.timestamp,
                       (
                           SELECT COUNT(*)
                           FROM message_branches b
                           WHERE b.conv_id = m.conv_id
                             AND b.base_message_id = m.id
                       ) AS branch_count
                FROM messages m
                WHERE m.conv_id = ?
                ORDER BY m.id
                """,
                (resolved,),
            ).fetchall()
            has_more = False
    messages = _rows_to_messages(rows)
    next_before_id = messages[0]["id"] if has_more and messages else None
    return messages, has_more, next_before_id


def rename_conversation(conv_id: str, title: str) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _connect() as db:
        db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE conv_id = ?",
            (title.strip(), _now(), resolved),
        )


def star_conversation(conv_id: str, starred: bool) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _connect() as db:
        db.execute(
            "UPDATE conversations SET starred = ? WHERE conv_id = ?",
            (int(starred), resolved),
        )


def delete_conversation(conv_id: str) -> None:
    resolved = resolve_conversation(conv_id)
    if not resolved:
        raise ConversationNotFound("conversation not found")
    with _connect() as db:
        aliases = [
            row["session_id"]
            for row in db.execute(
                "SELECT session_id FROM session_aliases WHERE conv_id = ?",
                (resolved,),
            ).fetchall()
        ]
        db.execute("DELETE FROM conversations WHERE conv_id = ?", (resolved,))
    for session_id in aliases:
        try:
            delete_session(session_id, directory=PROJECT_DIR)
        except Exception:
            pass
