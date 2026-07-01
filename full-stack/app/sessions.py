from typing import Any

from app.store import (
    conversation_list,
    conversation_messages,
    delete_conversation,
    rename_conversation,
    star_conversation,
)


def session_list() -> list[dict[str, Any]]:
    return conversation_list()


def session_messages(
    conversation_id: str,
    before_id: int | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    return conversation_messages(conversation_id, before_id, limit)


def set_session_title(conversation_id: str, title: str) -> None:
    rename_conversation(conversation_id, title)


def set_session_starred(conversation_id: str, starred: bool) -> None:
    star_conversation(conversation_id, starred)


def remove_session(conversation_id: str) -> None:
    delete_conversation(conversation_id)
