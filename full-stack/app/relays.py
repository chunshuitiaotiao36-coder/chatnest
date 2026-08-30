"""Relay (中转站) store: persist multiple API relay configs in /data/relays.json,
switch the active one at runtime by mutating ANTHROPIC_* env vars that Claude
Agent SDK subprocesses read on spawn. Seed from ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN (or OPENAI_* as fallback) plus bundled models.json when the
file is missing.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


logger = logging.getLogger(__name__)
# .info() on a root-attached logger is dropped (no logging config anywhere in
# this project); borrow uvicorn's for lines that must reach the Coolify log.
switch_logger = logging.getLogger("uvicorn.error")

STORE_PATH = Path(os.environ.get("RELAYS_FILE", "/data/relays.json")).expanduser()
_MODELS_SEED_PATH = Path(
    os.environ.get(
        "MODELS_FILE",
        Path(__file__).resolve().parent.parent / "models.json",
    )
).expanduser()

_lock = asyncio.Lock()
_cache: dict | None = None


# ---------- normalization ---------------------------------------------------


def _normalize_model(m: dict) -> dict:
    return {
        "id": str(m.get("id", "")).strip(),
        "label": str(m.get("label", m.get("id", ""))).strip(),
        "desc": str(m.get("desc", "")),
        "thinking": str(m.get("thinking", "adaptive")),
        "primary": bool(m.get("primary", True)),
    }


def _normalize_mode(value: Any) -> str:
    """`api` = go through a relay (ANTHROPIC_* env vars).
    `subscription` = no relay at all; let the CLI use the OAuth login in
    ~/.claude, i.e. the Claude subscription quota."""
    return "subscription" if str(value or "").strip() == "subscription" else "api"


def _normalize_capabilities(c: dict | None) -> dict:
    c = c or {}
    return {
        "streaming": bool(c.get("streaming", True)),
        "cache_control": bool(c.get("cache_control", True)),
        "reasoning": bool(c.get("reasoning", False)),
    }


def _seed_models_from_file() -> list[dict]:
    try:
        raw = json.loads(_MODELS_SEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [_normalize_model(m) for m in raw if str(m.get("id", "")).strip()]


def _seed_from_env() -> dict:
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    api_key = (
        os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    now = int(time.time())
    relay = {
        "id": uuid4().hex,
        "name": "默认（来自环境变量）",
        "base_url": base_url,
        "api_key": api_key,
        "mode": "api",
        "protocol": "openai-compatible",
        "capabilities": _normalize_capabilities(None),
        "models": _seed_models_from_file(),
        "created_at": now,
        "updated_at": now,
    }
    return {"active": relay["id"], "relays": [relay]}


# ---------- persistence -----------------------------------------------------


def _save(state: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(STORE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _load_or_seed() -> dict:
    if not STORE_PATH.exists():
        state = _seed_from_env()
        try:
            _save(state)
        except OSError as exc:
            logger.warning("relays: cannot persist seed to %s: %s", STORE_PATH, exc)
        return state
    try:
        state = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("relays"), list):
            raise ValueError("bad shape")
        if not state["relays"]:
            state = _seed_from_env()
            _save(state)
            return state
        for r in state["relays"]:
            r["mode"] = _normalize_mode(r.get("mode"))  # backfill pre-mode files
        active = state.get("active")
        if not active or all(r.get("id") != active for r in state["relays"]):
            state["active"] = state["relays"][0]["id"]
        return state
    except Exception as exc:
        backup = STORE_PATH.with_suffix(STORE_PATH.suffix + f".corrupt.{int(time.time())}")
        logger.error("relays.json corrupt (%s); backing up to %s and reseeding", exc, backup.name)
        try:
            STORE_PATH.rename(backup)
        except OSError:
            pass
        state = _seed_from_env()
        _save(state)
        return state


# ---------- env application -------------------------------------------------


_ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")


def _set_or_clear(key: str, value: str) -> None:
    """Assignment must be symmetric: leaving a stale var behind is how you end
    up silently talking to the previous relay."""
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


def _cli_base_url(base_url: str) -> str:
    """Claude Code CLI 认的 ANTHROPIC_BASE_URL 是根地址，它自己会拼
    /v1/messages。而中转站首页给的 URL 基本都带 /v1（那是给 OpenAI 格式
    客户端用的），原样设进去就是 /v1/v1/messages —— 404，而且**不报错**：
    SDK 会合成一个空壳 ResultMessage（<synthetic> / model=— / input=0）。

    跟 _models_url() 是同一个问题的两面，那边 07-29 修过，这边漏了。
    两边各自规范化，用户填哪种写法都对；存档里保持原样不动。"""
    b = (base_url or "").strip().rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def _apply_env(relay: dict) -> None:
    if _normalize_mode(relay.get("mode")) == "subscription":
        # Claude Code CLI only falls back to the OAuth credentials in
        # ~/.claude when none of these are set — so unset all three.
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        switch_logger.info("relays: subscription mode active, ANTHROPIC_* cleared")
        return
    api_key = relay.get("api_key") or ""
    _set_or_clear("ANTHROPIC_BASE_URL", _cli_base_url(relay.get("base_url") or ""))
    _set_or_clear("ANTHROPIC_AUTH_TOKEN", api_key)
    _set_or_clear("ANTHROPIC_API_KEY", api_key)
    # 这个洞难查，是因为「环境变量到底被设成了什么」是个黑箱。
    switch_logger.info(
        "relays: env applied base_url=%s token=%s",
        os.environ.get("ANTHROPIC_BASE_URL", "(unset)"),
        _mask_tail(api_key) or "(unset)",
    )


def _active_relay(state: dict) -> dict:
    aid = state.get("active")
    for r in state["relays"]:
        if r["id"] == aid:
            return r
    return state["relays"][0]


# ---------- public read-only ------------------------------------------------


def initialize() -> None:
    """Call once at process startup, before any actor is spawned."""
    global _cache
    _cache = _load_or_seed()
    _apply_env(_active_relay(_cache))


def _mask_tail(key: str) -> str:
    if not key:
        return ""
    return key[-4:] if len(key) > 4 else key


def _public_relay(r: dict, active_id: str) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "base_url": r["base_url"],
        "api_key_tail": _mask_tail(r.get("api_key", "")),
        "mode": _normalize_mode(r.get("mode")),
        "protocol": r.get("protocol", "openai-compatible"),
        "capabilities": _normalize_capabilities(r.get("capabilities")),
        # desc / thinking / primary 必须一起回：前端保存时按 id 从这里继承，
        # 少一个字段就等于每次在弹窗里点一次保存都把 models.json 种子里设的
        # primary:false 和 desc 抹平成默认值。
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "desc": m.get("desc", ""),
                "thinking": m.get("thinking", "adaptive"),
                "primary": bool(m.get("primary", True)),
            }
            for m in r.get("models", [])
        ],
        "active": r["id"] == active_id,
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def list_relays() -> list[dict]:
    active = _cache["active"]
    return [_public_relay(r, active) for r in _cache["relays"]]


def get_active_summary() -> dict:
    relay = _active_relay(_cache)
    return {
        "id": relay["id"],
        "name": relay["name"],
        "mode": _normalize_mode(relay.get("mode")),
        "models": [
            {
                "id": m["id"],
                "label": m["label"],
                "desc": m.get("desc", ""),
                "thinking": m.get("thinking", "adaptive"),
                "primary": bool(m.get("primary", True)),
            }
            for m in relay.get("models", [])
        ],
    }


def active_models_rich() -> list[dict]:
    """Model list in the shape the existing frontend + claude.py expect."""
    return get_active_summary()["models"]


def subscription_models() -> list[dict]:
    """订阅那条线路的模型列表。找不到就返回空——调用方要能接受
    「订阅线路不存在」这件事（小朵可以把它删掉）。

    跟 active_models_rich() 的区别：那个跟着当前激活线路走，这个永远
    指订阅。TG 焊死在订阅上之后，它要的模型必须从订阅那条线路取，而
    订阅那条未必是激活的。"""
    for r in (_cache or {}).get("relays") or []:
        if _normalize_mode(r.get("mode")) == "subscription":
            return r.get("models") or []
    return []


def fallback_relay(exclude_id: str = "") -> dict:
    """后台三条线的**第二条线**：当前这条打不通时换哪一条。

    🔴 为什么需要它：pro 过期那一个多星期，推送和寄相思全哑了。原因是
    background_model() 的回落方向写反了——它的注释写着「回落订阅线路，
    那条永远是她的底仓」，而订阅恰恰是会过期的那一条。订阅一断，
    keepalive/nightguard/回信 三条线同时抛异常，被各自的 except Exception
    吞进日志，她一条消息、一封信都没收到，而日志没有人看。

    这里只做只读的挑选，**不碰 os.environ**。切换激活线路是进程级操作
    （见 _apply_env），后台线程在她聊天的时候动环境变量会串线；第二条线
    改成直接拿这条线路的 base_url + api_key 走 SDK，谁也不影响。

    挑的规则：mode=api、有 key、有模型，排除掉刚失败的那条。
    返回 {} 表示没有第二条线可用——调用方必须能接受这件事。
    """
    for r in (_cache or {}).get("relays") or []:
        if r.get("id") == exclude_id:
            continue
        if _normalize_mode(r.get("mode")) != "api":
            continue
        if not (r.get("api_key") or "").strip():
            continue
        if not (r.get("models") or []):
            continue
        return {
            "id": r["id"],
            "name": r["name"],
            # 直连 SDK 用的是带 /v1 的那种写法由 SDK 自己拼，这里给根地址，
            # 跟 _cli_base_url 同一个规范化，免得又踩 /v1/v1 那个坑。
            "base_url": _cli_base_url(r.get("base_url") or ""),
            "api_key": r.get("api_key") or "",
            "models": [dict(m) for m in (r.get("models") or [])],
        }
    return {}


def active_relay_id() -> str:
    """当前激活线路的 id。第二条线要靠它把「刚失败的那条」排除掉。"""
    try:
        return _active_relay(_cache).get("id", "")
    except Exception:
        return ""


def subscription_summary() -> dict:
    """订阅那条线路的身份，跟 subscription_models() 同一个取法。

    为什么要有它：TG 焊死在订阅上（`subscription_env()` + `subscription_models()`），
    但记账那边拿的是 `get_active_summary()`，返回的是**当前激活**那条。于是小朵
    在小窝切到中转站之后，TG 明明跑在订阅上，每一轮却被贴上中转站的 mode——
    用量面板「订阅额度」那栏长期显示 0 轮。她原话：「我中转站没钱了所以肯定都是
    订阅额度，但是记到 API 那一栏去了」。

    `subscription_env()` 只换环境变量，从来不动「哪条是激活的」，所以这个偏差
    靠它是修不掉的，得在记账那一侧按线路取。

    找不到订阅线路就返回空 dict——调用方要能接受它不存在（她可以把它删掉）。"""
    for r in (_cache or {}).get("relays") or []:
        if _normalize_mode(r.get("mode")) == "subscription":
            return {"id": r["id"], "name": r["name"], "mode": "subscription"}
    return {}


@contextlib.asynccontextmanager
async def subscription_env():
    """TG 专用：这段期间强制走订阅，出去时原样恢复。

    订阅模式靠 pop 掉三个 ANTHROPIC_* 让 CLI fallback 到 ~/.claude 的凭据，
    而环境变量是**进程级**的，没法只对 TG 那条协程单独 unset。

    安全性靠 chat_lock 保证——小窝和 TG 抢的是同一把锁，不会并发，所以不
    存在「TG 把环境变量 pop 了，小窝的请求正好撞上来」。**调用方必须已经
    持有 chat_lock。**

    代价：进出各要 invalidate 一次 actor，所以 TG 和小窝交替时两边都冷启动。
    订阅线路本来就每轮新建子进程（复用会静默哑掉，结过案的老账），所以对 TG
    没有额外代价，代价落在小窝那边。
    """
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        # 无条件恢复：TG 那边抛异常也不能把小窝的环境变量留在被清空的状态，
        # 否则小窝下一条消息会莫名其妙走订阅。
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------- write API -------------------------------------------------------


async def create_relay(payload: dict) -> dict:
    async with _lock:
        now = int(time.time())
        mode = _normalize_mode(payload.get("mode"))
        base_url = str(payload.get("base_url", "")).strip()
        api_key = str(payload.get("api_key", "")).strip()
        # subscription relays legitimately have neither; _apply_env keys off
        # `mode`, so anything stored here simply never gets applied.
        if mode == "api" and (not base_url or not api_key):
            raise ValueError("API 中转站必须填地址和密钥")
        relay = {
            "id": uuid4().hex,
            "name": str(payload.get("name", "")).strip() or "未命名中转站",
            "base_url": base_url,
            "api_key": api_key,
            "mode": mode,
            "protocol": str(payload.get("protocol", "openai-compatible")).strip() or "openai-compatible",
            "capabilities": _normalize_capabilities(payload.get("capabilities")),
            "models": [_normalize_model(m) for m in (payload.get("models") or []) if str(m.get("id", "")).strip()],
            "created_at": now,
            "updated_at": now,
        }
        _cache["relays"].append(relay)
        _save(_cache)
        return _public_relay(relay, _cache["active"])


async def update_relay(relay_id: str, payload: dict) -> dict:
    async with _lock:
        target = next((r for r in _cache["relays"] if r["id"] == relay_id), None)
        if target is None:
            raise KeyError("relay not found")
        # resolve mode/url/key on the side first: validation must not leave a
        # half-updated relay behind in _cache when it rejects.
        mode = _normalize_mode(payload["mode"]) if "mode" in payload else _normalize_mode(target.get("mode"))
        base_url = str(payload["base_url"]).strip() if "base_url" in payload else (target.get("base_url") or "")
        api_key = target.get("api_key") or ""
        if "api_key" in payload:
            new_key = str(payload["api_key"] or "").strip()
            if new_key:  # empty string = keep existing (edit form doesn't resend key)
                api_key = new_key
        # url/key are kept even in subscription mode so flipping back doesn't
        # make her retype the key; _apply_env ignores them while subscribed.
        if mode == "api" and (not base_url or not api_key):
            raise ValueError("API 中转站必须填地址和密钥")
        target["mode"] = mode
        target["base_url"] = base_url
        target["api_key"] = api_key
        if "name" in payload:
            target["name"] = str(payload["name"]).strip() or target["name"]
        if "protocol" in payload:
            target["protocol"] = str(payload["protocol"] or "").strip() or "openai-compatible"
        if "capabilities" in payload and payload["capabilities"] is not None:
            target["capabilities"] = _normalize_capabilities(payload["capabilities"])
        if "models" in payload and payload["models"] is not None:
            target["models"] = [
                _normalize_model(m) for m in payload["models"] if str(m.get("id", "")).strip()
            ]
        target["updated_at"] = int(time.time())
        _save(_cache)
        if _cache["active"] == relay_id:
            _apply_env(target)
        return _public_relay(target, _cache["active"])


async def delete_relay(relay_id: str) -> None:
    async with _lock:
        if len(_cache["relays"]) <= 1:
            raise ValueError("至少要保留一个中转站")
        if _cache["active"] == relay_id:
            raise ValueError("不能删除当前活动中转站")
        _cache["relays"] = [r for r in _cache["relays"] if r["id"] != relay_id]
        _save(_cache)


async def activate(relay_id: str) -> dict:
    async with _lock:
        target = next((r for r in _cache["relays"] if r["id"] == relay_id), None)
        if target is None:
            raise KeyError("relay not found")
        _cache["active"] = relay_id
        _save(_cache)
        _apply_env(target)
        return get_active_summary()


# ---------- models endpoint -------------------------------------------------


def _models_url(base_url: str) -> str:
    """base_url 带不带 /v1 都要落到同一个地方。urljoin 在这里不能用——
    它会把 'https://x.com/v1/' + 'v1/models' 拼成 /v1/v1/models，而中转站的
    base_url 基本都带 /v1，线上这个探测一直在打一个 404 路径。"""
    b = base_url.strip().rstrip("/")
    return b + "/models" if b.endswith("/v1") else b + "/v1/models"


def _auth_headers(api_key: str) -> dict[str, str]:
    """两套认证头一起发，OpenAI 格式和 Anthropic 格式的中转站都能过。"""
    if not api_key:
        return {}
    return {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }


def _extract_models(payload: Any) -> list[dict]:
    """三种见过的格式都要吃下来：
    - OpenAI：{"data":[{"id":"...","object":"model"}]}
    - Anthropic：{"data":[{"id":"claude-...","display_name":"Claude ...","type":"model"}]}
    - 少数站直接返回数组，或者用 models 当键；元素也可能是裸字符串
    """
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or []
    else:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, str):
            mid, disp = it, ""
        elif isinstance(it, dict):
            mid = str(it.get("id") or it.get("name") or "").strip()
            disp = str(it.get("display_name") or "").strip()
        else:
            continue
        if mid:
            out.append({"id": mid, "display_name": disp})
    return out


# 订阅模式没有上游可拉：它直连 Anthropic、用 ~/.claude 里的 OAuth 凭据，
# v1/models 那个端点要的是 API key。给别名而不是完整 ID —— 完整 ID 会过期，
# CLI 自己知道 `opus` 当前指向哪个版本，写死反而会在模型换代时变成一条
# 打不通的线路。别名看不出具体版本这件事由 AssistantMessage.model 解决。
_SUBSCRIPTION_MODELS = [
    {"id": "opus", "display_name": "Opus（订阅当前版）"},
    {"id": "sonnet", "display_name": "Sonnet（订阅当前版）"},
    {"id": "haiku", "display_name": "Haiku（订阅当前版）"},
]

_SUBSCRIPTION_HINT = (
    "订阅线路直连 Anthropic，没有模型列表接口。这三个是 CLI 别名，"
    "实际版本由 CLI 决定——发一条消息就能在顶栏看到真实版本。"
)


async def fetch_models(base_url: str, api_key: str, protocol: str) -> dict:
    """返回 {"ok": bool, "models": [{"id","display_name"}], "detail": str}

    这是一个用户点了会等的按钮，任何情况都要给她一句话看，所以不抛异常。
    超时 10 秒：有些站的 models 端点比连通性探测慢。
    """
    if not base_url:
        return {"ok": False, "models": [], "detail": "缺少地址"}

    url = _models_url(base_url)
    headers = _auth_headers(api_key)

    def _do() -> dict:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                detail = "HTTP 401：密钥不对"
            elif e.code == 403:
                detail = "HTTP 403：密钥没有权限"
            elif e.code == 404:
                detail = f"HTTP 404：这个地址没有模型列表接口（{url}）"
            else:
                detail = f"HTTP {e.code}：上游拒绝了这次请求"
            return {"ok": False, "models": [], "detail": detail}
        except urllib.error.URLError as e:
            return {"ok": False, "models": [], "detail": f"连接失败：{getattr(e, 'reason', e)}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "models": [], "detail": f"错误：{e}"}
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "models": [], "detail": "返回的不是 JSON"}
        models = _extract_models(payload)
        if not models:
            return {"ok": False, "models": [], "detail": "上游返回了 JSON，但里面没有模型列表"}
        return {"ok": True, "models": models, "detail": f"上游 {len(models)} 个模型"}

    return await asyncio.to_thread(_do)


def _stored_key(relay_id: str) -> str:
    """编辑已存在的站时密钥框留空表示「不改」，拉取就用存档里那条的真 key。
    只在进程内用，永远不回给前端。"""
    if not relay_id or _cache is None:
        return ""
    target = next((r for r in _cache["relays"] if r["id"] == relay_id), None)
    return (target or {}).get("api_key", "") or ""


async def fetch_models_for(
    base_url: str, api_key: str, protocol: str, mode: str, relay_id: str
) -> dict:
    """端点用的入口：订阅模式直接返回内置清单，不碰网络。"""
    if _normalize_mode(mode) == "subscription":
        return {
            "ok": True,
            "models": [dict(m) for m in _SUBSCRIPTION_MODELS],
            "detail": _SUBSCRIPTION_HINT,
        }
    return await fetch_models(base_url, api_key.strip() or _stored_key(relay_id), protocol)


# ---------- probe -----------------------------------------------------------


async def probe(base_url: str, api_key: str, protocol: str) -> dict:
    """Minimal reachability check. Any HTTP response = server reachable.
    Connection error / timeout = failure. 5s timeout."""
    if not base_url:
        return {"ok": False, "status": 0, "detail": "缺少地址"}

    # 共用 _models_url：只会让探测更准，不会把原来能过的变成不能过
    # （原来 404 算可达，现在 200 更算可达）。
    url = _models_url(base_url)
    headers = _auth_headers(api_key)

    def _do() -> dict:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return {"ok": True, "status": resp.status, "detail": f"可达（HTTP {resp.status}）"}
        except urllib.error.HTTPError as e:
            # server answered with an HTTP error → endpoint reachable, auth may be wrong
            return {"ok": True, "status": e.code, "detail": f"可达（HTTP {e.code}）"}
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            return {"ok": False, "status": 0, "detail": f"连接失败：{reason}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": 0, "detail": f"错误：{e}"}

    return await asyncio.to_thread(_do)
