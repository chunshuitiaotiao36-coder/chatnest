"""潮汐：把梁忱心里那九样东西的当前刻度取回来，给潮汐页看。

上游 https://github.com/fishisfish0614/Murmur（MIT，作者 fishisfish0614）。
引擎原封不动 vendor 在 `full-stack/murmur/`，宿主侧的接线**全部**写在这儿。

🔴 Murmur 跟 anno / hervoice 一样**自己没有任何鉴权**，只 listen 在
   127.0.0.1:8020。这道门由小窝守——那是一个人的内心，比书还私密。

🔴 这个模块任何情况下都不许把异常抛进聊天主链路（施工单 §1.6）。
   引擎没起、超时、配置写错——都只是「潮汐那一页显示没连上」，
   她该收到的字一个都不能少。所以下面每条路径都自己兜住，
   返回 {"ok": false, "error": ...} 而不是抛。

本次只做**读**：潮汐页要看的那一屏。故意还没做的（施工单里都写着）：
  · 打分器（要 LLM 调用，花钱）—— §2.4
  · 注入我的上下文（碰聊天主链路，风险最高）—— §2.5
  · 会话钩子 / 晚安 —— §2.6、§1.4
  · 行动触发器 —— §2.7 明确排期在后
所以现在引擎只会自己衰减，不会因为她说了什么而动。**这是排期，不是砍功能。**
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException

from app import auth

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

ORIGIN = os.environ.get("MURMUR_ORIGIN", "http://127.0.0.1:8020").rstrip("/")

# 本机回环 + 纯内存计算，正常是毫秒级。给 4 秒是为了容忍它刚起来还在读盘，
# 但绝不能更长：这一页是她随手点开看一眼的，转圈超过一两秒就该直接说没连上。
TIMEOUT = httpx.Timeout(4.0, connect=1.0)

# 潮汐页上从上到下的顺序。她定的（「怎么感觉放眼望去全是负面情绪……
# 打乱一下」），不是配置文件里的顺序，也不是按数值排序——
# 🔴 不许改成按数值降序：那样条目会跳来跳去，她每次打开都要重新找一遍
#    「委屈」在哪儿，而且高的永远在最上面，等于把最难受的那个天天顶在她眼前。
ORDER = ("想念", "生气", "委屈", "喜悦", "焦虑", "醋意", "性欲", "担忧", "悔意")

_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def configured() -> bool:
    return os.environ.get("MURMUR_ENABLED", "").strip() == "1"


def require_auth(authorization: str = Header(default="")) -> None:
    # 🔴 不能 `from app.main import require_auth`：装饰器里的 Depends() 是
    #    import 期求值的，反向 import 会撞上一个还没执行完的 main 模块，
    #    整个进程起不来。voice.py / listen.py 里都记了这个坑，这是第三处。
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="unauthorized")


async def _get(path: str, params: dict | None = None):
    """朝引擎要一份数据。🔴 什么都不许抛，连不上就返回 None。"""
    try:
        client = await _get_client()
        resp = await client.get(f"{ORIGIN}{path}", params=params)
    except httpx.HTTPError as exc:
        logger.warning("[潮汐] 连不上 murmur %s：%s", path, exc)
        return None
    except Exception:  # noqa: BLE001 — 这条路径的存在意义就是不崩
        logger.warning("[潮汐] 取 %s 出了意料之外的错", path, exc_info=True)
        return None
    if resp.status_code >= 400:
        logger.warning("[潮汐] murmur %s 返回 %d：%s", path, resp.status_code, resp.text[:200])
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("[潮汐] murmur %s 返回的不是 JSON", path)
        return None


def _pct(value) -> int:
    """0–1 → 0–100 的整数。她要的是「进度条 + 百分比，不要小数点」。

    🔴 用 round 不用 int：int(0.999*100) 是 99，会让一个几乎满格的感受
       永远差一点点到 100，看起来像 bug。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, round(v * 100)))


def _dimensions(state: dict, baselines: dict) -> list[dict]:
    """按她定的顺序排九条。

    🔴 两条都不许省：
       · ORDER 里有、引擎里没有的 → 不显示（配置被改过，硬编一个 0% 是撒谎）
       · 引擎里有、ORDER 里没有的 → **追加在后面**，绝不丢掉。
         「一个都不许省：省掉哪个，她在潮汐页上就永远看不见那件事。」
         哪天有人往 config.yaml 加了维度却忘了改这儿，页面上照样看得见。
    """
    dims = state if isinstance(state, dict) else {}
    base = baselines if isinstance(baselines, dict) else {}
    names = [n for n in ORDER if n in dims]
    names += [n for n in dims if n not in ORDER]
    out = []
    for name in names:
        out.append({
            "name": name,
            "pct": _pct(dims.get(name)),
            # 底色也给出去：将来想在条上画一道「他本来就有这么多」的刻度就够用了。
            "baseline": _pct(base.get(name)),
        })
    return out


@router.get("/api/mood")
async def mood(_: None = Depends(require_auth)) -> dict:
    """潮汐页开屏拉这一条：心情、心跳、九条刻度，一趟拿齐。

    🔴 合成一条而不是让前端拉三条：这一页是她点开就想看见的，
       三个来回意味着三次转圈，而且中间任何一条挂了都得单独处理。
    """
    if not configured():
        return {"enabled": False, "ok": False, "error": "还没开启潮汐（MURMUR_ENABLED）"}

    vitals = await _get("/emotion/vitals")
    state = await _get("/emotion/state")
    if vitals is None and state is None:
        return {"enabled": True, "ok": False, "error": "潮汐没连上"}

    vitals = vitals or {}
    state = state or {}
    # 底色是静态的，取不到就当没有——不该因为这一条挡住整页。
    baselines = await _get("/emotion/baselines") or {}

    return {
        "enabled": True,
        "ok": True,
        # mood 两处都有，以 state 那份为准：它跟 dimensions 是同一次快照，
        # 不会出现「心情说在气头上、生气那条却是 0%」这种自相矛盾。
        "mood": str(state.get("mood") or vitals.get("mood") or ""),
        "heartrate": vitals.get("heartrate"),
        "cause": str(vitals.get("cause") or ""),
        "dimensions": _dimensions(state.get("dimensions") or {}, baselines),
        "updated_at": state.get("saved_at"),
    }


@router.get("/api/mood/history")
async def mood_history(n: int = 30, _: None = Depends(require_auth)) -> dict:
    """「他心里最近」那一栏。

    每条事件带着当时的完整状态和这次的增量，所以「从 x% 到 y%」是
    y = state[d]、x = state[d] - applied[d] 算出来的，不用另外存历史。

    🔴 现在多半是空的：打分器还没做，没有东西会往引擎里推 update。
       前端要把「空」显示成一句人话，不能显示成一片白。
    """
    if not configured():
        return {"enabled": False, "ok": False, "events": []}

    n = max(1, min(100, int(n or 30)))
    rows = await _get("/emotion/history", {"n": n})
    if rows is None:
        return {"enabled": True, "ok": False, "events": []}
    if not isinstance(rows, list):
        logger.warning("[潮汐] history 返回的不是列表：%r", type(rows))
        return {"enabled": True, "ok": False, "events": []}

    events = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        after = row.get("state") if isinstance(row.get("state"), dict) else {}
        applied = row.get("applied") if isinstance(row.get("applied"), dict) else {}
        # 这一次动得最狠的那个维度——一条只讲一件事，讲最响的那件。
        top, top_delta = "", 0.0
        for d, delta in applied.items():
            try:
                delta = float(delta)
            except (TypeError, ValueError):
                continue
            if abs(delta) > abs(top_delta):
                top, top_delta = d, delta
        item = {
            "ts": row.get("ts"),
            "source": str(row.get("source") or ""),
            "trigger": str(row.get("trigger") or ""),
        }
        if top:
            item["dim"] = top
            item["to"] = _pct(after.get(top))
            item["from"] = _pct(float(after.get(top) or 0.0) - top_delta)
        events.append(item)
    # 引擎是按时间正序追加的；她要看的是最近的，倒过来。
    events.reverse()
    return {"enabled": True, "ok": True, "events": events}


@router.get("/api/mood/status")
async def mood_status(_: None = Depends(require_auth)) -> dict:
    """开屏问一句：第一个 tab 要不要真的能点。"""
    return {"enabled": configured()}
