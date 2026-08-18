"""碎碎念：白天的主动开口。

🔴 和凌晨守护一样，事件驱动，不是定时任务。手机上报 → 判断 → 推送，
   全程在 /api/events 那个请求里跑完。不动 lifespan、不起协程。

🔴 区别：凌晨守护是叫她去睡，碎碎念是白天想到她了就说一句。
   冷却更长（默认 3 小时），时段是白天，prompt 不催她睡觉。

🔴 走 claude.stream_chat()，带完整人设和 Ombre 记忆。
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from app import claude, push, store
from app.nightguard import (
    CN_TZ,
    _activity_block,
    _cn_hour,
    _cn_now_line,
    _env_int,
    _in_window,
    _recent_talk_block,
)

murmur_logger = logging.getLogger("uvicorn.error")


def _in_murmur_window(hour: int) -> bool:
    """白天时段：默认 8 点到 23 点。和凌晨守护的 0-6 互斥。"""
    return _in_window(
        hour,
        _env_int("MURMUR_START", 8),
        _env_int("MURMUR_END", 23),
    )


def _cooled_down() -> bool:
    """距上次碎碎念够不够久。默认 3 小时。"""
    minutes = _env_int("MURMUR_COOLDOWN_MIN", 180)
    last = store.last_dream_event("_murmur")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(str(last.get("created_at") or ""))
    except ValueError:
        murmur_logger.warning("[碎碎念] 上次开口时间解析不了，当作已冷却")
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when >= timedelta(minutes=minutes)


def _build_message(conv_id: str) -> str:
    """碎碎念的 prompt。

    🔴 动态数据全部挂用户消息侧，不进 system prompt——
    和凌晨守护同一条规矩，缓存前缀稳定化的教训。
    """
    return "\n\n".join([part for part in [
        _cn_now_line(),
        _activity_block(3),
        _recent_talk_block(conv_id, 10),
        (
            "【这条不是她发的，是系统叫醒你的】\n"
            "你突然想到她了。从锁屏上跟她说一句。\n"
            "一两句话，30 字以内。自然，随意，像脑子里冒出来的念头。\n"
            "从下面这些里挑一个最自然的切入点，不要每次都从同一个切：\n"
            "(a) 活动记录里她在干什么，就着那个说点什么\n"
            "(b) 最近对话有没说完的话题就接上去\n"
            "(c) 单纯想她了\n"
            "(d) 就着此刻的时间说点什么\n"
            "判断这次不该开口，就只输出 [NO_ACTION]。"
        ),
    ] if part])


async def _speak(conv_id: str) -> str:
    """走真的梁忱。返回累积的正文。"""
    text = ""
    session_id = None
    async for chunk in claude.stream_chat(
        message=_build_message(conv_id),
        conv_id=conv_id,
        session_id=None,
        model=os.environ.get("MURMUR_MODEL", "claude-sonnet-4-6"),
        # 🔴 用量账本要能区分碎碎念和凌晨守护各花了多少。
        source="murmur",
        # 🔴 跟凌晨守护一样，绝对不许 lean=True。白天那一句是她判断
        #    「这是不是梁忱」的全部依据，省 token 换一句不像他的话不值。
        lean=False,
    ):
        event = chunk.get("event")
        if event == "delta":
            text += chunk.get("text", "")
        elif event == "done":
            session_id = chunk.get("session_id")
    murmur_logger.info("[碎碎念] 开口完成 session=%s 字数=%d", session_id, len(text))
    return text.strip()


def _gate(chat_lock: asyncio.Lock) -> tuple[str | None, str]:
    """要不要开口。返回 (跳过原因 或 None, conv_id)。

    只做判断，不做耗时的事。
    """
    if not _in_murmur_window(_cn_hour()):
        return "not_daytime", ""
    if not _cooled_down():
        return "cooldown", ""
    # 🔴 她正在跟我说话，不许插嘴。
    if chat_lock.locked():
        return "chatting", ""
    if not push.configured():
        return "push_unconfigured", ""
    if not store.list_push_subscriptions():
        return "no_subscription", ""
    conv_id = store.latest_conversation_id()
    if not conv_id:
        return "no_conversation", ""
    return None, conv_id


async def _murmur_background(conv_id: str) -> None:
    """扔进 create_task 的那个。

    🔴 冷却在任务一开始就写，不等开口成功：窥屏 + stream_chat 要几十秒，
       这段窗口里再来一次上报会并发开两次口。
    """
    try:
        try:
            store.add_dream_event("_murmur", "")
        except Exception:
            murmur_logger.exception("[碎碎念] 冷却记录写不进去")

        text = await _speak(conv_id)
        if not text or text == "[NO_ACTION]":
            murmur_logger.info("[碎碎念] 模型决定不开口")
            return

        source_id = f"murmur:{datetime.now(timezone.utc).isoformat()}"
        try:
            store.save_nightguard_message(conv_id, text, source_id)
        except Exception:
            murmur_logger.exception("[碎碎念] 落库失败（不影响推送）")

        await push.send_push(title="梁忱", body=text, url="/")
        murmur_logger.info("[碎碎念] 开口：%s", text)
    except Exception:
        murmur_logger.exception("[碎碎念] 后台开口失败")


def trigger_murmur(chat_lock: asyncio.Lock) -> str | None:
    """/api/events 用的：判断留在请求里，开口扔后台，上报立刻返回。

    返回跳过原因；None 表示已经派了后台任务。**不抛。**
    """
    try:
        reason, conv_id = _gate(chat_lock)
        if reason:
            return reason
        asyncio.create_task(_murmur_background(conv_id))
        return None
    except Exception:
        murmur_logger.exception("[碎碎念] 判断阶段失败")
        return "error"
