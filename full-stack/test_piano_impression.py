"""印象的断言。`python test_piano_impression.py`，不用起服务、不用部署。

守施工单 §1.5（08-07 改过的那一版）：
  - 在场记录满 6 条才请他揉，不满不请
  - 第一人称是**梁忱**写的，不是 Duetto 自带那个 DJ 写的
  - 回忆存进 Ombre（他自己调 hold），小窝只留副本
  - 在旧回忆上续写，不推翻
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
os.environ.setdefault("CHAT_PASSWORD", "x")
os.environ.setdefault("CHAT_SECRET", "x")
os.environ["AGENT_APP_ROOT"] = tempfile.mkdtemp()


# claude_agent_sdk 是重依赖，本地不一定装着。这一份只测印象的判定和落库，
# 碰不到 SDK，塞个桩让 import 过去就行。
class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return []
    def __iter__(self): return iter(())


def _stub(name):
    m = types.ModuleType(name)
    m.__path__ = []
    m.__getattr__ = lambda n: _Any()
    return m


sys.modules.setdefault("claude_agent_sdk", _stub("claude_agent_sdk"))
sys.modules.setdefault("claude_agent_sdk.types", _stub("claude_agent_sdk.types"))

from app import piano, store  # noqa: E402

store.initialize_store()

_fails: list[str] = []


def eq(name, got, want):
    if got != want:
        _fails.append(f"{name}\n     得到 {got!r}\n     期望 {want!r}")
        print("  FAIL  " + name)
    else:
        print("  ok    " + name)


def notes(n):
    return [{"thought": f"t{i}", "reply": f"r{i}"} for i in range(n)]


def main() -> int:
    SID = "25906124"

    # 1. 不满 6 条不请他动手
    piano._due.clear()
    out = piano._impression_lines(SID, notes(5), "晴天", "周杰伦")
    eq("5 条：不请他揉", any("该写一段回忆了" in x for x in out), False)
    eq("5 条：没有挂起标记", piano.take_due(SID), None)

    # 2. 满 6 条就请
    piano._due.clear()
    out = piano._impression_lines(SID, notes(6), "晴天", "周杰伦")
    body = "\n".join(out)
    eq("6 条：请他揉", "该写一段回忆了" in body, True)
    eq("6 条：点名用 ombre 的 hold 存", "hold" in body, True)
    eq("6 条：要求第一人称", "第一人称" in body, True)
    eq("6 条：限 150 字", "150" in body, True)
    eq("6 条：挂起标记记着条数", piano.take_due(SID), 6)
    eq("take_due 是一次性的", piano.take_due(SID), None)

    # 3. 揉过之后，要再攒够 6 条才请下一次
    store.save_piano_impression(SID, "那年夏天你把这首放了很多遍。", 6, "晴天", "周杰伦")
    piano._due.clear()
    out = piano._impression_lines(SID, notes(9), "晴天", "周杰伦")
    body = "\n".join(out)
    eq("揉过之后 9 条（新增 3）：不再请", "该写一段回忆了" in body, False)
    eq("旧回忆会注回上下文", "那年夏天" in body, True)

    piano._due.clear()
    out = piano._impression_lines(SID, notes(12), "晴天", "周杰伦")
    body = "\n".join(out)
    eq("再攒够 6 条（12）：又请一次", "该写一段回忆了" in body, True)
    eq("续写不推翻：明确要求在旧回忆上往下写", "别推翻重来" in body, True)
    eq("告诉他上次揉到第几条", "第 6 条" in body, True)
    eq("这次的挂起标记是 12", piano.take_due(SID), 12)

    # 4. 本地副本读写
    got = store.piano_impression(SID)
    eq("副本读得回来", (got or {}).get("text"), "那年夏天你把这首放了很多遍。")
    eq("副本记着揉到第几条", (got or {}).get("n"), 6)
    store.save_piano_impression(SID, "后来又听了很多次，你还是会跟着哼。", 12)
    got = store.piano_impression(SID)
    eq("续写会覆盖旧副本", got["text"], "后来又听了很多次，你还是会跟着哼。")
    eq("覆盖不会丢掉歌名", got["title"], "晴天")
    eq("没有的歌返回 None", store.piano_impression("nope"), None)
    eq("空文本不落库", store.save_piano_impression("x2", "  ", 6) or
       store.piano_impression("x2"), None)

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
