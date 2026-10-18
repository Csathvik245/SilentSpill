# SilentSpill frontend

Vite + React + TypeScript field-review workspace. The app uses shadcn/ui primitives, MapLibre GL, Recharts, and Motion.

## Run locally

Start the backend from the repository root:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --port 8000
```

Then start the frontend:

```powershell
cd frontend
npm install
npm run dev
```

The frontend serves on `http://localhost:5173`. It reads the API from `http://localhost:8000` by default. Set `VITE_API_BASE_URL` if the backend uses a different base URL.

## Data flow

- `GET /health` and `GET /reach` populate service status and river geometry.
- `POST /scenario/run` returns the incident id and agent modes.
- `GET /incidents/{id}` supplies the report and facility records; report text is rendered as returned.
- `GET /sensors/{id}` supplies the sensor history.
- `/stream/{id}?speed=600` drives replay time, sensor updates, plume cues, and agent progress.

The original simulated aerial clip is kept in `public/spill_20261009T2000Z_seed42.mp4`; a VP8 WebM playback copy is provided for browsers without MPEG-4 Part 2 decoding. The clip is only shown for the spill review. The product’s simulated-data banner is always visible; every result requires human review.

## Watershed map

The MapLibre view flies to a 55° pitch and a valley bearing derived from the API river geometry. Right-drag to rotate; the compass resets north. Main-stem segments are identified using the gauge-linked reach ID and river name. Animated dashes move in the digitized upstream-to-downstream direction, with a brighter, faster main stem and slower tributaries. These animation speeds illustrate direction, not measured water velocity.

Terrarium elevation tiles from AWS Terrain Tiles provide 1.6× terrain, with separate hillshade tiles and a subtle sky/fog. If elevation requests fail, the view silently retains the flat tilted map. Replay plume detections trigger a warm spreading glow along the main stem; this is an event cue, not an inferred plume location. Reduced-motion preferences disable flow and spreading animations, including when changed while the app is open.

For recording, use two PowerShell terminals from the repository root:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

```powershell
# Terminal 2
cd frontend
npm.cmd run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173/`. If the servers are already running on these ports, use the existing instances.

## Build

```powershell
npm run build
```

## Pages and review flow

The site uses hash routing with no routing dependency:

- `#/` - Overview, with a live watershed strip and API-backed data availability.
- `#/how` - Detection, rain checks, upstream tracing, and agent guardrails.
- `#/investigate` - Spill/storm controls, replay, ranked map pins, Facilities / Sensors / Report / Footage tabs, and human review.
- `#/data` - Real versus simulated records, plus limitations from the current or latest completed run in this browser session.
- `#/about` - Team story and editable learning notes. Replace `[TODO: hackathon name]` before presenting.

Switching pages retains the current investigation and replay. A full browser reload starts a fresh session. No incident content is invented or cached as a frontend fixture.

The review control requires a person to choose a decision. `POST /incidents/{id}/review` submits that choice and displays whether incident memory saved it, skipped it, or failed. It does not silently infer a human decision from the model classification.

Charts show conductivity, turbidity, and pH separately. Real observations use solid water-coloured lines; simulated values use dashed amber lines. Missing measurements are shown as unavailable. Replay is driven by the existing SSE endpoint.

Numbered map labels are 28 px and use screen-space offsets with connecting lines when they would overlap. Their API coordinates are unchanged. Hovering a facility lifts its label; selecting it flies to its location. The panel occupies its own column, so it does not cover the map camera.

Geist Mono is hosted locally in `public/geist-mono.ttf`; its SIL Open Font License is included alongside it. No packages were added for the multi-page redesign. The API client, API types, and backend remain unchanged.
