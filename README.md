PROJECT CONTEXT: SilentSpill


We're building a hackathon project (MLH Best Use of Gemini API, Sensors for Good, and a UI/UX prize). I'm only asking you to build the BACKEND. A teammate is building a 3D watershed map frontend, so your job is a clean API and data pipeline they can call.


THE IDEA
Rivers get polluted by spills and dumping, and by the time anyone notices, nobody knows where it came from. SilentSpill watches a river, detects a possible spill by combining camera footage with water sensors, traces it upstream through the real river network, and uses Gemini agents to rank which permitted facilities nearby have reported violations and draft an incident report. The goal is to turn "something looks wrong in the river" into "here is what happened, when it likely entered, and which upstream facilities to look at first," with a human making the final call.


WHAT'S REAL AND WHAT'S SIMULATED
We are an online team with no drone, no real spill, and no dataset that pairs spill video with sensors. So:
Real: river network (NHDPlus HR, Virginia), facilities and violations (EPA ECHO), toxic release data (EPA TRI), baseline river-gauge readings (USGS NWIS), rainfall (NOAA), terrain (3DEP).
Simulated: the aerial plume footage (rendered dye plume on real aerial imagery) and the spill spike injected into real sensor data.
Everything we output must be clearly labeled simulated and flagged for human review.


THE FLOW
1. Simulate a spill: inject a sensor spike into real baseline data and render a matching plume clip at the same timestamp.
2. Detect: find anomalies in conductivity, turbidity, and pH, detect the plume in video, and check rainfall so a normal storm isn't mistaken for dumping.
3. Fuse: raise an incident only when camera and sensors agree.
4. Trace upstream: walk the river graph backward from the spill point and use flow velocity to estimate travel time.
5. Find candidates: look up permitted facilities along that path and their reported violations and releases.
6. Agents: four Gemini agents (Triage, Trace, Compliance, Report) pass structured JSON down a chain and call plain Python tools. The final report includes confidence, assumptions, and limitations.


WHAT MATTERS MOST
Honesty: never claim a facility caused anything. Say "facilities with reported violations near this reach." Never invent facility names or data; only use what's in our local files.
Reliability: the demo must not break, so the agents need a deterministic fallback report if Gemini fails, and the whole scenario must replay the same way every time.
Verification: several data details are unconfirmed (NHDPlus velocity units and table names, which gauge parameters exist, ECHO coordinates being facility not outfall). Inspect the real files first and tell me what you find.
Now read README.md and start with the "Verify first" section.
SilentSpill: Backend Build Brief (for Claude Code)
Scope: backend only. Do NOT build any frontend, UI, HTML, or map rendering. A teammate builds the frontend separately; your job is a clean, tested API and data pipeline they can call.
SilentSpill detects a simulated spill in a Virginia river, traces it upstream through the river network, ranks candidate facilities with reported violations, and has Gemini agents draft a human-review incident report.
Ground rules
Everything is simulated where real data does not exist. The plume video and the spill sensor spike are synthetic. Every API response and report must carry `"simulated": true` and a `"requires_human_review": true` flag, controlled by `SIMULATION_MODE` in `.env`.
Non-accusatory language. Never say a facility "caused" or "is responsible for" a spill. Use "facilities with reported violations near this reach" and "consistent with."
Never invent facility names, IDs, or chemicals. Agents may only cite values returned from the local ECHO/TRI data.
Secrets: read all keys from `.env` (see `.env.example`). Never print, log, or commit keys. Ensure `.env` is in `.gitignore`.
Ask before downloading anything large or installing heavy system packages. Data is already downloaded locally under `./data/`.
Verify, do not assume. Several details below are unconfirmed; inspect the real files first (see "Verify first").
Tech stack
Python 3.11+, FastAPI + Uvicorn
GeoPandas, Shapely, NetworkX, pandas, numpy, scikit-learn
`rasterio` (only if you need elevation lookups; not required for core logic)
OpenCV + Ultralytics YOLO for the vision service
`google-genai` for Gemini (model name comes from `GEMINI_MODEL`)
pydantic for all schemas, pytest for tests, python-dotenv for config
Local data (already downloaded; paths in `.env`)
Data	Env var	Use
NHDPlus HR GeoPackage (one HUC4)	`NHDPLUS_GPKG_PATH`	River network, upstream tracing, velocity/flow
ECHO ICIS-NPDES facilities and effluent violations	`ECHO_NPDES_DIR`, `ECHO_EFFLUENT_DIR`	Candidate facilities and reported violations
TRI release data	`TRI_CSV_PATH`	Reported chemical releases (annual)
USGS NWIS gauge data (API pull, cached to CSV)	`NWIS_SITE_ID`	Baseline conductivity, turbidity, pH, discharge
NOAA CDO precipitation (API, token in env)	`NOAA_CDO_TOKEN`, `NOAA_STATION_ID`	Storm filter
Roboflow YOLO datasets	`YOLO_DATASET_DIR`	Debris detector training
Simulated footage	`SIMULATED_FOOTAGE_DIR`	Vision input
3DEP DEM, NAIP	`DEM_TIF_PATH`, `NAIP_PATH`	Only for simulation base imagery and optional elevation lookups
Verify first (do this before writing logic)
Write a short `scripts/inspect_data.py` that prints findings, and report them to the user:
List all layers in the NHDPlus GeoPackage. Confirm the flowline layer, the value-added attributes (VAA), and the velocity/flow tables (likely EROM tables; column names such as `V0001E` for velocity and `QA_MA` for mean annual flow are unconfirmed; velocity units may be feet per second, not m/s). Find the columns that link reaches (COMID, FromNode, ToNode, or hydrosequence/upstream-downstream fields).
Confirm the chosen `REACH_COMID` exists and the graph can be traced from it.
Confirm the chosen NWIS gauge has conductivity, turbidity, and pH, and note the actual parameter codes returned (do not assume turbidity code).
Inspect ECHO files: column names for facility ID, name, latitude/longitude, state, and the effluent violation fields. Note these are facility coordinates, not outfall coordinates, and violations are "reported" or "alleged."
Inspect TRI files: facility ID, coordinates, chemical names, release quantities, year. TRI is annual self-reported data and cannot match a live plume.
Link NWIS gauges to the nearest NHD reach yourself (gauges are not attached to a COMID).
Save the confirmed schema decisions to `docs/data_notes.md`.
Project structure
```
backend/
  app/
    main.py                 # FastAPI app and routes
    config.py               # loads .env via pydantic settings
    schemas.py              # all pydantic models
    data/
      nhd_graph.py          # build directed graph, upstream trace, travel time
      echo.py               # load and filter ECHO facilities/violations (VA)
      tri.py                # load TRI releases (VA)
      nwis.py               # fetch and cache gauge baselines
      noaa.py               # fetch and cache precipitation
    detection/
      sensor_anomaly.py     # rolling z-score / isolation forest
      storm_filter.py       # rain vs. spike rule
      vision.py             # YOLO inference + color-threshold plume mask
      fusion.py             # combine vision and sensor events
    simulation/
      spill_injector.py     # inject synthetic spike into real baseline
      plume_renderer.py     # composite simulated dye plume onto base imagery
      scenario.py           # one deterministic demo scenario
    agents/
      base.py               # Gemini client, JSON-schema output, retries
      tools.py              # plain Python tool functions agents may call
      triage_agent.py
      trace_agent.py
      compliance_agent.py
      report_agent.py
      orchestrator.py       # runs the chain and returns the final report
  tests/
  scripts/
    inspect_data.py
    run_scenario.py
  docs/data_notes.md
```
Modules to build
1. Config
Load all settings from `.env` with pydantic. Fail fast with a clear message if a required variable is empty.
2. River graph (`nhd_graph.py`)
Build a NetworkX directed graph from NHDPlus flowlines (edges flow downstream).
`trace_upstream(comid, max_km)` returns all reaches upstream with cumulative distance.
`travel_time(path)` returns hours using reach length divided by velocity. Confirm velocity units and convert to m/s. Return `None` with a reason if velocity is missing for a reach; never silently guess.
Cache the graph (pickle or parquet) so startup is fast.
3. Facilities (`echo.py`, `tri.py`)
Filter to state `VA`. Use facility coordinates to find facilities within a buffer of upstream reaches (use a projected CRS for distance, e.g. an appropriate UTM zone; document the choice).
For each candidate facility return: ID, name, distance to the nearest upstream reach, reported effluent violations (pollutant, period, status), and TRI chemicals if matched by facility.
Join ECHO and TRI on a shared facility identifier only if one exists in the files; otherwise match conservatively by proximity plus name and flag as "low-confidence match."
4. Sensors and rain (`nwis.py`, `noaa.py`)
Fetch and cache a few weeks of baseline for conductivity, turbidity, pH, discharge (USGS Water Data APIs).
Fetch and cache hourly precipitation from NOAA CDO. The token goes in a request header named `token`. Handle rate limits and missing data.
5. Simulation (`spill_injector.py`, `plume_renderer.py`, `scenario.py`)
Inject a synthetic spill (e.g. conductivity up, pH shift, turbidity bump) into the real baseline at a chosen timestamp, with configurable magnitude and decay. Mark injected rows with a `simulated=true` column.
Render a short simulated aerial clip: take a NAIP base image of the reach, advect a dye-colored plume along the river direction, and write an MP4 with a visible "SIMULATED" watermark. Time-sync the plume start to the sensor spike.
Generate extra negative clips (no plume; glare/shadow/algae look-alikes).
`scenario.py` creates one deterministic demo (fixed random seed) that the whole pipeline can replay.
6. Detection (`sensor_anomaly.py`, `storm_filter.py`, `vision.py`, `fusion.py`)
Sensor anomaly: rolling z-score (or isolation forest) per parameter against the baseline. Output events with timestamp, parameter, score.
Storm filter rule: turbidity spike AND rainfall above `RAIN_THRESHOLD_INCHES` in the last `RAIN_WINDOW_HOURS` means `storm_runoff`; turbidity spike with no rain means `flag_for_review` (not "dumping").
Vision: fine-tune YOLO on the Roboflow datasets for debris (note: River Trash is bounding boxes, not segmentation). Plume detection uses HSV color thresholding on the dye color. Output per-frame detections with confidence.
Fusion: an incident is raised only when vision and sensor events overlap in time; otherwise report as single-source and lower the confidence.
7. Agents (`agents/`)
Use Gemini function calling with strict JSON output validated by pydantic. Each agent returns structured JSON and the orchestrator passes it along.
Triage agent: input is fused events plus rain context. Output: `classification` (`incident`, `storm_runoff`, `noise`), `confidence` (0 to 1), `reasons[]`.
Trace agent: tools: `trace_upstream`, `travel_time`, `find_candidate_facilities`. Output: ranked candidates with distance, estimated travel time, and which of their reported pollutants match the anomalous parameters.
Compliance agent: tools: `get_facility_violations`, `get_tri_releases`. Output: only facts found in the data, each with a source field name.
Report agent: writes the final report (summary, timeline, ranked candidates, confidence, assumptions, limitations). Must include the simulated and human-review banner.
Guardrails: validate every output against its schema, reject any facility not present in the data, strip accusatory phrasing, retry once on invalid JSON, and fall back to a deterministic template report if Gemini fails (so the demo never breaks). Keep temperature low.
API endpoints (JSON only)
`GET /health`
`GET /reach` returns the chosen reach geometry (GeoJSON), upstream path, and gauge location
`GET /facilities?comid=...&max_km=...` returns candidate facilities (GeoJSON)
`POST /scenario/run` runs the deterministic simulated spill and returns an incident id
`GET /incidents/{id}` returns triage, trace, compliance, and report JSON
`GET /stream/{id}` Server-Sent Events replaying the scenario timeline (sensor ticks, plume detection events, agent step outputs) for the frontend to animate
`GET /sensors/{id}` returns baseline plus injected series
All responses include `simulated` and `requires_human_review` flags. Document the schemas in `docs/api.md` with example responses so the frontend teammate can build against them.
Build order
Config, schemas, `inspect_data.py`, `docs/data_notes.md`
NHD graph with upstream trace and travel time (with tests)
ECHO/TRI loaders and candidate facility search (with tests)
NWIS/NOAA fetch and cache
Spill injector, anomaly detection, storm filter (with tests)
Plume renderer, vision service, fusion
Agents and orchestrator with fallback template
FastAPI routes, SSE replay, `docs/api.md`
`scripts/run_scenario.py` for an end-to-end CLI run
Acceptance criteria
`python scripts/run_scenario.py` runs the full pipeline offline from cached data and prints a final report JSON.
Storm case: a turbidity spike with heavy rain is classified `storm_runoff`.
Spill case: injected spike plus simulated plume is classified `incident` with a ranked candidate list.
No candidate facility appears that is not in the local data.
Every output carries the simulated and human-review flags and uses non-accusatory wording.
Tests pass with `pytest`; no secrets appear in logs or git.
Notes for the human
Each unconfirmed item listed under "Verify first" should be reported back with what you actually found.
If a dataset is missing a field you need, say so and propose the simplest honest fallback rather than inventing data.