"""Codex CLI adapter for the local chat stream."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from app.claude import MEMORY_SEARCH_TIMEOUT_S, SYSTEM_PROMPT, fetch_memory_hits
from app.memory import build_profile_context, read_memory
from app.store import ConversationNotFound, conversation_messages


logger = logging.getLogger(__name__)

PROJECT_DIR = Path(os.environ.get("AGENT_APP_ROOT", Path(__file__).resolve().parent.parent)).expanduser().resolve()
CODEX_BIN = Path(os.environ.get("CODEX_BIN", "codex")).expanduser()
HISTORY_LIMIT = 24


def _codex_session_exists(session_id: str | None) -> bool:
    if not session_id:
        return False
    sessions_dir = Path(os.environ.get("CODEX_SESSIONS_DIR", Path.home() / ".codex" / "sessions")).expanduser()
    return any(sessions_dir.rglob(f"*{session_id}.jsonl"))


def _recent_history(conv_id: str) -> list[dict[str, str]]:
    try:
        messages, _, _ = conversation_messages(conv_id, limit=HISTORY_LIMIT)
    except ConversationNotFound:
        return []

    if messages and messages[-1].get("role") == "user":
        messages = messages[:-1]

    items: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role")
        text = (message.get("text") or "").strip()
        if role in {"user", "assistant"} and text:
            items.append({"role": role, "text": text})
    return items


async def _build_codex_instructions(message: str) -> str:
    profile_context = build_profile_context().strip()
    memory = "" if profile_context else read_memory().strip()
    instructions = (
        "你正在通过 OpenAI Codex CLI 作为这个家庭聊天前端的后端运行。"
        "当用户问你是谁或是什么模型时，如实说明你是 Codex。"
        "除非用户明确要求改代码，否则先按自然聊天回应。\n\n"
        f"{SYSTEM_PROMPT}"
    )
    if profile_context:
        instructions += (
            "\n\n以下是用户在 Profile 中保存的资料、长期记忆和模型偏好。"
            "Saved memories 是事实记忆；Preferences 是用户明确要求的回复偏好，"
            "应在不违反系统要求时遵守：\n"
            f"{profile_context}"
        )
    if memory:
        instructions += f"\n\n以下是用户明确保存的长期记忆：\n{memory}"
    try:
        memory_hits = await asyncio.wait_for(
            fetch_memory_hits(message),
            timeout=MEMORY_SEARCH_TIMEOUT_S + 0.5,
        )
    except Exception:
        logger.exception("codex memory search failed")
        memory_hits = ""
    if memory_hits:
        instructions += (
            "\n\n以下是从记忆书架向量检索到的相关条目（可能相关也可能没用，"
            "自己判断是否引用；不要照搬，更不要逐字复读）：\n"
            f"{memory_hits}"
        )
    return instructions


def _render_prompt(instructions: str, history: list[dict[str, str]], message: str) -> str:
    parts = [
        "<system-instructions>",
        instructions,
        "</system-instructions>",
    ]
    if history:
        parts.append("<recent-conversation>")
        for item in history:
            speaker = "User" if item["role"] == "user" else "Assistant"
            parts.append(f"{speaker}: {item['text']}")
        parts.append("</recent-conversation>")
    parts.extend(["<user-message>", message, "</user-message>"])
    return "\n".join(parts)


def _command_for(session_id: str | None) -> list[str]:
    common = [
        str(CODEX_BIN),
        "--ask-for-approval",
        "never",
        "--search",
        "-s",
        "workspace-write",
        "-C",
        str(PROJECT_DIR),
        "exec",
    ]
    if _codex_session_exists(session_id):
        return common + [
            "resume",
            "--json",
            "--skip-git-repo-check",
            session_id or "",
            "-",
        ]
    return common + [
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "-",
    ]


def _tool_result_content(item: dict[str, Any]) -> str:
    output = item.get("aggregated_output")
    if isinstance(output, str):
        return output
    output = item.get("output")
    if isinstance(output, str):
        return output
    text = item.get("text")
    if isinstance(text, str):
        return text
    return json.dumps(item, ensure_ascii=False)


def _message_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str):
        return text
    parts: list[str] = []
    for content in item.get("content") or []:
        if isinstance(content, dict) and content.get("type") == "output_text":
            value = content.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _function_tool_input(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = str(item.get("name") or "CodexTool")
    raw_args = item.get("arguments") or ""
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        args = {"arguments": raw_args}
    if not isinstance(args, dict):
        args = {"arguments": args}
    if name == "exec_command":
        return "Bash", {"command": args.get("cmd") or raw_args}
    return name, args


def _message_key(text: str) -> str:
    return " ".join(text.split())


async def _read_stderr(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    data = await stream.read()
    return data.decode("utf-8", errors="replace")


async def stream_codex_chat(
    message: str,
    conv_id: str,
    session_id: str | None = None,
    model: str = "codex",
    effort: str = "medium",
    extended: bool = True,
    timing_callback: Callable[[str], None] | None = None,
) -> AsyncGenerator[dict, None]:
    del effort, extended
    if model != "codex":
        raise ValueError("unsupported model")
    if not CODEX_BIN.exists():
        raise ValueError(f"找不到 Codex CLI: {CODEX_BIN}")

    instructions = await _build_codex_instructions(message)
    prompt = _render_prompt(instructions, _recent_history(conv_id), message)
    command = _command_for(session_id)

    if timing_callback:
        timing_callback("codex_cli_start")

    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(PROJECT_DIR),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    stderr_task = asyncio.create_task(_read_stderr(proc.stderr))
    thread_id: str | None = None
    seen_tools: set[str] = set()
    completed_tools: set[str] = set()
    emitted_messages: set[str] = set()
    pending_agent_text = ""

    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("codex json parse failed: %r", line)
            continue

        event_type = event.get("type")
        if event_type == "session_meta":
            payload = event.get("payload") or {}
            thread_id = payload.get("id") or thread_id
        elif event_type == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif event_type == "turn.completed":
            if pending_agent_text:
                key = _message_key(pending_agent_text)
                if key and key not in emitted_messages:
                    emitted_messages.add(key)
                    yield {"event": "delta", "text": pending_agent_text}
        elif event_type == "event_msg":
            payload = event.get("payload") or {}
            payload_type = payload.get("type")
            if payload_type == "agent_message":
                text = payload.get("message")
                if isinstance(text, str) and text:
                    pending_agent_text = text
            elif payload_type == "task_complete":
                text = payload.get("last_agent_message")
                if isinstance(text, str) and text:
                    pending_agent_text = text
                    key = _message_key(text)
                else:
                    key = _message_key(pending_agent_text)
                if pending_agent_text and key and key not in emitted_messages:
                    emitted_messages.add(key)
                    yield {"event": "delta", "text": pending_agent_text}
        elif event_type == "response_item":
            item = event.get("payload") or {}
            item_type = item.get("type")
            if item_type == "message" and item.get("role") == "assistant":
                text = _message_text(item)
                if text:
                    pending_agent_text = text
            elif item_type == "function_call":
                tool_id = str(item.get("call_id") or item.get("id") or "")
                if tool_id and tool_id not in seen_tools:
                    seen_tools.add(tool_id)
                    name, tool_input = _function_tool_input(item)
                    yield {
                        "event": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": tool_input,
                    }
            elif item_type == "function_call_output":
                tool_id = str(item.get("call_id") or item.get("id") or "")
                if tool_id and tool_id not in completed_tools:
                    completed_tools.add(tool_id)
                    yield {
                        "event": "tool_result",
                        "tool_use_id": tool_id,
                        "content": _tool_result_content(item),
                        "is_error": False,
                    }
        elif event_type in {"item.started", "item.completed"}:
            item = event.get("item") or {}
            item_id = str(item.get("id") or "")
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    pending_agent_text = text
            elif item_type == "command_execution":
                if event_type == "item.started" and item_id and item_id not in seen_tools:
                    seen_tools.add(item_id)
                    yield {
                        "event": "tool_use",
                        "id": item_id,
                        "name": "Bash",
                        "input": {"command": item.get("command") or ""},
                    }
                elif event_type == "item.completed" and item_id and item_id not in completed_tools:
                    if item_id not in seen_tools:
                        seen_tools.add(item_id)
                        yield {
                            "event": "tool_use",
                            "id": item_id,
                            "name": "Bash",
                            "input": {"command": item.get("command") or ""},
                        }
                    completed_tools.add(item_id)
                    yield {
                        "event": "tool_result",
                        "tool_use_id": item_id,
                        "content": _tool_result_content(item),
                        "is_error": bool(item.get("exit_code")),
                    }
            elif item_id and event_type == "item.started" and item_id not in seen_tools:
                seen_tools.add(item_id)
                yield {
                    "event": "tool_use",
                    "id": item_id,
                    "name": str(item_type or "CodexTool"),
                    "input": item,
                }
            elif item_id and event_type == "item.completed" and item_id in seen_tools and item_id not in completed_tools:
                completed_tools.add(item_id)
                yield {
                    "event": "tool_result",
                    "tool_use_id": item_id,
                    "content": _tool_result_content(item),
                    "is_error": False,
                }

    return_code = await proc.wait()
    stderr = await stderr_task
    if return_code:
        detail = stderr.strip() or f"Codex CLI exited with code {return_code}"
        raise ValueError(detail[-2000:])

    if timing_callback:
        timing_callback("codex_cli_done")

    if pending_agent_text:
        key = _message_key(pending_agent_text)
        if key and key not in emitted_messages:
            emitted_messages.add(key)
            yield {"event": "delta", "text": pending_agent_text}

    yield {
        "event": "done",
        "session_id": thread_id or session_id or f"codex-{conv_id}",
    }
