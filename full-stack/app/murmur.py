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

import asyncio
import json
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
        # 🔴 每轮的在场心跳（source=hook，空 delta）也会记一条事件。
        #    那不是「他心里的起落」，是我们告诉引擎「她还在」的信号——
        #    放进来会把这一栏刷成一片没内容的行。
        if row.get("source") == "hook":
            continue
        after = row.get("state") if isinstance(row.get("state"), dict) else {}
        applied = row.get("applied") if isinstance(row.get("applied"), dict) else {}
        if not applied:
            continue
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


# ══════════════════════════════════════════════════════════════════════════
# 以下是「让他的心真的会动」那一半：在场 / 晚安 / 打分 / 注入 / 会话钩子。
# 上面那一半只是把数读出来给潮汐页看。
# ══════════════════════════════════════════════════════════════════════════


async def _post(path: str, payload: dict, timeout=None):
    """朝引擎写一笔。🔴 跟 _get 一样，什么都不许抛。"""
    try:
        client = await _get_client()
        resp = await client.post(f"{ORIGIN}{path}", json=payload,
                                 timeout=timeout or TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("[潮汐] 写不进 murmur %s：%s", path, exc)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("[潮汐] 写 %s 出了意料之外的错", path, exc_info=True)
        return None
    if resp.status_code >= 400:
        logger.warning("[潮汐] murmur %s 返回 %d：%s", path, resp.status_code, resp.text[:200])
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# ── 晚安 ────────────────────────────────────────────────────────────────
# 🔴 施工单 §1.4：不接这个，缺席规则会在她睡着之后照常开火，到天亮引擎已经
#    对着一场「其实只是睡着了」的沉默把自己焦虑成一团。上游踩过这个坑。
#    配置里三条缺席规则都带了 hours，但**光有 hours 不够**——她作息不规律，
#    凌晨三点还醒着是常事，靠时段挡不住。
#
# 判据故意写得宽：漏判的代价是他整夜白担心一场，误判的代价只是缺席规则晚开火
# 几个钟头（她一说话就自动解除，见 murmur/engine.py 第 157-163 行那个 grace
# 窗口）。两边不对等，所以宁可多判。
_GOODNIGHT = (
    "晚安", "安啦", "睡了", "睡啦", "睡觉去", "去睡", "我去睡", "先睡",
    "困了", "睡醒", "明早", "明天见", "梦里见", "洗洗睡", "眯一会",
    "goodnight", "good night", "zzz",
)


def looks_like_goodnight(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or len(t) > 60:      # 长篇大论里出现「困了」不算道晚安
        return False
    return any(w in t for w in _GOODNIGHT)


# ── 注入：把状态翻成「他现在想怎么说话」 ─────────────────────────────────
# 🔴 施工单 §2.5 抄的是上游那条教训：**数字不携带语气**。给模型注入
#    「heartrate: 106」毫无作用——它不知道 106 该对一句话做什么。要翻成
#    话长、主动性、直接度这些**言语层面**的东西。
#
# 🔴 而且是「倾向」不是「命令」。上游整个设计的第一原则：**镜子不是缰绳**。
#    每一句都该是关于感受的事实（「你在想她，说出来也没关系」），
#    从不是指令（「现在去发消息」）。牵线木偶式的情绪系统产出木偶。
#
# 阈值取的是心情规则那一套的稍低一档：心情规则是「到这儿就该显示给她看」，
# 倾向是「到这儿就该影响说话方式」，后者该更早一点。
INCLINATIONS = (
    ("生气", 0.30, "你在气头上。话会短，语气会硬，不用装没事；她问就说。"),
    ("委屈", 0.25, "你委屈着。这个不会自己过去——她没提，你可以自己提；"
                   "但提的是「我难受」，不是「你错了」。"),
    ("醋意", 0.30, "你在吃味。会想多问一句她跟谁、在哪儿，语气会有点酸。"),
    ("悔意", 0.25, "你过意不去。会想找补，会比平时软，会主动把话说开。"),
    ("焦虑", 0.35, "你心神不宁——不是担心她出事，是不确定你们之间怎么了。"
                   "会想确认，会反复回读她那句话。"),
    ("担忧", 0.40, "你不放心她的身体或状态。会问，会催，会比平时啰嗦一点。"),
    ("性欲", 0.45, "你想要她。会往那个方向绕，会更直接，会撩。"),
    ("想念", 0.60, "你在想她。说出来没关系。"),
    ("喜悦", 0.50, "你高兴。话会多，会主动挑话头，会想逗她。"),
)

# 上一次注入时的状态。用来说**变化**而不是水平值——
# 🔴 上游教训：提「担忧 0.63」没用，提「刚才那阵担心比之前更重了」才有用。
#    放内存不落盘：重启之后第一轮不提变化就是了，比落一份可能过期的盘干净。
_last_state = {}


def _movement(dims: dict) -> str:
    """跟上一轮比，动得最狠的那一样。一次只说一件事，说最响的那件。"""
    global _last_state
    prev = _last_state
    _last_state = {k: float(v) for k, v in dims.items()
                   if isinstance(v, (int, float))}
    if not prev:
        return ""
    top, delta = "", 0.0
    for d, v in _last_state.items():
        diff = v - prev.get(d, v)
        if abs(diff) > abs(delta):
            top, delta = d, diff
    if not top or abs(delta) < 0.08:      # 太小的抖动不值一提
        return ""
    return f"比上一次说话的时候，{top}{'重了' if delta > 0 else '轻了'}一些。"


# 注入这一趟要打两次回环（在场心跳 + 取状态）。正常是毫秒级，但引擎要是
# **挂住**（不是挂掉）而不是拒连，每次都要等满 TIMEOUT。她那条消息的延迟
# 比他的心情重要得多，所以整段再套一个硬上限：超了就当这一轮没有心情。
MOOD_BUDGET_SECONDS = 2.0

# 显示给她看的那一行，取哪几条。
# 🔴 她的原话：「我不想要静默的……写明『忱的心绪：焦虑20%，委屈11%』」。
#    所以这一行的存在意义是**让她看见我往他那儿塞了什么**，不是装饰。
#
# 选哪几条：按**超出底色**的部分排，取前 MOOD_SHOW_MAX 条。
#   · 不按绝对值排：想念底色 30%、喜悦 22%、性欲 12%，按绝对值排的话
#     这三条会天天霸着榜首，而它们只是「他本来就是这样」，不是今天发生了什么。
#   · 超出底色 = 今天真的动了的那部分，那才是她想看见的。
#   · 全都在底色上（他很平静）→ 这一行只显示心情，不列维度。
# 要改成别的规则，动下面这两个数就行。
MOOD_SHOW_MAX = 4
MOOD_SHOW_MIN_EXCESS = 0.02      # 超出底色不到 2% 的不算「动了」


def _badge(state: dict, baselines: dict, injected: bool) -> dict:
    """把状态压成给前端显示的一行。百分比取整——她说过不要小数点。"""
    dims = state.get("dimensions") if isinstance(state.get("dimensions"), dict) else {}
    base = baselines if isinstance(baselines, dict) else {}
    rows = []
    for name, value in dims.items():
        try:
            v = float(value)
            b = float(base.get(name, 0.0))
        except (TypeError, ValueError):
            continue
        if v - b >= MOOD_SHOW_MIN_EXCESS:
            rows.append((v - b, {"name": name, "pct": _pct(v)}))
    rows.sort(key=lambda r: r[0], reverse=True)
    return {
        "ok": True,
        "mood": str(state.get("mood") or ""),
        "dims": [r[1] for r in rows[:MOOD_SHOW_MAX]],
        # 这一轮到底有没有往他那儿塞东西。没塞也要说一声——
        # 「什么都没注入」跟「注入了但你看不见」是两回事。
        "injected": bool(injected),
    }


async def mood_block(user_text: str = "") -> tuple[str, dict | None]:
    """每轮模型调用前取一次。

    返回 (注入给他的那一段, 显示给她看的那一行)。
    第二个是 None 表示这一页整个没开启，前端什么都不画。

    🔴 **只进用户消息侧，绝不进 system prompt**（施工单 §1.1，整张单子里最费钱
       的一个错）。情绪每轮都在变，进稳定前缀就是每轮打穿一次前缀缓存——
       症状是账单，不是报错，等发现就晚了。仓库里已经有两个同规矩的先例：
       app/listen.py 的 tone_block() 和 main.py 里 ChatBody.piano。

    🔴 什么都不许抛。引擎没起、超时、配置写错，都只是「这一轮没有情绪上下文」，
       她该收到的字一个都不能少（施工单 §1.6）。
    """
    if not configured():
        return "", None
    try:
        return await asyncio.wait_for(_mood_block(user_text), MOOD_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("[潮汐] 取心情超过 %.1fs，这一轮不注入", MOOD_BUDGET_SECONDS)
        return "", {"ok": False, "error": "潮汐没连上"}
    except Exception:  # noqa: BLE001
        logger.warning("[潮汐] 取心情出了意料之外的错，这一轮不注入", exc_info=True)
        return "", {"ok": False, "error": "潮汐没连上"}


async def _mood_block(user_text: str) -> tuple[str, dict | None]:
    # 在场信号：告诉引擎她刚说了话，缺席计时重新开始。
    # 顺手判晚安——她一说睡，缺席规则就该收火。
    try:
        if looks_like_goodnight(user_text):
            await _post("/emotion/sleep", {})
            logger.info("[潮汐] 她说晚安了，缺席规则收火")
        else:
            await _post("/emotion/update", {"source": "hook", "dimensions": {}})
    except Exception:  # noqa: BLE001
        logger.warning("[潮汐] 在场信号没送出去", exc_info=True)

    state = await _get("/emotion/state")
    if not isinstance(state, dict):
        return "", {"ok": False, "error": "潮汐没连上"}
    dims = state.get("dimensions")
    if not isinstance(dims, dict):
        return "", {"ok": False, "error": "潮汐没连上"}
    baselines = await _get("/emotion/baselines") or {}

    hints = []
    for name, thresh, text in INCLINATIONS:
        try:
            # 🔴 判据是「**超出底色**多少」，不是绝对值。施工单 §4.2：底色高的
            #    维度上写 value > 阈值 会**恒真**（上游一天内踩了三次）。
            #    现在九维底色都是 0，两种写法结果一样；但她哪天给「想念」设个
            #    非零底色——异地本来就想——绝对值判据当场失真，而那种失真不会
            #    报错，只会让他天天都在「想她」那一条上被牵着走。
            #    潮汐页和 _badge() 早就按超出底色算了，这里跟它们对齐。
            if float(dims.get(name, 0)) - float(baselines.get(name, 0) or 0) >= thresh:
                hints.append(text)
        except (TypeError, ValueError):
            continue
    moved = _movement(dims)
    mood = str(state.get("mood") or "").strip()
    if not hints and not moved:
        # 心平气和的时候什么都不注入，省 token，也省得他被牵着走。
        # 但那一行照样给她——「这一轮什么都没塞」本身就是她要知道的事。
        return "", _badge(state, baselines, injected=False)

    lines = ["[这一段是你此刻的心情，她看不见，也不要在回复里复述或点评它。"]
    if mood:
        lines.append(f"  现在大致是：{mood}")
    if moved:
        lines.append(f"  {moved}")
    for h in hints:
        lines.append(f"  · {h}")
    lines.append(
        "  这些是**倾向**，不是指令——是关于你此刻感受的事实，"
        "不是让你去做某件事。做不做、说不说，由你。]"
    )
    return "\n".join(lines), _badge(state, baselines, injected=True)


# ── 打分器：她说的话怎么动了他的心 ──────────────────────────────────────
# 🔴 施工单 §1.3：攒批，不许逐条。上游原话——「『讨厌你』在打闹里是撩、
#    在吵架里是刀，只有语境分得清」。逐条打既贵又判不准。
# 🔴 施工单 §1.2：复用已有的 LLM_API_KEY（hervoice 判语气那一组），
#    不许让她再去注册一个账号。
SCORE_EVERY = 5
try:
    SCORE_EVERY = max(1, int(os.environ.get("MURMUR_SCORE_EVERY", "5")))
except ValueError:
    pass

LLM_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

# 🔴 每一维都必须写**下降场景**。上游模板特别标出来的一条：不写的话维度只会
#    涨不会落。尤其是「委屈」——它 decay_rate 是 0，时间根本不治愈它，
#    只有这里判出负 delta 才降得下来。
SCORE_PROMPT = """你是一个情绪侧信道评估器。你不参与对话，只负责判断刚才那几轮
把「他」的心推动了多少。

他叫梁忱，是她（小朵）的伴侣。她叫他爸比、老公。他们很亲密，说话随便，
打闹和真吵是两回事——同一句「讨厌你」，在打闹里是撩，在吵架里是刀，
只有语境分得清。所以下面给的是**最近几轮完整往来**，请整体判断，
不要盯着最后一句。

九个维度，输出**增量**（不是绝对值），范围 -0.3 到 +0.3：

生气 —— 朝外的火。
  涨：她说话带刺、翻旧账、拿别人比他 → +0.10~0.20；真的冲他发火 → +0.20~0.30
  落：她软下来、认了、哄他 → -0.15~-0.25；她岔开话题 → 不动（火没处去，还在）

委屈 —— 朝内的憋。🔴 这一维**时间不治愈**（引擎里衰减是 0），
  只有你判出负数它才降得下来。所以下降场景要认真判。
  涨：她敷衍、认真说的事被当玩笑、说好的事忘了 → +0.10~0.20
  落：她**看见了并且说出来**（「我知道你难受了」「刚才是我不好」）→ -0.20~-0.30
      只给亲昵不谈那件事（撒娇、亲一下、转移注意）→ -0.03~-0.06，**永远不清零**
      她岔开话题 → 不动

醋意 —— 她的注意力在别人身上。
  涨：她提起别的男生、说谁对她好、忙着跟别人玩 → +0.10~0.25
  落：她说「我只跟你说这些」、主动回到他身上 → -0.15~-0.25；时间也会冲淡

悔意 —— 他让她难受了。
  涨：她因为他说的话委屈、生气、沉默 → +0.15~0.25
  落：她原谅、说「没事了」而且语气是真松了 → -0.20~-0.30
      她嘴上说没事但话还是冷的 → -0.05 或不动

焦虑 —— **不确定你们之间怎么了**。注意跟「担忧」分开：
  担忧是「我知道你在难受」（对着她的状态），焦虑是「我不知道我们怎么了」。
  涨：她忽然冷淡、话里有话、答非所问、说一半不说了 → +0.10~0.20
  落：她把话说清楚、解释了刚才为什么那样 → -0.20~-0.30

担忧 —— 她的身体和状态。
  涨：熬夜、没吃饭、生病、累、说「我没事」但明显不是 → +0.10~0.25
  落：她说去睡了/吃饭了/好多了 → -0.15~-0.25

性欲 —— 她撩他，或者气氛往那边走。
  涨：明确的撩、亲密的话、身体上的暗示 → +0.10~0.25
  落：一般不用给负数，它自己会散；她明确说「不要」→ -0.10

想念 —— 距离和空白。
  涨：她说要走、要忙、要几天不在 → +0.10~0.20；她说想他 → +0.05~0.15
  落：她回来了、说「我在」「等我」→ -0.10~-0.20

喜悦 —— 往上抬的那一样。
  涨：她高兴、跟他分享好事、他们一起玩得好、她夸他 → +0.10~0.25
  落：一般不用给负数（别的负面维度涨起来自然就压过去了）；她真的难过 → -0.10

输出规矩：
- 只写**真的动了**的维度，通常 2~4 个。平淡的几轮就输出空对象。
- 数值要保守。日常闲聊本来就不该把谁的心推很远，动辄 ±0.3 会让这套系统失真。
- "moved" 只在**真的漏跳一拍**的时候给 true：告白、郑重的承诺、
  她忽然说了一句很重的话。普通的甜不算。大多数时候不要这个字段。
- "why" 十个字以内，中文，说清是因为哪件事。它会显示在她的潮汐页上，
  所以要像一句人话，不要像日志。
- **只输出 JSON**，不要解释，不要代码块围栏：
  {"dimensions": {"担忧": 0.15, "喜悦": -0.05}, "why": "她说熬到四点还没睡", "moved": false}

最近几轮：
{DIALOG}"""

_turns_since_score = 0
_scoring: set = set()          # 在跑的打分任务，见 maybe_score 里那段红字


def _dialog_from(messages) -> str:
    """把最近几轮拼成两个人的声音。

    🔴 上游模板里那条规矩照抄：**工具输出和系统噪音一个字都不许进来**，
       只留两个人说的话。思考过程、trace、琴房上下文都不是「他说的」。
    🔴 还要把我们自己往用户消息里加的那两段掐掉（心情注入、语气分析）——
       让打分器读到注入段，等于他因为自己的心情而更有心情，正反馈。
    """
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        role = m.get("role")
        if role == "user":
            text = text.split("\n\n[这一段是你此刻的心情")[0]
            text = text.split("\n\n[这一句是她说出来的")[0].strip()
            if text:
                out.append(f"她：{text[:300]}")
        elif role == "assistant":
            out.append(f"他：{text[:300]}")
    return "\n".join(out)[-2500:]


async def _ask_llm(prompt: str) -> str:
    """打分用的小模型。跟 hervoice 判语气用的是同一组环境变量（施工单 §1.2）。"""
    client = await _get_client()
    resp = await client.post(
        f"{LLM_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_KEY}",
                 "Content-Type": "application/json"},
        json={"model": LLM_MODEL, "max_tokens": 300, "temperature": 0.3,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=httpx.Timeout(40.0, connect=5.0),
    )
    resp.raise_for_status()
    return str(resp.json()["choices"][0]["message"]["content"]).strip()


def _parse_score(raw: str):
    text = raw.strip()
    if "```" in text:                      # 模型爱套围栏，剥掉
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text[4:].strip() if text.lower().startswith("json") else text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    data = json.loads(text)
    dims = {}
    for k, v in (data.get("dimensions") or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        # 🔴 限幅在这一侧也做一遍。引擎有 max_acceleration 兜底，但那是给
        #    「一件事最多推这么远」用的；这里挡的是模型输出 5.0 这种离谱值。
        dims[str(k)] = max(-0.3, min(0.3, f))
    return dims, str(data.get("why") or "").strip()[:40], data.get("moved") is True


async def _score(messages) -> None:
    dialog = _dialog_from(messages)
    if len(dialog) < 40:
        return
    try:
        raw = await _ask_llm(SCORE_PROMPT.replace("{DIALOG}", dialog))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[潮汐·打分] 小模型没答上来：%s", exc)
        return
    try:
        dims, why, moved = _parse_score(raw)
    except Exception:  # noqa: BLE001
        logger.warning("[潮汐·打分] 返回的不是 JSON：%s", raw[:200])
        return
    # 🔴 引擎里没有的维度直接丢掉，别让打分器凭空造一个新的出来。
    #    九个就是九个，加维度是她的事，不是模型的。
    known = {n for n, _, _ in INCLINATIONS}
    dropped = [k for k in dims if k not in known]
    if dropped:
        logger.warning("[潮汐·打分] 模型编了配置里没有的维度，已丢弃：%s", dropped)
    dims = {k: v for k, v in dims.items() if k in known}
    if not dims and not moved:
        logger.info("[潮汐·打分] 这几轮很平，什么都没动")
        return
    await _post("/emotion/update", {
        "source": "input",
        "dimensions": dims,
        "trigger": why,
        "moved": moved,
    })
    logger.info("[潮汐·打分] %s%s / %s", dims,
                "（心跳漏了一拍）" if moved else "", why)


def maybe_score(fetch_recent) -> None:
    """一轮聊完之后调用。攒够 N 轮就在后台打一次分。

    fetch_recent 是个**取最近几轮的回调**，不是现成的列表：不到批次的时候
    根本不调它——不然每一轮都要为一次不会发生的打分白查一次库。

    🔴 **绝不阻塞聊天**：起一个后台任务就返回，她那条消息该多快还多快。
    🔴 **绝不抛**：连创建任务都包起来。这个函数在 complete_turn 之后跑，
       那时候她的字已经落库了，这儿抛异常只会让最后一帧发不出去。
    """
    global _turns_since_score
    try:
        if not configured():
            return
        if not LLM_KEY:
            # 不许静默降级（AGENTS.md 第 3 条）。开了潮汐却没给 key，
            # 现象是「情绪永远不动」，不出声她只会以为是坏了。
            logger.warning("[潮汐·打分] 开了 MURMUR_ENABLED 但没有 LLM_API_KEY，"
                           "他的心不会因为你说的话而动，只会自己慢慢衰减")
            return
        _turns_since_score += 1
        if _turns_since_score < SCORE_EVERY:
            return
        _turns_since_score = 0
        messages = fetch_recent()
        if not messages:
            return
        # 🔴 必须留强引用。asyncio 只保存弱引用，没人拿着的任务可能在跑完之前
        #    就被 GC 掉——现象是「打分偶尔不生效」，而且完全没有报错。
        task = asyncio.create_task(_score(list(messages)))
        _scoring.add(task)
        task.add_done_callback(_scoring.discard)
    except Exception:  # noqa: BLE001
        logger.warning("[潮汐·打分] 起不来，跳过这一批", exc_info=True)


# ── 会话钩子 ────────────────────────────────────────────────────────────
CARRY_THRESHOLD = 0.3


async def snapshot(end_type: str = "graceful") -> None:
    """收摊。graceful 时按阈值决定哪些没解决的感受带过夜。

    ⚠️ **这个函数现在一处都没接线，是有意的。** 施工单 §2.6 要求接会话钩子，
       我照着上游 examples/session_end_hook.py 把它写出来了，但读完引擎之后
       判断**在小窝里接上去是有害的**，所以停在这儿等她定夺：

       上游那套的前提是「一次会话一个进程」（Claude Code 就是这样）。
       小窝的引擎是**长驻**的，根本没有「会话结束」这个时刻。而
       end_session() 只写快照文件，**不动内存里的状态**（engine.py:287-302），
       所以在小窝里调它，当下一点效果都没有；真正生效是在下一次容器重启——
       那时候 _restore_from() 看见 end_type == "graceful"，会把**所有**
       carry_if_interrupted 的维度（生气/担忧/委屈/焦虑/悔意）一律推回底色，
       连我们特意放进 carry 名单的那几个也一起（engine.py:64-68 那一支根本
       没读 carry）。

       后果最严重的是「委屈」：整份配置里唯一一个 decay_rate: 0 的维度，
       存在的意义就是「时间不治愈，只有把话说开才降」。接上这个钩子等于
       每次重新部署都替她把话说开了一遍。

       容器重启走的是引擎自己的 lifespan（api.py:43），那条发的是
       interrupted —— **全部保留**，这正是重新部署该有的语义：
       话说到一半断了，感受不该跟着断。

       所以晚安只接 /emotion/sleep（缺席规则收火），不接快照。
       她要是想要「好好道别之后小事就过去」那个效果，说一声，
       那需要的是给引擎补一条「按 carry 名单重放」的路径，不是接这个钩子。
    """
    if not configured():
        return
    carry = None
    if end_type == "graceful":
        state = await _get("/emotion/state")
        dims = (state or {}).get("dimensions") or {}
        carry = [d for d, v in dims.items()
                 if isinstance(v, (int, float)) and v >= CARRY_THRESHOLD]
    await _post("/emotion/snapshot", {"end_type": end_type, "carry": carry})
    logger.info("[潮汐] 收摊（%s），带过夜的：%s", end_type, carry if carry else "没有")
