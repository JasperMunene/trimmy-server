"""Sending Expo push notifications (BK-41)."""
import logging
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

EXPO_BATCH = 100  # Expo accepts up to 100 messages per request


def is_expo_token(token: str) -> bool:
    return token.startswith(("ExponentPushToken[", "ExpoPushToken[")) and token.endswith("]")


async def send_expo(messages: list[dict[str, Any]]) -> list[str]:
    """Send messages to Expo. Returns the tokens Expo says are no longer registered."""
    if not messages:
        return []
    if not settings.EXPO_PUSH_URL:
        for m in messages:
            logger.info(f"[push not sent: no EXPO_PUSH_URL] to={m['to']} title={m.get('title')!r} data={m.get('data')}")
        return []

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if settings.EXPO_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {settings.EXPO_ACCESS_TOKEN}"
    dead: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for i in range(0, len(messages), EXPO_BATCH):
            batch = messages[i:i + EXPO_BATCH]
            resp = await client.post(settings.EXPO_PUSH_URL, json=batch, headers=headers)
            resp.raise_for_status()  # the task retries
            for message, ticket in zip(batch, resp.json().get("data", [])):
                if ticket.get("status") == "error":
                    error = (ticket.get("details") or {}).get("error")
                    if error == "DeviceNotRegistered":
                        dead.append(message["to"])
                    else:
                        logger.warning(f"Expo push error for {message['to']}: {ticket.get('message')} ({error})")
    return dead


def message(token: str, title: str, body: str, data: Optional[dict] = None) -> dict[str, Any]:
    return {"to": token, "title": title, "body": body, "data": data or {}, "sound": "default"}
