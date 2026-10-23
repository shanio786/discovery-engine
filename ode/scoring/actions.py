"""From scored topics to "do THIS, NOW": platform + language + why + first steps.

The operator produces everything with AI and automation, so any subject with
demand and room is fair game. For every scored topic in every market we
consider five formats (no affiliate, no SaaS, no product selling):

    BLOG      AI-written articles, AI-translated into other open languages (ads)
    YOUTUBE   faceless AI videos: script -> AI voice -> AI visuals (ads, sponsors)
    SHORTS    YouTube Shorts / TikTok / Instagram Reels, one AI video, three apps
    TOOLSITE  an AI-built free web tool (calculator / converter / generator), ads
    APP       an AI-built Google Play app (AdMob)

Each candidate gets
    EASE   0-100  how easy it is to rank / go viral there (supply vs demand)
    VALUE  0-100  what a visitor is worth (money paths x market value)
and   SCORE = momentum x geomean(ease, value, demand) x confidence factor

Because AI makes translation nearly free, every plan item also lists the
other languages where the same topic is open ("also_in"), and a topic proven
in one market with no Wikipedia article in another configured language
becomes a "be first" action in that language.

Pure functions, no I/O. Anything still guessed is listed in `unverified`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ode.learning import action_id

PLATFORMS = ("BLOG", "YOUTUBE", "SHORTS", "TOOLSITE", "APP")
PLATFORM_LABEL = {
    "BLOG": "AI blog / website",
    "YOUTUBE": "YouTube (faceless AI videos)",
    "SHORTS": "Shorts · TikTok · Reels (AI video)",
    "TOOLSITE": "AI-built web tool site",
    "APP": "AI-built Play Store app",
}
LANGUAGE_NAME = {
    "en": "English", "de": "German", "ja": "Japanese", "fr": "French", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "tr": "Turkish",
    "id": "Indonesian", "hi": "Hindi", "ko": "Korean", "ar": "Arabic", "vi": "Vietnamese",
    "sv": "Swedish",
}


@dataclass
class TopicView:
    """Everything the planner needs about one scored topic in one market."""

    market: str
    language: str
    name: str
    phase: str
    momentum: float
    demand: float
    money: float
    money_path: str
    money_paths: Mapping[str, str]
    gap: float | None
    competition: float | None
    confidence: float
    saturation: str
    mean30: float | None = None
    change30: float | None = None
    competing_videos: float | None = None
    competing_shorts: float | None = None   # short videos (< 4 min) in the same window
    google: Mapping[str, float] = field(default_factory=dict)  # DataForSEO: gsv, kd, serp_weak_share
    niche: str | None = None       # niche (cluster) label
    niche_size: int = 0
    outlier: float | None = None
    unanswered: float | None = None
    missing_in: list[str] = field(default_factory=list)
    wiki_langs: int | None = None
    market_value: float = 0.5
    playstore: Mapping[str, float] = field(default_factory=dict)
    topic_id: int | None = None
    tutorial: bool = False
    tool_keyword: str | None = None
    seasonal: Mapping[str, Any] | None = None   # {"month_name", "ratio", "weeks"}


@dataclass
class Action:
    platform: str
    market: str
    language: str
    topic: str
    score: float
    ease: float
    value: float
    why: list[str]
    steps: list[str]
    unverified: list[str]
    topic_id: int | None = None
    source_market: str | None = None   # set for cross-language "be first" actions
    also_in: list[tuple[str, float]] = field(default_factory=list)  # (language, ease)
    questions: list[str] = field(default_factory=list)    # Google Autocomplete long-tail
    complaints: list[str] = field(default_factory=list)   # 1-star reviews of top apps
    formats: list[dict[str, Any]] = field(default_factory=list)  # proven viral formats
    dollars: Any = None            # scoring.dollars.Dollars: $/month range + hours
    variant: str = ""              # e.g. "more:<content id>:<week>" for double-down items

    @property
    def action_id(self) -> str:
        """Stable id: `ode done <id>` / `ode result <id>`."""
        platform = f"{self.platform}:{self.variant}" if self.variant else self.platform
        return action_id(self.topic_id, self.market, platform, self.topic)

    @property
    def headline(self) -> str:
        lang = LANGUAGE_NAME.get(self.language, self.language)
        return f"{PLATFORM_LABEL[self.platform]} in {lang} — {self.topic}"


def _clip(v: float) -> float:
    return max(0.0, min(100.0, v))


def supply_demand_ease(competing_videos: float | None, mean30: float | None,
                       saturated_ratio: float = 100.0) -> float | None:
    """Competing YouTube videos (180d) per daily view, mapped to 0-100 (100 = empty).

    Comparable ACROSS markets, unlike within-market percentiles: 500 videos for
    2,000 views/day is an open field anywhere; 50,000 videos is not.
    """
    if competing_videos is None or not mean30 or mean30 <= 0:
        return None
    ratio = competing_videos / mean30
    return round(_clip(100.0 * (1.0 - math.log1p(ratio) / math.log1p(saturated_ratio))), 1)


def google_ease(g: Mapping[str, float]) -> tuple[float | None, list[str]]:
    """Measured Google ease (0-100) + reasons, from keyword difficulty and how
    many forum / user-content pages hold the top 10. None without data."""
    kd, weak, gsv = g.get("kd"), g.get("serp_weak_share"), g.get("gsv")
    why: list[str] = []
    if gsv is not None:
        tail = g.get("gsv_longtail")
        why.append(f"Google: {gsv:,.0f} searches/mahina"
                   + (f" + {tail:,.0f} long-tail sawalon par" if tail else ""))
    if kd is None and weak is None:
        return None, why
    if kd is not None:
        why.append(f"Google difficulty {kd:.0f}/100" + (" = aasaan" if kd < 30 else
                                                          " = mushkil" if kd > 60 else ""))
    if weak is not None and weak > 0:
        why.append(f"top 10 mein {weak:.0%} forum / user pages (Reddit, Quora …) = "
                   "achha article aage nikal sakta hai")
    parts = [(100.0 - kd, 0.7) if kd is not None else None,
             (100.0 * weak, 0.3) if weak is not None else None]
    used = [p for p in parts if p]
    return round(sum(v * w for v, w in used) / sum(w for _, w in used), 1), why


def _mean(values: list[float | None], default: float | None = None) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else default


def _score(momentum: float, ease: float, value: float, demand: float, confidence: float) -> float:
    geo = (max(1.0, ease) * max(1.0, value) * max(1.0, demand)) ** (1 / 3)
    return round(momentum * geo * (0.6 + 0.4 * confidence), 2)


def _views(t: TopicView) -> str:
    if t.mean30 is None:
        return ""
    trend = f" ({t.change30:+.0%} vs pichhle 30 din)" if t.change30 is not None else ""
    return f"{t.mean30:,.0f} log roz is topic ko Wikipedia par dhoondte hain{trend}"


def _phase_line(t: TopicView) -> str:
    return {
        "BREAKOUT": "🚀 BREAKOUT — demand tezi se barh rahi hai aur raftaar bhi barh rahi hai: abhi pakro",
        "GROWING": "📈 GROWING — demand lagataar barh rahi hai",
        "STABLE": "➖ STABLE — mustaqil (evergreen) demand",
        "PEAKING": "⛰ PEAKING — barh rahi hai lekin raftaar kam ho rahi hai",
        "DECLINING": "📉 DECLINING — demand gir rahi hai",
    }.get(t.phase, t.phase)


def _steps(platform: str, t: TopicView, lang_name: str) -> list[str]:
    n = t.name
    if platform == "BLOG":
        return [
            f"Ek Namecheap domain (ya existing site ka section) '{n}' ke liye.",
            (f"AI se {lang_name} mein 30 articles: har aam sawal ka ek (outline → AI draft → "
            "facts check → thori apni editing). Publishing n8n se automate."),
            ("Wohi articles AI se upar likhi khuli languages mein translate karo, har "
            "language ka apna folder (/de/, /ja/ …) = naya traffic, taqreeban muft."),
            ("Kamai: display ads (pehle AdSense, traffic barhe to Ezoic/Mediavine). 4–6 hafte "
            "baad Search Console: jo rank ho raha hai usi par aur likho."),
        ]
    if platform == "YOUTUBE":
        return [
            (f"Faceless AI channel '{n}' ({lang_name}): AI script → AI voice → AI visuals / "
            "stock clips → ek editing template."),
            ("Pehle 10 videos ka batch banao; upload, title, description, thumbnail n8n se "
            "automate."),
            "Har video ek sawal ka jawab de; title mein sawal + nateeja.",
            "Kamai: YouTube ads (1k subs + 4k ghante ke baad), phir sponsors.",
        ]
    if platform == "SHORTS":
        return [
            f"AI se roz 1–3 shorts '{n}' par (20–40 sec), pehle 2 second mein hook.",
            ("Ek automation pipeline: wohi video YouTube Shorts + TikTok + Instagram Reels "
            "par auto-post."),
            "Jo short chale, usi ka long video banao: asal kamai wahan hai.",
        ]
    if platform == "TOOLSITE":
        tool = f" ({t.tool_keyword})" if t.tool_keyword else ""
        return [
            (f"AI (Claude Code) se ek single-page free tool banao: '{n}'{tool}. Ek kaam, tez, "
            "mobile par achha."),
            "Interface AI se 12 languages mein translate; har language ka apna URL.",
            ("Tool ke neeche 300–600 words ki AI guide (Google ke liye) + apne doosre tools "
            "ke links."),
            "Kamai: display ads. Tool free hai, koi subscription nahi.",
        ]
    return [  # APP
        (f"Play Store par '{n}' ki top apps ki 1-star reviews parho: wohi shikayatein "
        "tumhari feature list hain."),
        "AI (Claude Code + Flutter) se ek kaam wali app 1–2 hafte mein; kamai AdMob ads se.",
        "Store listing (title, description, screenshot text) AI se 12 languages mein.",
        ("Har 2–4 hafte update: jo competitors saal bhar se update nahi hue, unhein yahin "
        "harao."),
    ]


def plan_topic(t: TopicView, *, config: Mapping[str, Any] | None = None,
               market_values: Mapping[str, float] | None = None,
               language_markets: Mapping[str, str] | None = None) -> list[Action]:
    """All candidate actions for one topic (including cross-language ones)."""
    cfg = config or {}
    sat_ratio = float(cfg.get("saturated_ratio", 100))
    if t.phase == "UNKNOWN" or (t.phase == "DECLINING" and not t.seasonal):
        return []  # never a dying or unmeasured topic — unless its yearly peak is coming
    if t.seasonal:  # a coming peak beats today's phase: prepare content in time
        t = replace(t, momentum=max(t.momentum, 2.0))
    paths = set(t.money_paths)
    value = _clip(t.money * t.market_value)
    sd = supply_demand_ease(t.competing_videos, t.mean30, sat_ratio)
    season_line = (f"📅 SEASONAL — har saal {t.seasonal['month_name']} mein "
                   f"~{t.seasonal['ratio']:.1f}x demand; peak {t.seasonal['weeks']} hafte mein. "
                   "AI content ABHI banao taake peak tak rank ho jaye") if t.seasonal else ""
    niche_line = (f"📦 niche “{t.niche}”: {t.niche_size} related topics = ek channel / site, "
                  f"{t.niche_size}+ videos / articles ka material"
                  if t.niche and t.niche_size >= 3 else "")
    base_why = [x for x in (season_line, _phase_line(t) if not t.seasonal else "", _views(t),
                            niche_line) if x]
    no_yt = [] if sd is not None or t.outlier is not None else [
        "YouTube competition abhi naapi nahi gayi (kal ka probe batayega)"]
    ps = t.playstore or {}
    tool_intent = bool(t.tool_keyword) or "APP" in paths
    actions: list[Action] = []

    def add(platform: str, ease: float, val: float, why: list[str], unverified: list[str],
            *, market: str | None = None, language: str | None = None,
            source: str | None = None) -> None:
        m, lg = market or t.market, language or t.language
        actions.append(Action(
            platform, m, lg, t.name, _score(t.momentum, ease, val, t.demand, t.confidence),
            round(ease, 1), round(val, 1), why, _steps(platform, t, LANGUAGE_NAME.get(lg, lg)),
            unverified, t.topic_id, source))

    # --- YOUTUBE: AI explainers work for almost any subject people look up
    if t.tutorial or "SPONSOR" in paths or t.demand >= 30:
        ease = _mean([sd, t.outlier, None if t.competition is None else 101 - t.competition], 50.0)
        why = [*base_why]
        if t.competing_videos is not None:
            why.append(f"pichhle 180 din mein {t.competing_videos:,.0f} competing YouTube videos"
                       + (f" (ease {sd:.0f}/100)" if sd is not None else ""))
        if t.outlier is not None and t.outlier >= 50:
            why.append("chhote channels apne subscribers se kai guna zyada views le rahe hain "
                       "= viral hona aasan")
        if t.tutorial:
            why.append("how-to / tech audience: ads ka RPM zyada")
        if "SPONSOR" in paths:
            why.append(f"sponsors mil sakte hain: {t.money_paths['SPONSOR']}")
        add("YOUTUBE", ease, value, why, list(no_yt))

    # --- SHORTS: ride a rising trend; lower RPM, huge reach
    if t.phase in ("BREAKOUT", "GROWING"):
        trend = 90.0 if t.phase == "BREAKOUT" else 70.0
        sd_short = supply_demand_ease(t.competing_shorts, t.mean30, sat_ratio)
        why = [*base_why, ("short-form trend ke shuru mein sab se tez barhta hai; ek AI video "
                           "teen platforms (Shorts, TikTok, Reels) par")]
        if t.competing_shorts is not None:
            why.append(f"pichhle 180 din mein sirf {t.competing_shorts:,.0f} competing short "
                       f"videos (shorts ease {sd_short:.0f}/100)" if sd_short is not None and
                       sd_short >= 50 else
                       f"pichhle 180 din mein {t.competing_shorts:,.0f} competing short videos")
        no_short = [] if t.competing_shorts is not None else no_yt
        add("SHORTS", _mean([trend, sd_short if sd_short is not None else sd], trend),
            _clip(value * 0.6), why,
            [*no_short, ("TikTok/Instagram ka direct data nahi (free API nahi): trend YouTube "
                        "aur Wikipedia se")])

    # --- BLOG: search demand; ranking difficulty measured by DataForSEO when available
    g = t.google or {}
    seo_ease, seo_why = google_ease(g)
    ease = (seo_ease if seo_ease is not None
            else _mean([sd, 60.0 if (t.wiki_langs or 99) < 15 else None], 50.0))
    why = [*base_why, "log ise search kar rahe hain = blog traffic ka asal source", *seo_why]
    if t.wiki_langs is not None and t.wiki_langs < 15:
        why.append(f"sirf {t.wiki_langs} languages mein Wikipedia article = niche, kam competition")
    add("BLOG", ease, value, why, [] if seo_ease is not None else
        ["Google ranking difficulty abhi naapi nahi (DataForSEO top 20/din mein aayega)"])

    # --- TOOLSITE: people want to calculate / convert / generate something
    if tool_intent:
        ps_gap = ps.get("ps_gap")
        ease = _mean([ps_gap, 60.0 if t.tool_keyword else None, seo_ease], 50.0)
        why = [*base_why, *seo_why]
        if t.tool_keyword:
            why.append(f"log '{t.tool_keyword}' jaisa tool dhoondte hain = seedha tool traffic")
        if ps.get("ps_top_rating") is not None and ps["ps_top_rating"] < 4.2:
            why.append(f"Play Store ki top apps sirf {ps['ps_top_rating']:.1f}★: ek achha web "
                       "tool aage nikal sakta hai")
        add("TOOLSITE", ease, value, why, [] if ps else
            ["web par maujooda tools ka muqabla abhi naapa nahi"])

    # --- APP: utility intent or a measured Play Store gap
    if "APP" in paths or ps:
        ease = _mean([ps.get("ps_gap")], 50.0)
        why = [*base_why]
        if ps.get("ps_top_installs"):
            why.append(f"top app ke {ps['ps_top_installs']:,.0f} installs = demand saabit")
        if ps.get("ps_top_rating") is not None:
            why.append(f"top apps ki rating sirf {ps['ps_top_rating']:.1f}★ — log naraz hain"
                       if ps["ps_top_rating"] < 4.2 else
                       f"top apps ki rating {ps['ps_top_rating']:.1f}★")
        if ps.get("ps_stale_share"):
            why.append(f"{ps['ps_stale_share']:.0%} top apps saal bhar se update nahi hui")
        if "APP" in paths:
            why.append(f"utility intent: {t.money_paths['APP']}")
        add("APP", ease, _clip(value * (1.2 if "APP" in paths else 0.8)), why,
            [] if ps else ["Play Store abhi check nahi hua (agle run mein hoga)"])

    # --- CROSS-LANGUAGE: proven here, no article there -> be first in that language
    mv = market_values or {}
    for lg in t.missing_in:
        target = (language_markets or {}).get(lg)
        if not target:
            continue
        target_value = _clip(t.money * mv.get(target, 0.5))
        why = [*base_why, (f"{t.market} mein demand saabit, lekin {LANGUAGE_NAME.get(lg, lg)} "
                           "Wikipedia par article hi nahi = us language mein supply taqreeban "
                           "zero"),
               "pehle aane wala rank karta hai; AI translation se lagat taqreeban zero"]
        guess = ["us language mein demand ka andaza (doosri language se liya gaya)"]
        add("BLOG", 85.0, target_value, why, guess, market=target, language=lg, source=t.market)
        add("YOUTUBE", 80.0, target_value, why, guess, market=target, language=lg,
            source=t.market)
        if tool_intent:
            add("TOOLSITE", 85.0, target_value, why, guess, market=target, language=lg,
                source=t.market)
    return actions


def attach_also_in(plan: list[Action], candidates: list[Action], max_n: int = 6) -> None:
    """For each plan item, the other languages where the SAME topic and format
    is also open, best first: one AI production, many languages."""
    by_key: dict[tuple[int | None, str], list[Action]] = {}
    for a in candidates:
        if a.topic_id is not None:
            by_key.setdefault((a.topic_id, a.platform), []).append(a)
    for a in plan:
        others = [o for o in by_key.get((a.topic_id, a.platform), []) if o.language != a.language]
        best: dict[str, float] = {}
        for o in sorted(others, key=lambda o: o.score, reverse=True):
            best.setdefault(o.language, o.ease)
        a.also_in = list(best.items())[:max_n]


def rank_key(rank_by: str = "score"):
    """Sort key for the plan: `score` (opportunity), `dollars` (expected $/month
    x score as tie-break) or `dollars_per_hour`."""
    def key(a: Action) -> tuple[float, float]:
        d = a.dollars
        if rank_by == "dollars" and d is not None:
            return (d.mid, a.score)
        if rank_by == "dollars_per_hour" and d is not None:
            return (d.per_hour, a.score)
        return (a.score, 0.0)
    return key


def select_plan(actions: list[Action], *, top_n: int = 25, per_market_max: int = 4,
                per_topic_max: int = 2, rank_by: str = "score") -> list[Action]:
    """Best actions first, spread over markets and topics so the plan is a
    portfolio, not 25 variations of one idea."""
    out: list[Action] = []
    per_market: dict[str, int] = {}
    per_topic: dict[tuple[str, str], int] = {}
    seen: set[tuple[str, str, str] | str] = set()
    for a in sorted(actions, key=rank_key(rank_by), reverse=True):
        if a.action_id in seen:  # same topic+market+platform reached two ways
            continue
        seen.add(a.action_id)
        key = (a.platform, a.language, a.topic.casefold())
        tkey = (a.market, a.topic.casefold())
        if key in seen or per_market.get(a.market, 0) >= per_market_max \
                or per_topic.get(tkey, 0) >= per_topic_max:
            continue
        seen.add(key)
        per_market[a.market] = per_market.get(a.market, 0) + 1
        per_topic[tkey] = per_topic.get(tkey, 0) + 1
        out.append(a)
        if len(out) >= top_n:
            break
    return out
