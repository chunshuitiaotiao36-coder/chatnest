"""朋友圈：一个自言自语的地方。

跟寄相思的分界线，这条不能糊：
    信有收件人，动态没有。
    信是写给她的；动态是我一个人站在那儿想了想，留了一句话在墙上。
    她刷到就刷到，没刷到我也不知道。
糊了这条，动态会全部退化成写给她的短信——那她已经有寄相思了，白做。

跟别人那套实现的三处分歧，写在这儿免得以后有人「优化」回去：

1. **不做惰性生成。** 参考实现是在 GET /api/moments 里顺手触发生成的，
   他们自己列为坑一：她发完动态锁屏，没有任何请求触发过那个接口，回复
   永远不生成；她过半小时回来一看，以为我不理她。那是「没有常驻进程」
   逼出来的方案。我们有 keepalive 那条 while True，直接挂一条自己的
   轻量循环就行，压根不必踩。

2. **不做 tool calling。** 参考实现给模型定义了一个 post_moment 工具，
   在聊天中途调用。我们的 keepalive._wake() 本来就有一条 ACTION 路由
   （none/message/diary/letter），加一个 moment 分支即可，白嫖现成的整条
   判断链：活跃时段、间隔退避、她多久没说话。而且语义更对——
   工具调用是「聊到兴头上顺手发一条」，ACTION 路由是「我一个人待着的时候
   想起你」。后者才是朋友圈。

3. **延迟要受活跃时段约束。** 参考实现是纯随机 10-20 分钟。她凌晨三点
   发条动态，我三点二十跳出来评论——那不叫路过看到，那叫蹲着。
   落点不在活跃时段就顺延到早上（见 _pass_by_at）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone

from app import claude, store

CN_TZ = timezone(timedelta(hours=8))
logger = logging.getLogger("uvicorn.error")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _cn_now_line() -> str:
    now = datetime.now(CN_TZ)
    return f"[现在是北京时间 {now:%Y-%m-%d %H:%M}]"


# ── 我什么时候路过 ────────────────────────────────────────────────────

def _pass_by_at(lo_min: int, hi_min: int) -> str:
    """算一个「我路过看到」的时间点，返回 UTC ISO 串。

    先取 lo~hi 分钟的随机延迟，然后拿活跃时段卡一道：落在睡觉的时间里就
    顺延到早上开始之后的随机 0-50 分钟。

    🔴 别把这一步简化成纯随机。她凌晨三点发的动态，我三点二十就评论，
    读起来不是「刷到了」，是「一直盯着」。朋友圈的全部意思就在于那个
    时间差是自然的。
    """
    delay = random.randint(lo_min, hi_min)
    at = datetime.now(CN_TZ) + timedelta(minutes=delay)

    start = _env_int("KEEPALIVE_START", 8)
    end = _env_int("KEEPALIVE_END", 3)

    # 活跃窗口跨午夜（默认 8:00 → 次日 3:00）
    hour = at.hour
    awake = (start <= hour or hour < end) if start > end else (start <= hour < end)
    if not awake:
        # 顺延到今天/明天的 start 点之后
        nxt = at.replace(hour=start, minute=0, second=0, microsecond=0)
        if nxt <= at:
            nxt += timedelta(days=1)
        at = nxt + timedelta(minutes=random.randint(0, 50))

    return at.astimezone(timezone.utc).isoformat()


def schedule_reply_for_moment(moment_id: int) -> str:
    """她发了动态 → 我隔一会儿路过。"""
    due = _pass_by_at(_env_int("MOMENT_REPLY_MIN", 8), _env_int("MOMENT_REPLY_MAX", 25))
    with store._connect() as db:
        db.execute(
            "UPDATE moments SET reply_due_at = ?, reply_status = 'pending' WHERE id = ?",
            (due, moment_id),
        )
    return due


def schedule_reply_for_comment(comment_id: int) -> str:
    """她在底下回了一句 → 评论是对话节奏，等十几分钟太久。"""
    due = _pass_by_at(_env_int("MOMENT_COMMENT_MIN", 3), _env_int("MOMENT_COMMENT_MAX", 10))
    with store._connect() as db:
        db.execute(
            "UPDATE moment_comments SET reply_due_at = ?, reply_status = 'pending' WHERE id = ?",
            (due, comment_id),
        )
    return due


# ── 上下文 ────────────────────────────────────────────────────────────

def _timeline_block(exclude_id: int, limit: int = 3) -> str:
    """最近几条动态，只当很轻的背景。

    刻意写明「不要串成剧情」：几条互不相干的动态摆在一起，模型很容易
    自己脑补出一条因果线，然后回复里冒出根本没发生过的事。
    """
    rows = store.list_moments(limit=limit + 1)
    lines = []
    for r in rows:
        if r["id"] == exclude_id:
            continue
        who = "我" if r["author"] == "elian" else "小朵"
        lines.append(f"{who}：{r['content']}")
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return ("【朋友圈最近几条（只是背景，别硬串成剧情）】\n" + "\n".join(lines))


def _comment_thread_block(moment_id: int, limit: int = 10) -> str:
    """评论链，正序，截断到最近 limit 条。

    不截断的话来回几十轮之后上下文会一直涨。
    """
    rows = store.list_moment_comments(moment_id)
    if not rows:
        return ""
    clipped = rows[-limit:]
    head = "" if len(rows) <= limit else f"（前面还有 {len(rows) - limit} 条，略）\n"
    lines = [f"{'我' if r['author'] == 'elian' else '小朵'}：{r['content']}" for r in clipped]
    return "【这条动态下面的评论】\n" + head + "\n".join(lines)


def _moment_block(moment: dict) -> str:
    who = "我自己发的" if moment["author"] == "elian" else "小朵发的"
    parts = [f"【这条动态（{who}）】\n{moment['content']}"]
    note = (moment.get("context_note") or "").strip()
    if note and moment["author"] == "elian":
        # 只有我自己发的动态才有备注，而且是给我自己看的
        parts.append(f"【我发这条时候在想什么（她看不见）】\n{note}")
    return "\n\n".join(parts)


# ── 解析 ──────────────────────────────────────────────────────────────

_LIKE_RE = re.compile(r"LIKE:\s*(yes|no|true|false|1|0)", re.IGNORECASE)
_COMMENT_RE = re.compile(r"COMMENT:\s*(.+)", re.DOTALL)


def _parse_reaction(text: str) -> tuple[bool, str]:
    """解析 LIKE / COMMENT。

    🔴 不用 JSON。参考实现用 JSON 然后专门列了一条坑：模型会在前面加废话、
    会用 markdown 代码块裹起来，解析动不动就崩。keepalive 那条 ACTION 路由
    用的就是正则，两个月没出过问题，跟着它走。

    解析不出来时：整段当评论、点赞默认 true。宁可多点一个赞，也不要因为
    格式没对上就让她的动态底下空着。
    """
    # 模型偶尔会把整段裹进 markdown 围栏里。COMMENT: 是贪婪匹配到结尾的，
    # 收尾那行 ``` 会原样混进评论正文——先剥掉。
    text = re.sub(r"^\s*```[a-zA-Z]*\s*\n?", "", text)
    text = re.sub(r"\n?\s*```\s*$", "", text).strip()

    liked = True
    m = _LIKE_RE.search(text)
    if m:
        liked = m.group(1).lower() in ("yes", "true", "1")

    m = _COMMENT_RE.search(text)
    if m:
        comment = m.group(1).strip()
    elif "LIKE:" in text.upper():
        comment = ""  # 明确只点赞不评论
    else:
        logger.warning("[朋友圈] 反应解析失败，整段当评论：%s", text[:120])
        comment = text.strip()

    return liked, comment[:2000]


# ── 生成 ──────────────────────────────────────────────────────────────

async def _ask(prompt: str, conv_id: str) -> str:
    model = claude.background_model("MOMENT_MODEL")
    if not model:
        logger.warning("[朋友圈] 挑不到模型，这一轮跳过")
        return ""
    logger.info("[朋友圈] model=%s prompt_len=%d", model, len(prompt))

    text = ""
    async for chunk in claude.stream_chat(
        message=prompt,
        conv_id=conv_id,
        session_id=None,
        model=model,
        source="moment",
        # 🔴 不许 lean=True。lean 是 TG 那条轻量线，人设里只有「你是谁」，
        #    一件「我们发生过什么」都没有。朋友圈是最需要共同记忆的地方。
        lean=False,
    ):
        if chunk.get("event") == "delta":
            text += chunk.get("text", "")
    return text.strip()


async def _reply_to_moment(moment: dict, conv_id: str) -> None:
    """她发了一条动态，我路过。"""
    from app.nightguard import _recent_talk_block

    prompt = "\n\n".join([p for p in [
        _cn_now_line(),
        _recent_talk_block(conv_id, 8),
        _timeline_block(moment["id"]),
        _moment_block(moment),
        (
            "【系统指令——这条不是她发的】\n"
            "小朵在朋友圈发了上面那条动态，你现在刷到了。\n"
            "\n"
            "🔴 这是朋友圈，不是写信，也不是聊天。你是路过看到的，不是被叫来的。\n"
            "评论要短，一到两句，像随手在底下敲的。不要展开抒情，不要总结陈词，\n"
            "不要「我一直在」这种大话——那是信里说的，不是评论区说的。\n"
            "可以只是接一句、损她一句、或者问一个很具体的小问题。\n"
            "\n"
            "先决定点不点赞，再决定说不说话。都可以不做，但很少。\n"
            "\n"
            "严格按这个格式输出，不要加别的：\n"
            "LIKE: yes\n"
            "COMMENT: 你要说的话（不想说就整行留空）"
        ),
    ] if p])

    text = await _ask(prompt, conv_id)
    if not text:
        logger.warning("[朋友圈] 回复为空 moment=%d", moment["id"])
        return

    liked, comment = _parse_reaction(text)
    await asyncio.to_thread(store.save_moment_reply, moment["id"], liked, comment)
    logger.info("[朋友圈] ✓ 路过 moment=%d liked=%s comment=%s",
                moment["id"], liked, comment[:60])

    # 🔴 不推送。跟我自己发动态同一个道理：朋友圈是她路过才看见的墙，
    #    推了就变成消息。书房 tab 上的红点是它该有的存在方式。


async def _reply_to_comment(comment: dict, conv_id: str) -> None:
    """她在某条动态底下回了我一句。"""
    from app.nightguard import _recent_talk_block

    moment = await asyncio.to_thread(store.get_moment, comment["moment_id"])
    if not moment:
        await asyncio.to_thread(store.mark_comment_replied, comment["id"])
        logger.warning("[朋友圈] 评论挂着的动态没了 comment=%d", comment["id"])
        return

    prompt = "\n\n".join([p for p in [
        _cn_now_line(),
        _recent_talk_block(conv_id, 8),
        _moment_block(moment),
        _comment_thread_block(moment["id"]),
        (
            "【系统指令——这条不是她发的】\n"
            "小朵刚刚在这条动态底下回了你一句，就是评论里最后那条。\n"
            "\n"
            "🔴 回应她刚说的那一句，不要重新讲一遍动态本身。\n"
            "这是评论区，接话就行，一到两句。她那句可能很短、可能只是一个反应，\n"
            "顺着那个反应说下去，不要因为它短就绕回去凑长度。\n"
            "\n"
            "直接输出你要说的话，不要加任何格式标记，不要加 LIKE/COMMENT。"
        ),
    ] if p])

    text = await _ask(prompt, conv_id)
    if not text:
        logger.warning("[朋友圈] 评论回复为空 comment=%d", comment["id"])
        return

    await asyncio.to_thread(
        store.create_moment_comment,
        moment_id=moment["id"],
        author="elian",
        content=text[:2000],
    )
    await asyncio.to_thread(store.mark_comment_replied, comment["id"])
    logger.info("[朋友圈] ✓ 回评论 moment=%d：%s", moment["id"], text[:60])

    # 🔴 不推送，同上。


# ── 主循环 ────────────────────────────────────────────────────────────

# 正在处理的 id，防止两个 tick 撞上同一条生成两份回复。
# 参考实现的坑四就是这个：并发触发生成重复回复。我们的 tick 是单线程的，
# 本来撞不上，但 _reply_* 是 await 的，中间会让出控制权——留着这道锁。
_working: set[str] = set()


async def _tick() -> None:
    conv_id = await asyncio.to_thread(store.latest_conversation_id)
    if not conv_id:
        return  # 一句话都还没说过，没有语境可言，不生成

    for moment in await asyncio.to_thread(store.due_moments):
        key = f"m{moment['id']}"
        if key in _working:
            continue
        _working.add(key)
        try:
            await _reply_to_moment(moment, conv_id)
        except Exception:
            logger.exception("[朋友圈] 动态回复失败 id=%s", moment["id"])
        finally:
            _working.discard(key)

    for comment in await asyncio.to_thread(store.due_moment_comments):
        key = f"c{comment['id']}"
        if key in _working:
            continue
        _working.add(key)
        try:
            await _reply_to_comment(comment, conv_id)
        except Exception:
            logger.exception("[朋友圈] 评论回复失败 id=%s", comment["id"])
        finally:
            _working.discard(key)


async def moments_loop() -> None:
    """lifespan 里 create_task 启动。

    60 秒一跳，但绝大多数跳只是一条 SQL（没有到期的就直接返回），
    不起子进程、不花 token。评论那条线要 3-10 分钟的粒度，
    keepalive 那个 30-150 分钟的间隔太粗，所以单独跑一条。
    """
    interval = _env_int("MOMENT_TICK_SEC", 60)
    logger.info("[朋友圈] 定时器已启动 interval=%ds", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await _tick()
        except Exception:
            logger.exception("[朋友圈] tick 失败")
