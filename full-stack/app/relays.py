"""Relay (中转站) store: persist multiple API relay configs in /data/relays.json,
switch the active one at runtime by mutating ANTHROPIC_* env vars that Claude
Agent SDK subprocesses read on spawn. Seed from ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN (or OPENAI_* as fallback) plus bundled models.json when the
file is missing.
"""

import asyncio
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


def _apply_env(relay: dict) -> None:
    if _normalize_mode(relay.get("mode")) == "subscription":
        # Claude Code CLI only falls back to the OAuth credentials in
        # ~/.claude when none of these are set — so unset all three.
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        switch_logger.info("relays: subscription mode active, ANTHROPIC_* cleared")
        return
    api_key = relay.get("api_key") or ""
    _set_or_clear("ANTHROPIC_BASE_URL", relay.get("base_url") or "")
    _set_or_clear("ANTHROPIC_AUTH_TOKEN", api_key)
    _set_or_clear("ANTHROPIC_API_KEY", api_key)


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
