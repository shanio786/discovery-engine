"""Production pack + n8n webhook (mocked with respx)."""

import hashlib
import hmac
import json

import httpx
import respx

from ode import export
from ode.config import Secrets
from ode.pipeline import http_client
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import action, cfg
from tests.test_scoring_db import AS_OF, seed_world

HOOK = "https://n8n.example.com/webhook/abc123"


def test_pack_item_has_everything_automation_needs():
    a = action(also_in=[("ja", 70.0)], questions=["wie funktioniert eine wärmepumpe"])
    item = export.pack_item(a, {"titles": ["T1"], "outline": "o", "format": "blog"})
    assert item["id"] == a.action_id and item["language_name"] == "German"
    assert item["also_in"] == [{"language": "ja", "language_name": "Japanese", "ease": 70.0}]
    assert item["questions"] and item["brief"]["titles"] == ["T1"]
    pack = export.build_pack([a], {}, AS_OF)
    assert pack["version"] == 1 and pack["day"] == str(AS_OF) and pack["items"][0]["brief"] is None
    json.dumps(pack)


def test_signature_is_hmac_sha256():
    body = b'{"a":1}'
    expect = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert export.sign(body, "s3cret") == f"sha256={expect}"


async def test_export_job_posts_once_per_day_signed(db, tmp_path, monkeypatch):
    monkeypatch.setenv("ODE_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(export, "datetime", _Frozen)
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    secrets = Secrets(_env_file=None, database_url="x", n8n_webhook_url=HOOK,  # type: ignore[call-arg]
                      n8n_webhook_secret="s3cret")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(HOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
        async with http_client(config) as client:
            first = await export.export_job(config, secrets, db, client)
            second = await export.export_job(config, secrets, db, client)
    assert first["status"] == "ok" and first["items"] > 0 and first["webhook"] == 200
    assert second == {"status": "skipped", "reason": "already sent today"}
    assert route.call_count == 1
    req = route.calls[0].request
    assert req.headers["X-ODE-Signature"] == export.sign(req.content, "s3cret")
    sent = json.loads(req.content)
    assert sent["items"][0]["id"] and sent["items"][0]["steps"]
    assert json.loads((tmp_path / "pack-latest.json").read_text())["day"] == str(AS_OF)


async def test_export_job_failure_is_reported_and_retried(db, tmp_path, monkeypatch):
    monkeypatch.setenv("ODE_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(export, "datetime", _Frozen)
    await seed_world(db)
    config = cfg()
    await Scorer(config, db).run(AS_OF)
    secrets = Secrets(_env_file=None, database_url="x", n8n_webhook_url=HOOK)  # type: ignore[call-arg]
    with respx.mock() as mock:
        mock.post(HOOK).mock(side_effect=[httpx.Response(500), httpx.ConnectError("boom"),
                                          httpx.Response(204)])
        async with http_client(config) as client:
            r1 = await export.export_job(config, secrets, db, client)
            r2 = await export.export_job(config, secrets, db, client)
            r3 = await export.export_job(config, secrets, db, client)
    assert r1 == {"status": "error", "error": "webhook answered HTTP 500"}
    assert r2["status"] == "error" and HOOK not in r2["error"]          # URL never leaks
    assert r3["status"] == "ok"
    no_hook = Secrets(_env_file=None, database_url="x")  # type: ignore[call-arg]
    async with http_client(config) as client:
        r4 = await export.export_job(config, no_hook, db, client)
    assert r4["webhook"] == "not set"


class _Frozen:
    """datetime stand-in so 'today' is the fixture's scoring day."""

    @staticmethod
    def now(tz=None):
        from datetime import datetime
        return datetime(AS_OF.year, AS_OF.month, AS_OF.day, 7, 0, tzinfo=tz)
