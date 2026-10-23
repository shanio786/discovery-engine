"""From scored topics to "do THIS, NOW": platform + language + why + first steps.

For every scored topic in every market we consider five ways to monetise it
without affiliate or SaaS:

    BLOG     search-intent articles (ads + your own product)
    YOUTUBE  long-form tutorials (ads + sponsors + your product)
    SHORTS   YouTube Shorts / TikTok / Instagram Reels (one video, three apps)
    APP      a small Google Play app (tracker / calculator / planner ...)
    PRODUCT  templates, scripts, mini-courses sold directly

Each candidate gets
    EASE   0-100  how easy it is to rank / go viral there (supply vs demand)
    VALUE  0-100  what a visitor is worth (money paths x market value)
and   SCORE = momentum x geomean(ease, value, demand) x confidence factor

Cross-language arbitrage is first-class: a topic proven in one market whose
Wikipedia has no article in another configured language becomes a BLOG /
YOUTUBE action IN that language, where there is demand nearby and no supply.

Pure functions: no I/O. Every signal that is still a guess is listed in
`unverified`, so the plan never pretends to know more than it does.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PLATFORMS = ("BLOG", "YOUTUBE", "SHORTS", "APP", "PRODUCT")
PLATFORM_LABEL = {
    "BLOG": "Blog / website",
    "YOUTUBE": "YouTube (long videos)",
    "SHORTS": "Shorts · TikTok · Reels",
    "APP": "Play Store app",
    "PRODUCT": "Digital product (template / course / script)",
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
    outlier: float | None = None
    unanswered: float | None = None
    missing_in: list[str] = field(default_factory=list)
    wiki_langs: int | None = None
    market_value: float = 0.5
    playstore: Mapping[str, float] = field(default_factory=dict)
    topic_id: int | None = None


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
    source_market: str | None = None   # set for cross-language actions

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
            f"Apne Namecheap domains mein se ek '{n}' ke liye rakho (ya existing site par alag section).",
            f"{lang_name} mein 10 'how-to' articles likho — har aam sawal ka ek alag article.",
            "Har article mein apna free checklist/template do (email list), phir paid version.",
            "Google Search Console lagao; 4–6 hafte baad dekho kaunse articles rank ho rahe hain, unhi par aur likho.",
        ]
    if platform == "YOUTUBE":
        return [
            f"{lang_name} mein 5 tutorial videos ki series: beginner → advanced '{n}'.",
            "Har video ek masla hal kare; title mein masla + nateeja (e.g. '… in 10 minutes').",
            "Description mein apna template/product; 1k subs ke baad sponsors ko email karo.",
        ]
    if platform == "SHORTS":
        return [
            f"7 din tak roz 1 short '{n}' par — ek hi video YouTube Shorts, TikTok aur Reels par.",
            "Pehle 2 second mein hook (nateeja dikhao), 20–40 second lambai.",
            "Jo short chale, usi ka long video banao (YouTube) — wahan asal kamai hai.",
        ]
    if platform == "APP":
        return [
            f"Play Store par '{n}' ki top apps ki 1-star reviews parho — wohi shikayatein tumhari feature list hain.",
            "Sirf ek kaam achhe se karne wali chhoti app (tracker / calculator / planner) 1–2 hafte mein.",
            f"ASO: app title aur description mein '{n}' + {lang_name} keywords; screenshots par fayda likho.",
            "Kamai: ads + ek baar ka 'Pro' unlock (subscription nahi).",
        ]
    return [  # PRODUCT
        f"'{n}' ke liye ek ready-made template / script / checklist banao (Notion, Excel, ya code).",
        "Gumroad ya Payhip par becho (khud ka product — affiliate nahi).",
        "Traffic: isi topic ke blog/YouTube/Shorts se; har content mein product ka link.",
    ]


def plan_topic(t: TopicView, *, config: Mapping[str, Any] | None = None,
               market_values: Mapping[str, float] | None = None,
               language_markets: Mapping[str, str] | None = None) -> list[Action]:
    """All candidate actions for one topic (including cross-language ones)."""
    cfg = config or {}
    sat_ratio = float(cfg.get("saturated_ratio", 100))
    if t.phase in ("UNKNOWN", "DECLINING"):
        return []  # never tell someone to start on a dying or unmeasured topic
    paths = set(t.money_paths)
    value = _clip(t.money * t.market_value)
    sd = supply_demand_ease(t.competing_videos, t.mean30, sat_ratio)
    views = _views(t)
    phase = _phase_line(t)
    base_why = [x for x in (phase, views) if x]
    no_yt = [] if sd is not None or t.outlier is not None else [
        "YouTube competition abhi naapi nahi gayi (kal ka probe batayega)"]
    actions: list[Action] = []

    def add(platform: str, ease: float, val: float, why: list[str], unverified: list[str],
            *, market: str | None = None, language: str | None = None,
            source: str | None = None) -> None:
        m, lg = market or t.market, language or t.language
        actions.append(Action(
            platform, m, lg, t.name, _score(t.momentum, ease, val, t.demand, t.confidence),
            round(ease, 1), round(val, 1), why, _steps(platform, t, LANGUAGE_NAME.get(lg, lg)),
            unverified, t.topic_id, source))

    # --- YOUTUBE: teachable topics; ease from supply/demand + small-channel outliers
    if paths & {"PRODUCT", "SPONSOR"} or t.demand >= 60:
        ease = _mean([sd, t.outlier, None if t.competition is None else 101 - t.competition], 50.0)
        why = [*base_why]
        if t.competing_videos is not None:
            why.append(f"pichhle 180 din mein sirf {t.competing_videos:,.0f} YouTube videos "
                       f"(ease {sd:.0f}/100)" if sd is not None else
                       f"{t.competing_videos:,.0f} competing YouTube videos")
        if t.outlier is not None and t.outlier >= 50:
            why.append("chhote channels apne subscribers se kai guna zyada views le rahe hain "
                       "= viral hona aasan")
        why.append(f"paisa: {t.money_path} ({t.money_paths.get(t.money_path, '')})")
        add("YOUTUBE", ease, value, why, list(no_yt))

    # --- SHORTS: ride a rising trend; lower RPM, huge reach
    if t.phase in ("BREAKOUT", "GROWING"):
        trend = 90.0 if t.phase == "BREAKOUT" else 70.0
        ease = _mean([trend, sd], trend)
        why = [*base_why, ("short-form trend ke shuru mein sab se tez barhta hai; ek video teen "
               "platforms (Shorts, TikTok, Reels) par")]
        add("SHORTS", ease, _clip(value * 0.6), why,
            [*no_yt, "TikTok/Instagram ka direct data nahi (free API nahi) — trend YouTube/Wikipedia se"])

    # --- BLOG: search demand; harder to judge without SERP data
    ease = _mean([sd, 60.0 if (t.wiki_langs or 99) < 15 else None], 50.0)
    why = [*base_why, "log ise search kar rahe hain = blog traffic ka asal source"]
    if t.wiki_langs is not None and t.wiki_langs < 15:
        why.append(f"sirf {t.wiki_langs} languages mein Wikipedia article = niche, kam competition")
    add("BLOG", ease, value, why,
        ["Google ranking difficulty abhi naapi nahi (Phase 4: DataForSEO top 20/din)"])

    # --- APP: utility intent or a measured Play Store gap
    ps = t.playstore or {}
    if "APP" in paths or ps:
        ps_gap = ps.get("ps_gap")
        ease = _mean([ps_gap], 50.0)
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
        unv = [] if ps else ["Play Store abhi check nahi hua (agle run mein hoga)"]
        add("APP", ease, _clip(value * (1.2 if "APP" in paths else 0.8)), why, unv)

    # --- PRODUCT: professional / how-to audiences buy templates & scripts
    if paths & {"PRODUCT", "SPONSOR"}:
        ease = _mean([t.gap], 50.0)
        why = [*base_why, f"paisa: {t.money_paths.get('PRODUCT') or t.money_paths.get('SPONSOR')}"]
        if t.unanswered:
            why.append(f"{t.unanswered:.0f}% Reddit sawalon ka jawab nahi mila = log madad dhoond rahe hain")
        add("PRODUCT", ease, _clip(value * 1.1), why, [] if t.gap is not None else [
            "gap abhi naapa nahi gaya"])

    # --- CROSS-LANGUAGE: proven here, no article there -> be first in that language
    mv = market_values or {}
    for lg in t.missing_in:
        target = (language_markets or {}).get(lg)
        if not target:
            continue
        target_value = _clip(t.money * mv.get(target, 0.5))
        why = [*base_why, (f"{t.market} mein demand saabit, lekin {LANGUAGE_NAME.get(lg, lg)} "
               "Wikipedia par article hi nahi = us language mein supply taqreeban zero"),
               "pehle aane wala rank karta hai"]
        add("BLOG", 85.0, target_value, why,
            ["us language mein demand ka andaza (doosri language se liya gaya)"],
            market=target, language=lg, source=t.market)
        if paths & {"PRODUCT", "SPONSOR"} or t.demand >= 60:
            add("YOUTUBE", 80.0, target_value, why,
                ["us language mein demand ka andaza (doosri language se liya gaya)"],
                market=target, language=lg, source=t.market)
    return actions


def select_plan(actions: list[Action], *, top_n: int = 25, per_market_max: int = 4,
                per_topic_max: int = 2) -> list[Action]:
    """Best actions first, spread over markets and topics so the plan is a
    portfolio, not 25 variations of one idea."""
    out: list[Action] = []
    per_market: dict[str, int] = {}
    per_topic: dict[tuple[str, str], int] = {}
    seen: set[tuple[str, str, str]] = set()
    for a in sorted(actions, key=lambda a: a.score, reverse=True):
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
