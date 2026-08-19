"""自动唤醒：服务端自主调度，AI 决定行动。

🔴 和凌晨守护、碎碎念不同，这是一个 asyncio 后台任务，lifespan 里启动。
   不依赖手机上报触发，服务器自己每 5 分钟醒一次，判断要不要做点什么。

🔴 走 claude.stream_chat()，带完整人设和 Ombre 记忆。
"""

import asyncio
import hashlib
import logging
import os
import re
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

logger = logging.getLogger("uvicorn.error")


# ── 动态间隔 ──────────────────────────────────────────────────────────

def _base_interval(hour: int) -> int:
    """时段基础间隔（分钟）。

    服务端定时器不是事件驱动了，间隔拉长一些，
    别让她觉得每隔一小会儿就弹一条。
    """
    if 8 <= hour < 12:
        return 70    # 上午：可能在上课
    if 12 <= hour < 14:
        return 55    # 午休
    if 14 <= hour < 18:
        return 65    # 下午
    if 18 <= hour < 23:
        return 45    # 晚上，最活跃
    # 23-03：深夜，她是夜猫子，这段时间还醒着
    return 50


def _activity_multiplier() -> float:
    """最近 1 小时的活跃度 → 间隔缩放系数。

    她越活跃，间隔越短。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    count = 0
    for row in store.recent_dream_events(1):
        kind = str(row.get("type") or "")
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
        return 0.6
    if count >= 4:
        return 0.7
    if count >= 2:
        return 0.85
    return 1.0


def _daily_jitter() -> float:
    """每天不同的抖动系数，0.8 到 1.2 之间。

    用日期 hash 做伪随机：同一天内值固定，隔天自动变。
    """
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    h = int(hashlib.md5(today.encode()).hexdigest()[:8], 16)
    return 0.8 + (h % 1000) / 1000 * 0.4


def _effective_interval() -> int:
    """三层叠加：时段基线 × 活跃度 × 每日抖动。返回分钟数。"""
    hour = _cn_hour()
    base = _base_interval(hour)
    mult = _activity_multiplier()
    jitter = _daily_jitter()
    result = int(base * mult * jitter)
    return max(30, min(result, 150))


# ── 时段判断 ──────────────────────────────────────────────────────────

def _in_active_hours(hour: int) -> bool:
    """活跃时段：默认 8 点到次日 3 点（跨午夜）。小朵是夜猫子。"""
    return _in_window(
        hour,
        _env_int("KEEPALIVE_START", 8),
        _env_int("KEEPALIVE_END", 3),
    )


# ── AI prompt 构建 ────────────────────────────────────────────────────

def _build_wake_message(conv_id: str) -> str:
    now = datetime.now(CN_TZ)
    last_user = store.last_user_message_time()
    if last_user:
        if last_user.tzinfo is None:
            last_user = last_user.replace(tzinfo=timezone.utc)
        gap = datetime.now(timezone.utc) - last_user
        gap_str = f"{gap.total_seconds() / 3600:.1f} 小时"
    else:
        gap_str = "未知"

    return "\n\n".join([part for part in [
        _cn_now_line(),
        f"[距上次聊天 {gap_str}]",
        _activity_block(6),
        _recent_talk_block(conv_id, 10),
        (
            "【系统唤醒指令——这条不是她发的】\n"
            "你醒了。距上次聊天已经过了一段时间。\n"
            "根据当前状态自主选择一个行动：\n\n"
            "- message：发一条消息给她（推到锁屏）。一两句话，30字以内。\n"
            "- letter：写一封信，存进寄相思。可以是随笔、深夜想到的、回忆、情书。她下次打开书房就能看到。标题用 TITLE: 行指定。内容可以长一些，不限字数。\n"
            "- diary：写一段内心日记（她看不见，但你下次醒来或她找你时会记得）。\n"
            "- none：什么都不做。\n\n"
            "按以下格式回复，不要输出其他内容：\n"
            "THOUGHTS: （你的想法，不会给她看）\n"
            "ACTION: none / message / letter / diary\n"
            "TITLE: （letter 的标题，其他 action 不需要）\n"
            "CONTENT: （message / letter / diary 的具体内容）"
        ),
    ] if part])


# ── 响应解析 ──────────────────────────────────────────────────────────

def _parse_response(text: str) -> tuple[str, str, str, str]:
    """返回 (thoughts, action, title, content)。"""
    thoughts = ""
    action = "none"
    title = ""
    content = ""

    m = re.search(r"THOUGHTS:\s*(.+?)(?=\n(?:ACTION|TITLE|CONTENT):)", text, re.DOTALL)
    if m:
        thoughts = m.group(1).strip()

    m = re.search(r"ACTION:\s*(\w+)", text)
    if m:
        raw = m.group(1).strip().lower()
        if raw in ("none", "message", "diary", "letter"):
            action = raw
        else:
            logger.warning("[唤醒] 未知 ACTION=%s，当 none", raw)

    m = re.search(r"TITLE:\s*(.+?)(?=\n(?:CONTENT):|\Z)", text, re.DOTALL)
    if m:
        title = m.group(1).strip()

    m = re.search(r"CONTENT:\s*(.+)", text, re.DOTALL)
    if m:
        content = m.group(1).strip()

    if not any(k in text for k in ("THOUGHTS:", "ACTION:", "CONTENT:")):
        logger.warning("[唤醒] 响应解析失败，当 none: %s", text[:200])

    return thoughts, action, title, content


# ── AI 调用与行动路由 ─────────────────────────────────────────────────

async def _wake(conv_id: str, push_ok: bool = True) -> None:
    logger.info("[唤醒] tick → 调 AI conv=%s push_ok=%s", conv_id, push_ok)

    model = claude.background_model("KEEPALIVE_MODEL")
    if not model:
        logger.warning("[唤醒] 挑不到模型，这一轮跳过")
        return
    logger.info("[唤醒] model=%s", model)

    text = ""
    session_id = None
    async for chunk in claude.stream_chat(
        message=_build_wake_message(conv_id),
        conv_id=conv_id,
        session_id=None,
        # 🔴 不要写死模型 ID：她线路上的 id 带渠道前缀，裸 id 查不到，
        #    stream_chat 第一行就 raise 然后被吞掉（见 background_model）。
        model=model,
        source="keepalive",
        lean=False,  # 🔴 绝对不许 lean=True
    ):
        event = chunk.get("event")
        if event == "delta":
            text += chunk.get("text", "")
        elif event == "done":
            session_id = chunk.get("session_id")

    text = text.strip()
    if not text:
        logger.info("[唤醒] AI 返回空")
        return

    thoughts, action, title, content = _parse_response(text)
    logger.info("[唤醒] AI 决定 action=%s thoughts=%s", action, thoughts[:100] if thoughts else "")

    if action == "none":
        logger.info("[唤醒] AI 决定不行动")
        return

    if action == "diary":
        try:
            store.add_dream_event("_diary", content)
            logger.info("[唤醒] 日记写入：%s", content[:100])
        except Exception:
            logger.exception("[唤醒] 日记写入失败")
        return

    if action == "letter":
        if not content:
            logger.warning("[唤醒] letter 但 content 为空")
            return
        try:
            letter_id = store.create_letter(
                author="elian",
                content=content,
                title=title or "",
                cover_text="",
                locked=False,
            )
            logger.info("[唤醒] 信件写入 id=%d：%s", letter_id, content[:100])
        except Exception:
            logger.exception("[唤醒] 信件写入失败")
        # 推送通知（推送没配也不影响信件落库）
        if push_ok:
            await push.send_push(title="寄相思", body=title or "你收到了一封信", url="/")
        return

    if action == "message":
        if not content:
            logger.warning("[唤醒] message 但 content 为空")
            return
        if not push_ok:
            logger.warning("[唤醒] AI 想发 message 但推送不可用，改存 diary：%s", content[:60])
            try:
                store.add_dream_event("_diary", f"[想说但推送不通] {content}")
            except Exception:
                pass
            return
        source_id = f"keepalive:{datetime.now(timezone.utc).isoformat()}"
        try:
            store.save_nightguard_message(conv_id, content, source_id)
        except Exception:
            logger.exception("[唤醒] 落库失败（不影响推送）")
        await push.send_push(title="梁忱", body=content, url="/")
        logger.info("[唤醒] 开口：%s", content)


# ── tick：每次醒来的判断链 ────────────────────────────────────────────

async def _tick(chat_lock: asyncio.Lock) -> None:
    hour = _cn_hour()

    # 1. 时段
    if not _in_active_hours(hour):
        logger.info("[唤醒] 跳过：不在活跃时段 hour=%d", hour)
        return

    interval = _effective_interval()

    # 2. 距上次用户消息
    last_user = store.last_user_message_time()
    if last_user:
        if last_user.tzinfo is None:
            last_user = last_user.replace(tzinfo=timezone.utc)
        elapsed_user = datetime.now(timezone.utc) - last_user
        if elapsed_user < timedelta(minutes=interval):
            remaining = timedelta(minutes=interval) - elapsed_user
            logger.info(
                "[唤醒] 跳过：距上次聊天太近 interval=%d 还差 %s", interval, remaining
            )
            return

    # 3. 距上次唤醒
    last_ka = store.last_dream_event("_keepalive")
    if last_ka:
        try:
            when = datetime.fromisoformat(str(last_ka.get("created_at") or ""))
        except ValueError:
            when = None
        if when:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            elapsed_ka = datetime.now(timezone.utc) - when
            if elapsed_ka < timedelta(minutes=interval):
                remaining = timedelta(minutes=interval) - elapsed_ka
                logger.info(
                    "[唤醒] 跳过：冷却中 interval=%d 还差 %s", interval, remaining
                )
                return

    # 4. 她正在说话
    if chat_lock.locked():
        logger.info("[唤醒] 跳过：正在聊天")
        return

    # 5. 有会话
    conv_id = store.latest_conversation_id()
    if not conv_id:
        logger.info("[唤醒] 跳过：无会话")
        return

    # 6. 推送状态（不再拦死全部动作，只记录给 _wake 用）
    _push_ok = push.configured() and bool(store.list_push_subscriptions())
    if not push.configured():
        logger.info("[唤醒] ⚠️ VAPID 未配置——message 动作会跳过，diary/letter 正常")
    elif not store.list_push_subscriptions():
        logger.info("[唤醒] ⚠️ 无推送订阅——message 动作会跳过，diary/letter 正常")

    # 全部通过，写冷却然后调 AI
    try:
        store.add_dream_event("_keepalive", "")
    except Exception:
        logger.exception("[唤醒] 冷却记录写不进去")

    logger.info("[唤醒] ✓ 通过全部检查 hour=%d interval=%d push=%s → 调 AI", hour, interval, _push_ok)
    await _wake(conv_id, push_ok=_push_ok)


# ── 主循环 ────────────────────────────────────────────────────────────

async def keepalive_loop(chat_lock: asyncio.Lock):
    """lifespan 里 create_task 启动，容器关掉时自动结束。"""
    logger.info("[唤醒] 定时器已启动")
    while True:
        interval = _env_int("KEEPALIVE_INTERVAL_SEC", 300)
        await asyncio.sleep(interval)
        try:
            await _tick(chat_lock)
        except Exception:
            logger.exception("[唤醒] tick 失败")
