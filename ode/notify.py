"""Alerts: Telegram delivery + a dedup log, so nothing pings twice.

Every alert is recorded in `alerts_sent` (key, kind, payload) whether or not
Telegram is configured, so `ode alerts` can always show what the engine
would have told you. A key that was sent within its cooldown is skipped.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx

from ode.collectors.base import make_fetcher
from ode.config import Config, Secrets
from ode.db import Database
from ode.logging import get_logger

log = get_logger(__name__)

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 4000  # Telegram hard limit is 4096 characters


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chunk(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on line boundaries so no message exceeds Telegram's limit."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single giant line
            out.append(cur + line[: limit - len(cur)])
            line, cur = line[limit - len(cur):], ""
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


class Notifier:
    def __init__(self, config: Config, secrets: Secrets, client: httpx.AsyncClient,
                 db: Database) -> None:
        self.db = db
        self.token = secrets.telegram_bot_token
        self.chat = secrets.telegram_chat_id
        self.http = make_fetcher(config, client, "telegram")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    async def _send(self, text: str) -> None:
        for part in chunk(text):
            await self.http.request_json(
                "POST", TG_API.format(token=self.token, method="sendMessage"),
                json_body={"chat_id": self.chat, "text": part, "parse_mode": "HTML",
                           "disable_web_page_preview": True})

    async def send_once(self, key: str, kind: str, text: str, *, cooldown: timedelta,
                        payload: dict[str, Any] | None = None) -> bool:
        """Send unless `key` was sent within `cooldown`. Returns True if sent/logged."""
        recent = await self.db.pool.fetchval(
            "SELECT 1 FROM alerts_sent WHERE key = $1 AND sent_at > now() - $2::interval",
            key, cooldown)
        if recent:
            return False
        if self.enabled:
            try:
                await self._send(text)
            except Exception as exc:  # noqa: BLE001 - never lose the alert record
                log.warning("notify.telegram_failed", key=key, error=str(exc)[:200])
                return False
        else:
            log.info("notify.log_only", key=key, hint="set TELEGRAM_BOT_TOKEN/CHAT_ID")
        await self.db.pool.execute(
            """
            INSERT INTO alerts_sent (key, kind, sent_at, payload) VALUES ($1, $2, now(), $3)
            ON CONFLICT (key) DO UPDATE SET sent_at = now(), payload = EXCLUDED.payload,
                kind = EXCLUDED.kind
            """,
            key, kind, json.dumps({"text": text, **(payload or {})}, ensure_ascii=False,
                                  default=str))
        return True
