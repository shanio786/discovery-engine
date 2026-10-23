"""Phone dashboard: auth, lockout, CSRF, done/result flow, escaping, headers."""

import re

import httpx

from ode import web, worklog
from ode.config import Secrets
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world

BASE = "http://ode.test"


def secrets(password="pw-123"):
    return Secrets(_env_file=None, database_url="x", dashboard_password=password)  # type: ignore[call-arg]


def client(app, auth=("ode", "pw-123"), **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, auth=auth,
                             **kw)


class _Day:
    @staticmethod
    def now(tz=None):
        from datetime import datetime
        return datetime(AS_OF.year, AS_OF.month, AS_OF.day, 9, tzinfo=tz)


def test_basic_auth_and_lockout_pure():
    import base64
    hdr = "Basic " + base64.b64encode(b"ode:secret").decode()
    assert web.check_basic(hdr, "secret")
    assert not web.check_basic(hdr, "other") and not web.check_basic(None, "secret")
    assert not web.check_basic("Basic !!!", "secret")
    lock = web.Lockout(failures=3, window=10)
    for t in (1, 2, 3):
        lock.fail("1.2.3.4", now=t)
    assert lock.blocked("1.2.3.4", now=4) and not lock.blocked("5.6.7.8", now=4)
    assert not lock.blocked("1.2.3.4", now=20)                  # window passed


async def test_no_password_means_no_dashboard(db):
    app = web.create_app(cfg(), secrets(password=None), db)
    async with client(app) as c:
        assert (await c.get("/")).status_code == 503
        assert (await c.get("/health")).text == "ok"


async def test_auth_required_and_lockout(db):
    app = web.create_app(cfg(), secrets(), db)
    async with client(app, auth=None) as c:
        r = await c.get("/")
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
        for _ in range(web.LOCKOUT_FAILURES):
            await c.get("/", auth=("ode", "wrong"))
        assert (await c.get("/", auth=("ode", "pw-123"))).status_code == 429   # locked out
    async with client(app, headers={"X-Forwarded-For": "9.9.9.9"}) as c:     # other IP fine
        assert (await c.get("/worklog")).status_code == 200


async def test_plan_done_result_flow(db, monkeypatch):
    monkeypatch.setattr(web, "datetime", _Day)
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    app = web.create_app(config, secrets(), db)
    async with client(app) as c:
        home = await c.get("/")
        assert home.status_code == 200 and "Heat pump" in home.text
        assert home.headers["x-frame-options"] == "DENY"
        assert home.headers["referrer-policy"] == "same-origin"   # no-referrer breaks POSTs
        r = await c.post("/done", data={"id": "x", "csrf": "y"}, headers={"Origin": "null"})
        assert r.status_code == 403
        assert "script-src" not in home.headers["content-security-policy"]
        assert "<script" not in home.text
        aid = re.search(r'name="id" value="([0-9a-f]{8})"', home.text).group(1)
        token = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)

        # CSRF: no token / wrong origin -> refused
        r = await c.post("/done", data={"id": aid, "status": "started"},
                         headers={"Origin": BASE})
        assert r.status_code == 403
        r = await c.post("/done", data={"id": aid, "status": "started", "csrf": token},
                         headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.post("/done", data={"id": aid, "status": "started", "csrf": token},
                         headers={"Origin": BASE, "Referer": f"{BASE}/"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert (await db.pool.fetchval("SELECT status FROM work_log WHERE action_id = $1",
                                       aid)) == "started"
        assert aid not in (await c.get("/")).text                    # gone from the plan

        detail = await c.get(f"/item/{aid}")
        assert detail.status_code == 200 and "Kamai likho" in detail.text
        r = await c.post("/result", data={"id": aid, "views": "12,000", "revenue": "14.5",
                                          "hours": "-3", "csrf": token},
                         headers={"Origin": BASE})
        assert r.status_code == 303
        row = (await worklog.entries(db))[0]
        assert row["views"] == 12000 and row["revenue_usd"] == 14.5 and row["hours"] is None
        wl = await c.get("/worklog")
        assert "$14.50" in wl.text and "published" in wl.text
        bad = await c.post("/done", data={"id": "nope0000", "csrf": token},
                           headers={"Origin": BASE})
        assert bad.status_code == 400
        assert (await c.get("/item/zzzz")).status_code == 200
        assert (await c.get("/gems")).status_code == 200
        assert (await c.get("/alerts")).status_code == 200


async def test_output_is_escaped(db, monkeypatch):
    monkeypatch.setattr(web, "datetime", _Day)
    await db.pool.execute(
        "INSERT INTO alerts_sent (kind, key, payload) VALUES ('trend', 'k', $1::jsonb)",
        '{"text": "<img src=x onerror=alert(1)> trending"}')
    app = web.create_app(cfg(), secrets(), db)
    async with client(app) as c:
        page = (await c.get("/alerts")).text
    assert "<img src=x" not in page and "&lt;img src=x" in page


async def test_content_page_track_and_untrack(db, monkeypatch):
    monkeypatch.setattr(web, "datetime", _Day)
    app = web.create_app(cfg(), secrets(), db)
    async with client(app) as c:
        page = await c.get("/content")
        assert page.status_code == 200 and "Link daalo" in page.text
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = await c.post("/track", data={"url": "https://youtu.be/BBBBBBBBBBB", "csrf": token},
                         headers={"Origin": BASE})
        assert r.status_code == 303
        bad = await c.post("/track", data={"url": "nonsense", "csrf": token},
                           headers={"Origin": BASE})
        assert bad.status_code == 400
        assert "youtu.be/BBBBBBBBBBB" in (await c.get("/content")).text
        cid = await db.pool.fetchval("SELECT id FROM my_content")
        r = await c.post("/untrack", data={"cid": str(cid), "csrf": token},
                         headers={"Origin": BASE})
        assert r.status_code == 303
        assert not await db.pool.fetchval("SELECT active FROM my_content")
