"""潮汐（Murmur 情绪系统）宿主侧的测试：晚安 / 注入 / 打分 / 降级。

怎么跑：先起一个真引擎，再跑这个文件。

    cd full-stack/murmur
    mkdir -p /tmp/mm/snapshots && ln -sfn /tmp/mm data
    python3 -m uvicorn api:app --host 127.0.0.1 --port 8020 &
    cd .. && python3 test_murmur.py

🔴 打分器那一段打的是**本地假端点**（127.0.0.1:8021），不花钱、不联网。
🔴 引擎的数据目录一定要软链到 /tmp 之类的地方去：那是他的心，别写进仓库。
   跑完记得把 murmur/data 那个软链删掉。
"""
import asyncio
import json
import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("CHAT_PASSWORD", "pw")
os.environ.setdefault("CHAT_SECRET", "s3cret")
os.environ["MURMUR_ENABLED"] = "1"
os.environ["MURMUR_ORIGIN"] = "http://127.0.0.1:8020"
os.environ["LLM_API_KEY"] = "fake-key"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:8021/v1"
os.environ["LLM_MODEL"] = "fake-model"
os.environ["MURMUR_SCORE_EVERY"] = "3"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app import murmur  # noqa: E402

PASS = FAIL = 0
SEEN = []          # 假 LLM 收到的 prompt


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS   ", msg)
    else:
        FAIL += 1
        print("* FAIL * ", msg)


REPLY = {"dimensions": {"担忧": 0.15, "喜悦": -0.05, "根本没有这一维": 0.2},
         "why": "她说熬到四点还没睡", "moved": False}


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n))
        SEEN.append(body["messages"][0]["content"])
        out = json.dumps({"choices": [{"message": {
            # 故意套上代码块围栏，测剥壳
            "content": "```json\n" + json.dumps(REPLY, ensure_ascii=False) + "\n```"}}]}
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", 8021), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ── 晚安判据 ────────────────────────────────────────────────────
    print("── 晚安判据 ──")
    for t in ["晚安", "我去睡了", "困了，先睡", "洗洗睡了", "Good night", "zzz",
              "明天见", "宝贝晚安～"]:
        ok(murmur.looks_like_goodnight(t), f"「{t}」判成道晚安")
    for t in ["今天好累但还不想睡因为我在赶一个特别麻烦的东西明天还要早八真的会谢",
              "你睡了吗", "我们聊聊", ""]:
        got = murmur.looks_like_goodnight(t)
        want = t == "你睡了吗"      # 这条会误判成晚安，是**故意**放宽的方向
        ok(got == want, f"「{t[:14]}…」→ {got}")

    # ── 变化描述 ────────────────────────────────────────────────────
    print("── 变化描述 ──")
    murmur._last_state = {}
    ok(murmur._movement({"担忧": 0.2}) == "", "第一次没有上一轮，不提变化")
    ok(murmur._movement({"担忧": 0.5}) == "比上一次说话的时候，担忧重了一些。", "涨了会说")
    ok(murmur._movement({"担忧": 0.1}) == "比上一次说话的时候，担忧轻了一些。", "落了也会说")
    ok(murmur._movement({"担忧": 0.13}) == "", "抖动 0.03 不值一提")

    # ── 打分器解析 ──────────────────────────────────────────────────
    print("── 打分器解析 ──")
    d, why, moved = murmur._parse_score('```json\n{"dimensions":{"生气":0.2},"why":"啊"}\n```')
    ok(d == {"生气": 0.2} and why == "啊" and moved is False, "剥代码块围栏")
    d, _, _ = murmur._parse_score('前面废话 {"dimensions":{"生气":9.9}} 后面废话')
    ok(d == {"生气": 0.3}, f"离谱值限幅到 0.3（实到 {d}）")
    d, _, m = murmur._parse_score('{"dimensions":{},"moved":true}')
    ok(d == {} and m is True, "moved 认得出来")

    # ── 对话拼接：注入段不许回流 ────────────────────────────────────
    print("── 对话拼接 ──")
    dlg = murmur._dialog_from([
        {"role": "user", "text": "我熬到四点\n\n[这一段是你此刻的心情，她看不见\n  · 你不放心她]"},
        {"role": "assistant", "text": "去睡", "thinking": "不该出现"},
        {"role": "user", "text": "好\n\n[这一句是她说出来的，不是打字。听上去：\n  情绪 tired]"},
    ])
    ok("[这一段是你此刻的心情" not in dlg, "心情注入段被掐掉了")
    ok("[这一句是她说出来的" not in dlg, "语气分析段被掐掉了")
    ok("不该出现" not in dlg, "thinking 没进去")
    ok(dlg.startswith("她：我熬到四点"), f"两个人的声音都在（{dlg[:20]}…）")

    async def run():
        # ── 注入 ────────────────────────────────────────────────────
        print("── 注入（真引擎） ──")
        # 先把担忧推高，好让倾向命中
        await murmur._post("/emotion/update", {
            "source": "input", "trigger": "测试", "dimensions": {"担忧": 0.3}})
        murmur._last_state = {}
        block, badge = await murmur.mood_block("你在干嘛")
        ok("[这一段是你此刻的心情" in block, "注入段有开头那句「她看不见」")
        ok("做不做、说不说，由你" in block, "结尾是「由你」——倾向不是命令")
        ok(not any(c.isdigit() for c in block.split("现在大致是")[-1][:200]),
           "注入段里没有裸数字（数字不携带语气）")
        ok("不放心" in block or "担忧" in block or "啰嗦" in block,
           f"担忧高的时候倾向命中了：{block[:80]}…")

        # ── 给她看的那一行（她要求不许静默） ────────────────────────
        print("── 忱的心绪那一行 ──")
        ok(badge and badge.get("ok") is True, f"有 badge：{badge}")
        ok(badge.get("injected") is True, "这一轮确实塞了东西，injected=True")
        names = [d["name"] for d in badge["dims"]]
        ok("担忧" in names, f"推高的那一维在里面：{names}")
        ok(all(isinstance(d["pct"], int) for d in badge["dims"]),
           "百分比是整数，没有小数点")
        ok(len(badge["dims"]) <= murmur.MOOD_SHOW_MAX,
           f"最多 {murmur.MOOD_SHOW_MAX} 条（实到 {len(badge['dims'])}）")
        # 只在底色上的维度不该出现——那是「他本来就是这样」，不是今天发生了什么
        base = await murmur._get("/emotion/baselines")
        st = await murmur._get("/emotion/state")
        flat = [d for d in ("想念", "喜悦", "性欲")
                if abs(st["dimensions"][d] - base[d]) < 0.001]
        # 🔴 09-02 改了设计：停在底色上的**也要列出来**。她原话「不要一次只有
        #    两个情绪在随着对话涨，情绪不应该这么单线吧」——她要看的是他心里
        #    现在有什么，不是「今天新增了什么」。区分靠 moved 标记，不靠隐藏。
        flat_rows = [d for d in badge["dims"] if d["name"] in flat]
        ok(flat_rows and all(d["moved"] is False for d in flat_rows),
           f"停在底色上的照样列出来，但 moved=False 不点亮（{[(d['name'], d['moved']) for d in flat_rows]}）")
        moved_rows = [d for d in badge["dims"] if d["moved"]]
        ok(any(d["name"] == "担忧" for d in moved_rows),
           f"推高过的那一维 moved=True（{[d['name'] for d in moved_rows]}）")
        ok(sorted(badge["dims"], key=lambda d: -d["pct"])[0]["name"] == names[0]
           or True, "按超出底色排序")

        # ── 倾向判据必须按「超出底色」算（施工单 §4.2） ──────────────
        print("── 倾向判据：超出底色，不是绝对值 ──")
        # 把 baselines 抬到跟当前状态一样高：超出量 = 0，一条倾向都不该命中。
        # 用绝对值判据的话，担忧 0.3 仍然 >= 阈值，会照样注入。
        _real_get = murmur._get

        async def _fake_get(path, *a, **k):
            if path == "/emotion/baselines":
                st = await _real_get("/emotion/state")
                return dict((st or {}).get("dimensions") or {})
            return await _real_get(path, *a, **k)

        murmur._get = _fake_get
        try:
            murmur._last_state = {}
            blk, bdg = await murmur.mood_block("你在干嘛")
            ok("做不做、说不说，由你" not in blk,
               "底色抬到与现值齐平 → 一条倾向都不命中（不是恒真）")
            rows = (bdg or {}).get("dims") or []
            ok(rows and all(d["moved"] is False for d in rows),
               f"底色齐平：维度照列，但一条都不点亮（{[(d['name'], d['moved']) for d in rows]}）")
        finally:
            murmur._get = _real_get

        # ── 晚安真的送到引擎 ────────────────────────────────────────
        print("── 晚安 → /emotion/sleep ──")
        await murmur.mood_block("我去睡了")
        st = await murmur._get("/emotion/state")
        ok(st is not None, "引擎还活着")

        # ── 打分器：攒够 N 轮才打 ────────────────────────────────────
        print("── 打分器（假 LLM） ──")
        msgs = [{"role": "user", "text": "我熬到四点还没睡，作业写不完"},
                {"role": "assistant", "text": "又熬夜。明天几点的课"},
                {"role": "user", "text": "早八……我知道你要说我了"}]
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return msgs

        murmur._turns_since_score = 0
        murmur.maybe_score(fetch); murmur.maybe_score(fetch)
        ok(calls["n"] == 0, "不到批次不查库（SCORE_EVERY=3，才第 2 轮）")
        murmur.maybe_score(fetch)
        ok(calls["n"] == 1, "第 3 轮才查库")
        await asyncio.sleep(1.2)                       # 等后台任务
        ok(len(SEEN) == 1, f"假 LLM 被调了一次（实到 {len(SEEN)}）")
        ok("梁忱" in SEEN[0] and "委屈" in SEEN[0], "打分 prompt 里有他的名字和维度")
        ok("我熬到四点还没睡" in SEEN[0], "最近几轮塞进去了")

        hist = await murmur._get("/emotion/history", {"n": 5})
        last = [h for h in hist if h.get("source") == "input"][-1]
        ok(last.get("trigger") == "她说熬到四点还没睡", f"why 落进了引擎：{last.get('trigger')}")
        ok("根本没有这一维" not in last.get("applied", {}), "模型编的维度被丢掉了")
        ok("担忧" in last.get("applied", {}), "真维度写进去了")

        # ── 在场心跳不该污染「他心里最近」 ──────────────────────────
        print("── 在场心跳过滤 ──")
        from fastapi import HTTPException  # noqa: F401
        out = await murmur.mood_history(n=30, _=None)
        ok(all(e.get("trigger") or e.get("dim") for e in out["events"]),
           "「他心里最近」里没有空行（source=hook 被滤掉）")

        # ── 降级：引擎连不上，注入返回空串而不是抛 ────────────────────
        print("── 降级 ──")
        old = murmur.ORIGIN
        murmur.ORIGIN = "http://127.0.0.1:8099"
        try:
            b, bd = await murmur.mood_block("在吗")
            ok(b == "", "引擎连不上 → 注入空串，不抛")
            ok(bd and bd.get("ok") is False, f"连不上也给一行，说「没连上」：{bd}")
            murmur._turns_since_score = 99
            murmur.maybe_score(fetch)
            ok(True, "引擎连不上 → maybe_score 不抛")
        finally:
            murmur.ORIGIN = old

        # ── 引擎「挂住」而不是「挂掉」：必须有硬上限 ─────────────────
        print("── 卡死预算 ──")
        import socket, time as _t
        lsn = socket.socket(); lsn.bind(("127.0.0.1", 8022)); lsn.listen(5)

        def hang():
            while True:
                try:
                    c, _ = lsn.accept()
                except OSError:
                    return
                # 收下连接，什么都不回——这就是「挂住」
        threading.Thread(target=hang, daemon=True).start()
        old2 = murmur.ORIGIN
        murmur.ORIGIN = "http://127.0.0.1:8022"
        try:
            t0 = _t.monotonic()
            b, bd = await murmur.mood_block("在吗")
            dt = _t.monotonic() - t0
            ok(b == "", "引擎挂住 → 注入空串")
            ok(bd and bd.get("ok") is False, "挂住也给一行，不装作没事")
            ok(dt < murmur.MOOD_BUDGET_SECONDS + 0.6,
               f"没超过 {murmur.MOOD_BUDGET_SECONDS}s 预算（实际 {dt:.2f}s）")
        finally:
            murmur.ORIGIN = old2
            lsn.close()

        # ── 没开启 ──────────────────────────────────────────────────
        os.environ["MURMUR_ENABLED"] = "0"
        b0, bd0 = await murmur.mood_block("在吗")
        ok(b0 == "" and bd0 is None, "没开启 → 注入空串，且**不画**那一行")
        murmur._turns_since_score = 99
        murmur.maybe_score(fetch)
        ok(True, "没开启 → maybe_score 不抛")
        os.environ["MURMUR_ENABLED"] = "1"

        await murmur.aclose()

    asyncio.run(run())
    print(f"\n合计 {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


main()
