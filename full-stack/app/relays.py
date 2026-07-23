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
from urllib.parse import urljoin
from uuid import uuid4


logger = logging.getLogger(__name__)

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


def _apply_env(relay: dict) -> None:
    base_url = relay.get("base_url") or ""
    api_key = relay.get("api_key") or ""
    if base_url:
        os.environ["ANTHROPIC_BASE_URL"] = base_url
    if api_key:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = api_key
        os.environ["ANTHROPIC_API_KEY"] = api_key


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
        "protocol": r.get("protocol", "openai-compatible"),
        "capabilities": _normalize_capabilities(r.get("capabilities")),
        "models": [{"id": m["id"], "label": m["label"]} for m in r.get("models", [])],
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
        relay = {
            "id": uuid4().hex,
            "name": str(payload.get("name", "")).strip() or "未命名中转站",
            "base_url": str(payload.get("base_url", "")).strip(),
            "api_key": str(payload.get("api_key", "")).strip(),
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
        if "name" in payload:
            target["name"] = str(payload["name"]).strip() or target["name"]
        if "base_url" in payload:
            target["base_url"] = str(payload["base_url"]).strip()
        if "api_key" in payload:
            new_key = str(payload["api_key"] or "").strip()
            if new_key:  # empty string = keep existing (edit form doesn't resend key)
                target["api_key"] = new_key
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


# ---------- probe -----------------------------------------------------------


async def probe(base_url: str, api_key: str, protocol: str) -> dict:
    """Minimal reachability check. Any HTTP response = server reachable.
    Connection error / timeout = failure. 5s timeout."""
    if not base_url:
        return {"ok": False, "status": 0, "detail": "缺少地址"}

    url = urljoin(base_url.rstrip("/") + "/", "v1/models")
    headers: dict[str, str] = {}
    if api_key:
        # send both auth headers so we probe successfully across OpenAI-format
        # and Anthropic-format relays without caring which one this is.
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"

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
