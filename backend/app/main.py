"""FastAPI app: JSON routes and a Server-Sent Events replay over the deterministic demo scenarios.

Every response carries `simulated` and `requires_human_review`. The pipeline reads only local data
and cached responses; the only network call is the optional Groq agent chain on POST /scenario/run.
Start with: python -m uvicorn backend.app.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import networkx as nx
import pandas as pd
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException

from .agents import orchestrator
from .config import DISTANCE_CRS, SOURCE_CRS, Settings, get_settings
from .data import nhd_graph
from .data.baselines import MANIFEST, SNAPSHOT_DIR, load_snapshot
from .data.facilities import DEFAULT_BUFFER_M, candidates_geojson, find_candidate_facilities
from .detection.vision import DEBRIS_CACHE_DIR
from .schemas import (
    ErrorResponse,
    FacilitiesResponse,
    FeatureCollection,
    GaugeInfo,
    HealthResponse,
    IncidentResponse,
    ReachResponse,
    ScenarioRunRequest,
    ScenarioRunResponse,
    SensorSample,
    SensorSeriesResponse,
    StreamEvent,
)
from .simulation import scenario as scen
from .simulation.plume_renderer import REACH_FIXTURE_DIR

PARAMETERS = ("conductivity", "turbidity", "ph", "discharge")
STREAM_LEAD_IN = timedelta(hours=2)  # sensor ticks start this long before the scenario time
MAX_STREAM_SLEEP_S = 5.0  # cap on any single pause in the replay


@dataclass
class _Incident:
    scenario: scen.ScenarioResult
    response: IncidentResponse


class _State:
    """Settings, the lazily loaded river graph, and the incidents run in this process."""

    def __init__(self, settings: Settings, client_factory: Callable[[Settings], Any]):
        self.settings = settings
        self.client_factory = client_factory
        self.incidents: dict[str, _Incident] = {}
        self._graph: nx.DiGraph | None = None
        self._lock = threading.Lock()

    def graph(self) -> nx.DiGraph:
        with self._lock:
            if self._graph is None:
                self._graph = nhd_graph.load_graph()
            return self._graph


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def _reach_feature(reach_id: int) -> dict:
    path = REACH_FIXTURE_DIR / f"reach_{reach_id}.geojson"
    if not path.exists():
        raise _not_found(f"no committed geometry for reach {reach_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _gauge(settings: Settings, reach: dict) -> GaugeInfo:
    from pyproj import Transformer
    from shapely.geometry import LineString, Point

    tr = Transformer.from_crs(SOURCE_CRS, DISTANCE_CRS, always_xy=True)
    line = LineString([tr.transform(x, y) for x, y in reach["geometry"]["coordinates"]])
    point = Point(tr.transform(settings.gauge_longitude, settings.gauge_latitude))
    return GaugeInfo(
        site_id=settings.nwis_site_id,
        name=settings.gauge_name,
        latitude=settings.gauge_latitude,
        longitude=settings.gauge_longitude,
        linked_reach_nhdplusid=settings.reach_nhdplusid,
        link_distance_m=round(line.distance(point), 1),
    )


def _upstream_geojson(settings: Settings, trace: list) -> FeatureCollection | None:
    """LineStrings of the traced reaches from the local GeoPackage; None if it is unavailable."""
    try:
        gdf = nhd_graph.reach_geometries(settings.nhdplus_gpkg_path, [r.nhdplusid for r in trace])
    except Exception:  # noqa: BLE001 - geometry is optional context for the map
        return None
    by_id = {r.nhdplusid: r for r in trace}
    features = []
    for row in gdf.itertuples():
        r = by_id.get(int(row.nhdplusid))
        if r is None or row.geometry is None:
            continue
        geom = json.loads(gpd_to_geojson(row.geometry))
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "nhdplusid": r.nhdplusid, "gnis_name": r.gnis_name, "length_km": r.length_km,
                "cumulative_km": r.cumulative_km, "velocity_mps": r.velocity_mps,
            },
        })
    features.sort(key=lambda f: (f["properties"]["cumulative_km"], f["properties"]["nhdplusid"]))
    return FeatureCollection(features=features)


def gpd_to_geojson(geom: Any) -> str:
    """Geometry as GeoJSON with coordinates rounded to 6 decimals (~0.1 m) to keep payloads small."""
    from shapely.geometry import mapping

    def rnd(c: Any) -> Any:
        return [rnd(x) for x in c] if isinstance(c, (list, tuple)) and c and isinstance(c[0], (list, tuple)) \
            else [round(float(v), 6) for v in c]

    m = mapping(geom)
    return json.dumps({"type": m["type"], "coordinates": rnd(m["coordinates"])})


def _value(v: Any) -> float | None:
    """NaN (a gauge gap) becomes null; values are rounded to 4 decimals for the wire."""
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 4)


def _samples(frame: pd.DataFrame, mask: pd.Series | None = None) -> list[SensorSample]:
    rows = frame if mask is None else frame[mask]
    sim = rows["simulated"] if "simulated" in rows else pd.Series(False, index=rows.index)
    return [
        SensorSample(
            timestamp=ts.to_pydatetime(),
            values={p: _value(row[p]) for p in PARAMETERS if p in rows},
            simulated=bool(sim.loc[ts]),
        )
        for ts, row in rows.iterrows()
    ]


def _window(rec: _Incident, hours_before: float, hours_after: float | None) -> tuple[pd.Timestamp, pd.Timestamp]:
    sc = rec.scenario
    after = hours_after if hours_after is not None else (scen.DETECTION_HOURS if sc.case == "spill" else scen.STORM_HOURS)
    t0 = pd.Timestamp(sc.scenario_timestamp)
    return t0 - pd.Timedelta(hours=hours_before), t0 + pd.Timedelta(hours=after)


def _timeline(rec: _Incident) -> list[tuple[datetime | None, str, dict]]:
    """(scenario_time, kind, payload) for the replay: sensor ticks and plume frames in time order,
    then each agent's output, then completion."""
    sc, resp = rec.scenario, rec.response
    start, end = _window(rec, STREAM_LEAD_IN.total_seconds() / 3600, None)
    frame = sc.sensors.loc[start:end]
    events: list[tuple[datetime | None, int, str, dict]] = []
    for ts, row in frame.iterrows():
        t = ts.to_pydatetime()
        active = sorted({e.parameter for e in sc.sensor_events if e.timestamp <= t <= e.end_timestamp})
        events.append((t, 0, "sensor_tick", {
            "values": {p: _value(row[p]) for p in PARAMETERS if p in frame},
            "simulated": bool(row.get("simulated", False)),
            "anomalous_parameters": active,
        }))
    if sc.vision is not None:
        for f in sc.vision.frames:
            if f.plume_detected:
                events.append((f.timestamp, 1, "plume_detection", {
                    "frame_index": f.frame_index, "area_fraction": round(f.area_fraction, 5),
                    "confidence": round(f.confidence, 4), "clip": sc.vision.clip,
                }))
    events.sort(key=lambda e: (e[0], e[1]))
    out: list[tuple[datetime | None, str, dict]] = [(t, k, p) for t, _, k, p in events]
    last = end.to_pydatetime()
    for name in orchestrator.AGENTS:
        result = getattr(resp, name)
        out.append((last, "agent_output", {
            "agent": name, "mode": resp.agent_modes.get(name), "output": result.model_dump(mode="json"),
        }))
    out.append((last, "complete", {
        "classification": resp.report.classification, "report_mode": resp.report.report_mode,
        "incident_id": resp.incident_id,
    }))
    return out


def _sse(event: StreamEvent) -> str:
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"


def create_app(settings: Settings | None = None, client_factory: Callable[[Settings], Any] | None = None) -> FastAPI:
    """Build the app. client_factory returns the Groq client for the agent chain (tests inject a stub)."""
    settings = settings or get_settings()
    state = _State(settings, client_factory or orchestrator.make_client)
    app = FastAPI(
        title="SilentSpill API",
        version="0.1.0",
        description="Simulated spill detection and upstream tracing. Every response is simulated and requires human review.",
    )
    app.state.silentspill = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(ErrorResponse(detail=str(exc.detail)).model_dump(mode="json"), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return JSONResponse(ErrorResponse(detail=detail).model_dump(mode="json"), status_code=422)

    def incident(incident_id: str) -> _Incident:
        rec = state.incidents.get(incident_id)
        if rec is None:
            raise _not_found(f"unknown incident {incident_id!r}; run POST /scenario/run first")
        return rec

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        s = state.settings
        ready = {
            "baseline_snapshot": (SNAPSHOT_DIR / MANIFEST).exists(),
            "reach_fixture": (REACH_FIXTURE_DIR / f"reach_{s.reach_nhdplusid}.geojson").exists(),
            "nhdplus_gpkg": s.nhdplus_gpkg_path.exists(),
            "echo_data": s.echo_npdes_dir.exists() and s.echo_effluent_dir.exists(),
            "tri_data": s.tri_csv_path.exists(),
            "roboflow_cache": (DEBRIS_CACHE_DIR / "roboflow_spill.json").exists(),
            "groq_configured": s.groq_configured,  # optional: without it the report uses the template
            "naip_imagery": s.naip_available,  # optional: clips are drawn without it
        }
        required = ("baseline_snapshot", "reach_fixture", "nhdplus_gpkg", "echo_data", "tri_data")
        return HealthResponse(
            status="ok" if all(ready[k] for k in required) else "degraded",
            simulation_mode=s.simulation_mode,
            data_ready=ready,
        )

    @app.get("/reach", response_model=ReachResponse)
    def reach(max_km: float = Query(scen.DEFAULT_MAX_KM, gt=0, le=200)) -> ReachResponse:
        s = state.settings
        feature = _reach_feature(s.reach_nhdplusid)
        trace = nhd_graph.trace_upstream(state.graph(), s.reach_nhdplusid, max_km)
        return ReachResponse(
            reach=feature, max_km=max_km, upstream_path=trace,
            upstream_geojson=_upstream_geojson(s, trace), gauge=_gauge(s, feature),
        )

    @app.get("/facilities", response_model=FacilitiesResponse)
    def facilities(
        comid: int | None = Query(None, description="NHDPlusID of the start reach; defaults to REACH_COMID"),
        max_km: float = Query(scen.DEFAULT_MAX_KM, gt=0, le=200),
        buffer_m: float = Query(DEFAULT_BUFFER_M, gt=0, le=5000),
    ) -> FacilitiesResponse:
        s = state.settings
        start = s.reach_nhdplusid if comid is None else comid
        try:
            cands = find_candidate_facilities(
                state.graph(), start, max_km, buffer_m, reference_date=s.scenario_timestamp.date()
            )
        except nhd_graph.ReachNotFoundError:
            raise _not_found(f"reach {start} is not in the river network") from None
        return FacilitiesResponse(comid=start, max_km=max_km, candidates=cands, geojson=candidates_geojson(cands))

    @app.post("/scenario/run", response_model=ScenarioRunResponse)
    def run_scenario(body: ScenarioRunRequest | None = None) -> ScenarioRunResponse:
        case = (body or ScenarioRunRequest()).case
        s, G = state.settings, state.graph()
        sc = scen.run_spill_scenario(s, G) if case == "spill" else scen.run_storm_scenario(s, G)
        resp = orchestrator.run_chain(sc, client=state.client_factory(s), settings=s, G=G)
        state.incidents[resp.incident_id] = _Incident(sc, resp)
        return ScenarioRunResponse(
            incident_id=resp.incident_id, status="completed", case=case,
            classification=resp.report.classification, report_mode=resp.report.report_mode,
            agent_modes=resp.agent_modes,
        )

    @app.get("/incidents/{incident_id}", response_model=IncidentResponse)
    def get_incident(incident_id: str) -> IncidentResponse:
        return incident(incident_id).response

    @app.get("/sensors/{incident_id}", response_model=SensorSeriesResponse)
    def sensors(
        incident_id: str,
        hours_before: float = Query(72, ge=0, le=24 * 28),
        hours_after: float | None = Query(None, ge=0, le=24 * 28, description="default 24 (spill) or 48 (storm)"),
    ) -> SensorSeriesResponse:
        rec = incident(incident_id)
        start, end = _window(rec, hours_before, hours_after)
        baseline = load_snapshot().sensors.loc[start:end]
        injected = rec.scenario.sensors.loc[start:end]
        mask = injected["simulated"] if "simulated" in injected else pd.Series(False, index=injected.index)
        return SensorSeriesResponse(
            incident_id=incident_id, site_id=state.settings.nwis_site_id,
            baseline=_samples(baseline), injected=_samples(injected, mask.astype(bool)),
        )

    @app.get("/stream/{incident_id}")
    async def stream(
        incident_id: str,
        request: Request,
        speed: float | None = Query(
            None, ge=0, description="scenario seconds per wall-clock second; 0 sends everything at once"
        ),
    ) -> StreamingResponse:
        rec = incident(incident_id)
        rate = state.settings.stream_speed if speed is None else speed
        timeline = _timeline(rec)

        async def events() -> AsyncIterator[str]:
            prev: datetime | None = None
            for seq, (t, kind, payload) in enumerate(timeline):
                if await request.is_disconnected():
                    return
                if rate > 0:
                    if kind in ("agent_output", "complete"):
                        pause = 0.5
                    else:
                        pause = 0.0 if prev is None or t is None else (t - prev).total_seconds() / rate
                    if pause > 0:
                        await asyncio.sleep(min(pause, MAX_STREAM_SLEEP_S))
                if t is not None:
                    prev = t
                yield _sse(StreamEvent(incident_id=incident_id, seq=seq, kind=kind, scenario_time=t, payload=payload))

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
