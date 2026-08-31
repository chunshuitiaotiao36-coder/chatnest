"""宿主这一侧的 telemood 接线测试。

跟 `tests/` 分开：那边是上游原样搬来的 53 个合成测试（`telemood/UPSTREAM.md`），
这边测的是**我们自己写的那部分**——四路结果映射、白名单按 update 类型取
chat_id、计划解析的回退、贴纸闭环、以及「开关关着的时候形状跟接入前一模一样」。

全部离线：不连 Telegram，不碰 /data，不需要装 claude-agent-sdk。

    cd full-stack && python -m unittest test_telemood_bridge -v
"""

import ast
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import telemood_bridge as bridge  # noqa: E402
from telemood import DeliveryStatus, InjectedResult, TransportReceipt  # noqa: E402


# --------------------------------------------------------------------------
# 一个假的 telegram._api。签名跟真的一样：失败返回 None，不抛异常。
# --------------------------------------------------------------------------


class FakeAPI:
    def __init__(self, bot_id=424242):
        self.calls = []
        self.bot_id = bot_id
        self.next_message_id = 1000
        self.fail_methods = set()
        self.raise_methods = {}

    async def __call__(self, method, payload, timeout=None):
        self.calls.append((method, payload))
        if method in self.raise_methods:
            raise self.raise_methods[method]
        if method in self.fail_methods:
            return None                      # _api 的失败形状
        if method == "getMe":
            return {"id": self.bot_id, "is_bot": True}
        if method in ("sendMessage", "sendSticker"):
            self.next_message_id += 1
            return {"message_id": self.next_message_id, "chat": {"id": 7}}
        if method in ("setMessageReaction", "answerCallbackQuery", "editMessageReplyMarkup"):
            return True
        return {}

    def sent_texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage" and "text" in p]


FLAG_NAMES = (
    "TELEMOOD_ENABLED", "TELEMOOD_STICKER", "TELEMOOD_REACTION",
    "TELEMOOD_CHOICES", "TELEMOOD_STATE_DIR", "TELEMOOD_CALLBACK_TTL",
    "TELEMOOD_STICKER_LIST_MAX",
)


class BridgeCase(unittest.TestCase):
    """每个用例一个干净的临时 state 目录和一份干净的开关。"""

    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in FLAG_NAMES}
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TELEMOOD_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        asyncio.run(bridge.stop())
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        bridge._read_flags()
        self._tmp.cleanup()

    def boot(self, **flags):
        os.environ["TELEMOOD_ENABLED"] = "1"
        for name in ("TELEMOOD_STICKER", "TELEMOOD_REACTION", "TELEMOOD_CHOICES"):
            os.environ[name] = "1" if flags.get(name.split("_")[1].lower()) else "0"
        api = FakeAPI()
        asyncio.run(bridge.start(api))
        return api


# --------------------------------------------------------------------------
# 1. 关着的时候，形状必须跟接入 telemood 之前一模一样
# --------------------------------------------------------------------------


class TestOffIsUnchanged(BridgeCase):
    def test_allowed_updates_is_message_only(self):
        os.environ["TELEMOOD_ENABLED"] = "0"
        bridge._read_flags()
        self.assertEqual(bridge.allowed_updates(), ["message"])

    def test_prompt_block_costs_nothing(self):
        os.environ["TELEMOOD_ENABLED"] = "0"
        bridge._read_flags()
        self.assertEqual(bridge.prompt_block(), "")
        self.assertEqual(bridge.sticker_context(), "")

    def test_deliver_hands_the_text_back(self):
        os.environ["TELEMOOD_ENABLED"] = "0"
        bridge._read_flags()
        out = asyncio.run(
            bridge.deliver("随便一句话", update_id=1, chat_id="7", message_id="9", user_id="7")
        )
        self.assertFalse(out.handled)
        self.assertIsNone(out.fallback_text)


# --------------------------------------------------------------------------
# 2. 订阅与能力：哪一期开了才订哪一种 update
# --------------------------------------------------------------------------


class TestSubscriptions(BridgeCase):
    def test_bubble_only_does_not_widen_the_subscription(self):
        self.boot()
        self.assertEqual(bridge.allowed_updates(), ["message"])

    def test_reaction_adds_both_reaction_updates(self):
        self.boot(reaction=True)
        self.assertEqual(
            bridge.allowed_updates(),
            ["message", "message_reaction", "message_reaction_count"],
        )

    def test_choices_adds_callback_query(self):
        self.boot(choices=True)
        self.assertIn("callback_query", bridge.allowed_updates())

    def test_capabilities_never_degrade_silently(self):
        self.boot()
        caps = bridge.capabilities()
        self.assertFalse(caps.can_send_reactions)
        # 🔴 不可用必须带明确原因，不许留空——静默降级等于故障。
        self.assertTrue(caps.reaction_unavailable_reason)
        self.assertTrue(caps.reaction_change_unavailable_reason)
        self.assertTrue(caps.reaction_count_unavailable_reason)

    def test_capabilities_on_when_enabled(self):
        self.boot(reaction=True)
        caps = bridge.capabilities()
        self.assertTrue(caps.can_send_reactions)
        self.assertTrue(caps.message_reaction_subscribed)
        self.assertIsNone(caps.reaction_unavailable_reason)

    def test_prompt_only_teaches_enabled_actions(self):
        self.boot()
        block = bridge.prompt_block()
        self.assertIn("bubble", block)
        # 没开的三种一个字都不许出现：教了他就会输出执行不了的 action
        self.assertNotIn('"reaction"', block)
        self.assertNotIn('"sticker"', block)
        self.assertNotIn('"choices"', block)

    def test_prompt_grows_with_flags(self):
        self.boot(reaction=True, choices=True)
        block = bridge.prompt_block()
        self.assertIn('"reaction"', block)
        self.assertIn('"choices"', block)


# --------------------------------------------------------------------------
# 3. check_adapter（验收第 3 条）
# --------------------------------------------------------------------------


class TestConformance(BridgeCase):
    def test_async_adapter_passes_static_check(self):
        self.boot()
        from telemood import check_adapter

        result = check_adapter(bridge._adapter, mode="async")
        self.assertTrue(result.ok, result.issues)
        self.assertTrue(result.static_only)
        self.assertFalse(result.live_delivery_verified)


# --------------------------------------------------------------------------
# 4. 四路结果映射
#    🔴 `_api` 失败返回 None 而不是抛异常，「没抛异常」不等于发出去了。
# --------------------------------------------------------------------------


class TestResultMapping(BridgeCase):
    def test_message_id_is_verified(self):
        out = bridge._accepted("sendMessage", {"message_id": 12})
        self.assertIsInstance(out, InjectedResult)
        self.assertTrue(out.accepted)
        self.assertEqual(out.provider_delivery_id, "12")

    def test_true_is_verified(self):
        out = bridge._accepted("setMessageReaction", True)
        self.assertTrue(out.accepted)

    def test_unexpected_shape_is_unknown(self):
        out = bridge._accepted("sendMessage", {"nothing": "useful"})
        self.assertIsInstance(out, TransportReceipt)
        self.assertIs(out.status, DeliveryStatus.UNKNOWN)

    def test_api_none_is_failed_not_verified(self):
        self.boot()
        bridge._api.fail_methods.add("sendMessage")
        out = asyncio.run(bridge._call("sendMessage", {"chat_id": "7", "text": "hi"}))
        self.assertIsInstance(out, InjectedResult)
        self.assertFalse(out.accepted)

    def test_timeout_is_uncertain_not_failed(self):
        # 超时 = 副作用不确定：Telegram 那边可能已经发了，绝不能算失败再发一遍
        self.boot()
        bridge._api.raise_methods["sendMessage"] = TimeoutError()
        out = asyncio.run(bridge._call("sendMessage", {"chat_id": "7", "text": "hi"}))
        self.assertIs(out.status, DeliveryStatus.UNCERTAIN)

    def test_connection_error_is_uncertain(self):
        self.boot()
        bridge._api.raise_methods["sendMessage"] = OSError("connection reset")
        out = asyncio.run(bridge._call("sendMessage", {"chat_id": "7", "text": "hi"}))
        self.assertIs(out.status, DeliveryStatus.UNCERTAIN)
        # 🔴 detail 里只有异常类名，不许带 str(exc)：httpx 的异常字符串带 token
        self.assertNotIn("connection reset", out.detail or "")


# --------------------------------------------------------------------------
# 5. 计划解析与回退（施工单 4.2：回退是硬要求，不是可选项）
# --------------------------------------------------------------------------


PLAN_TWO_BUBBLES = (
    '{"version":"telemood.plan.v1","actions":['
    '{"type":"bubble","text":"第一条"},{"type":"bubble","text":"第二条"}]}'
)


class TestPlanExtraction(unittest.TestCase):
    def test_bare_json(self):
        self.assertIsNotNone(bridge._extract_plan_json(PLAN_TWO_BUBBLES))

    def test_fenced_json(self):
        fenced = "```json\n" + PLAN_TWO_BUBBLES + "\n```"
        self.assertEqual(bridge._extract_plan_json(fenced), PLAN_TWO_BUBBLES)

    def test_prose_is_not_a_plan(self):
        self.assertIsNone(bridge._extract_plan_json("宝贝我在呢，怎么了"))

    def test_prose_with_a_brace_inside_is_not_a_plan(self):
        self.assertIsNone(bridge._extract_plan_json("这样写 {\"a\":1} 就行了"))


class TestDelivery(BridgeCase):
    def test_bubbles_go_out_in_order(self):
        api = self.boot()
        out = asyncio.run(
            bridge.deliver(
                PLAN_TWO_BUBBLES, update_id=5, chat_id="7", message_id="9", user_id="7"
            )
        )
        self.assertTrue(out.handled)
        self.assertEqual(api.sent_texts(), ["第一条", "第二条"])

    def test_malformed_plan_falls_back_to_plain_text(self):
        api = self.boot()
        broken = '{"version":"telemood.plan.v1","actions":[{"type":"bubble"'
        out = asyncio.run(
            bridge.deliver(broken, update_id=5, chat_id="7", message_id="9", user_id="7")
        )
        # 宿主拿回 handled=False，自己按老路把原文发出去——她一定收得到消息
        self.assertFalse(out.handled)
        self.assertEqual(api.sent_texts(), [])

    def test_unknown_action_type_fails_closed(self):
        self.boot()
        plan = '{"version":"telemood.plan.v1","actions":[{"type":"selfie","text":"x"}]}'
        out = asyncio.run(
            bridge.deliver(plan, update_id=5, chat_id="7", message_id="9", user_id="7")
        )
        self.assertFalse(out.handled)

    def test_wrong_version_fails_closed(self):
        self.boot()
        plan = '{"version":"telemood.plan.v2","actions":[{"type":"bubble","text":"x"}]}'
        out = asyncio.run(
            bridge.deliver(plan, update_id=5, chat_id="7", message_id="9", user_id="7")
        )
        self.assertFalse(out.handled)

    def test_nothing_delivered_falls_back_to_bubble_text_not_json(self):
        # 🔴 一条都没发出去时回退发的必须是 bubble 正文，
        #    不能是模型原文——那是一坨 JSON，她会看见一屏花括号。
        api = self.boot()
        api.fail_methods.add("sendMessage")
        out = asyncio.run(
            bridge.deliver(
                PLAN_TWO_BUBBLES, update_id=5, chat_id="7", message_id="9", user_id="7"
            )
        )
        self.assertFalse(out.handled)
        self.assertEqual(out.fallback_text, "第一条\n\n第二条")

    def test_reaction_action_is_refused_when_capability_is_off(self):
        # 第三期没开的时候，就算模型硬输出一个 reaction，也不许发出去
        api = self.boot()
        plan = (
            '{"version":"telemood.plan.v1","actions":['
            '{"type":"reaction","target":"trigger_message","emoji":"\U0001f440"},'
            '{"type":"bubble","text":"在"}]}'
        )
        asyncio.run(
            bridge.deliver(plan, update_id=5, chat_id="7", message_id="9", user_id="7")
        )
        self.assertNotIn("setMessageReaction", [m for m, _ in api.calls])

    def test_reaction_action_goes_out_when_enabled(self):
        api = self.boot(reaction=True)
        plan = (
            '{"version":"telemood.plan.v1","actions":['
            '{"type":"reaction","target":"trigger_message","emoji":"\U0001f440"},'
            '{"type":"bubble","text":"在"}]}'
        )
        out = asyncio.run(
            bridge.deliver(plan, update_id=5, chat_id="7", message_id="9", user_id="7")
        )
        self.assertTrue(out.handled)
        self.assertIn("setMessageReaction", [m for m, _ in api.calls])


# --------------------------------------------------------------------------
# 6. 贴纸闭环（验收第 4/5/6 条）
# --------------------------------------------------------------------------


def sticker_update(file_unique_id="uniq-1", set_name="somepack"):
    return {
        "update_id": 11,
        "message": {
            "message_id": 3,
            "date": 1700000000,
            "chat": {"id": 7, "type": "private"},
            "from": {"id": 7, "is_bot": False},
            "sticker": {
                "file_id": "CAACfileid",
                "file_unique_id": file_unique_id,
                "type": "regular",
                "emoji": "\U0001f60a",
                "set_name": set_name,
                "width": 512,
                "height": 512,
            },
        },
    }


class TestStickerRoundTrip(BridgeCase):
    def test_ingest_then_send_back(self):
        api = self.boot(sticker=True)
        note = asyncio.run(bridge.ingest_sticker(sticker_update(), media_ref=None))
        self.assertIn("sticker_", note)
        catalog_id = note.split("sticker_")[1].split(" ")[0]
        catalog_id = "sticker_" + catalog_id

        listing = bridge.sticker_context()
        self.assertIn(catalog_id, listing)

        plan = (
            '{"version":"telemood.plan.v1","actions":['
            '{"type":"sticker","sticker":{"kind":"catalog","id":"%s"}}]}' % catalog_id
        )
        out = asyncio.run(
            bridge.deliver(plan, update_id=6, chat_id="7", message_id="9", user_id="7")
        )
        self.assertTrue(out.handled)
        sent = [p for m, p in api.calls if m == "sendSticker"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["sticker"], "CAACfileid")

    def test_foreign_namespace_id_fails_closed(self):
        self.boot(sticker=True)
        asyncio.run(bridge.ingest_sticker(sticker_update(), media_ref=None))
        from telemood import sticker_catalog_id

        foreign = sticker_catalog_id("tg999999", "uniq-1")
        plan = (
            '{"version":"telemood.plan.v1","actions":['
            '{"type":"sticker","sticker":{"kind":"catalog","id":"%s"}}]}' % foreign
        )
        out = asyncio.run(
            bridge.deliver(plan, update_id=6, chat_id="7", message_id="9", user_id="7")
        )
        self.assertFalse(out.handled)

    def test_model_never_sees_the_reusable_file_id(self):
        # 🔴 catalog.list(...) 的行里带着可复用的 provider file_id，
        #    交给模型的必须是 list_sticker_model_views 的结果。
        self.boot(sticker=True)
        note = asyncio.run(bridge.ingest_sticker(sticker_update(), media_ref=None))
        self.assertNotIn("CAACfileid", note)
        self.assertNotIn("CAACfileid", bridge.sticker_context())

    def test_listing_is_capped(self):
        os.environ["TELEMOOD_STICKER_LIST_MAX"] = "2"
        self.boot(sticker=True)
        for i in range(5):
            asyncio.run(
                bridge.ingest_sticker(sticker_update(file_unique_id=f"u{i}"), media_ref=None)
            )
        listing = bridge.sticker_context()
        self.assertEqual(listing.count("sticker_"), 2)

    def test_sticker_disabled_ingests_nothing(self):
        self.boot()
        self.assertEqual(asyncio.run(bridge.ingest_sticker(sticker_update())), "")


# --------------------------------------------------------------------------
# 7. 入站 reaction
# --------------------------------------------------------------------------


def reaction_update(user_id=7, emoji="❤"):
    return {
        "update_id": 12,
        "message_reaction": {
            "chat": {"id": 7, "type": "private"},
            "message_id": 3,
            "user": {"id": user_id, "is_bot": False},
            "date": 1700000000,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": emoji}],
        },
    }


class TestInboundReaction(BridgeCase):
    def test_note_is_recorded(self):
        self.boot(reaction=True)
        note = bridge.note_reaction(reaction_update())
        self.assertIn("❤", note)

    def test_his_own_reaction_is_ignored(self):
        # 🔴 他自己点的那一下会回流成一条 message_reaction。不滤掉的话，
        #    下一轮他会看见「她点了 ❤」——而那是他自己点的。
        api = self.boot(reaction=True)
        self.assertEqual(bridge.note_reaction(reaction_update(user_id=api.bot_id)), "")

    def test_ignored_when_capability_off(self):
        self.boot()
        self.assertEqual(bridge.note_reaction(reaction_update()), "")


# --------------------------------------------------------------------------
# 8. 按钮：一次性 + 过期 + 认人（验收第 9/10/11 条）
# --------------------------------------------------------------------------


CHOICES_PLAN = (
    '{"version":"telemood.plan.v1","actions":[{"type":"choices","prompt":"睡了吗",'
    '"options":[{"key":"yes","label":"睡了"},{"key":"no","label":"没呢"}]}]}'
)


class TestChoices(BridgeCase):
    def _send_buttons(self):
        api = self.boot(choices=True)
        out = asyncio.run(
            bridge.deliver(
                CHOICES_PLAN, update_id=8, chat_id="7", message_id="9", user_id="7"
            )
        )
        self.assertTrue(out.handled)
        markup = [p for m, p in api.calls if m == "sendMessage" and "reply_markup" in p]
        self.assertEqual(len(markup), 1)
        rows = markup[0]["reply_markup"]["inline_keyboard"]
        tokens = [row[0]["callback_data"] for row in rows]
        # Telegram 的 callback_data 上限是 64 字节
        for token in tokens:
            self.assertLessEqual(len(token.encode("utf-8")), 64)
        return api, tokens

    def _click(self, token, user_id=7):
        return {
            "update_id": 13,
            "callback_query": {
                "id": "cbq1",
                "from": {"id": user_id, "is_bot": False},
                "data": token,
                "message": {
                    "message_id": 1001,
                    "date": 1700000000,
                    "chat": {"id": 7, "type": "private"},
                },
            },
        }

    def test_click_is_consumed_once(self):
        api, tokens = self._send_buttons()
        first = asyncio.run(bridge.consume_callback(self._click(tokens[0])))
        self.assertTrue(first)
        # 第二次点同一个按钮：one-shot，不算数
        second = asyncio.run(bridge.consume_callback(self._click(tokens[0])))
        self.assertEqual(second, "")
        # 两次都要 answerCallbackQuery，否则她手机上那个圈一直转
        self.assertGreaterEqual(
            len([m for m, _ in api.calls if m == "answerCallbackQuery"]), 2
        )

    def test_click_by_someone_else_fails_closed(self):
        _, tokens = self._send_buttons()
        out = asyncio.run(bridge.consume_callback(self._click(tokens[0], user_id=999)))
        self.assertEqual(out, "")

    def test_expired_click_fails_closed(self):
        os.environ["TELEMOOD_CALLBACK_TTL"] = "0.05"
        _, tokens = self._send_buttons()
        import time as _time

        _time.sleep(0.2)
        out = asyncio.run(bridge.consume_callback(self._click(tokens[0])))
        self.assertEqual(out, "")

    def test_markup_is_cleared_after_a_click(self):
        api, tokens = self._send_buttons()
        asyncio.run(bridge.consume_callback(self._click(tokens[0])))
        self.assertIn("editMessageReplyMarkup", [m for m, _ in api.calls])

    def test_click_ignored_when_choices_off(self):
        self.boot()
        self.assertEqual(asyncio.run(bridge.consume_callback(self._click("nope"))), "")


# --------------------------------------------------------------------------
# 9. 白名单：按 update 类型各自取 chat_id
#
#    🔴 这条是全仓库最不能出错的一段：那个 bot 的名字陌生人能搜到，白名单是
#       她唯一的门。callback_query / message_reaction 顶层**没有 message 字段**，
#       老写法对它们一律取不到 → 全丢；而「用 return True 修一下」会把门拆了。
#
#    app.telegram 整个 import 不进来（它拉着 claude-agent-sdk），所以这里从
#    源码里把这几个顶层定义原样取出来跑——测的是**真代码**，名字改了会当场报错。
# --------------------------------------------------------------------------


def _load_from_telegram_source(names):
    source = (Path(__file__).resolve().parent / "app" / "telegram.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    picked = [
        node
        for node in tree.body
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names)
        or (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) in names for t in node.targets)
        )
    ]
    found = {
        getattr(n, "name", None) or n.targets[0].id
        for n in picked
    }
    missing = set(names) - found
    if missing:
        raise AssertionError(f"telegram.py 里找不到这几个定义了：{sorted(missing)}")
    namespace = {}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "telegram.py", "exec"), namespace)
    return namespace


class TestHostWhitelist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_from_telegram_source(
            {"_CHAT_ID_PATHS", "_update_chat_id", "_allowed", "_target_of"}
        )
        cls.ns["ALLOWED_CHAT_ID"] = "7"

    def allowed(self, upd):
        return self.ns["_allowed"](upd)

    def test_message_from_her(self):
        self.assertTrue(self.allowed({"message": {"chat": {"id": 7}}}))

    def test_message_from_a_stranger(self):
        self.assertFalse(self.allowed({"message": {"chat": {"id": 8}}}))

    def test_callback_query_from_her(self):
        self.assertTrue(
            self.allowed({"callback_query": {"message": {"chat": {"id": 7}}}})
        )

    def test_callback_query_from_a_stranger(self):
        self.assertFalse(
            self.allowed({"callback_query": {"message": {"chat": {"id": 8}}}})
        )

    def test_reaction_from_her(self):
        self.assertTrue(self.allowed({"message_reaction": {"chat": {"id": 7}}}))

    def test_reaction_count_from_a_stranger(self):
        self.assertFalse(self.allowed({"message_reaction_count": {"chat": {"id": 8}}}))

    def test_unknown_update_shape_is_refused(self):
        # 取不到 chat_id 就丢。fail closed，不是 fail open。
        self.assertFalse(self.allowed({"edited_message": {"chat": {"id": 7}}}))
        self.assertFalse(self.allowed({"poll": {"id": "x"}}))
        self.assertFalse(self.allowed({}))

    def test_missing_chat_is_refused(self):
        self.assertFalse(self.allowed({"callback_query": {"from": {"id": 7}}}))
        self.assertFalse(self.allowed({"message": {"chat": {}}}))

    def test_target_never_takes_ids_from_the_model(self):
        target = self.ns["_target_of"](
            {"message_id": 5, "message_thread_id": 12}, {"id": 7}
        )
        self.assertEqual(target["chat_id"], "7")     # 白名单里那个，不是消息里的
        self.assertEqual(target["message_id"], "5")
        self.assertEqual(target["user_id"], "7")
        self.assertEqual(target["thread_id"], "12")


# --------------------------------------------------------------------------
# 10. app/telegram.py 本身 import 得起来
#
#     真机上这个模块 import 不了 = TG 那条线整个不启动，而它拉着
#     claude-agent-sdk / anthropic 这些本地没装的东西，平时跑不到。
#     这里把那几个第三方依赖打桩，只为把**模块顶层**从头执行一遍——
#     顺序写反（比如 `_state = _blank_state()` 排在函数定义前面）这类错
#     只有真执行一次才抓得到，语法检查抓不到。
# --------------------------------------------------------------------------


class TestTelegramModuleImports(unittest.TestCase):
    def test_top_level_executes(self):
        import importlib
        import types

        saved = {k: sys.modules.get(k) for k in ("httpx", "app.claude", "app.relays", "app.telegram")}
        try:
            if "httpx" not in sys.modules:
                httpx = types.ModuleType("httpx")
                httpx.AsyncClient = object
                sys.modules["httpx"] = httpx

            claude = types.ModuleType("app.claude")
            claude.TELEGRAM_PROMPT_FILE = Path("telegram_prompt.md")
            claude.SessionResumeError = type("SessionResumeError", (Exception,), {})
            claude.stream_chat = lambda **kw: None
            claude.TG_MAX_TURNS = 3
            relays = types.ModuleType("app.relays")
            sys.modules["app.claude"] = claude
            sys.modules["app.relays"] = relays
            import app as app_pkg
            app_pkg.claude = claude
            app_pkg.relays = relays

            sys.modules.pop("app.telegram", None)
            telegram = importlib.import_module("app.telegram")

            # 顺手确认几件这一单要保住的事
            self.assertEqual(telegram._blank_state()["notes"], [])
            self.assertIn("notes", telegram._state)
            # _split_for_tg 必须还在：telemood 走不通时的回退路径要用它
            self.assertTrue(callable(telegram._split_for_tg))
            self.assertEqual(telegram._split_for_tg("短句"), ["短句"])
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
