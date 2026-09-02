"""restore_branch 的列数回归。

为什么值得单独一个文件：09-01 给 messages 加 voice_say 那次，只往
restore_branch 里那条 INSERT 的**列名**补了一个词，占位符和值都没动，
于是它变成「11 列 10 个值」，每次调用必抛 OperationalError。

而 restore_branch 是「这一轮出错了，把她原来那几条消息放回去」的那条路：
/api/chat 的每一个 except 分支都会走它（会话不存在、参数错、恢复失败、
actor 忙）。它一抛，异常就顺着 SSE 生成器冒出去，最后一帧发不出，
前端那个「上一条消息仍在回复」的锁**永远不解**。

症状离病灶隔了三层，日志里也只有一行 sqlite 报错——所以要有这个测试：
以后再往 messages 加列，忘了改这三处的话，这儿会立刻红。

跑法：python3 test_branch_restore.py
"""
import os
import pathlib
import sys
import tempfile
import types

os.environ.setdefault("CHAT_PASSWORD", "pw")
os.environ.setdefault("CHAT_SECRET", "s3cret")
os.environ["AGENT_APP_ROOT"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# store 只从 SDK 里拿两个函数，跑这个测试不需要真装上它
if "claude_agent_sdk" not in sys.modules:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        _stub = types.ModuleType("claude_agent_sdk")
        _stub.delete_session = lambda *a, **k: None
        _stub.list_sessions = lambda *a, **k: []
        sys.modules["claude_agent_sdk"] = _stub

from app import store  # noqa: E402

PASS = FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS   ", msg)
    else:
        FAIL += 1
        print("* FAIL * ", msg)


def main():
    store.initialize_store()

    conv_id, _, _ = store.begin_turn("我熬到四点了")
    store.complete_turn(conv_id, "sess-1", "又熬夜。去睡", "", [], "去睡吧宝宝",
                        '{"mood":"不放心","dims":[{"name":"担忧","pct":63}]}')
    store.begin_turn("好啦我睡", conversation_id=conv_id)
    store.complete_turn(conv_id, "sess-2", "乖", "", [], "")

    msgs, _, _ = store.conversation_messages(conv_id)
    ok(len(msgs) == 4, f"四条消息都在（实到 {len(msgs)}）")
    first_reply = [m for m in msgs if m["role"] == "assistant"][0]
    ok(first_reply.get("voice_say") == "去睡吧宝宝", "voice_say 落库了")
    ok((first_reply.get("mood") or {}).get("mood") == "不放心",
       f"mood 落库并解析成对象了（{first_reply.get('mood')}）")

    # 重新生成：把尾巴挪进 message_branches
    last_assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    out = store.prepare_retry_turn(conv_id, int(last_assistant["id"]))
    branch_id = out.get("branch_id") if isinstance(out, dict) else out
    ok(bool(branch_id), f"prepare_retry_turn 拿到了 branch_id（{branch_id}）")

    # 这一步就是线上出错时走的那条路
    try:
        store.restore_branch(branch_id)
        crashed = ""
    except Exception as exc:  # noqa: BLE001
        crashed = f"{type(exc).__name__}: {exc}"
    ok(not crashed, f"restore_branch 没抛{'（' + crashed + '）' if crashed else ''}")

    msgs2, _, _ = store.conversation_messages(conv_id)
    ok(len(msgs2) == len(msgs), f"消息数回到 {len(msgs)}（实到 {len(msgs2)}）")
    replies = [m for m in msgs2 if m["role"] == "assistant"]
    ok(any(m.get("voice_say") == "去睡吧宝宝" for m in replies),
       "放回去之后 voice_say 还在")
    ok(any((m.get("mood") or {}).get("mood") == "不放心" for m in replies),
       "放回去之后 mood 还在")

    print(f"\n合计 {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


main()
