"""Plain Python tools the agents call. Every result is recorded in a ToolLedger.

The model never calls tools itself: agent code calls them, records the results, and passes them
to the model as JSON. Guardrails then accept only facility IDs, names and numbers in the ledger.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from ..data import nhd_graph
from ..schemas import CandidateFacility, TravelTime
from ..simulation.scenario import ScenarioResult
from .guardrails import normalize_number, number_tokens

# Per-reach values from trace_upstream are not citable numbers: a 50 km trace has hundreds of
# lengths and velocities, and rounding them would allow almost any small decimal in prose.
_TRACE_TOOL = "trace_upstream"


def _float_variants(x: float) -> set[str]:
    """A float as prose may cite it: rounded to 0-4 decimals, or floored to 1-2 (the '≥' bounds)."""
    if not math.isfinite(x):
        return set()
    x = abs(x)
    out = {normalize_number(f"{x:.{d}f}") for d in range(5)}
    out.add(normalize_number(f"{x:.10f}"))
    for d in (1, 2):
        out.add(normalize_number(f"{math.floor(x * 10**d) / 10**d:.{d}f}"))
    return out


def _collect_numbers(value: Any, out: set[str]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        out.add(str(abs(value)))
    elif isinstance(value, float):
        out |= _float_variants(value)
    elif isinstance(value, str):
        out.update(number_tokens(value))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_numbers(v, out)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _collect_numbers(v, out)


def _jsonable(value: Any) -> Any:
    """Datetimes to ISO strings so the ledger stays plain JSON."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _collect_facilities(value: Any, ids: set[str], names: set[str]) -> None:
    if isinstance(value, dict):
        fid = value.get("facility_id")
        if isinstance(fid, str) and fid:
            ids.add(fid)
            name = value.get("name")
            if isinstance(name, str) and name:
                names.add(name)
            gp = value.get("general_permit_id")  # master general permit ID, also from the ECHO data
            if isinstance(gp, str) and gp:
                ids.add(gp)
        for v in value.values():
            _collect_facilities(v, ids, names)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_facilities(v, ids, names)


@dataclass
class ToolLedger:
    """Every tool call and its result; the only source of citable IDs, names and numbers."""

    calls: list[dict] = field(default_factory=list)

    def record(self, tool: str, args: dict, result: Any) -> None:
        self.calls.append({"tool": tool, "args": copy.deepcopy(args), "result": copy.deepcopy(result)})

    def facility_ids(self) -> set[str]:
        ids: set[str] = set()
        for call in self.calls:
            _collect_facilities(call["result"], ids, set())
        return ids

    def facility_names(self) -> set[str]:
        names: set[str] = set()
        for call in self.calls:
            _collect_facilities(call["result"], set(), names)
        return names

    def numbers(self) -> set[str]:
        """Numbers in tool args and results, normalized like guardrails.number_tokens."""
        out: set[str] = set()
        for call in self.calls:
            _collect_numbers(call["args"], out)
            result = call["result"]
            if call["tool"] == _TRACE_TOOL and isinstance(result, list):
                # Reach IDs, the reach count and the traced extent only.
                _collect_numbers([r.get("nhdplusid") for r in result if isinstance(r, dict)], out)
                out.add(str(len(result)))
                km = [r.get("cumulative_km") for r in result if isinstance(r, dict)]
                km = [k for k in km if isinstance(k, (int, float))]
                if km:
                    _collect_numbers(max(km), out)
            else:
                _collect_numbers(result, out)
        return out

    def results(self, tool: str) -> list[Any]:
        """Results of every call to one tool, in call order."""
        return [c["result"] for c in self.calls if c["tool"] == tool]


class Tools:
    """Tool wrappers over one ScenarioResult. Unknown facility IDs raise ValueError."""

    def __init__(self, scenario: ScenarioResult, G: nx.DiGraph | None = None, ledger: ToolLedger | None = None):
        self.scenario = scenario
        self.G = G
        self.ledger = ledger if ledger is not None else ToolLedger()
        self._by_id: dict[str, CandidateFacility] = {c.facility_id: c for c in scenario.candidates}
        # The agents start from the pipeline outputs, so their values are citable like any tool result.
        self.get_pipeline_outputs()

    def get_pipeline_outputs(self) -> dict:
        """Gauge events, storm filter, rain, camera summary, fusion and the run settings.

        Per-frame vision values are left out (counts and the plume window are kept) for the same
        reason per-reach trace values are: hundreds of small decimals would allow almost any number.
        """
        sc = self.scenario
        vision = None
        if sc.vision is not None:
            v = sc.vision
            vision = {
                "clip": v.clip, "frames_analyzed": v.frames_analyzed, "plume_frames": len(v.plume_detections),
                "first_plume": v.first_plume, "last_plume": v.last_plume,
                "peak_plume_confidence": max((d.confidence for d in v.plume_detections), default=0.0),
                "debris_status": v.debris_status, "debris_predictions": len(v.debris),
            }
        from ..config import get_settings  # deferred: tests may build Tools without a full .env

        try:
            s = get_settings()
            run_settings = {
                "rain_threshold_inches": s.rain_threshold_inches, "rain_window_hours": s.rain_window_hours,
                "violation_window_years": s.violation_window_years,
            }
        except Exception:  # noqa: BLE001
            run_settings = {}
        result = {
            "case": sc.case,
            "scenario_timestamp": sc.scenario_timestamp,
            "data_origin": sc.data_origin,
            "max_km": sc.max_km,
            "max_delay_hours": sc.max_delay.total_seconds() / 3600,
            "anomalous_parameters": sc.anomalous_parameters,
            "sensor_events": [e.model_dump(mode="json") for e in sc.sensor_events],
            "storm_filter": sc.storm.model_dump(mode="json", exclude={"decisions": {"__all__": {"event"}}}),
            # Fused events repeat every per-frame detection; the plume window is in "vision" instead.
            "fusion": sc.fusion.model_dump(
                mode="json", exclude={"events": {"__all__": {"vision_detections", "sensor_events"}}}
            ),
            "vision": vision,
            "settings": run_settings,
        }
        result = _jsonable(result)
        self.ledger.record("get_pipeline_outputs", {}, result)
        return result

    def _candidate(self, facility_id: str) -> CandidateFacility:
        c = self._by_id.get(str(facility_id).strip()) if facility_id is not None else None
        if c is None:
            raise ValueError(f"unknown facility_id {facility_id!r}: not a candidate facility in this scenario")
        return c

    def _graph(self) -> nx.DiGraph:
        if self.G is None:
            self.G = nhd_graph.load_graph()
        return self.G

    def trace_upstream(self, max_km: float | None = None) -> list[dict]:
        """Reaches upstream of the scenario reach (start reach first), within max_km."""
        km = float(self.scenario.max_km if max_km is None else max_km)
        reaches = nhd_graph.trace_upstream(self._graph(), self.scenario.reach_nhdplusid, km)
        result = [r.model_dump(mode="json") for r in reaches]
        self.ledger.record("trace_upstream", {"max_km": km}, result)
        return result

    def travel_time(self, facility_id: str) -> dict:
        """River travel time from the candidate's nearest reach to the scenario reach."""
        c = self._candidate(facility_id)
        if self.G is not None:
            path = nhd_graph.downstream_path(self.G, c.nearest_reach_nhdplusid, self.scenario.reach_nhdplusid)
            tt = nhd_graph.travel_time(self.G, path)
        elif c.travel_time is not None:
            tt = c.travel_time
        else:
            tt = TravelTime(hours=None, reason="travel time was not computed for this facility")
        result = tt.model_dump(mode="json")
        self.ledger.record("travel_time", {"facility_id": c.facility_id}, result)
        return result

    def find_candidate_facilities(self) -> list[dict]:
        """The scenario's candidate facilities in rank order (river distance)."""
        ranked = sorted(self.scenario.candidates, key=lambda c: (c.rank is None, c.rank or 0))
        result = [c.model_dump(mode="json") for c in ranked]
        self.ledger.record(
            "find_candidate_facilities",
            {"reach_nhdplusid": self.scenario.reach_nhdplusid, "max_km": self.scenario.max_km},
            result,
        )
        return result

    def get_facility_violations(self, facility_id: str) -> dict:
        """Reported violations for one candidate, as counted in the scenario's window."""
        c = self._candidate(facility_id)
        result = {
            "facility_id": c.facility_id,
            "name": c.name,
            "window_label": c.violation_window_label,
            "window_start": c.violation_window_start.isoformat() if c.violation_window_start else None,
            "window_end": c.violation_window_end.isoformat() if c.violation_window_end else None,
            "numeric_violation_count": c.numeric_violation_count,
            "historical_numeric_violation_count": c.historical_numeric_violation_count,
            "reporting_violation_count": c.reporting_violation_count,
            "violations": [v.model_dump(mode="json") for v in c.violations],
        }
        self.ledger.record("get_facility_violations", {"facility_id": c.facility_id}, result)
        return result

    def get_tri_releases(self, facility_id: str) -> dict:
        """TRI releases matched to one candidate (annual, self-reported)."""
        c = self._candidate(facility_id)
        result = {
            "facility_id": c.facility_id,
            "name": c.name,
            "tri_match": c.tri_match,
            "tri_match_confidence": c.tri_match_confidence,
            "releases": [r.model_dump(mode="json") for r in c.tri_releases],
        }
        self.ledger.record("get_tri_releases", {"facility_id": c.facility_id}, result)
        return result
