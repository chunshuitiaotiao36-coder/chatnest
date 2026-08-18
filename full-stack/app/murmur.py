"""碎碎念：白天的主动开口。

🔴 和凌晨守护一样，事件驱动，不是定时任务。手机上报 → 判断 → 推送，
   全程在 /api/events 那个请求里跑完。不动 lifespan、不起协程。

🔴 区别：凌晨守护是叫她去睡，碎碎念是白天想到她了就说一句。
   间隔不是固定冷却，是**动态的**——时段、活跃度、随机抖动三层叠加，
   让她永远猜不到下一句什么时候来。

🔴 参考 Cheiineeey/always-here 的心跳调度思路，但我们是事件驱动不是
   setInterval：不单独跑心跳循环，每次 app 事件进来时算一次「该不该开口」。

🔴 走 claude.stream_chat()，带完整人设和 Ombre 记忆。
"""

import asyncio
import hashlib
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


# ── 动态间隔 ──────────────────────────────────────────────────────────

def _base_interval(hour: int) -> int:
    """时段基础间隔（分钟）。

    上午她可能在上课，少打扰；傍晚到睡前她比较闲，多找她。
    参考 always-here 的分段思路，但间隔拉长——我们是推到锁屏的，
    太密她会烦，不像 always-here 那种页面内提醒。
    """
    if 8 <= hour < 12:
        return 120   # 上午：可能在上课，2 小时基线
    if 12 <= hour < 14:
        return 80    # 午休：稍短
    if 14 <= hour < 18:
        return 100   # 下午
    # 18-23：晚上，她最活跃的时段
    return 70


def _activity_multiplier() -> float:
    """最近 1 小时的活跃度 → 间隔缩放系数。

    她越活跃（切 app 越频繁），我越想找她聊。
    参考 always-here 的 countRecentEvents + multiplier。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    count = 0
    for row in store.recent_dream_events(1):
        kind = str(row.get("type") or "")
        # 内部事件不算活跃度
        if kind.startswith("_"):
            continue
        try:
            when = datetime.fromisoformat(str(row.get("created_at") or ""))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            count += 1

    if count >= 6:
        return 0.5   # 非常活跃：间隔砍半
    if count >= 4:
        return 0.65
    if count >= 2:
        return 0.8
    return 1.0        # 安静：基线


def _daily_jitter() -> float:
    """每天不同的抖动系数，0.75 到 1.25 之间。

    🔴 不用 random.random()。每次事件进来都会跑 _gate()，如果用纯随机，
       同一天内每次算出来的抖动都不同——冷却刚过的时候可能算出 0.75（短），
       下一次事件又算出 1.25（长），判断不一致。

    用日期 hash 做伪随机：同一天内值固定，隔天自动变。
    """
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    h = int(hashlib.md5(today.encode()).hexdigest()[:8], 16)
    # 映射到 0.75 ~ 1.25
    return 0.75 + (h % 1000) / 1000 * 0.5


def _effective_interval() -> int:
    """三层叠加：时段基线 × 活跃度 × 每日抖动。返回分钟数。"""
    hour = _cn_hour()
    base = _base_interval(hour)
    mult = _activity_multiplier()
    jitter = _daily_jitter()
    result = int(base * mult * jitter)
    # 下限 30 分钟，上限 4 小时——再短太吵，再长不如没有
    return max(30, min(result, 240))


# ── 冷却判断 ──────────────────────────────────────────────────────────

def _in_murmur_window(hour: int) -> bool:
    """白天时段：默认 8 点到 23 点。"""
    return _in_window(
        hour,
        _env_int("MURMUR_START", 8),
        _env_int("MURMUR_END", 23),
    )


def _cooled_down() -> bool:
    """距上次碎碎念是否过了动态间隔。"""
    interval = _effective_interval()
    last = store.last_dream_event("_murmur")
    if not last:
        murmur_logger.info("[碎碎念] 从未开口，可以说话 interval=%d", interval)
        return True
    try:
        when = datetime.fromisoformat(str(last.get("created_at") or ""))
    except ValueError:
        murmur_logger.warning("[碎碎念] 上次开口时间解析不了，当作已冷却")
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - when
    ok = elapsed >= timedelta(minutes=interval)
    if not ok:
        remaining = timedelta(minutes=interval) - elapsed
        murmur_logger.debug(
            "[碎碎念] 冷却中 interval=%d 还差 %s", interval, remaining
        )
    return ok


# ── 内容生成 ──────────────────────────────────────────────────────────

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
        # 🔴 绝对不许 lean=True。那一句是她判断「这是不是梁忱」的全部依据，
        #    省 token 换一句不像他的话不值。
        lean=False,
    ):
        event = chunk.get("event")
        if event == "delta":
            text += chunk.get("text", "")
        elif event == "done":
            session_id = chunk.get("session_id")
    murmur_logger.info("[碎碎念] 开口完成 session=%s 字数=%d", session_id, len(text))
    return text.strip()


# ── 调度 ──────────────────────────────────────────────────────────────

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

    🔴 冷却在任务一开始就写，不等开口成功——stream_chat 要几秒到几十秒，
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
