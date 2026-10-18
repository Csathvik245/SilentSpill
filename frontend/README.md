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

## Build

```powershell
npm run build
```
