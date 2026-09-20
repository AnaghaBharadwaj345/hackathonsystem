"""API tests. These exercise the real app through the ASGI transport."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _new_run(**kwargs):
    body = {"seed": 4207, "policy": "medflow", "load_pct": 100}
    body.update(kwargs)
    r = client.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_health_and_config():
    assert client.get("/api/health").json()["status"] == "ok"
    cfg = client.get("/api/config").json()
    assert {p["key"] for p in cfg["policies"]} == {"fifo", "triage", "medflow"}
    assert cfg["target_wait_min"]["1"] == 5 or cfg["target_wait_min"][1] == 5


def test_create_advance_and_finish():
    run = _new_run()
    rid = run["run_id"]
    assert run["snapshot"]["t"] == 0

    r = client.post(f"/api/runs/{rid}/advance", json={"minutes": 480}).json()
    assert r["minutes_advanced"] == 480
    assert r["snapshot"]["t"] == 480
    assert r["snapshot"]["clock"] == "14:00"

    r = client.post(f"/api/runs/{rid}/advance", json={"minutes": 5000}).json()
    assert r["snapshot"]["finished"] is True
    assert r["snapshot"]["metrics"]["seen"] > 100


def test_snapshot_structure():
    run = _new_run()
    rid = run["run_id"]
    client.post(f"/api/runs/{rid}/advance", json={"minutes": 600})
    snap = client.get(f"/api/runs/{rid}").json()["snapshot"]

    assert {r["key"] for r in snap["resources"]} >= {"icu", "ward", "doctor"}
    for res in snap["resources"]:
        assert len(res["units"]) == res["capacity"]
        assert res["in_use"] == sum(1 for u in res["units"] if u is not None)
    assert snap["counts"]["waiting"] == len(snap["waiting"]) or snap["counts"]["waiting"] > 25


def test_surge_adds_patients():
    run = _new_run()
    rid = run["run_id"]
    client.post(f"/api/runs/{rid}/advance", json={"minutes": 200})
    before = client.get(f"/api/runs/{rid}").json()["snapshot"]["counts"]["waiting"]
    client.post(f"/api/runs/{rid}/surge", json={"count": 10})
    after = client.get(f"/api/runs/{rid}").json()["snapshot"]["counts"]["waiting"]
    assert after == before + 10


def test_reset_rewinds():
    run = _new_run()
    rid = run["run_id"]
    client.post(f"/api/runs/{rid}/advance", json={"minutes": 300})
    snap = client.post(f"/api/runs/{rid}/reset").json()["snapshot"]
    assert snap["t"] == 0
    assert snap["metrics"]["seen"] == 0


def test_capacity_override():
    run = _new_run(capacity={"icu": 14, "or": 5})
    res = {r["key"]: r for r in run["snapshot"]["resources"]}
    assert res["icu"]["capacity"] == 14
    assert res["or"]["capacity"] == 5


def test_csv_export():
    run = _new_run()
    rid = run["run_id"]
    client.post(f"/api/runs/{rid}/advance", json={"minutes": 1440})
    r = client.get(f"/api/runs/{rid}/patients.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("patient_id,")
    assert len(r.text.splitlines()) > 50


def test_delete_and_missing_run():
    rid = _new_run()["run_id"]
    assert client.delete(f"/api/runs/{rid}").status_code == 204
    assert client.get(f"/api/runs/{rid}").status_code == 404


def test_validation_rejects_bad_input():
    assert client.post("/api/runs", json={"policy": "random"}).status_code == 422
    assert client.post("/api/runs", json={"load_pct": 9999}).status_code == 422


def test_benchmark_endpoint():
    r = client.post("/api/benchmark",
                    json={"seed": 4207, "load_pct": 100, "replications": 2}).json()
    assert len(r["rows"]) == 3
    med = next(x for x in r["rows"] if x["policy"] == "medflow")
    tri = next(x for x in r["rows"] if x["policy"] == "triage")
    assert med["walked_out"] < tri["walked_out"]
    assert "best" in r


def test_what_if_endpoint():
    r = client.post("/api/what-if",
                    json={"seed": 4207, "load_pct": 150, "replications": 2,
                          "deltas": [{"resource": "doctor", "delta": 3},
                                     {"resource": "icu", "delta": 3}]}).json()
    assert len(r["options"]) == 2
    assert r["options"][0]["resource"] in {"doctor", "icu"}


def test_websocket_streams_and_accepts_commands():
    rid = _new_run()["run_id"]
    with client.websocket_connect(f"/ws/runs/{rid}?speed=120&interval_ms=40") as ws:
        first = ws.receive_json()["snapshot"]
        assert first["t"] == 120
        ws.send_json({"action": "surge", "count": 5})
        second = ws.receive_json()["snapshot"]
        assert second["t"] > first["t"]
        ws.send_json({"action": "stop"})


def test_websocket_rejects_unknown_run():
    with client.websocket_connect("/ws/runs/nope") as ws:
        assert "error" in ws.receive_json()
