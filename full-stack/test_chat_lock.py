"""chat_lock 的断言。`python test_chat_lock.py`，不用起服务、不用部署。

守的是「上一条消息仍在回复」那一单里最容易悄悄坏掉的三块：

  1. 锁在任何异常路径上都会释放。泄漏一次就得重启整个进程，而且**一个字的
     报错都没有**——她那边的表现就是「怎么发都发不出去，重新部署才好」。
  2. 秒拒改成了有限等待。改回秒拒的话她的每一次重发都会被挡在门外。
  3. 四个出处的报错文案互不相同。改回同一句，下次就又没人分得清她撞的是
     哪一道门（这一单的第 4.1 节就是为了这个排在第一位的）。

第 3 条和「acquire 之后、try 之前不许有语句」这条都是**读源码**验的：
它们是代码形状上的约束，跑不出来，但 ast 看得见。app/main.py import 了
fastapi / claude_agent_sdk，本机装不全，所以这里只 parse 不 import。
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CHAT_PASSWORD", "x")
os.environ.setdefault("CHAT_SECRET", "x")

from app.chatlock import ChatLock  # noqa: E402

_fails: list[str] = []
HERE = os.path.dirname(os.path.abspath(__file__))


def eq(name: str, got, want) -> None:
    if got != want:
        _fails.append(f"{name}\n     得到 {got!r}\n     期望 {want!r}")
        print(f"  FAIL  {name}")
    else:
        print(f"  ok    {name}")


def src(rel: str) -> str:
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


# ── 1. 释放 ────────────────────────────────────────────────────────

async def _leak_on_exception() -> bool:
    """故意在 acquire 之后抛异常，断言下一次不被拒。

    这就是 main.py 里 sse() 的形状：acquire → try → finally: release。
    """
    lock = ChatLock()
    try:
        await lock.acquire(holder="web", request_id="boom")
        try:
            raise RuntimeError("假装 ActStripper() 炸了")
        finally:
            lock.release("exception")
    except RuntimeError:
        pass
    # 下一次请求：不被拒，而且能立刻拿到
    return await lock.acquire_within(0.05, holder="web", request_id="next")


async def _hold_releases_on_exception() -> bool:
    lock = ChatLock()
    try:
        async with lock.hold("web", "x"):
            raise ValueError("炸")
    except ValueError:
        pass
    return not lock.locked()


# ── 2. 有限等待 ────────────────────────────────────────────────────

async def _waits_then_gets() -> tuple[bool, bool]:
    """上一轮正在收尾时立刻重发：等得到，不是秒拒。"""
    lock = ChatLock()
    await lock.acquire(holder="web", request_id="first")

    async def _finish_soon():
        await asyncio.sleep(0.15)
        lock.release("ok")

    asyncio.create_task(_finish_soon())
    got = await lock.acquire_within(2.0, holder="web", request_id="second")
    return got, lock.status()["holder"] == "web"


async def _times_out_without_holding() -> tuple[bool, bool]:
    """等不到的时候：返回 False，而且**没有**握着锁。

    握着的话下一次 release 会 RuntimeError，锁就永久卡死了——比秒拒糟得多。
    """
    lock = ChatLock()
    await lock.acquire(holder="tg", request_id="long")
    got = await lock.acquire_within(0.05, holder="web", request_id="me")
    still_tg = lock.status()["holder"] == "tg"
    lock.release("ok")
    return got, still_tg


async def _status_reports_holder() -> dict:
    lock = ChatLock()
    await lock.acquire(holder="tg", request_id="abc123")
    snap = lock.status()
    lock.release("client_gone")
    return {"live": snap, "last": lock.status()["last"]}


# ── 3. 源码形状 ────────────────────────────────────────────────────

def _busy_messages() -> list[str]:
    """四个出处的那四句话，按源码里出现的顺序取出来。"""
    out: list[str] = []
    for rel in ("app/main.py", "app/registry.py", "app/actor.py"):
        tree = ast.parse(src(rel))
        for node in ast.walk(tree):
            # registry / actor：raise ActorBusyError("…")
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                fn = node.exc.func
                if getattr(fn, "id", "") == "ActorBusyError" and node.exc.args:
                    a = node.exc.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        out.append(a.value)
    return out


def _sse_shape() -> dict:
    """acquire 和 try 之间不许有语句；finally 里必须 release。"""
    tree = ast.parse(src("app/main.py"))
    sse = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "sse":
            sse = node
            break
    if sse is None:
        return {"found": False}

    # 找到 `got_lock = await chat_lock.acquire_within(...)` 那一句的下标
    idx = None
    for i, st in enumerate(sse.body):
        if "acquire_within" in ast.dump(st):
            idx = i
            break
    if idx is None:
        return {"found": True, "acquire": False}

    rest = sse.body[idx + 1:]
    # 紧跟着的应该是「等不到就 return」那个 if，然后就是 try——中间不许有别的
    after_guard = rest[1:] if rest and isinstance(rest[0], ast.If) else rest
    first = after_guard[0] if after_guard else None
    has_try = isinstance(first, ast.Try)
    releases_in_finally = False
    if has_try:
        releases_in_finally = "chat_lock" in ast.dump(ast.Module(
            body=first.finalbody, type_ignores=[]
        )) and "release" in ast.dump(ast.Module(
            body=first.finalbody, type_ignores=[]
        ))
    return {
        "found": True,
        "acquire": True,
        "gap": len(after_guard) - 1 if has_try else -1,
        "try_is_next": has_try,
        "release_in_finally": releases_in_finally,
    }


def main() -> int:
    print("chat_lock 断言")
    print()

    print("  —— 释放 ——")
    eq("acquire 之后抛异常，下一次不被拒", asyncio.run(_leak_on_exception()), True)
    eq("hold() 的异常路径也放锁", asyncio.run(_hold_releases_on_exception()), True)

    print("  —— 有限等待 ——")
    got, holder_ok = asyncio.run(_waits_then_gets())
    eq("上一轮正在收尾：等得到，不是秒拒", got, True)
    eq("等到之后持有者记的是新的那个", holder_ok, True)
    got2, still_tg = asyncio.run(_times_out_without_holding())
    eq("等不到：返回 False", got2, False)
    eq("等不到：没有握着锁（持有者还是原来那个）", still_tg, True)

    print("  —— 说得出谁拿的 ——")
    snap = asyncio.run(_status_reports_holder())
    eq("locked", snap["live"]["locked"], True)
    eq("holder", snap["live"]["holder"], "tg")
    eq("她看得懂的来源名", snap["live"]["holder_label"], "TG")
    eq("request_id", snap["live"]["request_id"], "abc123")
    eq("持有时长是个数", isinstance(snap["live"]["held_seconds"], float), True)
    eq("放锁之后还留着尸检记录", snap["last"]["holder"], "tg")
    eq("记得是怎么结束的", snap["last"]["outcome"], "client_gone")

    print("  —— 源码形状 ——")
    msgs = _busy_messages()
    eq("三个 ActorBusyError 出处都还在", len(msgs), 3)
    eq("三句话互不相同", len(set(msgs)), len(msgs))
    eq(
        "没有一句还是那句分不清的「上一条消息仍在回复」",
        [m for m in msgs if m == "上一条消息仍在回复"],
        [],
    )
    eq(
        "main.py 那句带上了被占了多久",
        "held_seconds" in src("app/main.py") and "上一条还在回，已经" in src("app/main.py"),
        True,
    )
    # 🔴 施工单第 2.1 条：这把锁在守一个**进程级**的全局状态
    #    （relays.subscription_env() pop 掉的那三个 ANTHROPIC_*）。
    #    全仓库只许有一个实例，而且小窝和 TG 抢的必须是同一个。
    #    改成「每个会话一把」就会出现「TG 把环境变量 pop 了，网页端正好撞上来」。
    all_src = src("app/main.py") + src("app/telegram.py") + src("app/registry.py")
    eq("只有一处 ChatLock() 实例化", src("app/chatlock.py").count("chat_lock = ChatLock()"), 1)
    eq("没有第二把锁被 new 出来", all_src.count("ChatLock("), 0)
    eq(
        "小窝和 TG 用的是同一个 chat_lock",
        "from app.chatlock import" in src("app/main.py")
        and "from app.main import chat_lock" in src("app/telegram.py"),
        True,
    )
    eq(
        "relays.py 那条「调用方必须已经持有 chat_lock」的约束还在",
        "调用方必须已经" in src("app/relays.py") and "chat_lock" in src("app/relays.py"),
        True,
    )
    # 第 2.2 条：不许靠调小 180 秒来「解决」问题
    eq(
        "RESPONSE_TIMEOUT_SECONDS 还是 180",
        "RESPONSE_TIMEOUT_SECONDS = 180" in src("app/actor.py"),
        True,
    )

    shape = _sse_shape()
    eq("找得到 sse()", shape.get("found"), True)
    eq("走的是有限等待 acquire_within", shape.get("acquire"), True)
    eq("🔴 acquire 和 try 之间一行都没有", shape.get("gap"), 0)
    eq("紧跟着就是 try", shape.get("try_is_next"), True)
    eq("finally 里 release", shape.get("release_in_finally"), True)

    print()
    if _fails:
        print("FAILED:")
        for f in _fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
