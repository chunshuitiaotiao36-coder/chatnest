"""ActStripper 的断言。`python test_piano_act.py`，不用起服务、不用部署。

这一份专门守着 piano.py 里那个剥 <<ACT>> 的缓冲机。它是琴房 DJ 最容易
悄悄坏掉的一块：改坏了不会报错，只会让她在聊天里看见一行 `<<ACT>>{...}`，
或者更糟——把半个标记吐出去之后再也收不回来。改完 piano.py 跑一遍。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CHAT_PASSWORD", "x")
os.environ.setdefault("CHAT_SECRET", "x")

from app.piano import ActStripper, now_playing_block  # noqa: E402

_fails: list[str] = []


def eq(name: str, got, want) -> None:
    if got != want:
        _fails.append(f"{name}\n     得到 {got!r}\n     期望 {want!r}")
        print(f"  FAIL  {name}")
    else:
        print(f"  ok    {name}")


REPLY = '放好了，一起听。\n<<ACT>>{"type":"play","query":"晴天 周杰伦"}<<>>'
PLAY = {"type": "play", "query": "晴天 周杰伦"}


def main() -> int:
    # 1. 整块喂
    s = ActStripper()
    t, a = s.feed(REPLY)
    t += s.flush()
    eq("整块喂：文本干净", t, "放好了，一起听。\n")
    eq("整块喂：动作解析", a, [PLAY])

    # 2. 逐字符喂（真实的 token 流长这样）—— 最要命的一条
    s = ActStripper()
    out, acts = "", []
    for ch in REPLY:
        c, k = s.feed(ch)
        out += c
        acts += k
    out += s.flush()
    eq("逐字符：文本干净", out, "放好了，一起听。\n")
    eq("逐字符：动作解析", acts, [PLAY])
    eq("逐字符：没有半个标记漏出去", "<" in out, False)

    # 3. 两条 ACT 都要执行
    #    上游 Duetto 的正则不带 g，只执行第一条却把两条都剥掉（views.jsx:1004）。
    #    那是个 bug，不许跟着抄。
    s = ActStripper()
    t, a = s.feed('好。<<ACT>>{"type":"pause"}<<>>再来<<ACT>>{"type":"like"}<<>>尾巴')
    t += s.flush()
    eq("两条 ACT：文本", t, "好。再来尾巴")
    eq("两条 ACT：都执行", a, [{"type": "pause"}, {"type": "like"}])

    # 4. 正文里的裸 < 和 << 不能被误扣
    s = ActStripper()
    t, a = s.feed("a < b << c")
    t += s.flush()
    eq("裸 < 不误扣", t, "a < b << c")
    eq("裸 < 无动作", a, [])

    # 5. 坏 JSON：剥掉、不执行、不崩
    s = ActStripper()
    t, a = s.feed('前<<ACT>>{"type":}<<>>后')
    t += s.flush()
    eq("坏 JSON：照样剥掉", t, "前后")
    eq("坏 JSON：不执行", a, [])

    # 6. 开了标记不闭合：不许把整条回复吞掉
    unclosed = '说了一句话<<ACT>>{"type":"play"'
    s = ActStripper()
    t, a = s.feed(unclosed)
    t += s.flush()
    eq("不闭合：正文还在", t.startswith("说了一句话"), True)
    eq("不闭合：一个字没吞", len(t), len(unclosed))

    # 7. 帧边界正好切在标记中间
    s = ActStripper()
    t1, a1 = s.feed("嗯<<AC")
    t2, a2 = s.feed('T>>{"type":"next"}<<>>好')
    t = t1 + t2 + s.flush()
    eq("跨帧：第一帧不许吐出 <<AC", t1, "嗯")
    eq("跨帧：文本", t, "嗯好")
    eq("跨帧：动作", a1 + a2, [{"type": "next"}])

    # 8. now_playing_block：没在放歌但有歌单，也要出内容
    #    「随便放一首」正好是没在放的时候说的。
    blk = asyncio.run(now_playing_block({"lists": ["我喜欢的音乐", "深夜"]}))
    eq("没放歌也带歌单", "我喜欢的音乐" in blk and "深夜" in blk, True)
    eq("没放歌不写「正在一起听」", "正在一起听" in blk, False)
    eq("什么都没有就返回空串", asyncio.run(now_playing_block({})), "")

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
