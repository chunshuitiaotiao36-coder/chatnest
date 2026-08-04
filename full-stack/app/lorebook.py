"""世界书 / 调性：同一张表、同一套注入引擎。

调性 = 世界书里「常驻 + 系统提示前 + 无关键词」的那一类，不是另做一套。
`kind` 只用来分 UI 的两个 tab，注入引擎不看它。

🔴 这个模块存在的头号理由是**保护一小时的 prompt 缓存**。
缓存是字节级前缀匹配，前面变一个字后面全废。所以：

    常驻条目（always_on=1）        -> 允许注入系统提示前后：内容每轮一样，前缀稳定
    关键词触发条目                 -> 禁止注入系统提示前后：命中与否每轮不同，
                                      前缀每轮都变，缓存全废

这条不是建议，是硬校验。后端这一道是最后一道（前端还会把选项置灰），
两道都要在。
"""

from __future__ import annotations

import json
import logging
import re
import time

from app import store

logger = logging.getLogger("chatnest.lorebook")

# 注入位置。前两个贴着系统提示，后三个骑在对话侧。
POSITION_SYSTEM = ("system_before", "system_after")
POSITION_CHAT = ("chat_top", "chat_bottom", "depth")
POSITIONS = POSITION_SYSTEM + POSITION_CHAT
ROLES = ("user", "assistant")
KINDS = ("lore", "tone")


class LorebookError(ValueError):
    """给前端看的人话错误。main.py 直接把 str(exc) 塞进 400 的 detail。"""


def validate(entry: dict) -> None:
    """保存前的校验。**缓存那条红线就在这儿把关。**"""
    name = str(entry.get("name") or "").strip()
    if not name:
        raise LorebookError("条目要有名字")
    if not str(entry.get("content") or "").strip():
        raise LorebookError("条目内容不能为空")

    position = entry.get("position") or "system_before"
    if position not in POSITIONS:
        raise LorebookError(f"位置只能是 {' / '.join(POSITIONS)} 之一")

    role = entry.get("role") or "user"
    if role not in ROLES:
        raise LorebookError(f"角色只能是 {' / '.join(ROLES)} 之一")
    if role == "assistant":
        raise LorebookError("assistant 角色暂未支持，先用 user")

    if (entry.get("kind") or "lore") not in KINDS:
        raise LorebookError("kind 只能是 lore 或 tone")

    always_on = bool(entry.get("always_on"))
    keywords = _as_keywords(entry.get("keywords"))

    # 🔴 红线：靠关键词触发的条目不许贴系统提示
    if position in POSITION_SYSTEM and not always_on:
        raise LorebookError(
            "关键词触发的条目只能注入到对话侧（对话顶部 / 对话底部 / 指定深度）。"
            "放进系统提示会让每一轮的前缀都不一样，一小时的缓存会全部重建。"
        )
    if not always_on and not keywords:
        raise LorebookError("不常驻的条目至少要有一个关键词，否则永远不会触发")

    if position == "depth":
        depth = entry.get("depth")
        if depth is None or int(depth) < 0:
            raise LorebookError("位置选了「指定深度」就要填一个 ≥0 的深度")

    scan_depth = int(entry.get("scan_depth") or 5)
    if scan_depth < 1:
        raise LorebookError("扫描深度至少是 1")

    if entry.get("use_regex"):
        for kw in keywords:
            try:
                re.compile(kw)
            except re.error as exc:
                raise LorebookError(f"正则「{kw}」编译不过：{exc}") from exc


def _as_keywords(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _row_to_entry(row) -> dict:
    entry = dict(row)
    entry["keywords"] = _as_keywords(entry.get("keywords"))
    for flag in ("enabled", "always_on", "use_regex", "case_sensitive"):
        entry[flag] = bool(entry.get(flag))
    return entry


def list_entries() -> list[dict]:
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM lorebook_entries ORDER BY priority DESC, id ASC"
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def create_entry(payload: dict) -> dict:
    validate(payload)
    now = int(time.time())
    with store._connect() as db:
        cur = db.execute(
            """
            INSERT INTO lorebook_entries
                (name, enabled, content, always_on, keywords, use_regex,
                 case_sensitive, scan_depth, position, depth, role, priority,
                 kind, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            _bind(payload) + (now, now),
        )
        row = db.execute(
            "SELECT * FROM lorebook_entries WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _row_to_entry(row)


def update_entry(entry_id: int, payload: dict) -> dict:
    with store._connect() as db:
        row = db.execute(
            "SELECT * FROM lorebook_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise LookupError(entry_id)
        merged = {**_row_to_entry(row), **payload}
        validate(merged)
        db.execute(
            """
            UPDATE lorebook_entries SET
                name=?, enabled=?, content=?, always_on=?, keywords=?, use_regex=?,
                case_sensitive=?, scan_depth=?, position=?, depth=?, role=?,
                priority=?, kind=?, updated_at=?
            WHERE id=?
            """,
            _bind(merged) + (int(time.time()), entry_id),
        )
        row = db.execute(
            "SELECT * FROM lorebook_entries WHERE id = ?", (entry_id,)
        ).fetchone()
    return _row_to_entry(row)


def delete_entry(entry_id: int) -> None:
    with store._connect() as db:
        db.execute("DELETE FROM lorebook_entries WHERE id = ?", (entry_id,))


def _bind(e: dict) -> tuple:
    return (
        str(e.get("name") or "").strip(),
        int(bool(e.get("enabled", True))),
        str(e.get("content") or ""),
        int(bool(e.get("always_on"))),
        json.dumps(_as_keywords(e.get("keywords")), ensure_ascii=False),
        int(bool(e.get("use_regex"))),
        int(bool(e.get("case_sensitive"))),
        int(e.get("scan_depth") or 5),
        e.get("position") or "system_before",
        int(e["depth"]) if e.get("depth") is not None else None,
        e.get("role") or "user",
        int(e.get("priority") if e.get("priority") is not None else 100),
        e.get("kind") or "lore",
    )


def _matches(entry: dict, haystack: str) -> bool:
    """一条条目的关键词有没有命中。任意一个命中就算命中。"""
    keywords = entry["keywords"]
    if not keywords:
        return False
    if entry["use_regex"]:
        flags = 0 if entry["case_sensitive"] else re.IGNORECASE
        for kw in keywords:
            try:
                if re.search(kw, haystack, flags):
                    return True
            except re.error as exc:
                # 写坏的正则不许把聊天打挂：退化成普通字符串比对，记一条 warning
                logger.warning("世界书条目 %s 的正则「%s」编译不过，按普通文本处理：%s",
                               entry.get("id"), kw, exc)
                if _plain_hit(kw, haystack, entry["case_sensitive"]):
                    return True
        return False
    return any(_plain_hit(kw, haystack, entry["case_sensitive"]) for kw in keywords)


def _plain_hit(kw: str, haystack: str, case_sensitive: bool) -> bool:
    return (kw in haystack) if case_sensitive else (kw.lower() in haystack.lower())


def collect(user_message: str, recent_messages: list[str] | None = None) -> dict[str, list[str]]:
    """返回 {position: [content, ...]}，同一 position 内按 priority 降序。

    - enabled=0 的跳过
    - always_on=1 的无条件收
    - 否则扫 recent_messages 的最后 scan_depth 条 **+ 当前这条 user_message**
      （当前这条必须算进去，不然她刚说了「人称」，那条要等下一轮才生效）
    """
    recent = list(recent_messages or [])
    out: dict[str, list[str]] = {p: [] for p in POSITIONS}
    try:
        entries = list_entries()
    except Exception as exc:  # 表还没建、库锁住……都不该让聊天挂掉
        logger.warning("世界书读取失败，这一轮不注入任何条目：%s", exc)
        return out

    for entry in entries:
        if not entry["enabled"]:
            continue
        if entry["always_on"]:
            hit = True
        else:
            window = recent[-max(1, int(entry["scan_depth"] or 5)):]
            haystack = "\n".join([*window, user_message or ""])
            hit = _matches(entry, haystack)
        if not hit:
            continue
        position = entry["position"] if entry["position"] in out else "system_before"
        out[position].append(entry["content"])
    return out


def render_chat_block(collected: dict[str, list[str]], position: str) -> str:
    """把某个对话侧位置的条目拼成一段文本。空的就返回空串。"""
    items = [c for c in collected.get(position, []) if c.strip()]
    if not items:
        return ""
    body = "\n\n".join(items)
    return f"<lorebook>\n{body}\n</lorebook>"
