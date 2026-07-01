"""Client for the local memory search service (memory/search.py @ localhost:3900).

Failures are swallowed silently — memory recall is best-effort context;
it should never block or break the chat flow.
"""
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

SEARCH_URL = "http://127.0.0.1:3900/search"
TIMEOUT = 1.5  # seconds — bail out rather than block the chat
MIN_QUERY_CHARS = 4


def recall(query: str, budget_chars: int = 2000, top_k: int = 8) -> str:
    """Query the memory search service. Returns joined text or '' on failure."""
    q = (query or "").strip()
    if len(q) < MIN_QUERY_CHARS:
        return ""
    try:
        payload = json.dumps(
            {"query": q, "top_k": top_k, "budget_chars": budget_chars},
        ).encode("utf-8")
        req = urllib.request.Request(
            SEARCH_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("joined", "")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        logger.warning("memory recall failed", exc_info=True)
        return ""
