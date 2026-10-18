"""Regenerate docs/api.md from a real run of every endpoint (in-process, via FastAPI's TestClient).

Usage: python scripts/make_api_docs.py
Long lists are trimmed in the doc (noted inline); the full responses are written to
docs/api_examples/<name>.json. Uses the configured Groq client if GROQ_API_KEY and GROQ_MODEL are
set, otherwise the template fallback; the doc states which mode produced the examples.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.config import PROJECT_ROOT, get_settings  # noqa: E402
from backend.app.main import create_app  # noqa: E402

DOC = PROJECT_ROOT / "docs" / "api.md"
EXAMPLES = PROJECT_ROOT / "docs" / "api_examples"
KEEP = 2  # list items shown per list in the doc


def trim(value, keep: int = KEEP, path: str = ""):
    """Copy of value with long lists cut to `keep` items plus a note of how many were left out."""
    if isinstance(value, dict):
        return {k: trim(v, keep, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        if path.endswith("coordinates") and value and isinstance(value[0], list):
            head = [trim(v, keep, path) for v in value[:3]]
            return head + ([f"... {len(value) - 3} more points"] if len(value) > 3 else [])
        head = [trim(v, keep, path) for v in value[:keep]]
        return head + ([f"... {len(value) - keep} more"] if len(value) > keep else [])
    return value


def block(obj) -> str:
    return "```json\n" + json.dumps(obj, indent=2, ensure_ascii=False) + "\n```"


def main() -> int:
    settings = get_settings()
    client = TestClient(create_app(settings))
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    shots: dict[str, object] = {}

    def save(name: str, obj) -> None:
        shots[name] = obj
        (EXAMPLES / f"{name}.json").write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    save("health", client.get("/health").json())
    save("reach", client.get("/reach", params={"max_km": 50}).json())
    save("facilities", client.get("/facilities", params={"max_km": 50}).json())
    save("scenario_run_spill", client.post("/scenario/run", json={"case": "spill"}).json())
    save("scenario_run_storm", client.post("/scenario/run", json={"case": "storm"}).json())
    spill_id = shots["scenario_run_spill"]["incident_id"]
    storm_id = shots["scenario_run_storm"]["incident_id"]
    save("review_spill", client.post(f"/incidents/{spill_id}/review", json={
        "decision": "confirmed", "note": "Example review posted by scripts/make_api_docs.py."}).json())
    save("incident_spill", client.get(f"/incidents/{spill_id}").json())
    save("incident_storm", client.get(f"/incidents/{storm_id}").json())
    save("sensors_spill", client.get(f"/sensors/{spill_id}", params={"hours_before": 1, "hours_after": 1}).json())
    stream_text = client.get(f"/stream/{spill_id}", params={"speed": 0}).text
    (EXAMPLES / "stream_spill.txt").write_text(stream_text, encoding="utf-8")
    save("error_404", client.get("/incidents/unknown-id").json())

    # One raw SSE block per event kind, as it arrives on the wire.
    first_of: dict[str, str] = {}
    for blk in stream_text.split("\n\n"):
        for line in blk.split("\n"):
            if line.startswith("event: "):
                first_of.setdefault(line[7:], blk)
    counts = {k: stream_text.count(f"event: {k}\n") for k in first_of}
    sse_examples = []
    for kind, blk in first_of.items():
        head, data = blk.rsplit("data: ", 1)
        pretty = json.dumps(trim(json.loads(data), keep=1), indent=2, ensure_ascii=False)
        sse_examples.append(f"`{kind}` ({counts[kind]} in the spill replay):\n\n```text\n{head}data: {pretty}\n```")

    mode = shots["scenario_run_spill"]["report_mode"]
    modes = shots["scenario_run_spill"]["agent_modes"]
    doc = DOC_TEMPLATE.format(
        mode=mode,
        modes=", ".join(f"{k} `{v}`" for k, v in modes.items()),
        health=block(shots["health"]),
        reach=block(trim(shots["reach"])),
        facilities=block(trim(shots["facilities"])),
        run_spill=block(shots["scenario_run_spill"]),
        run_storm=block(shots["scenario_run_storm"]),
        incident_spill=block(trim(shots["incident_spill"])),
        incident_storm=block(trim(shots["incident_storm"], keep=3)),
        sensors=block(trim(shots["sensors_spill"])),
        sse="\n\n".join(sse_examples),
        error=block(shots["error_404"]),
        review=block(shots["review_spill"]),
        memory_mode=shots["scenario_run_spill"].get("memory_mode"),
        origins=", ".join(f"`{o}`" for o in settings.cors_origin_list),
        speed=settings.stream_speed,
    )
    DOC.write_text(doc, encoding="utf-8")
    print(f"wrote {DOC.relative_to(PROJECT_ROOT)} and {len(list(EXAMPLES.iterdir()))} files in "
          f"{EXAMPLES.relative_to(PROJECT_ROOT)} (report mode: {mode})")
    return 0


DOC_TEMPLATE = """# SilentSpill API

JSON over HTTP plus one Server-Sent Events stream. Every response body, including errors and every
SSE event, carries `"simulated": true` and `"requires_human_review": true`.

The examples below come from a real run of this backend (`python scripts/make_api_docs.py`,
which regenerates this file). Long lists are cut to a few items with a `"... N more"` marker; the
full responses are in [`docs/api_examples/`](api_examples/). In this run the incident report was
produced in **`{mode}`** mode ({modes}).

## Running the server

```
.venv\\Scripts\\python.exe -m uvicorn backend.app.main:app --reload --port 8000
```

- Base URL: `http://localhost:8000`. Interactive schema docs: `http://localhost:8000/docs`.
- CORS allows these origins (`CORS_ORIGINS` in `.env`, comma-separated): {origins}.
- Typical frontend flow: `GET /reach` and `GET /facilities` for the map, then `POST /scenario/run`,
  then open `GET /stream/{{incident_id}}` to animate, and `GET /incidents/{{incident_id}}` for the report.

## Conventions

- Times are UTC ISO 8601 (`2026-10-09T20:00:00Z`).
- GeoJSON is in EPSG:4269 (NAD83) lon/lat, which a web map can treat as WGS84 at this scale.
- A gauge gap is `null`, never `0`. A missing rain reading is "no rain reading", never "no rain".
- `report_mode` / `agent_modes`: `llm` (the Groq model answered and passed the guardrails),
  `fallback` (the model was expected but failed or is not configured; the template was used),
  `skipped` (no model call by design, e.g. trace and compliance for storm runoff).
- Travel times use `travel_time.display`. A value starting with `≥` is a lower bound (some reaches
  have no velocity data); show it as written, never as an estimate.
- Wording is non-accusatory: facilities are "facilities with reported violations near this reach".
- Incident memory (optional, Backboard): every completed run is saved, and before Triage up to 3
  memories of past runs and reviews on the same reach are recalled. They pass through the same
  guardrails as model output (no new facilities or numbers) and never change the classification or
  confidence. The report lists them under `related_past_events` (`related_past_events_text` is
  `"none"` when there are none). `memory_mode` on the incident and run responses is `backboard`,
  `skipped` (no `BACKBOARD_API_KEY`) or `failed` (a call failed; the run still completes). In this run:
  `{memory_mode}`.
- Every incident response (`POST /scenario/run`, `GET /incidents/{{id}}`) and every report carry a fixed
  `disclaimer`: "Simulated incident. Facilities are listed only because they have reported permit
  violations upstream; no link to any real event is implied." Show it with the report.
- Each candidate has `permit_type` (`individual`, `general` or null), `facility_type`, and
  `small_domestic_general_permit` (true for a small domestic sewage general permit, such as Virginia's VAG40).
  `permit_note` is a ready-to-show sentence. These never affect the ranking.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Redirects to the interactive docs at `/docs` |
| GET | `/health` | Readiness of local data and optional services |
| GET | `/reach?max_km=50` | Demo reach geometry, the traced upstream network, and the gauge |
| GET | `/facilities?comid=&max_km=50&buffer_m=1000` | Candidate facilities with reported violations (GeoJSON + details) |
| POST | `/scenario/run` | Run the deterministic simulated spill (`{{"case": "spill"}}`) or the real storm (`{{"case": "storm"}}`) |
| GET | `/incidents/{{id}}` | Triage, trace, compliance and the final report |
| POST | `/incidents/{{id}}/review` | Record a human review decision (saved to incident memory) |
| GET | `/sensors/{{id}}?hours_before=72&hours_after=` | Real baseline series plus the injected (simulated) rows |
| GET | `/stream/{{id}}?speed=` | SSE replay of the scenario timeline |

### GET /health

`status` is `ok` when the required local data is present (`groq_configured` and `naip_imagery` are optional).

{health}

### GET /reach

`reach` is the demo reach (LineString, upstream → downstream). `upstream_path` lists every traced
reach nearest-first with its river distance; `upstream_geojson` has their LineStrings (null if the
GeoPackage is not on this machine). `gauge` is the USGS gauge and its distance to the reach.

{reach}

### GET /facilities

Permits with at least one reported numeric limit violation in the violation window, within
`buffer_m` of the traced river, ranked by river distance (never by travel time). `location_type`
says whether the point is a reported outfall or only the facility location.

{facilities}

### POST /scenario/run

Body (optional): `{{"case": "spill"}}` (default) or `{{"case": "storm"}}`. Runs synchronously (about
10 s, longer with the Groq chain) and stores the result in memory under a deterministic id.

Spill:

{run_spill}

Storm (real observed data; storm runoff gets no candidate list, so trace and compliance are skipped):

{run_storm}

### GET /incidents/{{id}}

Spill (`incident`, ranked candidates):

{incident_spill}

Storm (`storm_runoff`, no candidates):

{incident_storm}

### POST /incidents/{{id}}/review

Records a person's decision on an incident and saves it to incident memory (Backboard), linked by
`incident_id`, so later runs on the same reach can recall it. The decision does not change the
stored incident.

Body: `{{"decision": "confirmed" | "storm_runoff" | "false_alarm", "note": "..."}}` (`note` optional, up
to 2,000 characters). Unknown incident: 404. Any other decision: 422. `memory_mode` is `backboard`
when saved, `skipped` without `BACKBOARD_API_KEY`, `failed` when the call failed (the review is still
returned; `memory_reason` gives the error type, never the key).

{review}

### GET /sensors/{{id}}

`baseline` is the real gauge record; `injected` holds only the rows the simulation changed, with
`simulated: true`. `discharge` is never injected. (Example requested with `hours_before=1&hours_after=1`.)

{sensors}

### GET /stream/{{id}} (Server-Sent Events)

Replays the scenario: `sensor_tick` every 15 minutes of scenario time (from 2 h before the
scenario timestamp), `plume_detection` for each camera frame with dye, interleaved in time order,
then one `agent_output` per agent (triage, trace, compliance, report), then `complete`.

- `speed` = scenario seconds per wall-clock second (default {speed:g} from `STREAM_SPEED`: one sensor
  tick every 1.5 s). `speed=0` sends everything at once. Single pauses are capped at 5 s.
- Each message has `id: <seq>`, `event: <kind>` and `data: <StreamEvent JSON>`. On the wire `data:`
  is one JSON line; it is pretty-printed below for reading.

```js
const es = new EventSource(`${{base}}/stream/${{incidentId}}?speed=600`);
es.addEventListener("sensor_tick", (e) => {{ const ev = JSON.parse(e.data); /* ev.payload.values */ }});
es.addEventListener("plume_detection", (e) => {{ /* JSON.parse(e.data).payload.confidence */ }});
es.addEventListener("agent_output", (e) => {{ /* JSON.parse(e.data).payload.agent, .output */ }});
es.addEventListener("complete", () => es.close());
```

{sse}

### Errors

Errors use the same flags plus `detail`. Unknown incident ids return 404 (run `POST /scenario/run`
first; incidents live in server memory), and invalid query values return 422.

{error}
"""

if __name__ == "__main__":
    sys.exit(main())
