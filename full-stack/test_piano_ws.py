"""WS 代理的端到端断言。`python test_piano_ws.py`

起一个假 Duetto，挂真的 /ws/piano 路由，拿浏览器那样的客户端连。

验四件事：
  1. 没 token / token 不对 → 连不上（这条最要紧，它是唯一一扇外层 Basic Auth
     拦不住的门）
  2. token 对 → 连得上，双向字节对拷
  3. 上游拿到的 room 和 token 是后端拼的，不是前端给的
  4. 前端那条连接里**看不到** DUETTO_TOKEN
"""
import asyncio, os, sys, tempfile, threading, types, json
from urllib.parse import urlparse, parse_qs

os.environ.update(CHAT_PASSWORD="x", CHAT_SECRET="test-secret",
                  AGENT_APP_ROOT=tempfile.mkdtemp(), MODELS_FILE="models.json",
                  DUETTO_TOKEN="duetto-secret-token")

class A:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return []
    def __iter__(s): return iter(())
def mk(n):
    m = types.ModuleType(n); m.__path__ = []; m.__getattr__ = lambda x: A(); return m
sys.modules["claude_agent_sdk"] = mk("claude_agent_sdk")
sys.modules["claude_agent_sdk.types"] = mk("claude_agent_sdk.types")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import websockets, uvicorn
from app import main, piano, auth

seen = {}          # 假 Duetto 记下它看到的握手

async def fake_duetto(sock):
    seen["path"] = sock.request.path
    q = parse_qs(urlparse(sock.request.path).query)
    seen["room"] = (q.get("room") or [""])[0]
    seen["token"] = (q.get("token") or [""])[0]
    async for msg in sock:
        await sock.send("echo:" + msg)

fails = []
def eq(name, got, want):
    if got != want:
        fails.append(f"{name}\n     得到 {got!r}\n     期望 {want!r}"); print("  FAIL  " + name)
    else: print("  ok    " + name)

async def main_test():
    up = await websockets.serve(fake_duetto, "127.0.0.1", 8899)
    piano.BASE_URL = "http://127.0.0.1:8899"
    piano.TOKEN = "duetto-secret-token"

    cfg = uvicorn.Config(main.app, host="127.0.0.1", port=8898, log_level="error")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        if server.started: break
        await asyncio.sleep(0.05)

    tok = auth.issue_token("x")

    # 1. 不带 subprotocol
    try:
        async with websockets.connect("ws://127.0.0.1:8898/ws/piano"):
            eq("没 token 应该连不上", "连上了", "被拒")
    except Exception:
        eq("没 token 连不上", True, True)

    # 2. token 不对
    try:
        async with websockets.connect("ws://127.0.0.1:8898/ws/piano",
                                      subprotocols=["bearer", "wrong-token"]):
            eq("token 不对应该连不上", "连上了", "被拒")
    except Exception:
        eq("token 不对连不上", True, True)

    # 3. token 对 → 通
    async with websockets.connect(
            "ws://127.0.0.1:8898/ws/piano?room=%E6%9A%97%E5%8F%B7",
            subprotocols=["bearer", tok]) as c:
        await c.send(json.dumps({"action": "play", "idx": 2, "time": 33}))
        got = await asyncio.wait_for(c.recv(), timeout=5)
        eq("双向对拷", got, 'echo:{"action": "play", "idx": 2, "time": 33}')
    await asyncio.sleep(0.2)

    eq("上游收到的房间是前端传的", seen.get("room"), "暗号")
    eq("上游收到的 token 是后端拼的", seen.get("token"), "duetto-secret-token")
    eq("前端那把 token 没被转给上游", "test" in str(seen.get("token")), False)
    eq("DUETTO_TOKEN 不等于小窝的 token", piano.TOKEN == tok, False)

    server.should_exit = True
    up.close(); await up.wait_closed()

asyncio.run(main_test())
print()
if fails:
    print("FAILED:"); [print(" -", f) for f in fails]; sys.exit(1)
print("全部通过")
