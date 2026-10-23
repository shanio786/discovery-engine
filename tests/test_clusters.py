"""Niches: leader clustering, niche scoring, embeddings endpoint (respx), plan use."""

import json

import httpx
import numpy as np
import respx

from ode import clusters
from ode.config import Secrets
from ode.pipeline import http_client
from ode.scoring import plan as planmod
from ode.scoring.scorer import Scorer
from tests.test_plan_briefs import cfg
from tests.test_scoring_db import AS_OF, seed_world

URL = "https://api.runpod.ai/v2/abc123/openai/v1/embeddings"


def test_leader_cluster_groups_similar_rows():
    v = np.array([[1, 0, 0], [0.95, 0.1, 0], [0, 1, 0], [0.9, 0.05, 0.1], [0, 0.97, 0.2],
                  [0, 0, 1]], dtype=float)
    labels = clusters.leader_cluster(v, 0.8)
    assert labels[0] == labels[1] == labels[3]
    assert labels[2] == labels[4] != labels[0]
    assert labels[5] not in (labels[0], labels[2])
    assert clusters.leader_cluster(np.zeros((0, 3)), 0.8) == []
    assert clusters.leader_cluster(v, 1.01) == list(range(6))     # nothing similar enough


def test_niche_score_prefers_big_growing_open_money():
    def m(i, views, growing, open_, money, market="en-US"):
        return clusters.Member(i, f"t{i}", views, growing, open_, money, {market: views})
    hot = [m(1, 5000, True, 80, 70), m(2, 3000, True, 70, 60), m(3, 2000, False, 60, 50)]
    cold = [m(4, 50, False, 10, 20), m(5, 40, False, 20, 20), m(6, 30, False, None, 20)]
    a, b = clusters.niche_score(hot, {"en-US": 1.0}), clusters.niche_score(cold, {"en-US": 1.0})
    assert a["score"] > b["score"] and a["growing_share"] == 0.67 and a["size"] == 3
    assert a["best_markets"] == ["en-US"] and b["openness"] == 15.0
    assert clusters.signature([3, 1, 2]) == clusters.signature([1, 2, 3])


def vec_for(text):
    t = text.lower()
    if "fad" in t:
        return [0.0, 1.0, 0.0]
    if any(k in t for k in ("heat", "gadget", "raspberry", "wärme")):
        return [1.0, 0.05 * len(t) / 20, 0.0]
    return [0.0, 0.0, 1.0]


async def test_cluster_job_end_to_end(db, monkeypatch):
    monkeypatch.setattr(clusters, "_today", lambda: AS_OF)
    await seed_world(db)
    config = cfg()
    config.raw()["clusters"] = {"threshold": 0.9, "min_size": 3}
    config.raw()["rate_limits"]["embeddings"] = {"rate": 10_000.0, "burst": 10_000}
    await Scorer(config, db).run(AS_OF)
    secrets = Secrets(_env_file=None, database_url="x", embeddings_url=URL,  # type: ignore[call-arg]
                      embeddings_api_key="rp-key")
    seen_inputs = []

    def answer(request):
        body = json.loads(request.content)
        seen_inputs.extend(body["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": vec_for(t)}
                                                  for i, t in enumerate(body["input"])]})
    with respx.mock() as mock:
        route = mock.post(URL).mock(side_effect=answer)
        async with http_client(config) as client:
            r = await clusters.cluster_job(db, config, secrets, client)
            again = await clusters.cluster_job(db, config, secrets, client)
        assert route.calls[0].request.headers["authorization"] == "Bearer rp-key"
        assert json.loads(route.calls[0].request.content)["model"] == "BAAI/bge-m3"
    assert r["status"] == "ok" and r["niches"] == 1 and r["embedded"] >= 4
    assert again["embedded"] == 0                                    # cached vectors
    niches = await clusters.load_niches(db)
    assert len(niches) == 1 and niches[0]["size"] == 3
    assert {m["name"] for m in niches[0]["members"]} == {"Heat pump", "Gadget tracker",
                                                         "Raspberry Pi"}
    assert niches[0]["label"] == "Raspberry Pi"                       # strongest topic
    plan = await planmod.build_plan(db, config, AS_OF)
    assert any("niche “Raspberry Pi”: 3 related topics" in w
               for a in plan if a.topic == "Heat pump" for w in a.why)
    assert "Raspberry Pi — 3 topics" in clusters.render_niches(niches)


async def test_cluster_job_skips_without_provider(db):
    async with http_client(cfg()) as client:
        r = await clusters.cluster_job(db, cfg(), Secrets(_env_file=None, database_url="x"),  # type: ignore[call-arg]
                                       client)
    assert r["status"] == "skipped" and "EMBEDDINGS_URL" in r["reason"]
