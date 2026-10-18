# SilentSpill API

JSON over HTTP plus one Server-Sent Events stream. Every response body, including errors and every
SSE event, carries `"simulated": true` and `"requires_human_review": true`.

The examples below come from a real run of this backend (`python scripts/make_api_docs.py`,
which regenerates this file). Long lists are cut to a few items with a `"... N more"` marker; the
full responses are in [`docs/api_examples/`](api_examples/). In this run the incident report was
produced in **`llm`** mode (triage `llm`, trace `llm`, compliance `llm`, report `llm`).

## Running the server

```
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --port 8000
```

- Base URL: `http://localhost:8000`. Interactive schema docs: `http://localhost:8000/docs`.
- CORS allows these origins (`CORS_ORIGINS` in `.env`, comma-separated): `http://localhost:5173`, `http://127.0.0.1:5173`, `http://localhost:3000`, `http://127.0.0.1:3000`.
- Typical frontend flow: `GET /reach` and `GET /facilities` for the map, then `POST /scenario/run`,
  then open `GET /stream/{incident_id}` to animate, and `GET /incidents/{incident_id}` for the report.

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
- Every incident response (`POST /scenario/run`, `GET /incidents/{id}`) and every report carry a fixed
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
| POST | `/scenario/run` | Run the deterministic simulated spill (`{"case": "spill"}`) or the real storm (`{"case": "storm"}`) |
| GET | `/incidents/{id}` | Triage, trace, compliance and the final report |
| GET | `/sensors/{id}?hours_before=72&hours_after=` | Real baseline series plus the injected (simulated) rows |
| GET | `/stream/{id}?speed=` | SSE replay of the scenario timeline |

### GET /health

`status` is `ok` when the required local data is present (`groq_configured` and `naip_imagery` are optional).

```json
{
  "simulated": true,
  "requires_human_review": true,
  "status": "ok",
  "simulation_mode": true,
  "data_ready": {
    "baseline_snapshot": true,
    "reach_fixture": true,
    "nhdplus_gpkg": true,
    "echo_data": true,
    "tri_data": true,
    "roboflow_cache": true,
    "groq_configured": true,
    "naip_imagery": false
  }
}
```

### GET /reach

`reach` is the demo reach (LineString, upstream → downstream). `upstream_path` lists every traced
reach nearest-first with its river distance; `upstream_geojson` has their LineStrings (null if the
GeoPackage is not on this machine). `gauge` is the USGS gauge and its distance to the reach.

```json
{
  "simulated": true,
  "requires_human_review": true,
  "reach": {
    "type": "Feature",
    "properties": {
      "nhdplusid": 10000500083697,
      "gnis_name": "North Fork Shenandoah River",
      "length_km": 3.16015443,
      "velocity_fps": 0.96159656,
      "orientation": "upstream -> downstream: as digitized; last vertex meets downstream reach 10000500066141",
      "crs": "EPSG:4269",
      "source": "NHDPlus HR EPA snapshot 2022"
    },
    "geometry": {
      "type": "LineString",
      "coordinates": [
        [
          -78.33887539,
          38.96922467
        ],
        [
          -78.33812232,
          38.96958827
        ],
        [
          -78.33793332,
          38.96977901
        ],
        "... 41 more points"
      ]
    }
  },
  "max_km": 50.0,
  "upstream_path": [
    {
      "nhdplusid": 10000500083697,
      "gnis_name": "North Fork Shenandoah River",
      "length_km": 3.16015443,
      "cumulative_km": 0.0,
      "velocity_mps": 0.293094631488
    },
    {
      "nhdplusid": 10000500042869,
      "gnis_name": "North Fork Shenandoah River",
      "length_km": 0.01442288,
      "cumulative_km": 0.01442288,
      "velocity_mps": 0.29303375988
    },
    "... 545 more"
  ],
  "upstream_geojson": {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "geometry": {
          "type": "LineString",
          "coordinates": [
            [
              -78.338875,
              38.969225
            ],
            [
              -78.338122,
              38.969588
            ],
            [
              -78.337933,
              38.969779
            ],
            "... 41 more points"
          ]
        },
        "properties": {
          "nhdplusid": 10000500083697,
          "gnis_name": "North Fork Shenandoah River",
          "length_km": 3.16015443,
          "cumulative_km": 0.0,
          "velocity_mps": 0.293094631488
        }
      },
      {
        "type": "Feature",
        "geometry": {
          "type": "LineString",
          "coordinates": [
            [
              -78.33903,
              38.969174
            ],
            [
              -78.338875,
              38.969225
            ]
          ]
        },
        "properties": {
          "nhdplusid": 10000500042869,
          "gnis_name": "North Fork Shenandoah River",
          "length_km": 0.01442288,
          "cumulative_km": 0.01442288,
          "velocity_mps": 0.29303375988
        }
      },
      "... 545 more"
    ]
  },
  "gauge": {
    "site_id": "01634000",
    "name": "North Fork Shenandoah River near Strasburg, VA",
    "latitude": 38.9768,
    "longitude": -78.3361,
    "linked_reach_nhdplusid": 10000500083697,
    "link_distance_m": 32.7
  }
}
```

### GET /facilities

Permits with at least one reported numeric limit violation in the violation window, within
`buffer_m` of the traced river, ranked by river distance (never by travel time). `location_type`
says whether the point is a reported outfall or only the facility location.

```json
{
  "simulated": true,
  "requires_human_review": true,
  "comid": 10000500083697,
  "max_km": 50.0,
  "candidates": [
    {
      "facility_id": "VA0052621",
      "name": "VALLEY MILK PRODUCTS LLC",
      "frs_id": "110000341194",
      "latitude": 38.985,
      "longitude": -78.36,
      "location_type": "outfall",
      "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
      "distance_km_to_reach": 0.010733496183242186,
      "nearest_reach_nhdplusid": 10000500083644,
      "upstream_distance_km": 2.02075429,
      "travel_time": {
        "hours": 7.357208998648063,
        "travel_time_hours_min": 7.357208998648063,
        "is_lower_bound": false,
        "km_with_velocity": 7.574557109999999,
        "km_total": 7.574557109999999,
        "missing_reach_count": 0,
        "reaches_missing_velocity": [],
        "reason": null,
        "display": "7.4 h"
      },
      "numeric_violation_count": 3,
      "reporting_violation_count": 5,
      "historical_numeric_violation_count": 6,
      "violation_window_start": "2021-10-09",
      "violation_window_end": "2026-10-09",
      "violation_window_label": "reported numeric limit violations, 2021–2026",
      "violations": [
        {
          "parameter": "NOAEL Static 48Hr Acute Ceriodaphnia dubia",
          "monitoring_period_end": "2023-12-31",
          "violation_desc": "DMR, Limited - Numeric Violation",
          "exceedance_pct": 50.0,
          "status": null,
          "outfall": "001",
          "source_fields": [
            "PARAMETER_DESC",
            "VIOLATION_DESC",
            "... 3 more"
          ]
        },
        {
          "parameter": "Oxygen, dissolved [DO]",
          "monitoring_period_end": "2022-06-30",
          "violation_desc": "DMR, Limited - Numeric Violation",
          "exceedance_pct": 1.0,
          "status": null,
          "outfall": "001",
          "source_fields": [
            "PARAMETER_DESC",
            "VIOLATION_DESC",
            "... 3 more"
          ]
        },
        "... 1 more"
      ],
      "matched_parameters": [],
      "tri_releases": [],
      "tri_match": null,
      "tri_match_confidence": null,
      "permit_type": "individual",
      "permit_type_code": "NPD",
      "permit_type_desc": "NPDES Individual Permit",
      "general_permit_id": null,
      "general_permit_name": null,
      "small_domestic_general_permit": false,
      "facility_type": "Corporation",
      "facility_type_code": "COR",
      "facility_type_indicator": "NON-POTW",
      "permit_note": "Holds an individual NPDES permit.",
      "rank": 1,
      "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
    },
    {
      "facility_id": "VAG408560",
      "name": "ERBACH ENTERPRISES LLC PROPERTY 2",
      "frs_id": "110071437515",
      "latitude": 38.98511,
      "longitude": -78.396488,
      "location_type": "outfall",
      "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
      "distance_km_to_reach": 0.08292030155136107,
      "nearest_reach_nhdplusid": 10000500008235,
      "upstream_distance_km": 7.13152533,
      "travel_time": {
        "hours": null,
        "travel_time_hours_min": 9.128923682471024,
        "is_lower_bound": true,
        "km_with_velocity": 10.291679760000001,
        "km_total": 10.563315600000001,
        "missing_reach_count": 1,
        "reaches_missing_velocity": [
          10000500008235
        ],
        "reason": "1 of 18 reaches on the path (0.3 of 10.6 km) has no EROM velocity; time over those reaches is not counted, so this is a lower bound.",
        "display": "≥ 9.1 h (velocity available for 10.3 of 10.6 km)"
      },
      "numeric_violation_count": 2,
      "reporting_violation_count": 0,
      "historical_numeric_violation_count": 0,
      "violation_window_start": "2021-10-09",
      "violation_window_end": "2026-10-09",
      "violation_window_label": "reported numeric limit violations, 2021–2026",
      "violations": [
        {
          "parameter": "Solids, total suspended",
          "monitoring_period_end": "2025-08-31",
          "violation_desc": "DMR, Limited - Numeric Violation",
          "exceedance_pct": 205.0,
          "status": null,
          "outfall": "001",
          "source_fields": [
            "PARAMETER_DESC",
            "VIOLATION_DESC",
            "... 3 more"
          ]
        },
        {
          "parameter": "Solids, total suspended",
          "monitoring_period_end": "2024-08-31",
          "violation_desc": "DMR, Limited - Numeric Violation",
          "exceedance_pct": 126.0,
          "status": null,
          "outfall": "001",
          "source_fields": [
            "PARAMETER_DESC",
            "VIOLATION_DESC",
            "... 3 more"
          ]
        }
      ],
      "matched_parameters": [
        {
          "sensor_parameter": "turbidity",
          "echo_parameter": "Solids, total suspended",
          "is_proxy": true,
          "note": "Total suspended solids (TSS) is used as a proxy for turbidity; it is an indirect indicator."
        }
      ],
      "tri_releases": [],
      "tri_match": null,
      "tri_match_confidence": null,
      "permit_type": "general",
      "permit_type_code": "GPC",
      "permit_type_desc": "General Permit Covered Facility",
      "general_permit_id": "VAG400000",
      "general_permit_name": "VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day",
      "small_domestic_general_permit": true,
      "facility_type": "Privately Owned Facility",
      "facility_type_code": "POF",
      "facility_type_indicator": "NON-POTW",
      "permit_note": "Holds a small domestic general permit (VAG400000: VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day), not an individual industrial discharge permit.",
      "rank": 2,
      "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
    },
    "... 2 more"
  ],
  "geojson": {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "geometry": {
          "type": "Point",
          "coordinates": [
            -78.36,
            38.985
          ]
        },
        "properties": {
          "facility_id": "VA0052621",
          "name": "VALLEY MILK PRODUCTS LLC",
          "frs_id": "110000341194",
          "location_type": "outfall",
          "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
          "distance_km_to_reach": 0.010733496183242186,
          "nearest_reach_nhdplusid": 10000500083644,
          "upstream_distance_km": 2.02075429,
          "travel_time": {
            "hours": 7.357208998648063,
            "travel_time_hours_min": 7.357208998648063,
            "is_lower_bound": false,
            "km_with_velocity": 7.574557109999999,
            "km_total": 7.574557109999999,
            "missing_reach_count": 0,
            "reaches_missing_velocity": [],
            "reason": null,
            "display": "7.4 h"
          },
          "numeric_violation_count": 3,
          "reporting_violation_count": 5,
          "historical_numeric_violation_count": 6,
          "violation_window_start": "2021-10-09",
          "violation_window_end": "2026-10-09",
          "violation_window_label": "reported numeric limit violations, 2021–2026",
          "violations": [
            {
              "parameter": "NOAEL Static 48Hr Acute Ceriodaphnia dubia",
              "monitoring_period_end": "2023-12-31",
              "violation_desc": "DMR, Limited - Numeric Violation",
              "exceedance_pct": 50.0,
              "status": null,
              "outfall": "001",
              "source_fields": [
                "PARAMETER_DESC",
                "VIOLATION_DESC",
                "... 3 more"
              ]
            },
            {
              "parameter": "Oxygen, dissolved [DO]",
              "monitoring_period_end": "2022-06-30",
              "violation_desc": "DMR, Limited - Numeric Violation",
              "exceedance_pct": 1.0,
              "status": null,
              "outfall": "001",
              "source_fields": [
                "PARAMETER_DESC",
                "VIOLATION_DESC",
                "... 3 more"
              ]
            },
            "... 1 more"
          ],
          "matched_parameters": [],
          "tri_releases": [],
          "tri_match": null,
          "tri_match_confidence": null,
          "permit_type": "individual",
          "permit_type_code": "NPD",
          "permit_type_desc": "NPDES Individual Permit",
          "general_permit_id": null,
          "general_permit_name": null,
          "small_domestic_general_permit": false,
          "facility_type": "Corporation",
          "facility_type_code": "COR",
          "facility_type_indicator": "NON-POTW",
          "permit_note": "Holds an individual NPDES permit.",
          "rank": 1,
          "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
        }
      },
      {
        "type": "Feature",
        "geometry": {
          "type": "Point",
          "coordinates": [
            -78.396488,
            38.98511
          ]
        },
        "properties": {
          "facility_id": "VAG408560",
          "name": "ERBACH ENTERPRISES LLC PROPERTY 2",
          "frs_id": "110071437515",
          "location_type": "outfall",
          "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
          "distance_km_to_reach": 0.08292030155136107,
          "nearest_reach_nhdplusid": 10000500008235,
          "upstream_distance_km": 7.13152533,
          "travel_time": {
            "hours": null,
            "travel_time_hours_min": 9.128923682471024,
            "is_lower_bound": true,
            "km_with_velocity": 10.291679760000001,
            "km_total": 10.563315600000001,
            "missing_reach_count": 1,
            "reaches_missing_velocity": [
              10000500008235
            ],
            "reason": "1 of 18 reaches on the path (0.3 of 10.6 km) has no EROM velocity; time over those reaches is not counted, so this is a lower bound.",
            "display": "≥ 9.1 h (velocity available for 10.3 of 10.6 km)"
          },
          "numeric_violation_count": 2,
          "reporting_violation_count": 0,
          "historical_numeric_violation_count": 0,
          "violation_window_start": "2021-10-09",
          "violation_window_end": "2026-10-09",
          "violation_window_label": "reported numeric limit violations, 2021–2026",
          "violations": [
            {
              "parameter": "Solids, total suspended",
              "monitoring_period_end": "2025-08-31",
              "violation_desc": "DMR, Limited - Numeric Violation",
              "exceedance_pct": 205.0,
              "status": null,
              "outfall": "001",
              "source_fields": [
                "PARAMETER_DESC",
                "VIOLATION_DESC",
                "... 3 more"
              ]
            },
            {
              "parameter": "Solids, total suspended",
              "monitoring_period_end": "2024-08-31",
              "violation_desc": "DMR, Limited - Numeric Violation",
              "exceedance_pct": 126.0,
              "status": null,
              "outfall": "001",
              "source_fields": [
                "PARAMETER_DESC",
                "VIOLATION_DESC",
                "... 3 more"
              ]
            }
          ],
          "matched_parameters": [
            {
              "sensor_parameter": "turbidity",
              "echo_parameter": "Solids, total suspended",
              "is_proxy": true,
              "note": "Total suspended solids (TSS) is used as a proxy for turbidity; it is an indirect indicator."
            }
          ],
          "tri_releases": [],
          "tri_match": null,
          "tri_match_confidence": null,
          "permit_type": "general",
          "permit_type_code": "GPC",
          "permit_type_desc": "General Permit Covered Facility",
          "general_permit_id": "VAG400000",
          "general_permit_name": "VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day",
          "small_domestic_general_permit": true,
          "facility_type": "Privately Owned Facility",
          "facility_type_code": "POF",
          "facility_type_indicator": "NON-POTW",
          "permit_note": "Holds a small domestic general permit (VAG400000: VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day), not an individual industrial discharge permit.",
          "rank": 2,
          "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
        }
      },
      "... 2 more"
    ]
  }
}
```

### POST /scenario/run

Body (optional): `{"case": "spill"}` (default) or `{"case": "storm"}`. Runs synchronously (about
10 s, longer with the Groq chain) and stores the result in memory under a deterministic id.

Spill:

```json
{
  "simulated": true,
  "requires_human_review": true,
  "disclaimer": "Simulated incident. Facilities are listed only because they have reported permit violations upstream; no link to any real event is implied.",
  "incident_id": "spill-20261009T2000Z",
  "status": "completed",
  "case": "spill",
  "classification": "incident",
  "report_mode": "llm",
  "agent_modes": {
    "triage": "llm",
    "trace": "llm",
    "compliance": "llm",
    "report": "llm"
  }
}
```

Storm (real observed data; storm runoff gets no candidate list, so trace and compliance are skipped):

```json
{
  "simulated": true,
  "requires_human_review": true,
  "disclaimer": "Simulated incident. Facilities are listed only because they have reported permit violations upstream; no link to any real event is implied.",
  "incident_id": "storm-20260922T0000Z",
  "status": "completed",
  "case": "storm",
  "classification": "storm_runoff",
  "report_mode": "llm",
  "agent_modes": {
    "triage": "llm",
    "trace": "skipped",
    "compliance": "skipped",
    "report": "llm"
  }
}
```

### GET /incidents/{id}

Spill (`incident`, ranked candidates):

```json
{
  "simulated": true,
  "requires_human_review": true,
  "disclaimer": "Simulated incident. Facilities are listed only because they have reported permit violations upstream; no link to any real event is implied.",
  "incident_id": "spill-20261009T2000Z",
  "triage": {
    "simulated": true,
    "requires_human_review": true,
    "classification": "incident",
    "fusion_classification": "incident",
    "confidence": 0.95,
    "reasons": [
      "Camera clip labeled 'spill' shows dye‑coloured water from 2026-10-09 20:02 to 2026-10-09 21:49 UTC (216 of 240 frames, peak plume confidence 1.00).",
      "Gauge anomalies in conductivity rise, turbidity rise, and pH drop occur within the ±3.0 h allowance of the camera plume, providing two independent sources of agreement."
    ]
  },
  "trace": {
    "simulated": true,
    "requires_human_review": true,
    "candidates": [
      {
        "facility_id": "VA0052621",
        "name": "VALLEY MILK PRODUCTS LLC",
        "frs_id": "110000341194",
        "latitude": 38.985,
        "longitude": -78.36,
        "location_type": "outfall",
        "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
        "distance_km_to_reach": 0.010733496183242186,
        "nearest_reach_nhdplusid": 10000500083644,
        "upstream_distance_km": 2.02075429,
        "travel_time": {
          "hours": 7.357208998648063,
          "travel_time_hours_min": 7.357208998648063,
          "is_lower_bound": false,
          "km_with_velocity": 7.574557109999999,
          "km_total": 7.574557109999999,
          "missing_reach_count": 0,
          "reaches_missing_velocity": [],
          "reason": null,
          "display": "7.4 h"
        },
        "numeric_violation_count": 3,
        "reporting_violation_count": 5,
        "historical_numeric_violation_count": 6,
        "violation_window_start": "2021-10-09",
        "violation_window_end": "2026-10-09",
        "violation_window_label": "reported numeric limit violations, 2021–2026",
        "violations": [
          {
            "parameter": "NOAEL Static 48Hr Acute Ceriodaphnia dubia",
            "monitoring_period_end": "2023-12-31",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 50.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          },
          {
            "parameter": "Oxygen, dissolved [DO]",
            "monitoring_period_end": "2022-06-30",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 1.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          },
          "... 1 more"
        ],
        "matched_parameters": [],
        "tri_releases": [],
        "tri_match": null,
        "tri_match_confidence": null,
        "permit_type": "individual",
        "permit_type_code": "NPD",
        "permit_type_desc": "NPDES Individual Permit",
        "general_permit_id": null,
        "general_permit_name": null,
        "small_domestic_general_permit": false,
        "facility_type": "Corporation",
        "facility_type_code": "COR",
        "facility_type_indicator": "NON-POTW",
        "permit_note": "Holds an individual NPDES permit.",
        "rank": 1,
        "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
      },
      {
        "facility_id": "VAG408560",
        "name": "ERBACH ENTERPRISES LLC PROPERTY 2",
        "frs_id": "110071437515",
        "latitude": 38.98511,
        "longitude": -78.396488,
        "location_type": "outfall",
        "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
        "distance_km_to_reach": 0.08292030155136107,
        "nearest_reach_nhdplusid": 10000500008235,
        "upstream_distance_km": 7.13152533,
        "travel_time": {
          "hours": null,
          "travel_time_hours_min": 9.128923682471024,
          "is_lower_bound": true,
          "km_with_velocity": 10.291679760000001,
          "km_total": 10.563315600000001,
          "missing_reach_count": 1,
          "reaches_missing_velocity": [
            10000500008235
          ],
          "reason": "1 of 18 reaches on the path (0.3 of 10.6 km) has no EROM velocity; time over those reaches is not counted, so this is a lower bound.",
          "display": "≥ 9.1 h (velocity available for 10.3 of 10.6 km)"
        },
        "numeric_violation_count": 2,
        "reporting_violation_count": 0,
        "historical_numeric_violation_count": 0,
        "violation_window_start": "2021-10-09",
        "violation_window_end": "2026-10-09",
        "violation_window_label": "reported numeric limit violations, 2021–2026",
        "violations": [
          {
            "parameter": "Solids, total suspended",
            "monitoring_period_end": "2025-08-31",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 205.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          },
          {
            "parameter": "Solids, total suspended",
            "monitoring_period_end": "2024-08-31",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 126.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          }
        ],
        "matched_parameters": [
          {
            "sensor_parameter": "turbidity",
            "echo_parameter": "Solids, total suspended",
            "is_proxy": true,
            "note": "Total suspended solids (TSS) is used as a proxy for turbidity; it is an indirect indicator."
          }
        ],
        "tri_releases": [],
        "tri_match": null,
        "tri_match_confidence": null,
        "permit_type": "general",
        "permit_type_code": "GPC",
        "permit_type_desc": "General Permit Covered Facility",
        "general_permit_id": "VAG400000",
        "general_permit_name": "VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day",
        "small_domestic_general_permit": true,
        "facility_type": "Privately Owned Facility",
        "facility_type_code": "POF",
        "facility_type_indicator": "NON-POTW",
        "permit_note": "Holds a small domestic general permit (VAG400000: VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day), not an individual industrial discharge permit.",
        "rank": 2,
        "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
      },
      "... 2 more"
    ],
    "candidate_notes": {
      "VA0052621": "2.02 km upstream; river travel time 7.4 h; 3 reported numeric limit violations in the window; no matched parameters.",
      "VAG408560": "7.13 km upstream; river travel time ≥ 9.1 h (velocity available for 10.3 of 10.6 km); 2 reported numeric limit violations in the window; reported turbidity matches (proxy).",
      "VAG408449": "7.4 km upstream; river travel time ≥ 9.4 h (velocity available for 10.7 of 11.0 km); 3 reported numeric limit violations in the window; no matched parameters.",
      "VAG408158": "17.57 km upstream; river travel time ≥ 18.7 h (velocity available for 20.6 of 20.8 km); 2 reported numeric limit violations in the window; no matched parameters."
    },
    "assumptions": [
      "Candidates are ranked by river distance upstream of the spill reach, then distance to the river; not by travel time.",
      "Travel times use the mean annual EROM velocity, treated as ft/s (a documented assumption); missing velocities are never filled in.",
      "... 2 more"
    ]
  },
  "compliance": {
    "simulated": true,
    "requires_human_review": true,
    "facts": [
      {
        "facility_id": "VA0052621",
        "statement": "3 reported numeric limit violations in the window (reported or alleged).",
        "value": "3",
        "source_field": "numeric_violation_count",
        "source_file": "VA_NPDES_EFF_VIOLATIONS.csv"
      },
      {
        "facility_id": "VA0052621",
        "statement": "6 historical numeric limit violations.",
        "value": "6",
        "source_field": "historical_numeric_violation_count",
        "source_file": "VA_NPDES_EFF_VIOLATIONS.csv"
      },
      "... 38 more"
    ]
  },
  "report": {
    "simulated": true,
    "requires_human_review": true,
    "disclaimer": "Simulated incident. Facilities are listed only because they have reported permit violations upstream; no link to any real event is implied.",
    "banner": "SIMULATED DEMONSTRATION. The spill, plume footage and sensor spike are synthetic. This draft requires human review before any action.",
    "summary": "The incident involves a simulated spill injected into a real baseline, and the fusion and triage systems both classify it as an incident with high confidence (0.95). Gauge data show rises in conductivity and turbidity and a drop in pH that align with the camera plume timing, and these three parameters are listed as anomalous. Rainfall records for the event day and the prior day have no readings, so rain does not explain the anomalies. Four facilities with reported violations are identified upstream: Rank 1 VALLEY MILK PRODUCTS LLC, 2.02 km upstream, travel time 7.4 h, 3 reported numeric limit violations; Rank 2 ERBACH ENTERPRISES LLC PROPERTY 2, 7.13 km upstream, travel time ≥ 9.1 h (velocity available for 10.3 of 10.6 km), 2 reported numeric limit violations; Rank 3 ERBACH ENTERPRISES LLC PROPERTY, 7.4 km upstream, travel time ≥ 9.4 h (velocity available for 10.7 of 11.0 km), 3 reported numeric limit violations; Rank 4 SHENANDOAH VALLEY BOARDING AND TRAINING LLC, 17.57 km upstream, travel time ≥ 18.7 h (velocity available for 20.6 of 20.8 km), 2 reported numeric limit violations. Facilities ranked 2, 3, and 4 each hold a small domestic general permit (VAG400000). ERBACH ENTERPRISES LLC PROPERTY 2 (VAG408560) holds a small domestic general permit (VAG400000). ERBACH ENTERPRISES LLC PROPERTY (VAG408449) holds a small domestic general permit (VAG400000). SHENANDOAH VALLEY BOARDING AND TRAINING LLC (VAG408158) holds a small domestic general permit (VAG400000).",
    "timeline": [
      {
        "timestamp": "2026-10-09T20:00:00Z",
        "event": "Simulated spill release begins."
      },
      {
        "timestamp": "2026-10-09T20:02:00Z",
        "event": "Camera first shows a plume (simulated) (216 of 240 frames)."
      },
      "... 5 more"
    ],
    "ranked_candidates": [
      {
        "facility_id": "VA0052621",
        "name": "VALLEY MILK PRODUCTS LLC",
        "frs_id": "110000341194",
        "latitude": 38.985,
        "longitude": -78.36,
        "location_type": "outfall",
        "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
        "distance_km_to_reach": 0.010733496183242186,
        "nearest_reach_nhdplusid": 10000500083644,
        "upstream_distance_km": 2.02075429,
        "travel_time": {
          "hours": 7.357208998648063,
          "travel_time_hours_min": 7.357208998648063,
          "is_lower_bound": false,
          "km_with_velocity": 7.574557109999999,
          "km_total": 7.574557109999999,
          "missing_reach_count": 0,
          "reaches_missing_velocity": [],
          "reason": null,
          "display": "7.4 h"
        },
        "numeric_violation_count": 3,
        "reporting_violation_count": 5,
        "historical_numeric_violation_count": 6,
        "violation_window_start": "2021-10-09",
        "violation_window_end": "2026-10-09",
        "violation_window_label": "reported numeric limit violations, 2021–2026",
        "violations": [
          {
            "parameter": "NOAEL Static 48Hr Acute Ceriodaphnia dubia",
            "monitoring_period_end": "2023-12-31",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 50.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          },
          {
            "parameter": "Oxygen, dissolved [DO]",
            "monitoring_period_end": "2022-06-30",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 1.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          },
          "... 1 more"
        ],
        "matched_parameters": [],
        "tri_releases": [],
        "tri_match": null,
        "tri_match_confidence": null,
        "permit_type": "individual",
        "permit_type_code": "NPD",
        "permit_type_desc": "NPDES Individual Permit",
        "general_permit_id": null,
        "general_permit_name": null,
        "small_domestic_general_permit": false,
        "facility_type": "Corporation",
        "facility_type_code": "COR",
        "facility_type_indicator": "NON-POTW",
        "permit_note": "Holds an individual NPDES permit.",
        "rank": 1,
        "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
      },
      {
        "facility_id": "VAG408560",
        "name": "ERBACH ENTERPRISES LLC PROPERTY 2",
        "frs_id": "110071437515",
        "latitude": 38.98511,
        "longitude": -78.396488,
        "location_type": "outfall",
        "location_note": "Reported outfall location (ECHO permitted feature) 001; not a surveyed discharge point.",
        "distance_km_to_reach": 0.08292030155136107,
        "nearest_reach_nhdplusid": 10000500008235,
        "upstream_distance_km": 7.13152533,
        "travel_time": {
          "hours": null,
          "travel_time_hours_min": 9.128923682471024,
          "is_lower_bound": true,
          "km_with_velocity": 10.291679760000001,
          "km_total": 10.563315600000001,
          "missing_reach_count": 1,
          "reaches_missing_velocity": [
            10000500008235
          ],
          "reason": "1 of 18 reaches on the path (0.3 of 10.6 km) has no EROM velocity; time over those reaches is not counted, so this is a lower bound.",
          "display": "≥ 9.1 h (velocity available for 10.3 of 10.6 km)"
        },
        "numeric_violation_count": 2,
        "reporting_violation_count": 0,
        "historical_numeric_violation_count": 0,
        "violation_window_start": "2021-10-09",
        "violation_window_end": "2026-10-09",
        "violation_window_label": "reported numeric limit violations, 2021–2026",
        "violations": [
          {
            "parameter": "Solids, total suspended",
            "monitoring_period_end": "2025-08-31",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 205.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          },
          {
            "parameter": "Solids, total suspended",
            "monitoring_period_end": "2024-08-31",
            "violation_desc": "DMR, Limited - Numeric Violation",
            "exceedance_pct": 126.0,
            "status": null,
            "outfall": "001",
            "source_fields": [
              "PARAMETER_DESC",
              "VIOLATION_DESC",
              "... 3 more"
            ]
          }
        ],
        "matched_parameters": [
          {
            "sensor_parameter": "turbidity",
            "echo_parameter": "Solids, total suspended",
            "is_proxy": true,
            "note": "Total suspended solids (TSS) is used as a proxy for turbidity; it is an indirect indicator."
          }
        ],
        "tri_releases": [],
        "tri_match": null,
        "tri_match_confidence": null,
        "permit_type": "general",
        "permit_type_code": "GPC",
        "permit_type_desc": "General Permit Covered Facility",
        "general_permit_id": "VAG400000",
        "general_permit_name": "VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day",
        "small_domestic_general_permit": true,
        "facility_type": "Privately Owned Facility",
        "facility_type_code": "POF",
        "facility_type_indicator": "NON-POTW",
        "permit_note": "Holds a small domestic general permit (VAG400000: VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day), not an individual industrial discharge permit.",
        "rank": 2,
        "rank_basis": "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
      },
      "... 2 more"
    ],
    "confidence": 0.95,
    "assumptions": [
      "Candidates are ranked by river distance upstream of the spill reach, then distance to the river; not by travel time.",
      "Travel times use the mean annual EROM velocity, treated as ft/s (a documented assumption); missing velocities are never filled in.",
      "... 2 more"
    ],
    "limitations": [
      "The spill, the gauge sensor spike and the plume camera footage are simulated: the spike is injected into a real gauge baseline and the footage is rendered.",
      "Facility coordinates are reported locations from ECHO (outfall points where available, otherwise facility geocodes), not surveyed discharge points.",
      "... 6 more"
    ],
    "classification": "incident",
    "data_origin": "simulated spill injected into real baseline",
    "report_mode": "llm",
    "fallback_reason": null,
    "guardrail_notes": [
      "triage: dropped a sentence with numbers not in the tool data (10)",
      "report: added the small domestic general permit statement for VAG408560",
      "... 2 more"
    ]
  },
  "agent_modes": {
    "triage": "llm",
    "trace": "llm",
    "compliance": "llm",
    "report": "llm"
  }
}
```

Storm (`storm_runoff`, no candidates):

```json
{
  "simulated": true,
  "requires_human_review": true,
  "disclaimer": "Simulated incident. Facilities are listed only because they have reported permit violations upstream; no link to any real event is implied.",
  "incident_id": "storm-20260922T0000Z",
  "triage": {
    "simulated": true,
    "requires_human_review": true,
    "classification": "storm_runoff",
    "fusion_classification": "storm_runoff",
    "confidence": 0.65,
    "reasons": [
      "Turbidity rises on 2026-09-22 align with 1.23 in total rain exceeding the 0.5 in threshold.",
      "Conductivity drop coincides with the turbidity rise, matching the storm‑runoff pattern.",
      "No camera analysis was available; the classification relies on sensor data and rain record alone."
    ]
  },
  "trace": {
    "simulated": true,
    "requires_human_review": true,
    "candidates": [],
    "candidate_notes": {},
    "assumptions": [
      "Storm runoff gets no candidate list: the anomaly is consistent with rain, so no permit holder is named."
    ]
  },
  "compliance": {
    "simulated": true,
    "requires_human_review": true,
    "facts": []
  },
  "report": {
    "simulated": true,
    "requires_human_review": true,
    "disclaimer": "Simulated incident. Facilities are listed only because they have reported permit violations upstream; no link to any real event is implied.",
    "banner": "SIMULATED DEMONSTRATION. The spill, plume footage and sensor spike are synthetic. This draft requires human review before any action.",
    "summary": "Real observed gauge and rain data classify this incident as storm runoff (confidence 0.65). Because the pattern is explained by the storm, no anomalous parameters remain and no facility candidates are generated by design. The report does not identify a source and only notes that the observed changes are consistent with storm‑runoff conditions.",
    "timeline": [
      {
        "timestamp": "2026-09-21T00:00:00Z",
        "event": "Daily total at GHCND:USW00013731: 0.28 in of rain (daily totals only)."
      },
      {
        "timestamp": "2026-09-22T00:00:00Z",
        "event": "Daily total at GHCND:USW00013731: 0.95 in of rain (daily totals only)."
      },
      {
        "timestamp": "2026-09-22T01:30:00Z",
        "event": "Gauge turbidity rise begins; peak at 2026-09-22 01:30 UTC, ends 2026-09-22 01:30 UTC."
      },
      "... 3 more"
    ],
    "ranked_candidates": [],
    "confidence": 0.65,
    "assumptions": [
      "Storm runoff gets no candidate list: the anomaly is consistent with rain, so no permit holder is named."
    ],
    "limitations": [
      "This case uses real observed gauge and rain data, not simulated; there is no camera footage for it.",
      "Facility coordinates are reported locations from ECHO (outfall points where available, otherwise facility geocodes), not surveyed discharge points.",
      "Rain is daily totals only (the event day and the day before); timing within a day is unknown, and a missing day is reported as no rain reading, not zero rain.",
      "... 5 more"
    ],
    "classification": "storm_runoff",
    "data_origin": "real observed data, not simulated",
    "report_mode": "llm",
    "fallback_reason": null,
    "guardrail_notes": [
      "report summary: dropped a sentence with numbers not in the tool data (22)"
    ]
  },
  "agent_modes": {
    "triage": "llm",
    "trace": "skipped",
    "compliance": "skipped",
    "report": "llm"
  }
}
```

### GET /sensors/{id}

`baseline` is the real gauge record; `injected` holds only the rows the simulation changed, with
`simulated: true`. `discharge` is never injected. (Example requested with `hours_before=1&hours_after=1`.)

```json
{
  "simulated": true,
  "requires_human_review": true,
  "incident_id": "spill-20261009T2000Z",
  "site_id": "01634000",
  "baseline": [
    {
      "timestamp": "2026-10-09T19:00:00Z",
      "values": {
        "conductivity": 405.0,
        "turbidity": 0.5,
        "ph": 8.7,
        "discharge": 118.0
      },
      "simulated": false
    },
    {
      "timestamp": "2026-10-09T19:15:00Z",
      "values": {
        "conductivity": 404.0,
        "turbidity": 0.5,
        "ph": 8.7,
        "discharge": 118.0
      },
      "simulated": false
    },
    "... 7 more"
  ],
  "injected": [
    {
      "timestamp": "2026-10-09T20:15:00Z",
      "values": {
        "conductivity": 484.0951,
        "turbidity": 5.6126,
        "ph": 8.3771,
        "discharge": 115.0
      },
      "simulated": true
    },
    {
      "timestamp": "2026-10-09T20:30:00Z",
      "values": {
        "conductivity": 571.3695,
        "turbidity": 10.0093,
        "ph": 8.0724,
        "discharge": 112.0
      },
      "simulated": true
    },
    "... 2 more"
  ]
}
```

### GET /stream/{id} (Server-Sent Events)

Replays the scenario: `sensor_tick` every 15 minutes of scenario time (from 2 h before the
scenario timestamp), `plume_detection` for each camera frame with dye, interleaved in time order,
then one `agent_output` per agent (triage, trace, compliance, report), then `complete`.

- `speed` = scenario seconds per wall-clock second (default 600 from `STREAM_SPEED`: one sensor
  tick every 1.5 s). `speed=0` sends everything at once. Single pauses are capped at 5 s.
- Each message has `id: <seq>`, `event: <kind>` and `data: <StreamEvent JSON>`. On the wire `data:`
  is one JSON line; it is pretty-printed below for reading.

```js
const es = new EventSource(`${base}/stream/${incidentId}?speed=600`);
es.addEventListener("sensor_tick", (e) => { const ev = JSON.parse(e.data); /* ev.payload.values */ });
es.addEventListener("plume_detection", (e) => { /* JSON.parse(e.data).payload.confidence */ });
es.addEventListener("agent_output", (e) => { /* JSON.parse(e.data).payload.agent, .output */ });
es.addEventListener("complete", () => es.close());
```

`sensor_tick` (105 in the spill replay):

```text
id: 0
event: sensor_tick
data: {
  "simulated": true,
  "requires_human_review": true,
  "incident_id": "spill-20261009T2000Z",
  "seq": 0,
  "kind": "sensor_tick",
  "scenario_time": "2026-10-09T18:00:00Z",
  "payload": {
    "values": {
      "conductivity": 409.0,
      "turbidity": 0.5,
      "ph": 8.6,
      "discharge": 121.0
    },
    "simulated": false,
    "anomalous_parameters": []
  }
}
```

`plume_detection` (216 in the spill replay):

```text
id: 9
event: plume_detection
data: {
  "simulated": true,
  "requires_human_review": true,
  "incident_id": "spill-20261009T2000Z",
  "seq": 9,
  "kind": "plume_detection",
  "scenario_time": "2026-10-09T20:02:00Z",
  "payload": {
    "frame_index": 24,
    "area_fraction": 0.00356,
    "confidence": 0.1187,
    "clip": "spill"
  }
}
```

`agent_output` (4 in the spill replay):

```text
id: 321
event: agent_output
data: {
  "simulated": true,
  "requires_human_review": true,
  "incident_id": "spill-20261009T2000Z",
  "seq": 321,
  "kind": "agent_output",
  "scenario_time": "2026-10-10T20:00:00Z",
  "payload": {
    "agent": "triage",
    "mode": "llm",
    "output": {
      "simulated": true,
      "requires_human_review": true,
      "classification": "incident",
      "fusion_classification": "incident",
      "confidence": 0.95,
      "reasons": [
        "Camera clip labeled 'spill' shows dye‑coloured water from 2026-10-09 20:02 to 2026-10-09 21:49 UTC (216 of 240 frames, peak plume confidence 1.00).",
        "... 1 more"
      ]
    }
  }
}
```

`complete` (1 in the spill replay):

```text
id: 325
event: complete
data: {
  "simulated": true,
  "requires_human_review": true,
  "incident_id": "spill-20261009T2000Z",
  "seq": 325,
  "kind": "complete",
  "scenario_time": "2026-10-10T20:00:00Z",
  "payload": {
    "classification": "incident",
    "report_mode": "llm",
    "incident_id": "spill-20261009T2000Z"
  }
}
```

### Errors

Errors use the same flags plus `detail`. Unknown incident ids return 404 (run `POST /scenario/run`
first; incidents live in server memory), and invalid query values return 422.

```json
{
  "simulated": true,
  "requires_human_review": true,
  "detail": "unknown incident 'unknown-id'; run POST /scenario/run first"
}
```
