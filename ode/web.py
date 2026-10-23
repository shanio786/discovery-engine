"""Phone dashboard (`ode web`, compose service `web`).

Today's plan as cards, one tap to mark an item started / dropped, a form to
log views and revenue, plus gems, alerts and your work log. Server-rendered
HTML, no JavaScript, works on any phone.

Security:
  * refuses to serve anything until DASHBOARD_PASSWORD is set;
  * HTTP Basic auth (user `ode`), constant-time compare, per-IP lockout
    after repeated failures;
  * every POST needs a CSRF token and a same-origin Origin/Referer;
  * strict security headers (CSP without scripts, no framing, referrer only
    to this site);
  * listens on 127.0.0.1 in compose — reach it through Caddy (automatic
    HTTPS on your domain) or an SSH tunnel, never plain HTTP on the internet.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets as pysecrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from ode import worklog
from ode.config import Config, Secrets
from ode.db import Database
from ode.logging import get_logger
from ode.scoring.actions import LANGUAGE_NAME, PLATFORM_LABEL, Action

log = get_logger(__name__)

USER = "ode"
PLAN_CACHE_SECONDS = 300
LOCKOUT_FAILURES = 10
LOCKOUT_WINDOW = 600.0
HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "img-src 'self' data:; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # not "no-referrer": with it browsers send `Origin: null` on our own form posts,
    # which the same-origin CSRF check (rightly) refuses
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
ICON = {"BLOG": "📝", "YOUTUBE": "🎬", "SHORTS": "📱", "TOOLSITE": "🧰", "APP": "📲"}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#14181f;--mut:#5b6472;--acc:#2456d6;--ok:#137a3a;
--bd:#e3e6eb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#181c22;--fg:#e8ebf0;
--mut:#9aa3b2;--acc:#7aa2ff;--ok:#5fd08a;--bd:#2a3039}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--bd);
padding:10px 16px;display:flex;gap:14px;overflow-x:auto;white-space:nowrap}
header a{color:var(--mut);text-decoration:none;font-weight:600}header a.on{color:var(--acc)}
main{max-width:760px;margin:0 auto;padding:12px 16px 48px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px;
margin:12px 0}
h1{font-size:20px;margin:8px 0}h2{font-size:17px;margin:0 0 6px}h3{font-size:15px;margin:14px 0 4px}
.mut{color:var(--mut);font-size:14px}.tag{display:inline-block;border:1px solid var(--bd);
border-radius:99px;padding:1px 8px;font-size:13px;margin:2px 4px 2px 0}
ul{padding-left:20px;margin:6px 0}li{margin:3px 0}
form.inline{display:inline}button{font:inherit;border:0;border-radius:8px;padding:9px 14px;
background:var(--acc);color:#fff;font-weight:600;margin:6px 6px 0 0}
button.sec{background:transparent;color:var(--mut);border:1px solid var(--bd)}
input{font:inherit;width:100%;padding:9px;border:1px solid var(--bd);border-radius:8px;
background:var(--bg);color:var(--fg);margin:4px 0 10px}
label{font-size:14px;color:var(--mut)}a{color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 4px;
border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}
.note{font-size:13px;color:var(--mut);border-left:3px solid var(--bd);padding-left:8px}
.ok{color:var(--ok);font-weight:600}
"""


def e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


class Lockout:
    """Per-IP failed-login limiter (in memory)."""

    def __init__(self, failures: int = LOCKOUT_FAILURES, window: float = LOCKOUT_WINDOW) -> None:
        self.failures, self.window = failures, window
        self.seen: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now or time.monotonic()
        q = self.seen[ip]
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) >= self.failures

    def fail(self, ip: str, now: float | None = None) -> None:
        self.seen[ip].append(now or time.monotonic())

    def clear(self, ip: str) -> None:
        self.seen.pop(ip, None)


def check_basic(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, given = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    user_ok = hmac.compare_digest(user.encode(), USER.encode())
    pass_ok = hmac.compare_digest(given.encode(), password.encode())
    return user_ok and pass_ok  # both compared first: no timing hint which one failed


def same_origin(request: Request) -> bool:
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return False
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return urlsplit(src).netloc == host


class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        st = request.app.state
        if request.url.path == "/health":
            response = PlainTextResponse("ok")
        elif not st.password:
            response = PlainTextResponse("Set DASHBOARD_PASSWORD in .env to enable the "
                                         "dashboard.", status_code=503)
        else:
            ip = client_ip(request, st.trust_proxy)
            if st.lockout.blocked(ip):
                response = PlainTextResponse("Too many failed logins. Try again later.",
                                             status_code=429)
            elif not check_basic(request.headers.get("authorization"), st.password):
                if request.headers.get("authorization"):
                    st.lockout.fail(ip)
                    log.warning("web.login_failed", ip=ip)
                response = PlainTextResponse("Login required", status_code=401, headers={
                    "WWW-Authenticate": 'Basic realm="ODE", charset="UTF-8"'})
            else:
                st.lockout.clear(ip)
                response = await call_next(request)
        return _secure(response)


def _secure(response: Response) -> Response:
    for k, v in HEADERS.items():
        response.headers.setdefault(k, v)
    return response


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---------------------------------------------------------------- rendering
NAV = [("/", "Aaj ka plan"), ("/content", "Mera content"), ("/worklog", "Mera kaam"),
       ("/gems", "Gems"), ("/alerts", "Alerts")]


def page(title: str, body: str, path: str = "/") -> HTMLResponse:
    nav = "".join(f'<a href="{href}" class="{"on" if href == path else ""}">{e(label)}</a>'
                  for href, label in NAV)
    return HTMLResponse(
        f'<!doctype html><html lang="ur-Latn"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(title)} · ODE</title><style>{CSS}</style></head>"
        f"<body><header>{nav}</header><main>{body}</main></body></html>")


def _csrf(request: Request) -> str:
    return f'<input type="hidden" name="csrf" value="{e(request.app.state.csrf)}">'


def _status_buttons(request: Request, a: Action, link: bool = True) -> str:
    def btn(status: str, label: str, cls: str = "") -> str:
        return (f'<form class="inline" method="post" action="/done">{_csrf(request)}'
                f'<input type="hidden" name="id" value="{e(a.action_id)}">'
                f'<input type="hidden" name="status" value="{status}">'
                f'<button class="{cls}">{label}</button></form>')
    more = f' <a href="/item/{e(a.action_id)}">poori detail →</a>' if link else ""
    return btn("started", "✅ Shuru kiya") + btn("dropped", "Chhor do", "sec") + more


def _card(request: Request, i: int | None, a: Action, link: bool = True) -> str:
    lang = LANGUAGE_NAME.get(a.language, a.language)
    also = "".join(f'<span class="tag">{e(LANGUAGE_NAME.get(lg, lg))} {ease:.0f}</span>'
                   for lg, ease in a.also_in)
    qs = "".join(f"<li>{e(q)}</li>" for q in a.questions[:5])
    src = f" · demand {e(a.source_market)} se" if a.source_market else ""
    num = f"{i}. " if i is not None else ""
    return (f'<div class="card"><h2>{num}{ICON.get(a.platform, "")} {e(PLATFORM_LABEL[a.platform])}'
            f" · {e(lang)}</h2><div><b>{e(a.topic)}</b></div>"
            + (f'<div class="ok">💵 Andazan {e(a.dollars.label())} · ~{a.dollars.hours:.0f} '
               f'ghante <span class="mut">({e(a.dollars.basis)})</span></div>'
               if a.dollars and a.dollars.high else "")
            + f'<div class="mut">score {a.score:.0f} · aasani {a.ease:.0f} · value {a.value:.0f} · '
            f"{e(a.market)}{src} · id <code>{e(a.action_id)}</code></div>"
            f"<ul>{''.join(f'<li>{e(w)}</li>' for w in a.why[:3])}</ul>"
            + (f'<div class="mut">Yahan bhi khula: {also}</div>' if also else "")
            + (f"<h3>Log poochh rahe hain</h3><ul>{qs}</ul>" if qs else "")
            + _status_buttons(request, a, link) + "</div>")


async def _plan(request: Request) -> list[Action]:
    from ode.scoring.plan import build_plan

    st = request.app.state
    today = datetime.now(UTC).date()
    cached = st.plan_cache
    if cached and cached[0] == today and time.monotonic() - cached[1] < PLAN_CACHE_SECONDS:
        return cached[2]
    plan = await build_plan(st.db, st.config, today)
    st.plan_cache = (today, time.monotonic(), plan)
    return plan


async def home(request: Request) -> HTMLResponse:
    from ode.backtest import validation_note

    plan = await _plan(request)
    note = await validation_note(request.app.state.db)
    today = datetime.now(UTC).date()
    cards = "".join(_card(request, i, a) for i, a in enumerate(plan, 1)) or (
        '<div class="card">Abhi plan nahi — engine pehla daily run kar raha hai '
        "(60 din ki history chahiye).</div>")
    return page("Aaj ka plan", f"<h1>Aaj kya karna hai — {today}</h1>"
                f'<p class="note">{e(note)}</p>{cards}', "/")


async def item(request: Request) -> HTMLResponse:
    from ode.scoring.plan import attach_briefs

    st = request.app.state
    aid = request.path_params["aid"]
    plan = await _plan(request)
    a = next((x for x in plan if x.action_id == aid), None)
    entry = await st.db.pool.fetchrow("SELECT * FROM work_log WHERE action_id = $1", aid)
    if a is None and entry is None:
        stored = await st.db.pool.fetchrow("SELECT * FROM plan_items WHERE action_id = $1", aid)
        if stored is None:
            return page("Nahi mila", '<div class="card">Yeh item nahi mila.</div>')
    parts: list[str] = []
    if a is not None:
        briefs = await attach_briefs(st.db, [a], int(st.config.gemini.get("reuse_days", 14)))
        b = briefs.get(0)
        parts.append(_card(request, None, a, link=False))
        parts.append('<div class="card"><h3>Pehle steps</h3><ol>'
                     + "".join(f"<li>{e(s)}</li>" for s in a.steps) + "</ol>")
        if a.questions:
            parts.append("<h3>Sab sawal (har sawal = ek article / video)</h3><ul>"
                         + "".join(f"<li>{e(q)}</li>" for q in a.questions) + "</ul>")
        if a.complaints:
            parts.append("<h3>Top apps ki 1-star shikayatein</h3><ul>"
                         + "".join(f"<li>“{e(c[:300])}”</li>" for c in a.complaints) + "</ul>")
        for f in a.formats:
            parts.append(f"<h3>Viral format: {e(f['channel'])} ({f['outliers']} hits)</h3>"
                         + (f"<p>{e(f['formula'])}</p>" if f.get("formula") else "")
                         + "<ul>" + "".join(f"<li>{e(t)}</li>" for t in f["titles"][:5])
                         + "</ul>")
        if b:
            parts.append("<h3>Title ideas (Gemini)</h3><ul>"
                         + "".join(f"<li>{e(t)}</li>" for t in b["titles"]) + "</ul>"
                         + (f"<h3>Outline</h3><pre style='white-space:pre-wrap'>"
                            f"{e(b['outline'])}</pre>" if b.get("outline") else ""))
        if a.unverified:
            parts.append(f'<p class="note">Abhi verify nahi: {e("; ".join(a.unverified))}</p>')
        parts.append("</div>")
    status = (f'<p class="ok">Status: {e(entry["status"])}</p>' if entry else "")

    def v(key: str) -> str:
        return "" if not entry or entry[key] is None else e(entry[key])

    parts.append(
        f'<div class="card"><h3>Kamai likho (ab tak ka total)</h3>{status}'
        f'<form method="post" action="/result">{_csrf(request)}'
        f'<input type="hidden" name="id" value="{e(aid)}">'
        f'<label>Views</label><input name="views" inputmode="numeric" value="{v("views")}">'
        f'<label>Revenue (USD)</label><input name="revenue" inputmode="decimal" '
        f'value="{v("revenue_usd")}">'
        f'<label>Ghante lagaye</label><input name="hours" inputmode="decimal" '
        f'value="{v("hours")}"><button>Save</button></form>'
        f"<h3>Publish ho gaya? Link daalo — engine roz dekhega chal raha hai ya nahi</h3>"
        f"{_track_form(request, aid)}</div>")
    return page("Item", "".join(parts))


class Forbidden(Exception):
    pass


async def _form(request: Request):
    """POST body, only with a valid CSRF token from a same-origin page."""
    form = await request.form()
    token = str(form.get("csrf") or "")
    if not (hmac.compare_digest(token, request.app.state.csrf) and same_origin(request)):
        raise Forbidden
    return form


def _num(value: Any, cast) -> Any:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        n = cast(float(text))
    except ValueError:
        return None
    return n if n >= 0 else None


def _back(request: Request, fallback: str = "/") -> str:
    ref = request.headers.get("referer")
    path = urlsplit(ref).path if ref and same_origin(request) else fallback
    return path if path.startswith("/") and not path.startswith("//") else fallback


async def done(request: Request) -> Response:
    try:
        form = await _form(request)
    except Forbidden:
        return PlainTextResponse("Bad request (CSRF)", status_code=403)
    try:
        await worklog.mark(request.app.state.db, str(form.get("id") or ""),
                           str(form.get("status") or "started"))
    except (worklog.UnknownAction, ValueError) as exc:
        return PlainTextResponse(str(exc), status_code=400)
    request.app.state.plan_cache = None
    return RedirectResponse(_back(request), status_code=303)


async def result(request: Request) -> Response:
    try:
        form = await _form(request)
    except Forbidden:
        return PlainTextResponse("Bad request (CSRF)", status_code=403)
    aid = str(form.get("id") or "")
    try:
        await worklog.record_result(request.app.state.db, aid,
                                    views=_num(form.get("views"), int),
                                    revenue=_num(form.get("revenue"), float),
                                    hours=_num(form.get("hours"), float))
    except worklog.UnknownAction as exc:
        return PlainTextResponse(str(exc), status_code=400)
    request.app.state.plan_cache = None
    return RedirectResponse(f"/item/{aid}", status_code=303)


def _track_form(request: Request, aid: str = "") -> str:
    hidden = f'<input type="hidden" name="id" value="{e(aid)}">' if aid else (
        '<label>Plan item id (agar hai)</label><input name="id" maxlength="8">')
    return (f'<form method="post" action="/track">{_csrf(request)}{hidden}'
            '<label>Link (YouTube / Play Store / tumhara page)</label>'
            '<input name="url" type="url" required placeholder="https://">'
            '<label>Google keyword (sirf blog / tool page)</label><input name="keyword">'
            '<button>Track karo</button></form>')


async def content_page(request: Request) -> HTMLResponse:
    from ode.content import ICON

    rows = await request.app.state.db.pool.fetch(
        "SELECT c.*, (SELECT views FROM content_stats s WHERE s.content_id = c.id AND views "
        "IS NOT NULL ORDER BY date DESC LIMIT 1) AS views, (SELECT position FROM content_stats s "
        "WHERE s.content_id = c.id AND position IS NOT NULL ORDER BY date DESC LIMIT 1) AS pos "
        "FROM my_content c WHERE active ORDER BY (verdict = 'WINNER') DESC NULLS LAST, "
        "added_at DESC")
    cards = []
    for r in rows:
        d = r["verdict_detail"] if isinstance(r["verdict_detail"], dict) else json.loads(
            r["verdict_detail"] or "{}")
        sugg = "".join(f"<li>{e(s)}</li>" for s in (d.get("suggestions") or [])[:5])
        cards.append(
            f'<div class="card"><h2>{ICON.get(r["verdict"] or "UNKNOWN", "")} '
            f'{e(r["verdict"] or "naya")} · {e(r["platform"])}</h2>'
            f'<div><a href="{e(r["url"])}" rel="noopener noreferrer">'
            f'{e(r["title"] or r["url"])}</a></div>'
            f'<div class="mut">{e(d.get("text", "kal tak numbers aa jayenge"))}</div>'
            + (f"<h3>Isi par agle 5</h3><ul>{sugg}</ul>" if sugg else "")
            + f'<form class="inline" method="post" action="/untrack">{_csrf(request)}'
            f'<input type="hidden" name="cid" value="{r["id"]}">'
            f'<button class="sec">Track band karo</button></form></div>')
    body = ('<h1>Mera content</h1><div class="card"><h3>Kuch publish kiya? Link daalo</h3>'
            + _track_form(request) + "</div>" + ("".join(cards) or
            '<div class="card">Abhi kuch track nahi ho raha.</div>'))
    return page("Mera content", body, "/content")


async def track(request: Request) -> Response:
    from ode import content

    try:
        form = await _form(request)
    except Forbidden:
        return PlainTextResponse("Bad request (CSRF)", status_code=403)
    try:
        await content.add_content(request.app.state.db, str(form.get("url") or ""),
                                  action_id=str(form.get("id") or "").strip() or None,
                                  keyword=str(form.get("keyword") or "").strip() or None)
    except (content.ContentError, worklog.UnknownAction) as exc:
        return PlainTextResponse(str(exc), status_code=400)
    request.app.state.plan_cache = None
    return RedirectResponse("/content", status_code=303)


async def untrack_view(request: Request) -> Response:
    from ode import content

    try:
        form = await _form(request)
    except Forbidden:
        return PlainTextResponse("Bad request (CSRF)", status_code=403)
    cid = _num(form.get("cid"), int)
    if cid is not None:
        await content.untrack(request.app.state.db, cid)
    return RedirectResponse("/content", status_code=303)


async def worklog_page(request: Request) -> HTMLResponse:
    st = request.app.state
    rows = await worklog.entries(st.db, 200)
    values, plat = await worklog.calibration(st.db, st.config)
    rev = sum(r["revenue_usd"] or 0 for r in rows)
    hours = sum(r["hours"] or 0 for r in rows)
    body = [f'<h1>Mera kaam</h1><div class="card"><b>${rev:,.2f}</b> kamai · {hours:.0f} ghante'
            + (f" · ${rev / hours:,.2f}/ghanta" if hours else "") + "</div>"]
    def row(r: dict[str, Any]) -> str:
        money = "" if r["revenue_usd"] is None else f"{r['revenue_usd']:,.2f}"
        return (f'<tr><td><a href="/item/{e(r["action_id"])}">{e(r["topic"])}</a>'
                f'<div class="mut">{e(r["platform"])} · {e(r["market"])}</div></td>'
                f'<td>{e(r["status"])}</td><td>{e(r["views"] or "")}</td><td>{money}</td></tr>')

    if rows:
        body.append('<div class="card"><table><tr><th>Item</th><th>Status</th><th>Views</th>'
                    "<th>$</th></tr>" + "".join(row(r) for r in rows) + "</table></div>")
    else:
        body.append('<div class="card">Abhi kuch nahi. Plan mein kisi item par '
                    "“Shuru kiya” dabao.</div>")
    learned = [f"{e(m)} {st.config.market_value(m):.2f} → {v:.2f}"
               for m, v in sorted(values.items()) if abs(v - st.config.market_value(m)) > 1e-9]
    weights = [f"{e(p)} ×{v:.2f}" for p, v in sorted(plat.items())]
    if learned or weights:
        body.append('<div class="card"><h3>Engine ne tumhare results se seekha</h3>'
                    + (f"<p>Market value: {', '.join(learned)}</p>" if learned else "")
                    + (f"<p>Platform weight: {', '.join(weights)}</p>" if weights else "")
                    + "</div>")
    return page("Mera kaam", "".join(body), "/worklog")


async def gems_page(request: Request) -> HTMLResponse:
    from ode.scoring.report import load_gems

    gems = await load_gems(request.app.state.db, datetime.now(UTC).date(), limit=40)
    rows = "".join(f"<tr><td>{g.rank}</td><td><b>{e(g.name)}</b><div class='mut'>{e(g.why())}"
                   f"</div></td><td>{e(g.market)}</td><td>{g.composite:.0f}</td></tr>"
                   for g in gems)
    body = (f'<h1>Gems</h1><div class="card"><table><tr><th>#</th><th>Topic</th><th>Mkt</th>'
            f"<th>Score</th></tr>{rows}</table></div>" if gems else
            '<h1>Gems</h1><div class="card">Abhi scores nahi.</div>')
    return page("Gems", body, "/gems")


async def alerts_page(request: Request) -> HTMLResponse:
    rows = await request.app.state.db.pool.fetch(
        "SELECT kind, key, sent_at, payload FROM alerts_sent ORDER BY sent_at DESC LIMIT 60")
    items = []
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        text = (p.get("text") or r["key"]).replace("<b>", "").replace("</b>", "")
        items.append(f'<div class="card"><div class="mut">{r["sent_at"]:%Y-%m-%d %H:%M} · '
                     f'{e(r["kind"])}</div><div style="white-space:pre-wrap">{e(text)}</div></div>')
    return page("Alerts", "<h1>Alerts</h1>" + ("".join(items) or
                                                '<div class="card">Abhi koi alert nahi.</div>'),
                "/alerts")


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def create_app(config: Config, secrets: Secrets, db: Database | None = None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        own = db is None
        app.state.db = db or await Database.connect(secrets.database_url, max_size=4)
        if own:
            await app.state.db.init_schema()
        try:
            yield
        finally:
            if own:
                await app.state.db.close()

    app = Starlette(
        routes=[Route("/", home), Route("/item/{aid}", item), Route("/done", done, methods=["POST"]),
                Route("/result", result, methods=["POST"]), Route("/worklog", worklog_page),
                Route("/content", content_page), Route("/track", track, methods=["POST"]),
                Route("/untrack", untrack_view, methods=["POST"]),
                Route("/gems", gems_page), Route("/alerts", alerts_page), Route("/health", health)],
        middleware=[Middleware(Guard)], lifespan=lifespan)
    app.state.config = config
    app.state.password = secrets.dashboard_password or ""
    app.state.csrf = pysecrets.token_urlsafe(32)
    app.state.lockout = Lockout()
    app.state.trust_proxy = bool(config.dashboard.get("trust_proxy", True))
    app.state.plan_cache = None
    if db is not None:
        app.state.db = db
    return app


def serve(config: Config, secrets: Secrets, host: str, port: int) -> int:
    import uvicorn

    if not secrets.dashboard_password:
        log.warning("web.no_password", hint="set DASHBOARD_PASSWORD; every page answers 503")
    uvicorn.run(create_app(config, secrets), host=host, port=port, proxy_headers=True,
                forwarded_allow_ips="*", log_level="warning", access_log=False)
    return 0
