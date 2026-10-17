"""API tests with FastAPI's TestClient. The Groq client is stubbed, so nothing leaves the machine."""
import json
import math

import pytest
from fastapi.testclient import TestClient

from backend.app.config import ConfigError, get_settings

SPILL_ID = "spill-20261009T2000Z"
STORM_ID = "storm-20260922T0000Z"
DEV_ORIGIN = "http://localhost:5173"


class NoLLM:
    """Stand-in Groq client: unavailable, so the chain uses the template and makes no call."""

    available = False
    unavailable_reason = "GROQ_API_KEY or GROQ_MODEL not set"


@pytest.fixture(scope="module")
def client():
    try:
        settings = get_settings()
    except ConfigError:
        pytest.skip("no .env")
    from backend.app.main import create_app

    with TestClient(create_app(settings, client_factory=lambda s: NoLLM())) as c:
        assert c.post("/scenario/run", json={"case": "spill"}).status_code == 200
        assert c.post("/scenario/run", json={"case": "storm"}).status_code == 200
        yield c


def assert_flags(body: dict) -> None:
    assert body["simulated"] is True and body["requires_human_review"] is True


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_health(client):
    body = client.get("/health").json()
    assert_flags(body)
    assert body["status"] == "ok"
    assert body["data_ready"]["baseline_snapshot"] and body["data_ready"]["reach_fixture"]


def test_reach(client):
    body = client.get("/reach", params={"max_km": 10}).json()
    assert_flags(body)
    assert body["reach"]["geometry"]["type"] == "LineString"
    assert body["reach"]["properties"]["nhdplusid"] == get_settings().reach_nhdplusid
    path = body["upstream_path"]
    assert path[0]["cumulative_km"] == 0 and path[0]["nhdplusid"] == get_settings().reach_nhdplusid
    assert all(a["cumulative_km"] <= b["cumulative_km"] for a, b in zip(path, path[1:]))
    if body["upstream_geojson"] is not None:
        assert len(body["upstream_geojson"]["features"]) == len(path)
    gauge = body["gauge"]
    assert gauge["site_id"] == "01634000" and gauge["link_distance_m"] < 100


def test_facilities(client):
    body = client.get("/facilities").json()
    assert_flags(body)
    ranks = [c["rank"] for c in body["candidates"]]
    assert ranks == list(range(1, len(ranks) + 1))
    dist = [c["upstream_distance_km"] for c in body["candidates"]]
    assert dist == sorted(dist)
    feats = body["geojson"]["features"]
    assert len(feats) == len(ranks) and all(f["geometry"]["type"] == "Point" for f in feats)


def test_facilities_unknown_reach_is_404_with_flags(client):
    r = client.get("/facilities", params={"comid": 1})
    assert r.status_code == 404
    assert_flags(r.json())


def test_scenario_run_returns_incident_ids(client):
    spill = client.post("/scenario/run", json={"case": "spill"}).json()
    assert_flags(spill)
    assert spill["incident_id"] == SPILL_ID and spill["classification"] == "incident"
    assert spill["report_mode"] == "fallback"  # the stubbed client is unavailable
    storm = client.post("/scenario/run", json={"case": "storm"}).json()
    assert storm["incident_id"] == STORM_ID and storm["classification"] == "storm_runoff"
    assert storm["agent_modes"]["trace"] == storm["agent_modes"]["compliance"] == "skipped"


def test_scenario_run_defaults_to_spill(client):
    assert client.post("/scenario/run").json()["incident_id"] == SPILL_ID


def test_incident(client):
    body = client.get(f"/incidents/{SPILL_ID}").json()
    assert_flags(body)
    for part in ("triage", "trace", "compliance", "report"):
        assert_flags(body[part])
    assert body["report"]["ranked_candidates"]
    storm = client.get(f"/incidents/{STORM_ID}").json()
    assert storm["report"]["classification"] == "storm_runoff" and storm["report"]["ranked_candidates"] == []


def test_unknown_incident_is_404_with_flags(client):
    for path in ("/incidents/nope", "/sensors/nope", "/stream/nope"):
        r = client.get(path)
        assert r.status_code == 404
        assert_flags(r.json())


def test_validation_error_has_flags(client):
    r = client.get("/reach", params={"max_km": -1})
    assert r.status_code == 422
    assert_flags(r.json())


def test_sensors_spill(client):
    body = client.get(f"/sensors/{SPILL_ID}").json()
    assert_flags(body)
    assert len(body["baseline"]) == (72 + 24) * 4 + 1
    assert all(not s["simulated"] for s in body["baseline"])
    assert body["injected"] and all(s["simulated"] for s in body["injected"])
    base = {s["timestamp"]: s["values"] for s in body["baseline"]}
    first = body["injected"][0]
    assert first["values"]["conductivity"] > base[first["timestamp"]]["conductivity"]
    assert first["values"]["discharge"] == base[first["timestamp"]]["discharge"]  # never injected


def test_sensors_storm_has_no_injected_rows(client):
    body = client.get(f"/sensors/{STORM_ID}", params={"hours_before": 0}).json()
    assert body["injected"] == [] and len(body["baseline"]) == 48 * 4 + 1


def test_sensor_gaps_are_null_not_zero(client):
    body = client.get(f"/sensors/{SPILL_ID}", params={"hours_before": 24 * 8}).json()
    values = [s["values"]["turbidity"] for s in body["baseline"]]
    assert None in values  # the 10-03 to 10-06 outage
    assert all(v is None or (isinstance(v, float) and not math.isnan(v)) for v in values)


def test_stream_spill_replays_timeline(client):
    r = client.get(f"/stream/{SPILL_ID}", params={"speed": 0})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds.count("sensor_tick") == (2 + 24) * 4 + 1
    assert kinds.count("plume_detection") > 100
    assert kinds[-5:] == ["agent_output"] * 4 + ["complete"]
    assert [d["payload"]["agent"] for k, d in events if k == "agent_output"] == ["triage", "trace", "compliance", "report"]
    for i, (_, data) in enumerate(events):
        assert data["seq"] == i and data["incident_id"] == SPILL_ID
        assert_flags(data)
    times = [d["scenario_time"] for k, d in events if k in ("sensor_tick", "plume_detection")]
    assert times == sorted(times)
    ticks = [d["payload"] for k, d in events if k == "sensor_tick"]
    assert any(t["simulated"] for t in ticks) and not ticks[0]["simulated"]
    assert events[-1][1]["payload"]["classification"] == "incident"


def test_stream_storm_has_no_plume_events(client):
    kinds = [k for k, _ in parse_sse(client.get(f"/stream/{STORM_ID}", params={"speed": 0}).text)]
    assert "plume_detection" not in kinds and kinds[-1] == "complete"


def test_stream_speed_paces_the_replay(client, monkeypatch):
    import backend.app.main as main

    pauses = []

    async def fake_sleep(s):
        pauses.append(s)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    client.get(f"/stream/{STORM_ID}", params={"speed": 900})  # 15-min ticks -> 1 s apart
    ticks = (48 + 2) * 4  # storm window plus the 2 h lead-in: one pause between consecutive ticks
    assert sum(1 for p in pauses if p == pytest.approx(1.0)) == ticks
    assert pauses.count(0.5) == 5  # fixed short pause before each agent output and completion
    assert max(pauses) <= main.MAX_STREAM_SLEEP_S
    pauses.clear()
    client.get(f"/stream/{STORM_ID}", params={"speed": 0})
    assert pauses == []


def test_cors_allows_dev_origin_only(client):
    ok = client.options("/health", headers={"Origin": DEV_ORIGIN, "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == DEV_ORIGIN
    bad = client.options("/health", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert bad.headers.get("access-control-allow-origin") is None


def test_no_secret_in_any_response(client):
    s = get_settings()
    secrets = [x.get_secret_value() for x in (s.groq_api_key, s.noaa_cdo_token, s.roboflow_api_key) if x]
    bodies = [client.get(p).text for p in ("/health", f"/incidents/{SPILL_ID}", f"/incidents/{STORM_ID}")]
    assert not any(sec in b for sec in secrets for b in bodies)
