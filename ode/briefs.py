"""Gemini briefs for the top actions of the day (optional; GEMINI_API_KEY).

For each of the best few plan items without a recent brief: 10 titles in the
target language, an outline with retention beats, the recommended format,
the monetization path with reasoning, and estimated production hours.
Capped per day; every call is rate-limited and recorded in collector_runs.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import httpx

from ode.collectors.base import make_fetcher
from ode.config import Config, Secrets
from ode.db import Database
from ode.logging import get_logger
from ode.scoring.actions import LANGUAGE_NAME, PLATFORM_LABEL, Action

log = get_logger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def build_prompt(a: Action) -> str:
    lang = LANGUAGE_NAME.get(a.language, a.language)
    why = "\n".join(f"- {w}" for w in a.why)
    return f"""You are a content strategist. Plan ONE piece of work.

Platform: {PLATFORM_LABEL[a.platform]}
Language and audience: {lang} ({a.market})
Topic: {a.topic}
Evidence:
{why}

Rules: monetization must be one of: direct sponsors, the creator's own product
(template / course / script), a Google Play app, or ads. NO affiliate links,
NO SaaS. Prefer tutorial / build / how-to angles over comparisons or reviews.

Return ONLY JSON with exactly these keys:
{{"titles": [10 strings, written in {lang}, concrete and click-worthy, no clickbait lies],
  "outline": "markdown bullet outline with retention beats (hook, payoff, next step)",
  "format": "long-form video | short | blog | app | product",
  "monetization": "path + one-sentence reason",
  "hours": number (realistic production hours for one person)}}"""


def parse_brief(payload: Any) -> dict[str, Any]:
    """Gemini generateContent response -> validated brief dict (raises ValueError)."""
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("no candidate text in Gemini response") from exc
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    titles = [str(t).strip() for t in data.get("titles") or [] if str(t).strip()][:10]
    if not titles:
        raise ValueError("brief has no titles")
    outline = str(data.get("outline") or "").strip()
    extra = []
    if data.get("monetization"):
        extra.append(f"**Monetization:** {data['monetization']}")
    if data.get("hours") is not None:
        extra.append(f"**Estimated hours:** {data['hours']}")
    return {"titles": titles, "outline": "\n\n".join([outline, *extra]).strip(),
            "format": str(data.get("format") or "").strip()[:60]}


async def generate_briefs(db: Database, config: Config, secrets: Secrets, plan: list[Action],
                          client: httpx.AsyncClient) -> dict[str, Any]:
    g = config.gemini
    if not secrets.gemini_api_key:
        return {"status": "skipped", "reason": "GEMINI_API_KEY not set"}
    limit = int(g.get("max_briefs_per_day", 5))
    reuse = int(g.get("reuse_days", 14))
    today = datetime.now(UTC).date()
    done_today = await db.pool.fetchval(
        "SELECT count(*) FROM briefs WHERE generated_at::date = $1", today)
    budget = max(0, limit - int(done_today or 0))
    run_id = await db.start_run("briefs", None)
    fetcher = make_fetcher(config, client, "gemini")
    url = API.format(model=g.get("model", "gemini-2.5-flash"))
    made, errors = 0, 0
    for a in plan:
        if made >= budget:
            break
        if a.topic_id is None:
            continue
        platform_key = f"{a.platform}:{a.language}"
        recent = await db.pool.fetchval(
            "SELECT 1 FROM briefs WHERE topic_id = $1 AND platform = $2 "
            "AND generated_at > now() - make_interval(days => $3)",
            a.topic_id, platform_key, reuse)
        if recent:
            continue
        try:
            payload = await fetcher.request_json(
                "POST", url, headers={"x-goog-api-key": secrets.gemini_api_key},
                json_body={"contents": [{"role": "user", "parts": [{"text": build_prompt(a)}]}],
                           "generationConfig": {"responseMimeType": "application/json",
                                                "temperature": 0.7}})
            brief = parse_brief(payload)
        except Exception as exc:  # noqa: BLE001 - one brief must not stop the rest
            errors += 1
            log.warning("briefs.failed", topic=a.topic, error=str(exc)[:300])
            if fetcher.circuit_open:
                break
            continue
        await db.pool.execute(
            "INSERT INTO briefs (topic_id, generated_at, titles, outline, format, platform) "
            "VALUES ($1, now(), $2, $3, $4, $5)",
            a.topic_id, json.dumps(brief["titles"], ensure_ascii=False), brief["outline"],
            brief["format"], platform_key)
        made += 1
    status = "error" if errors and not made else ("partial" if errors else "ok")
    await db.finish_run(run_id, status=status, items=made, errors=errors)
    return {"status": status, "made": made, "errors": errors}


async def run_briefs(config: Config, secrets: Secrets, as_of: date | None = None) -> dict:
    from ode.pipeline import http_client
    from ode.scoring.plan import build_plan

    db = await Database.connect(secrets.database_url)
    try:
        plan = await build_plan(db, config, as_of or datetime.now(UTC).date())
        async with http_client(config) as client:
            return await generate_briefs(db, config, secrets, plan, client)
    finally:
        await db.close()
