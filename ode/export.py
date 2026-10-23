"""Production pack: today's plan as machine-readable JSON for your automation.

Each item carries everything an n8n / Make / script workflow needs to start
AI production without you copying anything: platform, language, topic,
why, steps, the other open languages, long-tail questions, app complaints,
proven viral formats and the Gemini brief (titles + outline).

Delivered once per day to N8N_WEBHOOK_URL (POST, JSON). With
N8N_WEBHOOK_SECRET set, the body is signed: header
`X-ODE-Signature: sha256=<hex hmac of the raw body>` so your workflow can
reject anything that did not come from the engine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from ode.config import Config, Secrets
from ode.db import Database
from ode.logging import get_logger
from ode.scoring.actions import LANGUAGE_NAME, PLATFORM_LABEL, Action

log = get_logger(__name__)

PACK_VERSION = 1


def pack_item(a: Action, brief: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": a.action_id,
        "platform": a.platform,
        "platform_label": PLATFORM_LABEL[a.platform],
        "market": a.market,
        "language": a.language,
        "language_name": LANGUAGE_NAME.get(a.language, a.language),
        "topic": a.topic,
        "topic_id": a.topic_id,
        "headline": a.headline,
        "score": a.score,
        "ease": a.ease,
        "value": a.value,
        "demand_from": a.source_market,
        "why": a.why,
        "steps": a.steps,
        "unverified": a.unverified,
        "also_in": [{"language": lg, "language_name": LANGUAGE_NAME.get(lg, lg), "ease": e}
                    for lg, e in a.also_in],
        "questions": a.questions,
        "complaints": a.complaints,
        "formats": a.formats,
        "brief": brief or None,
        "languages": a.languages,
        "dollars_per_month": (None if a.dollars is None else
                              {"low": a.dollars.low, "high": a.dollars.high,
                               "hours": a.dollars.hours, "basis": a.dollars.basis}),
    }


def build_pack(plan: list[Action], briefs: dict[int, dict] | None, day: date) -> dict[str, Any]:
    briefs = briefs or {}
    return {"version": PACK_VERSION, "day": day.isoformat(),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "items": [pack_item(a, briefs.get(i)) for i, a in enumerate(plan)]}


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def make_pack(db: Database, config: Config, day: date) -> dict[str, Any]:
    from ode.scoring import plan as planmod

    plan = await planmod.build_plan(db, config, day)
    briefs = await planmod.attach_briefs(db, plan, int(config.gemini.get("reuse_days", 14)))
    return build_pack(plan, briefs, day)


async def deliver(client: httpx.AsyncClient, url: str, pack: dict[str, Any],
                  secret: str | None = None, timeout: float = 30.0) -> int:
    """POST the pack; returns the HTTP status. The URL is never logged
    (webhook URLs are bearer secrets)."""
    body = json.dumps(pack, ensure_ascii=False, default=str).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-ODE-Signature"] = sign(body, secret)
    resp = await client.post(url, content=body, headers=headers, timeout=timeout)
    return resp.status_code


async def export_job(config: Config, secrets: Secrets, db: Database, client
                     ) -> dict[str, Any]:
    """Write reports/pack-latest.json and, once per day, POST it to n8n."""
    today = datetime.now(UTC).date()
    pack = await make_pack(db, config, today)
    out_dir = Path(os.environ.get("ODE_REPORTS_DIR", "reports"))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(pack, ensure_ascii=False, default=str, indent=2)
        (out_dir / f"pack-{today}.json").write_text(text, encoding="utf-8")
        (out_dir / "pack-latest.json").write_text(text, encoding="utf-8")
    except OSError as exc:
        log.warning("export.write_failed", error=str(exc)[:200])
    if not pack["items"]:
        return {"status": "skipped", "reason": "no plan yet"}
    url = secrets.n8n_webhook_url
    if not url:
        return {"status": "ok", "items": len(pack["items"]), "webhook": "not set"}
    if await db.pool.fetchval("SELECT 1 FROM exports_sent WHERE day = $1 AND target = 'n8n'",
                              today):
        return {"status": "skipped", "reason": "already sent today"}
    max_items = int(config.export.get("max_items", 25))
    pack = {**pack, "items": pack["items"][:max_items]}
    try:
        code = await deliver(client, url, pack, secrets.n8n_webhook_secret)
    except httpx.HTTPError as exc:
        log.warning("export.webhook_failed", error=type(exc).__name__)
        return {"status": "error", "error": f"webhook: {type(exc).__name__}"}
    if code >= 300:
        log.warning("export.webhook_rejected", status=code)
        return {"status": "error", "error": f"webhook answered HTTP {code}"}
    await db.pool.execute("INSERT INTO exports_sent (day, target, items) VALUES ($1, 'n8n', $2) "
                          "ON CONFLICT DO NOTHING", today, len(pack["items"]))
    log.info("export.sent", items=len(pack["items"]))
    return {"status": "ok", "items": len(pack["items"]), "webhook": code}
