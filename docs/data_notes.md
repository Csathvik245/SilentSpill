# SilentSpill data notes

Confirmed schema decisions for the backend build. Items are marked **verified** (read from the local files), **user-confirmed** (supplied by the team, not re-checked here) or **assumption**.

All outputs derived from this data are simulated demo output and require human review (`simulated: true`, `requires_human_review: true`).

## Demo setup

| Setting | Value | Source |
|---|---|---|
| Gauge | `NWIS_SITE_ID=01634000`, North Fork Shenandoah River near Strasburg, VA (38.9768, -78.3361, NAD83) | user-confirmed |
| Reach | `REACH_COMID=10000500083697` (an NHDPlusID, see below) | verified in the GeoPackage |
| Rain station | `NOAA_STATION_ID=GHCND:USW00013731` (Front Royal, about 9 km east) | user-confirmed |
| Rain rule | `RAIN_THRESHOLD_INCHES=0.5` within `RAIN_WINDOW_HOURS=24` | starting values, tunable |

Reach 10000500083697 (verified): `gnis_name` North Fork Shenandoah River, `lengthkm` 3.16, `streamorde` 6, `totdasqkm` 1,904, `qa` 658.3, `va` 0.962. Its `ftype`/`fcode` is 558/55800 (ArtificialPath, a centerline through a wide-river polygon), not 460 (StreamRiver). The tracer must not filter to `ftype=460` or it will drop the start reach.

## Local files

| Data | Path | Notes |
|---|---|---|
| NHDPlus HR EPA snapshot 2022, Virginia | `data/nhdplus_epasnapshot2022_va_gpkg/nhdplus_epasnapshot2022_va.gpkg` (2.36 GB) | Statewide Virginia, EPSG:4269 (NAD83) |
| ECHO ICIS-NPDES | `data/npdes_downloads/` (15 CSVs) | `data/npdes_downloads (1)/` is a byte-identical duplicate, to be removed by the user |
| ECHO effluent violations | `data/VA_NPDES_EFF_VIOLATIONS/VA_NPDES_EFF_VIOLATIONS.csv` | Use this. The national file has the same columns and is 15.6 GB. |
| ECHO outfalls layer | `data/npdes_outfalls_layer/npdes_outfalls_layer.csv` | Permit-level attributes plus coordinates |
| TRI | `data/2024_us.csv` | 2024 only |
| 3DEP | `data/USGS_3DEP_13_Collection.xml` | Metadata only, no raster |

Optional inputs, not yet downloaded. The code must treat all of these as absent-tolerant:

- `./data/naip/naip_nf_shenandoah_strasburg.tif` (`NAIP_PATH`)
- `./data/dem/USGS_13_n39w079.tif` (`DEM_TIF_PATH`)
- `./data/yolo/illegal-dumping-detection` (`YOLO_DATASET_DIR`)
- `./data/simulated_footage` (`SIMULATED_FOOTAGE_DIR`, written by the pipeline)

## NHDPlus GeoPackage (verified)

Layers: `nhdflowline_va` (383,203 rows), `nhdplusflowlinevaa_va`, `nhdpluseromma_va`, `nhdplusflow_va` (500,800 rows), `nhdline_va`, `nhdarea_va`, `nhdpoint_va`, `nhdwaterbody_va`, `catchment_fabric_va`.

- **Reach key:** `nhdplusid` (float, 14 digits). There is no `COMID` column. Use `nhdplusid` everywhere and keep the config name `REACH_COMID` only as the env var.
- **Column names are lowercase.** The brief's guesses `V0001E` and `QA_MA` do not exist.
- **Edges:** `nhdplusflow_va.fromnhdpid -> tonhdpid` is the downstream edge list. `fromnhdpid = 0` (121,220 rows) marks a start with no upstream neighbor, and `tonhdpid = 0` (839 rows) marks an outlet. Drop rows where either is 0 when building graph edges.
- **VAA** (`nhdplusflowlinevaa_va`): `fromnode`, `tonode`, `hydroseq`, `dnhydroseq`, `uphydroseq`, `levelpathi`, `pathlength`, `streamorde`, `totdasqkm`, `slope`. `dnhydroseq` is 0 on 778 rows and null on 60.
- **EROM** (`nhdpluseromma_va`): `qa`..`qe` are flow and `va`..`ve` are velocity for flow scenarios a to e. Use `qa` and `va` (mean annual, scenario a). `qa` and `va` are identical to `qb`/`vb` in the sample inspected.
- **Join coverage:** 374,129 of 383,203 flowlines join to both VAA and EROM. The 9,074 that do not are the rows with `flowdir = 0`. Treat them as outside the network.
- **Gauge ids:** `smgageid` is `'0'` on every EROM row, so gauges cannot be linked by id. Link them spatially (done for this gauge, see above).
- **Flowline types:** 460 StreamRiver (256,145), 558 ArtificialPath (114,167), 336 CanalDitch (8,305), 566 Coastline (3,596), 334 Connector (862), 428 Pipeline (127), 420 Underground Conduit (1).
- **Length:** `lengthkm` has no nulls or zeros (range 1.6e-6 to 40.6 km).
- **Velocity nodata:** `va < 0` (the value -9998) on 59,276 of 373,875 EROM rows (16%). Reaches with valid `va > 0` and `ftype = 460`: 255,929.

### Velocity and flow units (assumption, documented)

`va` is treated as feet per second and `qa` as cubic feet per second, per the NHDPlus HR documentation for EROM fields. The GeoPackage itself has no metadata that states units. Supporting check: `qa = 658` over `totdasqkm = 1,904` is plausible for the North Fork Shenandoah in cfs, and would be about 35 times too high if read as m3/s.

- Convert with `m/s = va * 0.3048`.
- Report this as an assumption in the incident report's assumptions list.

### Travel time rules

- `travel_time(path)` counts the **full length of every reach on the path, including the start reach** (the reach at the spill point is counted end to end, not from the gauge's position along it).
- A velocity is missing when `va <= 0` (the -9998 nodata value) or there is no EROM row. Missing velocities are never guessed or filled in.
- All reaches have velocity: `hours` is the travel time, `is_lower_bound = false`.
- Some reaches lack velocity: `hours = null`; `travel_time_hours_min` is the time over the reaches that have velocity, `is_lower_bound = true`, with `km_with_velocity`, `km_total`, `missing_reach_count`, `reaches_missing_velocity` and a `reason`. This is a **lower bound, not an estimate**.
- No reach has velocity: `hours` and `travel_time_hours_min` are both null, with a `reason`.
- Wording in the API and reports (the `display` field): `≥ 2.4 h (velocity available for 46.1 of 47.3 km)`. The `≥` is never dropped, the bound is rounded **down** to 0.1 h, and it is never called an estimate.
- Candidates are **ranked by distance, not travel time**, so missing velocities cannot change the order.
- On the demo trace, 118 of 547 reaches within 50 km (22%) lack velocity, so lower bounds will be common.

## USGS NWIS (user-confirmed, to be fetched and cached)

Fetch from the new USGS Water Data API (`https://api.waterdata.usgs.gov/ogcapi/v1`) and cache to CSV so the demo replays offline.

| Code | Parameter | Use |
|---|---|---|
| `00095` | Specific conductance (uS/cm at 25 C) | Sensor anomaly |
| `63680` | Turbidity (FNU) | Sensor anomaly, storm filter |
| `00400` | pH | Sensor anomaly |
| `00060` | Discharge (cfs) | Context |
| `00010`, `00300`, `99133` | Water temperature, dissolved oxygen, nitrate | Optional |

### Pulled baseline (verified, step 4)

- Window 2026-09-16T00:00Z to 2026-10-14T00:00Z, 2,689 expected 15-minute slots. Source: Water Data API `continuous` collection; the legacy IV fallback was not needed. All values provisional ("P").
- Rows: conductivity 2,411, turbidity 1,917, pH 2,418, discharge 2,687. One series per parameter at this site.
- Gaps are kept as NaN. A shared water-quality outage runs from 2026-10-03 19:45 to 10-06 00:45 (213 slots), and turbidity alone is out from 2026-09-24 18:45 to 09-29 15:30 (468 slots).
- Qualifiers: Water Data uses words (`ESTIMATED`, `ICE`) and an approval status, while the legacy service uses codes (`e`, `Ice`, `P`, `A`). Both are stored verbatim after an approval code (`P` or `A`).
- The demo reads only the committed snapshot in `backend/fixtures/baseline/`. `scripts/fetch_baselines.py` is the only code that calls the network.

## NOAA rain (user-confirmed)

- Pulled 2026-09-16 to 2026-10-14: 11 days with a PRCP reading, 18 without. Total 2.25 in; the only day ≥ 0.5 in is 2026-09-22 (0.95 in). Attribute flags are `H,,2,`.
- **Missing days are probably dry days, not outages.** The station reported temperature on every day up to 10-10 but PRCP on only 11 days, and no PRCP value is 0, so it appears to omit dry days. They are still stored as missing (never 0), and the storm filter and report must say "no rain reading" instead of "no rain". Days after 10-10 are missing because of normal reporting lag.

- CDO GHCND gives **daily** `PRCP` only. `PRECIP_HLY` stopped updating around 2014.
- **Limitation:** the storm filter works on daily totals. `RAIN_WINDOW_HOURS = 24` therefore means "the calendar day of the spike and the day before", and rain timing within a day is unknown. State this in the report limitations.
- Woodstock 2 NE (`GHCND:USC00449263`) is upstream of the reach but also daily-only. It is a possible second station.
- Auth: token in a request header named `token`.

## Demo scenarios (step 5)

- **Spill:** `SCENARIO_TIMESTAMP=2026-10-09T20:00:00Z` (16:00 local). All four sensors are continuous from 2026-10-06 19:00Z to 10-12 12:45Z, which gives 72 h before and 24 h after. It is clear of the 10-03 to 10-06 outage and the turbidity gap. There is no rain reading on 10-08 or 10-09 (likely dry; the station reported temperature).
  - It was first set to 10-10 14:00Z and moved earlier: the **real** data has a natural runoff-like event starting about 2026-10-11 09:00Z (discharge 115 to 256 cfs, turbidity 0.7 to 9.2 FNU, conductivity 406 to 314) with no rain reading, which would have fallen inside the spill's 24 h window. The filter classifies that real event `flag_for_review` with "no rain reading".
  - Injected spill defaults: conductivity +250 uS/cm, pH -1.0, turbidity +15 FNU, 45 min rise, 6 h e-folding decay, 24 h duration, 3% seeded noise.
  - pH is compared with the **same time of day over the previous 72 h** (±30 min, at least 6 points), which removes its daily 8.2-8.7 swing. Against that baseline a -0.8 shift scores z 6.9 and -1.0 scores z 8.9 at the 20:00Z daily high, so -1.0 is used. The std floor for this baseline is 0.1, the gauge's pH resolution; with the 0.02 floor, single 0.1 steps scored as anomalies (28 false events over three weeks). With 0.1 there are no pH events from 09-19 to 10-11 without injection, and one real pH drop during the natural 10-11 runoff.
- **Storm:** 2026-09-22 (`STORM_CASE_DATE`), real observed data, unchanged. Detection runs over the storm day and the next day. A one-reading turbidity spike on the evening of 09-21 has only 0.28 in in its daily window (09-20 no reading, 09-21 0.28 in), so the strict daily rule flags it for review. That is reported, not hidden.
- **Event merging:** events of the same parameter and direction less than 1 h apart (end to next onset) are joined into one.
- **Detection defaults:** z ≥ 4 against a 24 h time-based baseline of preceding values (at least 48 points) for conductivity and turbidity, and the same-time-of-day baseline for pH, with std floors of 2 uS/cm, 0.2 FNU and 0.1 pH. Over the quiet stretch 10-07 to 10-11 this gives one single-reading turbidity false event (10-10 11:00Z) and no pH false events.

## Camera simulation and fusion (step 6)

- No NAIP imagery: clips are drawn top-down on a synthetic river along the real flowline of the demo reach (`backend/fixtures/reach/reach_10000500083697.geojson`, digitized upstream to downstream), at about 3.5 m per pixel with a 40 m wide channel. Every frame carries a "SIMULATED" watermark and a UTC timestamp.
- The spill clip runs from 19:50 to 21:49Z (30 s of scenario time per frame). The dye is released at `SCENARIO_TIMESTAMP` and drifts at the reach's EROM velocity (0.293 m/s). The negatives are glare, cloud shadow and algae, with no dye.
- Plume detection is an HSV threshold on the dye colour. It finds the plume from 20:02Z in 216 of 240 frames, and nothing in any negative. There are no YOLO weights (ultralytics is not installed), so HSV is the only plume detector.
- Roboflow debris model: one live run of 48 frames, all HTTP 200, **0 predictions on every clip**. The responses are cached in `backend/fixtures/vision/` and the demo reads only the cache. The model detects trash and people, not plumes, so it is supporting context only.
- Fusion: an incident needs a plume and an unexplained gauge anomaly within ±3.0 h (the reach's travel time). A single source gives flag_for_review with confidence at most 0.4, and storm-runoff events never count as corroboration.

## Agents on Groq (step 7)

- Chain: Triage → Trace → Compliance → Report (`backend/app/agents/`), run with `python scripts/run_scenario.py --case spill|storm`. The model never calls tools: agent code calls the plain Python tools, records every returned value in a ledger, and shows the model those results as JSON. The model only selects and writes prose.
- **Agent modes** (`agent_modes`, and `report_mode` on the report):
  - `llm`: the model answered and its output passed the guardrails.
  - `fallback`: the model was expected but failed (no key or model set, invalid JSON twice, rate limit exhausted, timeout, API error), so the deterministic template was used. `fallback_reason` says why, and never contains the key.
  - `skipped`: the agent makes no model call by design. Storm runoff gets no candidate list, so trace and compliance are skipped. `skipped` never means a failure.
- Guardrails (in code):
  - facility IDs and names must come from tool results;
  - accusatory wording is rewritten or the sentence is dropped;
  - triage can't be stronger than fusion, and confidence is fusion's;
  - candidate values are copied from tools, in distance order;
  - `simulated` and `requires_human_review` are always true.
  The notes record each rejection without repeating the rejected name or wording.
- **Number guardrail:** a sentence is dropped if it cites a number that is not in the tool results, the pipeline outputs (gauge events, storm filter, rain, camera summary, fusion, run settings) or the context shown to the model. Dates, times, year ranges and the "≥ X h" bounds are recognised. Per-reach trace values and per-frame camera values are not citable, so the allowed set stays small. Limit: numbers are checked without their units, so a real value cited with the wrong unit (e.g. a 3.4 FNU turbidity peak written as "3.4 in" of rain) would pass.

## ECHO (verified)

### Facilities (`ICIS_FACILITIES.csv`)

Key `NPDES_ID`; also `FACILITY_NAME`, `STATE_CODE`, `GEOCODE_LATITUDE`, `GEOCODE_LONGITUDE`, `FACILITY_UIN` (the FRS registry id, used for the TRI join). Virginia: 18,688 permits, 18,486 with coordinates. Drop the 16 with bad coordinates (latitude 0 or positive longitude), and read the file with `encoding="latin-1"`.

### Coordinates: outfall versus facility

- `GEOCODE_LATITUDE/LONGITUDE` in `ICIS_FACILITIES` is a **facility** point.
- `NPDES_PERM_FEATURE_COORDS.csv` (`EXTERNAL_PERMIT_NMBR`, `PERM_FEATURE_NMBR`, `LATITUDE_MEASURE`, `LONGITUDE_MEASURE`) holds **outfall** points: 14,072 Virginia rows for 6,974 permits.
- Of 4,744 (permit, outfall) pairs in the Virginia violations file, 4,516 match an outfall point. 2,686 of the 2,764 violating permits have at least one outfall point.
- **Rule:** use the outfall point for a violation's outfall when it exists, otherwise the facility point. Every candidate carries a `location_type` of `outfall` or `facility`, and facility points are labeled as less precise. Both are reported coordinates, not surveyed discharge locations.
- `npdes_outfalls_layer.csv` has `LATITUDE83/LONGITUDE83` with `LATLONG_TYPE` of `Permitted Feature` (12,970 Virginia rows) or `Facility` (9,758). Rows with `Facility` have no `PERM_FEATURE_NMBR`.

### Effluent violations (`VA_NPDES_EFF_VIOLATIONS.csv`)

- 172,533 rows, 2,764 permits, `MONITORING_PERIOD_END_DATE` from 1989-02-28 to 2026-07-31, stored as `MM/DD/YYYY` text.
- Fields to use: `NPDES_ID`, `PERM_FEATURE_NMBR`, `PARAMETER_DESC`, `VIOLATION_DESC`, `MONITORING_PERIOD_END_DATE`, `EXCEEDENCE_PCT`, `RNC_RESOLUTION_DESC`, `DMR_VALUE_STANDARD_UNITS`, `LIMIT_VALUE_STANDARD_UNITS`, `STANDARD_UNIT_DESC`.
- `VIOLATION_TYPE_DESC` is almost always empty. Use `VIOLATION_DESC`.
- **Most rows are reporting violations, not pollutant exceedances.** "DMR, Limited - Overdue" (86,277) and "DMR, Monitor Only - Overdue" (62,714) make up about 86% of rows. "DMR, Limited - Numeric Violation" is 23,542 rows, and `EXCEEDENCE_PCT` is filled on 23,124. Candidate ranking uses numeric violations only. Overdue reports may be listed separately and must not be presented as pollution.
- Wording: all violations are **reported** or **alleged** in the ECHO data. Use "reported violation" and never "caused" or "responsible for".

### Permit and facility type (`ICIS_PERMITS.csv`, `ICIS_FACILITIES.csv`)

- `ICIS_PERMITS.csv` has one row per permit version (36,809 Virginia rows). Each permit uses its `EFF` (effective) version when there is one, otherwise its highest `VERSION_NMBR`.
- `PERMIT_TYPE_CODE` → `permit_type`: `NPD` (NPDES Individual Permit) is `individual`; `GPC` (General Permit Covered Facility) and `NGP` (NPDES Master General Permit) are `general`. Other codes (`APR`, `UFT`, `IIU`, …) keep the code and ECHO's description, with `permit_type` null. The code meanings come from the ECHO ICIS-NPDES download summary (checked 2026-10-18).
- A covered permit's `MASTER_EXTERNAL_PERMIT_NMBR` points to its master general permit, whose `PERMIT_NAME` is the permit's title. Virginia's `VAG400000` is "VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day". A general permit whose master title contains "domestic sewage" is flagged `small_domestic_general_permit`, and the report says so plainly.
- `ICIS_FACILITIES.FACILITY_TYPE_CODE` → `facility_type`, e.g. `COR` Corporation, `POF` Privately Owned Facility. `ICIS_PERMITS.FACILITY_TYPE_INDICATOR` (POTW, NON-POTW, FEDERAL) is kept as `facility_type_indicator`.
- These fields are context only. They never change the ranking, which is by river distance.
- Demo reach: VA0052621 holds an individual permit. VAG408560, VAG408449 and VAG408158 hold the VAG40 small domestic general permit.

### Disclaimer

Every incident response (`/scenario/run`, `/incidents/{id}`, the CLI output) and the top of every report carry the fixed field `disclaimer`: "Simulated incident. Facilities are listed only because they have reported permit violations upstream; no link to any real event is implied." It is a schema constant, so neither the model nor the template can change it.

### Parameter proxies (assumption, documented)

The sensors measure conductivity and turbidity, but ECHO reports hardly any of either (`PARAMETER_DESC` matches: conductivity 14 rows, turbidity 2 rows). Matching therefore uses these proxies, and every match is labeled as a proxy:

| Sensor | ECHO parameters matched | Rows (approx.) |
|---|---|---|
| Specific conductance (`00095`) | Solids, total dissolved [TDS]; any `PARAMETER_DESC` containing "conductance" | 13,647 TDS |
| Turbidity (`63680`) | Solids, total suspended | 31,013 |
| pH (`00400`) | pH | 18,705 |

These are indirect indicators and prove nothing about a source. The report lists them under assumptions.

## TRI (verified)

- Columns are numbered ("1. YEAR", "8. ST", "12. LATITUDE", "37. CHEMICAL", "53. 5.3 - WATER", "107. TOTAL RELEASES"). Strip the numeric prefix when loading. 122 columns.
- Year 2024 only. Virginia: 1,207 rows, 410 facilities, all with coordinates. `HORIZONTAL DATUM` is empty, so treat coordinates as approximate.
- `UNIT OF MEASURE`: pounds (1,197 rows) and grams (10 rows). Normalize or carry the unit.
- Releases to water (`5.3 - WATER` > 0): 229 rows from 96 facilities.
- TRI is annual, self-reported data. It cannot match a live plume and is shown only as context.
- **Join to ECHO:** `TRI FRS ID` matches `ICIS_FACILITIES.FACILITY_UIN` for 243 of 397 Virginia FRS ids (61%). Match on that id first. For the rest, match by proximity plus name only and flag it `low-confidence match`.

## Candidate facility search (step 3)

- Candidates are permits with a point within `buffer_m` (default 1,000 m) of a reach in the upstream trace, and at least one **numeric limit violation** in the window. Permits with only overdue-report violations are excluded. Their overdue count is shown separately as `reporting_violation_count` and is never ranked.
- Violation window: `VIOLATION_WINDOW_YEARS` (default 5) ending at the **scenario date** (`reference_date`), never today's date, so the demo replays identically. Without a reference date it ends at the latest `MONITORING_PERIOD_END_DATE` in the file (2026-07-31). Violations after the reference date are left out.
- Each candidate reports `numeric_violation_count` (in the window), `historical_numeric_violation_count` (older numeric violations, labeled historical and never ranked), the window dates, and `violation_window_label`, e.g. "reported numeric limit violations, 2021–2026", which reports must use when describing the window.
- Location: outfall points replace the facility point when a permit has any. Each candidate carries `location_type` and a `location_note`.
- Rank: river distance upstream (`upstream_distance_km`), then distance to the river, then permit id. Travel time never affects the order.
- `upstream_distance_km` is the river distance from the top of the start reach to the downstream end of the facility's nearest reach (0.0 for the start reach itself).
- TRI: matched on FRS id (`high`), otherwise within 1 km and name similarity ≥ 0.5 among TRI sites not already tied to an ECHO facility (`low`).
- Demo result (reach 10000500083697, 50 km, 1 km): **4 candidates**, all at outfall points, 2 to 18 km upstream. Only one matches a sensor parameter (TSS → turbidity, proxy) and none match TRI. Several nearby treatment plants have numeric violations, but only before the window.

## Spatial conventions

- Source CRS is EPSG:4269 (NAD83). Compute distances in a projected CRS: **EPSG:32617 (UTM zone 17N)** for the demo area (the Shenandoah, about 78.3 W). Document the choice in code, and note that distance error grows for features far west or east of the zone.
- Facility and gauge points are treated as NAD83/WGS84-equivalent for this purpose.

## Limitations to carry into reports

1. Facility coordinates are reported locations, not confirmed discharge points. Outfall points are used where available.
2. Violations are reported or alleged, and most rows in the data are overdue reports.
3. Velocity and flow units are a documented assumption (ft/s and cfs). 16% of reaches have no velocity, so some travel times are unavailable.
4. Rain is daily only, so the storm filter is coarse.
5. TRI is annual (2024) and cannot match a live event.
6. Sensor-to-pollutant matching uses proxies (TDS, TSS).
7. The spill, plume footage and sensor spike are simulated.
