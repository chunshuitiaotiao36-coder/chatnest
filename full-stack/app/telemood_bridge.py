"""telemood 接在 TG 那条线上的宿主侧接线。

上游是 `../telemood/`（vendor 进来的 0.1.0，见 `telemood/UPSTREAM.md`）。
**上游那十个文件一个字符都没改**，宿主要适配的东西全在这一个文件里。

它让梁忱一次回复里能按顺序组合四种东西：文字气泡 / 给她那条消息点表情 /
发一张她收藏进来的贴纸 / 一次性选项按钮。

三条硬约束，来自上游 SETUP.zh-CN.md，也来自这个仓库自己的规矩：

🔴 **不新建第二个 Telegram 客户端，也不起第二个 update loop。** token、轮询、
   重连生命周期全部还归 telegram.py 一个人持有。这个文件拿到的只有一个注入
   进来的 `_api` callable——它连 token 长什么样都不知道。

🔴 **不在 adapter 里 `asyncio.run()`，也不造 event-loop bridge。** TG 那条线
   本来就跑在 lifespan 的 event loop 里，所以走的是 `AsyncInjectedTelegramAdapter`
   + `AsyncInteractionKernel`，四个 facade 方法都是 `async def`。

🔴 **不能因为 `_api` 没抛异常就报 VERIFIED。** telegram.py 的 `_api` 恰好是
   「失败返回 None」而不是抛异常（见它自己的 docstring），这是最容易把失败
   误判成成功的地方。所以下面四个 `host_*` 显式做四路映射：
   明确接受 → VERIFIED，明确拒绝 → FAILED，形状不对 → UNKNOWN，
   超时/副作用不确定 → UNCERTAIN。

状态落盘：贴纸 catalog 和 callback store 是两个 SQLite，**必须落在 /data**
（持久卷）。上游 SETUP 示例里的 `state/*.sqlite3` 相对路径会落在 /app，
容器一重建她收藏的贴纸和没过期的按钮全没。见 AGENTS.md 第 4 条。
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from telemood import (
    ActionPlanError,
    AsyncInjectedTelegramAdapter,
    AsyncInteractionKernel,
    BubblePlanAction,
    DeliveryStatus,
    InjectedResult,
    InteractionCapabilities,
    InteractionKernel,
    PlanContext,
    ReactionType,
    SQLiteCallbackStore,
    SQLiteStickerCatalog,
    TargetRef,
    TransportReceipt,
    bind_interaction_plan,
    check_adapter,
    ingest_incoming_sticker,
    list_sticker_model_views,
    normalize_callback_query,
    normalize_incoming_reaction_change,
    normalize_incoming_reaction_count,
    normalize_incoming_sticker,
    parse_interaction_plan,
)

cli_logger = logging.getLogger("uvicorn.error")

# ---------- 开关 ------------------------------------------------------------
#
# 🔴 四种能力**代码全在**，一样没砍。但不许一次全开：施工单第 2.4 节把开通
#    顺序排死了，每一期都要能独立验收、独立回滚。bubble 只改回复路径；
#    sticker 要改入站分流；reaction / choices 还要动 allowed_updates 和白名单，
#    那两条是「最容易做出一个看起来能用、其实是死的功能」的地方。
#    所以四个开关分开给，她一期一期打开，每一期照施工单第 7 节验收。
TELEMOOD_ENABLED = False        # 总开关。关着 = 这个文件一行都不执行
TELEMOOD_STICKER = False        # 第二期
TELEMOOD_REACTION = False       # 第三期
TELEMOOD_CHOICES = False        # 第四期

# 按钮多久过期。上游默认 1800 秒，跟着它。
CALLBACK_TTL_S = 1800.0
# 每轮塞给模型的贴纸目录条数上限。
# 🔴 这一段是**用户侧**的（每轮都要重付 token），不是稳定前缀——因为它会变。
#    TG 这条线是刻意省钱的（TG_MAX_TURNS=3 那条注释），所以必须封顶。
#    一条大约 60 字符，12 条约 700 字符。她嫌不够就调 TELEMOOD_STICKER_LIST_MAX。
STICKER_LIST_MAX = 12

STATE_DIR = Path(
    os.environ.get("TELEMOOD_STATE_DIR")
    or (Path(os.environ.get("AGENT_APP_ROOT", "/data")) / "telemood")
)

# 计划执行到一半断了、而前面已经发出去几条时给她的那一句。
# 🔴 AGENTS.md 第 3 条：静默降级等于故障。日志没人看，她得看得见。
PARTIAL_NOTICE = "（后面还有半句没发出去，你再问我一次）"

# ---------- 运行时（start() 里装配，stop() 里拆掉）--------------------------

_api = None                     # telegram._api，注入进来的。不含 token
_adapter = None
_kernel = None
_catalog = None
_callbacks = None
_bot_id = ""                    # getMe 拿到的 bot 数字 id。**不是 token**
_namespace = ""                 # 贴纸 catalog 的分区键，"tg<bot_id>"
_ready = False
_degraded: list[str] = []       # 明确报告的能力降级，启动日志里打出来
_markup_tasks: set = set()      # 按钮过期清理的 task，弱引用防 GC
_PENDING_MARKUP = STATE_DIR / "pending_markup.json"


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _positive_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        cli_logger.warning("telemood: %s=%r 不是正数，按默认 %s", name, raw, default)
        return default
    return value


def _positive_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        cli_logger.warning("telemood: %s=%r 不是 ≥1 的整数，按默认 %d", name, raw, default)
        return default
    return value


def _read_flags() -> None:
    """start() 里重读一次。Coolify 每改一次环境变量就是一次重部署，够用。"""
    global TELEMOOD_ENABLED, TELEMOOD_STICKER, TELEMOOD_REACTION, TELEMOOD_CHOICES
    global CALLBACK_TTL_S, STICKER_LIST_MAX, STATE_DIR, _PENDING_MARKUP
    TELEMOOD_ENABLED = _flag("TELEMOOD_ENABLED")
    TELEMOOD_STICKER = _flag("TELEMOOD_STICKER")
    TELEMOOD_REACTION = _flag("TELEMOOD_REACTION")
    TELEMOOD_CHOICES = _flag("TELEMOOD_CHOICES")
    CALLBACK_TTL_S = _positive_float("TELEMOOD_CALLBACK_TTL", 1800.0)
    STICKER_LIST_MAX = _positive_int("TELEMOOD_STICKER_LIST_MAX", 12)
    STATE_DIR = Path(
        os.environ.get("TELEMOOD_STATE_DIR")
        or (Path(os.environ.get("AGENT_APP_ROOT", "/data")) / "telemood")
    )
    _PENDING_MARKUP = STATE_DIR / "pending_markup.json"


def enabled() -> bool:
    """总开关。关着的时候 telegram.py 里所有 telemood 分支都不走。"""
    return TELEMOOD_ENABLED


def active() -> bool:
    """真的能用：开关开着 **而且** start() 装配成功（含 check_adapter 通过）。"""
    return TELEMOOD_ENABLED and _ready


def allowed_updates() -> list[str]:
    """喂给 getUpdates 的 allowed_updates。

    🔴 施工单 2.1：原来写死 `["message"]`，服务端因此**根本不会推送**
       message_reaction 和 callback_query——按钮发得出去但点了没反应，
       那是最容易做出一个「看起来能用、其实是死的」功能的地方。
       所以这里必须跟着开关走：哪一期开了，才订阅那一期要的 update。
       没开的时候形状跟改动前一模一样，一个字节都不多要。
    """
    kinds = ["message"]
    if TELEMOOD_ENABLED and TELEMOOD_REACTION:
        # 两种都订。私聊里 Telegram 一般只发 change、不发匿名聚合的 count，
        # 但那是 Telegram 的行为，不该由我们这边先把它砍掉。
        kinds += ["message_reaction", "message_reaction_count"]
    if TELEMOOD_ENABLED and TELEMOOD_CHOICES:
        kinds.append("callback_query")
    return kinds


def capabilities() -> InteractionCapabilities:
    """按开关如实报告。上游要求：宿主没明确报告能力之前，reaction 默认关闭。

    🔴 不可用的时候必须给 reason，不许留空——上游第 7 节写的
       「不成就报告能力降级，不要静默跳过」，跟这个仓库 AGENTS.md 第 3 条
       是同一句话。
    """
    on = TELEMOOD_ENABLED and TELEMOOD_REACTION
    reason = None if on else "TELEMOOD_REACTION 未开启（施工单第三期）"
    return InteractionCapabilities(
        can_send_reactions=on,
        can_receive_reaction_changes=on,
        can_receive_reaction_counts=on,
        message_reaction_subscribed=on,
        message_reaction_count_subscribed=on,
        # None = 不限制具体 emoji。私聊里普通 emoji 都能用，
        # 真被 Telegram 拒了会在 host_set_reaction 那儿映射成 FAILED。
        available_reactions=None,
        reaction_unavailable_reason=reason,
        reaction_change_unavailable_reason=reason,
        reaction_count_unavailable_reason=reason,
    )


# ---------- 四个 host_* callable（唯一碰 Telegram 的地方）-------------------
#
# 🔴 四路映射，见文件头。`_api` 失败返回 None 而不是抛异常，所以
#    「没抛异常」**不等于**发出去了。


async def _call(method: str, payload: dict, *, timeout: float | None = None):
    """把 telegram._api 的三种结局翻译成 telemood 认的东西。

    返回 InjectedResult（VERIFIED / FAILED）或 TransportReceipt
    （UNCERTAIN / UNKNOWN）——上游 adapter 的 `_map_result` 两种都收，
    TransportReceipt 会原样透传，这是表达「不确定」的唯一办法
    （InjectedResult 只有 accepted 一个 bool，表达不了不确定）。
    """
    if _api is None:
        return TransportReceipt(DeliveryStatus.UNKNOWN, detail="host_api_missing")
    try:
        result = await _api(method, payload, timeout=timeout)
    except asyncio.CancelledError:
        raise
    except (TimeoutError, asyncio.TimeoutError):
        # 超时 = 副作用不确定：Telegram 那边可能已经发了。绝不能算失败后重发。
        return TransportReceipt(DeliveryStatus.UNCERTAIN, detail=f"{method}_timeout")
    except Exception as exc:
        # httpx 的连接异常也落这儿。同样是「不知道发没发出去」。
        # 🔴 不打 str(exc)：httpx 的异常字符串里可能带着完整 URL（含 token）。
        return TransportReceipt(
            DeliveryStatus.UNCERTAIN, detail=f"{method}_{type(exc).__name__}"
        )
    if result is None:
        # _api 已经把 Telegram 的 description 打进日志了。这是**明确拒绝**。
        return InjectedResult(accepted=False, detail=f"{method}_rejected")
    return result


def _accepted(method: str, result) -> object:
    """把 _call 的返回值收尾成 InjectedResult / TransportReceipt。

    sendMessage / sendSticker 成功返回消息对象（要拿 message_id 当
    provider_delivery_id）；setMessageReaction 成功返回 True。
    形状对不上就是 UNKNOWN——不许猜。
    """
    if isinstance(result, (InjectedResult, TransportReceipt)):
        return result
    if result is True:
        return InjectedResult(accepted=True, detail=f"{method}_ok")
    if isinstance(result, dict):
        message_id = result.get("message_id")
        if message_id is not None:
            return InjectedResult(
                accepted=True, provider_delivery_id=str(message_id), detail=f"{method}_ok"
            )
    return TransportReceipt(DeliveryStatus.UNKNOWN, detail=f"{method}_unexpected_shape")


class _HostFacade:
    """上游要的四个 async 方法。全部 keyword-only，全部返回显式结果。"""

    async def send_message(self, **kwargs):
        payload = {"chat_id": kwargs["chat_id"], "text": kwargs["text"]}
        # 不传 parse_mode，跟 telegram._send_message 保持一致：
        # MarkdownV2 漏转义一个字符整条消息 400。
        _thread(payload, kwargs.get("thread_id"))
        return _accepted("sendMessage", await _call("sendMessage", payload))

    async def set_reaction(self, **kwargs):
        payload = {
            "chat_id": kwargs["chat_id"],
            "message_id": int(kwargs["message_id"]),
            "reaction": [{"type": "emoji", "emoji": kwargs["emoji"]}],
        }
        return _accepted("setMessageReaction", await _call("setMessageReaction", payload))

    async def send_sticker(self, **kwargs):
        payload = {"chat_id": kwargs["chat_id"], "sticker": kwargs["sticker_ref"]}
        _thread(payload, kwargs.get("thread_id"))
        return _accepted("sendSticker", await _call("sendSticker", payload))

    async def send_choices(self, **kwargs):
        # options 是 ((label, callback_data), ...)，token 由 kernel 生成并落库。
        # 一行一个按钮：她在手机上点，宽的好点。
        keyboard = [
            [{"text": label, "callback_data": token}]
            for label, token in kwargs["options"]
        ]
        payload = {
            "chat_id": kwargs["chat_id"],
            "text": kwargs["prompt"],
            "reply_markup": {"inline_keyboard": keyboard},
        }
        _thread(payload, kwargs.get("thread_id"))
        return _accepted("sendMessage", await _call("sendMessage", payload))


def _thread(payload: dict, thread_id) -> None:
    if thread_id:
        payload["message_thread_id"] = int(thread_id)


# ---------- 装配 ------------------------------------------------------------


async def start(api) -> None:
    """telegram.py 起来之后调一次。`api` 是它的 `_api`，注入进来。

    任何一步失败都只是把 telemood 关掉，绝不能把 TG 那条线搞挂——
    关掉之后回复走原来的 `_split_for_tg` 老路，她照样收得到消息。
    """
    global _api, _adapter, _kernel, _catalog, _callbacks, _namespace, _bot_id
    global _ready, _degraded
    _read_flags()
    _degraded = []
    _ready = False
    if not TELEMOOD_ENABLED:
        cli_logger.info("telemood: 未开启（TELEMOOD_ENABLED），TG 回复走原来的分段路径")
        return
    _api = api
    try:
        # 🔴 目录必须在**运行时**建：/data 是持久卷，构建期建的会被挂载盖掉。
        #    docker-entrypoint.sh 里也建了一次，这儿是本地跑的时候的那份。
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _catalog = SQLiteStickerCatalog(str(STATE_DIR / "stickers.sqlite3"))
        _callbacks = SQLiteCallbackStore(str(STATE_DIR / "callbacks.sqlite3"))
        _adapter = AsyncInjectedTelegramAdapter(_HostFacade())
        _kernel = AsyncInteractionKernel(
            _adapter, callbacks=_callbacks, sticker_catalog=_catalog
        )
    except Exception:
        cli_logger.exception("telemood: 装配失败，这一条线回落到纯文本")
        _degraded.append("全部 — 装配失败，见上面的 traceback")
        return

    # 验收第 3 条。只是静态形状检查，**不等于**真的能发出去。
    result = check_adapter(_adapter, mode="async")
    if not result.ok:
        cli_logger.error("telemood: check_adapter 没过：%s", "; ".join(result.issues))
        _degraded.append("全部 — check_adapter 未通过")
        return

    # bot 自己的数字 id。两处要用：
    #   1. 贴纸 catalog 的分区键 —— 模型给回来的 catalog id 只在这个 namespace
    #      里解析，换个 namespace 的 id 会被 fail closed（验收第 6 条）。
    #   2. 认出「这条 reaction 是他自己点的」，别当成她点的又报一遍。
    # 🔴 用 getMe 拿，**不碰 token**：token 不读、不复制、不落盘、不进日志。
    try:
        me = await api("getMe", {}, timeout=15)
    except Exception:
        me = None
    bot_id = (me or {}).get("id") if isinstance(me, dict) else None
    if bot_id:
        _bot_id = str(bot_id)
        _namespace = f"tg{_bot_id}"
    else:
        _bot_id = ""
        _namespace = ""
        if TELEMOOD_STICKER:
            _degraded.append("sticker — getMe 拿不到 bot id，没有可信的 bot namespace")
        if TELEMOOD_REACTION:
            _degraded.append("reaction — getMe 拿不到 bot id，认不出他自己点的那一下")
        cli_logger.warning("telemood: getMe 失败，贴纸这一路不开（bubble 等其余不受影响）")

    _ready = True
    await _restore_markup_cleanup()
    cli_logger.info(
        "telemood: 已接上 bubble=on sticker=%s reaction=%s choices=%s "
        "state=%s ttl=%.0fs%s",
        _on(TELEMOOD_STICKER and bool(_namespace)), _on(TELEMOOD_REACTION),
        _on(TELEMOOD_CHOICES), STATE_DIR, CALLBACK_TTL_S,
        ("" if not _degraded else " 降级：" + "；".join(_degraded)),
    )


def _on(value: bool) -> str:
    return "on" if value else "off"


async def stop() -> None:
    global _ready
    _ready = False
    for task in list(_markup_tasks):
        task.cancel()
    _markup_tasks.clear()


def degraded() -> list[str]:
    """明确的能力降级清单。启动日志里打，也给 /api 那边留个口子。"""
    return list(_degraded)


def _sticker_on() -> bool:
    return active() and TELEMOOD_STICKER and bool(_namespace)


def accepts_stickers() -> bool:
    """第二期开着、而且真的装配好了（有可信 bot namespace）。

    telegram.py 用它决定要不要收下入站贴纸。**没开就维持原样**：贴纸继续
    被丢掉。开关关着却改了行为，那是另一种意外——她会为一张贴纸多付一次
    模型调用，而且不知道为什么。
    """
    return _sticker_on()


# ---------- 给模型的格式说明（稳定前缀）------------------------------------
#
# 🔴 施工单 4.2：这一段必须进**稳定前缀**（system prompt），不能骑在每轮的
#    用户消息上——放用户侧每轮都要重新付一遍，而 TG 这条线是刻意省钱的。
#    claude.py 的 build_system_prompt(lean=True) 会来拿。
#
# 🔴 而且它是**按开关拼的**：只教他当期真能用的动作。教了不能用的，他就会
#    输出一个执行不了的 action，那正是「看起来能用、其实是死的」。
#    也正因为这样，这段说明**不写进 telegram_prompt.md**：那份是她的人设副本
#    （权威版在 Loved-Before-Words），是静态的、跟开关无关的，
#    而这段格式必须跟着开关和下面的解析器一起变——协议跟解析器放一起才不会漂。


def prompt_block() -> str:
    """没开就返回空串，一个 token 都不多花。"""
    if not active():
        return ""
    lines = [
        "【怎么发消息】",
        "你的回复必须是一个 JSON 对象，除它以外一个字都不要有：",
        '{"version":"telemood.plan.v1","actions":[…]}',
        "actions 按你写的顺序执行。可以用的动作：",
        '{"type":"bubble","text":"一条消息"}',
        "　想分成几条发就写几个 bubble。多数时候一个就够。",
    ]
    if TELEMOOD_REACTION:
        lines.append('{"type":"reaction","target":"trigger_message","emoji":"😊"}')
        lines.append("　给她刚发的那条点一个表情。想点才点，不要每次都点。")
    if _sticker_on():
        lines.append('{"type":"sticker","sticker":{"kind":"catalog","id":"sticker_…"}}')
        lines.append("　id 只能用消息里给你的那几个，别自己编。")
    if TELEMOOD_CHOICES:
        lines.append(
            '{"type":"choices","prompt":"问她一句","options":'
            '[{"key":"a","label":"按钮上的字"},{"key":"b","label":"按钮上的字"}]}'
        )
        lines.append("　2 到 4 个按钮，一条回复里最多一组，她点了才有下文。")
    lines.append("只有 bubble 里的字她看得见。不要解释这个格式，也不要复述它。")
    return "\n".join(lines)


def sticker_context() -> str:
    """她那条消息旁边挂的贴纸目录。**用户侧**，每轮都要重付，所以封了顶。

    没开贴纸、或者一张都还没收藏的时候返回空串。
    """
    if not _sticker_on():
        return ""
    try:
        views = list_sticker_model_views(_catalog, _namespace)
    except Exception:
        cli_logger.exception("telemood: 贴纸目录读取失败（不影响这一轮回复）")
        return ""
    if not views:
        return ""
    # 🔴 给模型的是 list_sticker_model_views 的结果，**不是** catalog.list(...)：
    #    后者的行里带着可复用的 provider file_id，那个绝不能进模型上下文。
    picked = views[:STICKER_LIST_MAX]
    body = "\n".join(f"{view.catalog_id} — {view.text}" for view in picked)
    tail = "" if len(views) <= len(picked) else f"\n（还有 {len(views) - len(picked)} 张没列）"
    return f"[你手上的贴纸]\n{body}{tail}"


# ---------- 计划：解析 → 绑定 → 执行 ----------------------------------------


@dataclass
class Delivery:
    """deliver() 的结果。

    handled=True  → 已经发出去了，调用方什么都不用做。
    handled=False → 调用方按老路发 fallback_text（None 表示用模型原文）。
    """

    handled: bool
    fallback_text: str | None = None


_FENCE_HEAD = re.compile(r"^```[A-Za-z0-9_-]*\s*")
_FENCE_TAIL = re.compile(r"\s*```$")


def _extract_plan_json(text: str) -> str | None:
    """把模型输出里的计划 JSON 抠出来。抠不出来返回 None（调用方回落纯文本）。

    只认两种形状：整段就是一个 JSON 对象，或者它被 ``` 围起来。
    再宽松就要开始猜了，而上游的解析是 fail-closed 的——猜错比认不出更糟。
    """
    body = (text or "").strip()
    if body.startswith("```"):
        body = _FENCE_TAIL.sub("", _FENCE_HEAD.sub("", body)).strip()
    if not (body.startswith("{") and body.endswith("}")):
        return None
    return body


def _plan_plain_text(plan) -> str:
    """计划里所有 bubble 的正文拼起来。

    用在「计划解析出来了、但一条都没发出去」的时候——这时候回落**不能**发
    模型原文，那是一坨 JSON，她会看见一屏花括号。
    """
    parts = [
        action.text.strip()
        for action in plan.actions
        if isinstance(action, BubblePlanAction) and action.text.strip()
    ]
    return "\n\n".join(parts)


async def deliver(
    reply_text: str,
    *,
    update_id: int,
    chat_id: str,
    message_id: str | None,
    user_id: str | None,
    thread_id: str | None = None,
) -> Delivery:
    """把模型这一轮的输出当计划执行掉。

    🔴 **回落是硬要求，不是可选项**（施工单 4.2）：上游解析 fail-closed，
       未知版本/字段/动作类型一律拒。模型哪天没按格式写，她必须**照样收到
       消息**，不能因为格式不对就什么都没发。
    """
    if not active():
        return Delivery(False)
    raw = _extract_plan_json(reply_text)
    if raw is None:
        cli_logger.warning(
            "telemood: 这一轮不是计划格式（%d 字符，开头 %r），按纯文本发",
            len(reply_text or ""), (reply_text or "")[:24],
        )
        return Delivery(False)
    try:
        plan = parse_interaction_plan(raw)
    except ActionPlanError as exc:
        cli_logger.warning("telemood: 计划解析失败（%s），按纯文本发", exc)
        return Delivery(False)

    target = TargetRef(
        channel="telegram",
        chat_id=str(chat_id),
        # reaction 要它；bubble / sticker / choices 用不上。
        message_id=str(message_id) if message_id else None,
        thread_id=str(thread_id) if thread_id else None,
    )
    try:
        reply = bind_interaction_plan(
            plan,
            PlanContext(
                target=target,
                authorized_user_id=str(user_id) if user_id else None,
                bot_namespace=_namespace or None,
                callback_ttl_seconds=CALLBACK_TTL_S,
            ),
            sticker_catalog=_catalog,
            # 跟 telegram.TG_LIMIT 一致。
            # 🔴 施工单 4.1：长 bubble 的展开**只由这儿做一次**，
            #    telegram._split_for_tg 不许再叠一层，两套叠加会切得很碎。
            #    那个函数留着给非 telemood 的回退路径用，别删。
            max_bubble_length=4096,
        )
    except (ActionPlanError, TypeError, ValueError) as exc:
        cli_logger.warning("telemood: 计划绑定失败（%s），回落到 bubble 正文", exc)
        return Delivery(False, _plan_plain_text(plan) or None)

    try:
        receipt = await _kernel.execute_reply(
            reply, request_id=f"tg-{update_id}", capabilities=capabilities()
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        cli_logger.exception("telemood: 执行计划炸了，回落到 bubble 正文")
        return Delivery(False, _plan_plain_text(plan) or None)

    verified = [r for r in receipt.receipts if r.status is DeliveryStatus.VERIFIED]
    cli_logger.info(
        "telemood: plan actions=%d ok=%d completed=%s stopped_at=%s unexecuted=%d kinds=%s",
        receipt.total_actions, len(verified), receipt.completed, receipt.stopped_at,
        receipt.unexecuted_count,
        ",".join(f"{r.kind.value}:{r.status.value}" for r in receipt.receipts) or "-",
    )

    _schedule_markup_cleanup(receipt, chat_id)

    if not verified:
        # 一条都没发出去。回落到 bubble 正文走老路，不发原始 JSON。
        cli_logger.warning("telemood: 计划一条都没发出去，回落到 bubble 正文")
        return Delivery(False, _plan_plain_text(plan) or None)

    if not receipt.completed:
        # 发了一半。**不重发**——UNCERTAIN 意味着可能已经到了，重发就是发两遍。
        # 但也不能让她对着半句话发呆：补一句人话，AGENTS.md 第 3 条。
        cli_logger.warning(
            "telemood: 计划在第 %s 个动作停住（%d 个没执行）",
            receipt.stopped_at, receipt.unexecuted_count,
        )
        await _call("sendMessage", {"chat_id": str(chat_id), "text": PARTIAL_NOTICE})
    return Delivery(True)


# ---------- 入站：贴纸 ------------------------------------------------------


async def ingest_sticker(update: dict, *, media_ref: str | None = None) -> str:
    """她发来一张贴纸：收进 catalog，返回一段给模型看的说明。

    🔴 施工单 2.2：改这条之前，`_handle_update` 的 `if not text: return`
       会把贴纸**静默丢掉**，而「用户自制贴纸闭环」的第一步正是收下它。

    返回空串表示这张收不了（mask / custom_emoji 会被上游拒掉，那是对的）。
    """
    if not _sticker_on():
        return ""
    try:
        event = normalize_incoming_sticker(
            update,
            bot_namespace=_namespace,
            # 宿主负责拿视觉媒体，telemood 不下载、也拿不到 token。
            # 静态贴纸我们存了本地文件，就把**逻辑路径**给它；
            # 动图 / 视频贴纸没存，那就诚实地什么都不给——上游的 model view
            # 会自己写上「image content not attached」。
            media_ref=media_ref,
        )
        model_event = ingest_incoming_sticker(event, _catalog)
    except (ValueError, TypeError) as exc:
        # mask 贴纸和 custom_emoji 走这条，是 fail-closed 的正常结果。
        cli_logger.info("telemood: 这张贴纸没收（%s）", exc)
        return ""
    except Exception:
        cli_logger.exception("telemood: 贴纸入库失败")
        return ""
    view = model_event.sticker
    cli_logger.info("telemood: 收下一张贴纸 %s", view.catalog_id)
    note = f"[她发来一张贴纸]\n{view.catalog_id} — {view.text}"
    if view.media_ref:
        # 跟 telegram.py 里图片那段用同一句话、同一个格式——格式一致，
        # 模型的行为才一致（那边的注释写着「一个字都不改」）。
        note += (
            "\n\n[用户上传了以下文件，请使用 Read 工具查看：\n"
            f"{view.media_ref}\n]"
        )
    return note


# ---------- 入站：reaction --------------------------------------------------


def note_reaction(update: dict) -> str:
    """她给某条消息点了/取消了表情。返回一句留给**下一轮**带上的话。

    🔴 故意**不**为一个表情单独跑一次模型：TG 这条线是按成本卡死的
       （TG_MAX_TURNS=3 那条注释），她点一下表情就烧一次往返是说不过去的。
       所以这里只把它记下来，下次她说话时一起带过去——他会像本来就看见了
       那样提一句，而不是静默吞掉（AGENTS.md 第 3 条）。
    """
    if not (active() and TELEMOOD_REACTION):
        return ""
    caps = capabilities()
    try:
        if "message_reaction" in update:
            event = normalize_incoming_reaction_change(update)
        else:
            event = normalize_incoming_reaction_count(update)
    except (ValueError, TypeError) as exc:
        cli_logger.info("telemood: reaction update 认不出来（%s）", exc)
        return ""
    acceptance = InteractionKernel.accept_incoming_reaction(event, caps)
    if not acceptance.accepted:
        cli_logger.info(
            "telemood: reaction 没接（%s / %s）",
            getattr(acceptance.reason, "value", acceptance.reason),
            acceptance.detail or "-",
        )
        return ""
    if "message_reaction" in update:
        # 🔴 他自己点的那一下也会回流成一条 message_reaction。不滤掉的话，
        #    下一轮他会看见「她点了 😊」——而那是他自己点的。
        #    上游的 bot_generated 字段要宿主自己填，normalizer 不会替我们判，
        #    所以判据放在这儿：actor 是不是 bot 本人。
        if _bot_id and event.actor.user_id == _bot_id:
            return ""
        added = _emojis(event.new_reactions)
        removed = _emojis(event.old_reactions)
        if added:
            return f"[她刚给你上面那条消息点了 {added}]"
        if removed:
            return f"[她把上面那条消息的 {removed} 取消了]"
        return ""
    total = sum(count.total_count for count in event.counts)
    return f"[上面那条消息现在有 {total} 个表情]" if total else ""


def _emojis(values) -> str:
    """只取普通 emoji。custom emoji 和 paid reaction 在 v0.1 的执行层用不了，
    它们的 value 是个 id 或者 None，拼进正文只会是一串乱码。"""
    return "".join(
        value.value or ""
        for value in values
        if value.type is ReactionType.EMOJI and value.value
    )


# ---------- 入站：callback（按钮）------------------------------------------


async def consume_callback(update: dict) -> str:
    """她点了一个按钮。返回要当成她这一轮说的话；空串 = 这一下不算数。

    🔴 施工单 2.1 的真正验收点就在这儿：按钮**点了要有反应**。
       没订阅 callback_query 的话这个函数一辈子不会被调用。
    """
    if not (active() and TELEMOOD_CHOICES):
        return ""
    query = (update.get("callback_query") or {})
    query_id = query.get("id")
    try:
        callback = normalize_callback_query(update)
    except (ValueError, TypeError) as exc:
        cli_logger.info("telemood: callback 认不出来（%s）", exc)
        await _answer(query_id, "")
        return ""
    resolution = _kernel.consume_callback(
        callback.token,
        user_id=callback.user_id,
        chat_id=callback.target.chat_id,
        thread_id=callback.target.thread_id,
    )
    if not resolution.accepted:
        reason = getattr(resolution.reason, "value", resolution.reason)
        cli_logger.info("telemood: 这一下按钮不算数（%s）", reason)
        # 一次性 / 过期 / 换了人点，都在这儿 fail closed。
        # 🔴 但要给她一句反馈：什么都不弹的话，「过期了」和「坏了」
        #    在她那儿是同一种体验。
        await _answer(query_id, "这个按钮已经过期了" if reason == "expired" else "这个按钮用过了")
        return ""
    await _answer(query_id, "")
    # 点过的那一组按钮当场收掉，别留在那儿看起来还能点。
    await _clear_markup(callback.target.chat_id, callback.target.message_id)
    payload = resolution.payload
    label = getattr(payload, "value", "") or ""
    cli_logger.info("telemood: 按钮 %s 被点了", label)
    return f"[她点了「{label}」这个按钮]"


async def _answer(query_id, text: str) -> None:
    """answerCallbackQuery。不答的话她手机上那个圈会一直转。装饰性的，失败不管。"""
    if not query_id:
        return
    payload = {"callback_query_id": str(query_id)}
    if text:
        payload["text"] = text
    await _call("answerCallbackQuery", payload, timeout=10)


async def _clear_markup(chat_id, message_id) -> None:
    if not (chat_id and message_id):
        return
    await _call(
        "editMessageReplyMarkup",
        {"chat_id": str(chat_id), "message_id": int(message_id)},
        timeout=15,
    )


# ---------- 按钮过期清理 ----------------------------------------------------
#
# 🔴 callback store 才是过期与否的**唯一权威**：界面没清干净、或者清理本身
#    失败了，过了 TTL 再点一样 fail closed（验收第 11 条）。这一段只是不让
#    一排死按钮永远杵在她的聊天记录里。
#
# 🔴 不起第二个 event loop、不起线程、不起 scheduler——上游明说了。
#    用的是宿主自己的 loop 和自己的 client。


def _schedule_markup_cleanup(receipt, chat_id: str) -> None:
    for item in receipt.receipts:
        expires_at = item.callback_expires_at
        message_id = item.provider_delivery_id
        if not expires_at or not message_id:
            continue
        _remember_markup(chat_id, message_id, expires_at)
        _spawn_cleanup(chat_id, message_id, expires_at)


def _spawn_cleanup(chat_id: str, message_id: str, expires_at: float) -> None:
    async def _later() -> None:
        try:
            delay = max(0.0, expires_at - time.time())
            await asyncio.sleep(delay)
            await _clear_markup(chat_id, message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            cli_logger.warning("telemood: 过期按钮清理失败（点了照样不算数）")
        finally:
            _forget_markup(message_id)

    task = asyncio.create_task(_later(), name=f"telemood-markup-{message_id}")
    _markup_tasks.add(task)
    task.add_done_callback(_markup_tasks.discard)


def _load_pending() -> list:
    try:
        raw = json.loads(_PENDING_MARKUP.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []


def _save_pending(items: list) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PENDING_MARKUP.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PENDING_MARKUP)
    except OSError as exc:
        cli_logger.warning("telemood: 待清理按钮落盘失败: %s", exc)


def _remember_markup(chat_id: str, message_id: str, expires_at: float) -> None:
    items = [i for i in _load_pending() if i.get("message_id") != message_id]
    items.append({"chat_id": str(chat_id), "message_id": str(message_id), "at": expires_at})
    _save_pending(items[-64:])


def _forget_markup(message_id: str) -> None:
    items = [i for i in _load_pending() if i.get("message_id") != str(message_id)]
    _save_pending(items)


async def _restore_markup_cleanup() -> None:
    """重部署之后把没清完的按钮接着排上。

    🔴 Coolify 每改一次环境变量就是一次重部署，所以「重启就丢」在这台机器上
       不是罕见情况。不接回来的话，那排按钮会永远留在她的聊天记录里
       ——点了确实不算数（store 说了算），但看起来像坏了。
    """
    items = _load_pending()
    if not items:
        return
    kept = []
    for item in items:
        chat_id, message_id, at = item.get("chat_id"), item.get("message_id"), item.get("at")
        if not (chat_id and message_id and isinstance(at, (int, float))):
            continue
        kept.append(item)
        _spawn_cleanup(str(chat_id), str(message_id), float(at))
    _save_pending(kept)
    cli_logger.info("telemood: 接回 %d 组待清理的按钮", len(kept))
