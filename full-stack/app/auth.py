import hashlib
import hmac
import os


CHAT_PASSWORD = os.environ["CHAT_PASSWORD"]
CHAT_SECRET = os.environ["CHAT_SECRET"]


def issue_token(password: str) -> str | None:
    if hmac.compare_digest(password, CHAT_PASSWORD):
        return hmac.new(
            CHAT_SECRET.encode(),
            b"chat-v1",
            hashlib.sha256,
        ).hexdigest()
    return None


def verify_token(token: str) -> bool:
    expected = hmac.new(
        CHAT_SECRET.encode(),
        b"chat-v1",
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(token, expected)
