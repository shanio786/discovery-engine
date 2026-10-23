"""One production, many languages: a ready pack for every `also_in` language.

For the top plan items (max `max_items` per day) and each other language
where the same topic is open, Gemini writes the pack in that language:

    keyword     how people there actually search for it (not a word-for-word
                translation)
    titles      10 titles
    questions   8 questions to answer (articles / videos / shorts)

Then the keyword is checked against Google Autocomplete IN THAT LANGUAGE
AND COUNTRY: the real suggestions are stored next to the AI's guesses, so
you see what people really type. Packs are reused for `reuse_days`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from ode.collectors.base import make_fetcher
from ode.config import Config, Secrets
from ode.db import Database
from ode.logging import get_logger
from ode.scoring.actions import LANGUAGE_NAME, PLATFORM_LABEL, Action

log = get_logger(__name__)


def build_prompt(a: Action, languages: list[str], brief_titles: list[str]) -> str:
    src = LANGUAGE_NAME.get(a.language, a.language)
    langs = ", ".join(f"{lg} ({LANGUAGE_NAME.get(lg, lg)})" for lg in languages)
    qs = "\n".join(f"- {q}" for q in a.questions[:10]) or "- (none yet)"
    titles = "\n".join(f"- {t}" for t in brief_titles[:5]) or "- (none yet)"
    return f"""Localize ONE content idea for several languages.

Platform: {PLATFORM_LABEL[a.platform]}
Topic: {a.topic} (source language: {src})
Questions people ask in {src}:
{qs}
Existing titles in {src}:
{titles}

For EACH of these languages: {langs}
write what a native creator in that country would use — not a literal
translation: the search keyword people there really type, 10 titles, and 8
questions to answer. The creator produces everything with AI; no affiliate,
no product selling.

Return ONLY JSON:
{{"packs": [{{"language": "<code>", "keyword": "...", "titles": ["..."],
             "questions": ["..."]}}]}}"""


def parse_packs(payload: Any, languages: list[str]) -> dict[str, dict[str, Any]]:
    """Gemini response -> {language: {keyword, titles, questions}} (only asked languages)."""
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("no candidate text in Gemini response") from exc
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    out: dict[str, dict[str, Any]] = {}
    for p in data.get("packs") or []:
        lg = str(p.get("language") or "").split("-")[0].lower()
        titles = [str(t).strip() for t in p.get("titles") or [] if str(t).strip()][:10]
        if lg not in languages or not titles:
            continue
        out[lg] = {"keyword": str(p.get("keyword") or "").strip()[:120] or None,
                   "titles": titles,
                   "questions": [str(q).strip() for q in p.get("questions") or []
                                 if str(q).strip()][:8]}
    return out


async def _verify(client: httpx.AsyncClient, config: Config, market_code: str,
                  keyword: str) -> list[str]:
    """Real Google Autocomplete suggestions for the keyword in that market."""
    from ode.miners import SUGGEST, parse_suggestions, suggest_seeds

    market = config.market(market_code)
    fetcher = make_fetcher(config, client, "autocomplete")
    found: dict[str, None] = {}
    for seed in suggest_seeds(keyword.casefold(), market.language, letters=False)[:4]:
        payload = await fetcher.get_json(SUGGEST, params={
            "client": "firefox", "hl": market.language, "gl": market.region.lower(),
            "ie": "utf-8", "oe": "utf-8", "q": seed})
        for s in parse_suggestions(payload):
            if s.casefold() != keyword.casefold():
                found.setdefault(s, None)
    return list(found)[:15]


async def langpacks_job(db: Database, config: Config, secrets: Secrets,
                        client: httpx.AsyncClient) -> dict[str, Any]:
    from ode.briefs import API
    from ode.scoring.plan import build_plan

    c = config.langpacks
    if not c.get("enabled", True):
        return {"status": "skipped", "reason": "langpacks.enabled = false"}
    if not secrets.gemini_api_key:
        return {"status": "skipped", "reason": "GEMINI_API_KEY not set"}
    today = datetime.now(UTC).date()
    lang_market: dict[str, str] = {}
    for m in config.markets():
        lang_market.setdefault(m.language, m.code)
    plan = await build_plan(db, config, today, save=False)
    reuse = int(c.get("reuse_days", 30))
    fetcher = make_fetcher(config, client, "gemini")
    url = API.format(model=config.gemini.get("model", "gemini-2.5-flash"))
    made = items = errors = verified = 0
    for a in plan:
        if items >= int(c.get("max_items", 5)):
            break
        langs = [lg for lg, _ in a.also_in][: int(c.get("max_languages", 6))]
        if not langs:
            continue
        have = {r["language"] for r in await db.pool.fetch(
            "SELECT language FROM language_packs WHERE action_id = $1 "
            "AND generated_at > now() - make_interval(days => $2)", a.action_id, reuse)}
        todo = [lg for lg in langs if lg not in have]
        if not todo:
            continue
        items += 1
        brief = await db.pool.fetchval(
            "SELECT titles FROM briefs WHERE topic_id = $1 ORDER BY generated_at DESC LIMIT 1",
            a.topic_id) if a.topic_id else None
        brief_titles = (brief if isinstance(brief, list) else json.loads(brief)) if brief else []
        try:
            payload = await fetcher.request_json(
                "POST", url, headers={"x-goog-api-key": secrets.gemini_api_key},
                json_body={"contents": [{"role": "user", "parts": [
                    {"text": build_prompt(a, todo, brief_titles)}]}],
                    "generationConfig": {"responseMimeType": "application/json",
                                         "temperature": 0.6}})
            packs = parse_packs(payload, todo)
        except Exception as exc:  # noqa: BLE001 - one item must not stop the rest
            errors += 1
            log.warning("langpacks.failed", topic=a.topic, error=str(exc)[:200])
            if fetcher.circuit_open:
                break
            continue
        for lg, p in packs.items():
            real: list[str] = []
            if p["keyword"] and lg in lang_market and c.get("verify", True):
                try:
                    real = await _verify(client, config, lang_market[lg], p["keyword"])
                    verified += bool(real)
                except Exception as exc:  # noqa: BLE001 - verification is a bonus
                    log.debug("langpacks.verify_failed", error=str(exc)[:200])
            await db.pool.execute(
                """
                INSERT INTO language_packs (action_id, language, keyword, titles, questions,
                                            verified, generated_at)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, now())
                ON CONFLICT (action_id, language) DO UPDATE SET keyword = EXCLUDED.keyword,
                    titles = EXCLUDED.titles, questions = EXCLUDED.questions,
                    verified = EXCLUDED.verified, generated_at = now()
                """,
                a.action_id, lg, p["keyword"], json.dumps(p["titles"], ensure_ascii=False),
                json.dumps(p["questions"], ensure_ascii=False),
                json.dumps(real, ensure_ascii=False))
            made += 1
    status = "error" if errors and not made else "ok"
    return {"status": status, "items": items, "packs": made, "verified": verified,
            "errors": errors}


async def load_packs(db: Database, action_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    rows = await db.pool.fetch(
        "SELECT * FROM language_packs WHERE action_id = ANY($1) ORDER BY language", action_ids)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        def j(v: Any) -> list[str]:
            return v if isinstance(v, list) else json.loads(v or "[]")
        out.setdefault(r["action_id"], []).append({
            "language": r["language"], "language_name": LANGUAGE_NAME.get(r["language"],
                                                                          r["language"]),
            "keyword": r["keyword"], "titles": j(r["titles"]), "questions": j(r["questions"]),
            "real_searches": j(r["verified"])})
    return out
