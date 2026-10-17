"""Vision service: HSV dye-plume detection on simulated footage, plus cached Roboflow debris context.

Plume detection is a colour threshold for the rhodamine-like dye drawn by the plume renderer. It runs
offline on every frame. The Roboflow model detects trash and people, not plumes: its results are
supporting context only, fetched once by scripts/run_debris_detection.py and read from a committed cache.
All footage here is SIMULATED.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np

from ..config import PROJECT_ROOT, Settings, get_settings
from ..schemas import VisionDetection, VisionFrame, VisionSummary
from ..simulation.plume_renderer import DYE_BGR, FPS, FRAME_STEP_S, WATER_BGR  # noqa: F401  (DYE_BGR documents the target)

# Dye range in OpenCV HSV (H 0-179). Pure dye is ~(158, 211, 230); dye blended over water at alpha 0.6 is
# ~(153, 162, 154). Glare (low S), shadow (low V) and algae (H ~25-80) fall outside.
HSV_LOWER = (140, 100, 80)
HSV_UPPER = (175, 255, 255)
MIN_AREA_FRACTION = 0.002  # dye pixels / river pixels needed to call a plume in a frame
FULL_CONFIDENCE_AREA = 0.03  # area fraction at which confidence reaches 1.0

# River-mask estimate (used only when the caller has no mask): water-coloured pixels around WATER_BGR,
# whose HSV is ~(96, 142, 90). V has a low floor so shadowed water still counts.
WATER_HSV_LOWER = (80, 60, 20)
WATER_HSV_UPPER = (112, 255, 200)
MIN_RIVER_FRACTION = 0.01  # below this the estimate is not trusted and the whole frame is used

DEBRIS_CACHE_DIR = PROJECT_ROOT / "backend" / "fixtures" / "vision"
DEBRIS_NOTE = (
    "The Roboflow model detects trash and people, not plumes; its results are supporting context only. "
    "Frames are SIMULATED footage."
)
REQUEST_TIMEOUT_S = 30
MAX_ATTEMPTS = 3
JPEG_QUALITY = 85

_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


# ---- HSV plume detection ---------------------------------------------------------------------
def plume_mask(frame_bgr: np.ndarray, river_mask: np.ndarray | None = None) -> np.ndarray:
    """Bool mask of dye-coloured pixels (inside river_mask if given), after a 3x3 opening to drop speckle."""
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(HSV_LOWER, np.uint8), np.array(HSV_UPPER, np.uint8))
    if river_mask is not None:
        mask[~river_mask.astype(bool)] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _KERNEL)
    return mask.astype(bool)


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of the mask's pixels, as cv2.boundingRect."""
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    x0, y0 = int(xs.min()), int(ys.min())
    return x0, y0, int(xs.max()) - x0 + 1, int(ys.max()) - y0 + 1


def _analyze(
    frame_bgr: np.ndarray, frame_index: int, timestamp: datetime, river_mask: np.ndarray | None
) -> tuple[VisionFrame, np.ndarray]:
    mask = plume_mask(frame_bgr, river_mask)
    denom = int(river_mask.sum()) if river_mask is not None else mask.size
    area = float(mask.sum()) / denom if denom else 0.0
    detected = area >= MIN_AREA_FRACTION
    # confidence = 0 below MIN_AREA_FRACTION, else area / FULL_CONFIDENCE_AREA capped at 1.
    conf = min(1.0, area / FULL_CONFIDENCE_AREA) if detected else 0.0
    vf = VisionFrame(
        frame_index=frame_index,
        timestamp=_utc(timestamp),
        plume_detected=detected,
        area_fraction=area,
        confidence=conf,
    )
    return vf, mask


def analyze_frame(
    frame_bgr: np.ndarray, frame_index: int, timestamp: datetime, river_mask: np.ndarray | None = None
) -> VisionFrame:
    """HSV result for one frame. area_fraction = dye px / river px (or / frame px without a river mask);
    plume_detected = area_fraction >= MIN_AREA_FRACTION; confidence = min(1, area_fraction / FULL_CONFIDENCE_AREA)
    when detected, else 0."""
    return _analyze(frame_bgr, frame_index, timestamp, river_mask)[0]


def estimate_river_mask(frames: Sequence[np.ndarray], n_samples: int = 12) -> np.ndarray | None:
    """Union of water- or dye-coloured pixels over evenly spaced frames, closed and opened.

    The union covers water briefly hidden by glare, algae or dye. Returns None when the result is
    under MIN_RIVER_FRACTION of the frame (callers then use the whole frame).
    """
    if not frames:
        return None
    idx = np.unique(np.linspace(0, len(frames) - 1, min(n_samples, len(frames))).round().astype(int))
    union = np.zeros(frames[0].shape[:2], np.uint8)
    for i in idx:
        hsv = cv2.cvtColor(frames[i], cv2.COLOR_BGR2HSV)
        water = cv2.inRange(hsv, np.array(WATER_HSV_LOWER, np.uint8), np.array(WATER_HSV_UPPER, np.uint8))
        dye = cv2.inRange(hsv, np.array(HSV_LOWER, np.uint8), np.array(HSV_UPPER, np.uint8))
        union |= water | dye
    big = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    union = cv2.morphologyEx(union, cv2.MORPH_CLOSE, big)
    union = cv2.morphologyEx(union, cv2.MORPH_OPEN, big)
    mask = union.astype(bool)
    return mask if mask.mean() >= MIN_RIVER_FRACTION else None


def analyze_frames(
    frames: Sequence[np.ndarray],
    timestamps: Sequence[datetime],
    river_mask: np.ndarray | None = None,
    clip_name: str = "",
    clip_kind: str = "spill",
    debris: list[VisionDetection] | None = None,
    debris_status: str = "not run",
) -> VisionSummary:
    """Run the HSV detector over a clip. Without a river_mask one is estimated (estimate_river_mask),
    falling back to the whole frame. time_s of a detection is the video time, frame_index / FPS."""
    if len(frames) != len(timestamps):
        raise ValueError("frames and timestamps must have the same length")
    if river_mask is None:
        river_mask = estimate_river_mask(frames)
    results: list[VisionFrame] = []
    detections: list[VisionDetection] = []
    for i, (frame, ts) in enumerate(zip(frames, timestamps)):
        vf, mask = _analyze(frame, i, ts, river_mask)
        results.append(vf)
        if vf.plume_detected:
            detections.append(
                VisionDetection(
                    frame_index=i,
                    time_s=i / FPS,
                    timestamp=vf.timestamp,
                    label="plume",
                    confidence=vf.confidence,
                    area_fraction=vf.area_fraction,
                    bbox=_bbox(mask),
                    detector="hsv",
                )
            )
    return VisionSummary(
        clip=clip_name,
        clip_kind=clip_kind,
        frames_analyzed=len(results),
        frames=results,
        plume_detections=detections,
        first_plume=detections[0].timestamp if detections else None,
        last_plume=detections[-1].timestamp if detections else None,
        debris=list(debris or []),
        debris_status=debris_status,
    )


def read_clip(mp4_path: Path | str) -> tuple[list[np.ndarray], list[datetime], dict]:
    """Frames via cv2.VideoCapture; timestamps rebuilt from the sidecar JSON (first timestamp + i * frame_step_s)."""
    mp4_path = Path(mp4_path)
    meta = json.loads(mp4_path.with_suffix(".json").read_text(encoding="utf-8"))
    cap = cv2.VideoCapture(str(mp4_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"could not open video {mp4_path}")
    frames: list[np.ndarray] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        cap.release()
    first = meta.get("first_timestamp") or meta.get("start") or meta.get("clip_start")
    if first is None:
        raise ValueError(f"sidecar for {mp4_path.name} has no first timestamp")
    start = _utc(datetime.fromisoformat(str(first).replace("Z", "+00:00")))
    step = timedelta(seconds=float(meta.get("frame_step_s", FRAME_STEP_S)))
    return frames, [start + i * step for i in range(len(frames))], meta


# ---- Roboflow debris (supporting context only) -----------------------------------------------
def _cache_path(clip_name: str, cache_dir: Path | None) -> Path:
    return Path(cache_dir or DEBRIS_CACHE_DIR) / f"roboflow_{clip_name}.json"


def _scrub(obj: Any, secret: str | None) -> Any:
    """Replace any occurrence of the secret in strings (belt and braces before saving)."""
    if not secret:
        return obj
    if isinstance(obj, str):
        return obj.replace(secret, "***")
    if isinstance(obj, list):
        return [_scrub(v, secret) for v in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v, secret) for k, v in obj.items()}
    return obj


def _write_cache(path: Path, payload: dict, secret: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_scrub(payload, secret), indent=1) + "\n", encoding="utf-8")
    return path


def _error_text(resp: Any) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            msg = body.get("message") or body.get("error") or body.get("detail")
            if msg:
                return str(msg)[:300]
    except Exception:
        pass
    return str(getattr(resp, "text", "") or "")[:300]


def _post(session: Any, url: str, **kwargs: Any) -> Any:
    """POST with a timeout, retrying 429/5xx and connection errors with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = session.post(url, timeout=REQUEST_TIMEOUT_S, **kwargs)
        except Exception as exc:  # requests.RequestException and friends
            last_exc = exc
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            if attempt == MAX_ATTEMPTS - 1:
                return resp
        time.sleep(2**attempt)
    raise ConnectionError(type(last_exc).__name__ if last_exc else "request failed")


REQUEST_FORMAT = (
    "POST {api_url}/{model_id}, base64 JPEG body (application/x-www-form-urlencoded), "
    "key in an Authorization: Bearer header (verified live; the key is never in the URL or body)"
)


def _predictions(body: Any) -> list[dict]:
    if isinstance(body, list):  # batched responses come back as a list
        body = body[0] if body else {}
    preds = body.get("predictions", []) if isinstance(body, dict) else []
    keep = ("class", "class_id", "confidence", "x", "y", "width", "height", "detection_id")
    return [{k: p[k] for k in keep if k in p} for p in preds if isinstance(p, dict)]


def fetch_debris(
    frames: Sequence[np.ndarray],
    timestamps: Sequence[datetime],
    clip_name: str,
    sample_every: int = 20,
    settings: Settings | None = None,
    session: Any = None,
    cache_dir: Path | None = None,
) -> Path:
    """LIVE NETWORK. Send every Nth frame to the hosted Roboflow model and cache the raw predictions.

    Only scripts/run_debris_detection.py calls this. It never raises for network or API errors: the cache
    records status "ok", "skipped" or "failed" with a reason that never contains the key. The key travels
    only in the Authorization header; a rejected key (HTTP 401/403) is recorded as "failed".
    """
    settings = settings or get_settings()
    path = _cache_path(clip_name, cache_dir)
    payload: dict[str, Any] = {
        "clip_name": clip_name,
        "model_id": settings.roboflow_model_id,
        "status": "skipped",
        "reason": "",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sample_every": sample_every,
        "request_format": None,
        "simulated": True,
        "note": DEBRIS_NOTE,
        "responses": [],
    }
    if settings.roboflow_api_key is None or not settings.roboflow_api_key.get_secret_value():
        payload["reason"] = "ROBOFLOW_API_KEY not set"
        return _write_cache(path, payload, None)

    key = settings.roboflow_api_key.get_secret_value()
    if session is None:
        import requests

        session = requests.Session()
    url = f"{settings.roboflow_api_url.rstrip('/')}/{settings.roboflow_model_id.strip('/')}"
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Bearer {key}"}
    payload["request_format"] = REQUEST_FORMAT
    try:
        for i in range(0, len(frames), max(1, sample_every)):
            ok, buf = cv2.imencode(".jpg", frames[i], [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not ok:
                raise ValueError(f"JPEG encoding failed for frame {i}")
            resp = _post(session, url, data=base64.b64encode(buf.tobytes()).decode("ascii"), headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} on frame {i}: {_error_text(resp)}")
            payload["responses"].append(
                {
                    "frame_index": i,
                    "timestamp": _utc(timestamps[i]).isoformat(),
                    "predictions": _predictions(resp.json()),
                }
            )
        payload["status"] = "ok"
        payload["reason"] = f"{len(payload['responses'])} sampled frames sent to the hosted model"
    except Exception as exc:
        payload["status"] = "failed"
        payload["reason"] = str(exc).replace(key, "***") or type(exc).__name__
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return _write_cache(path, payload, key)


def load_debris(clip_name: str, cache_dir: Path | None = None) -> tuple[list[VisionDetection], str]:
    """Read the cached Roboflow response (never the network). bbox is (x, y, width, height) from the
    model's centre x/y and size; time_s is frame_index / FPS."""
    path = _cache_path(clip_name, cache_dir)
    if not path.exists():
        return [], "skipped: no cached Roboflow response (run scripts/run_debris_detection.py)"
    data = json.loads(path.read_text(encoding="utf-8"))
    status = data.get("status", "failed")
    if status != "ok":
        return [], f"{status}: {data.get('reason', '')}"
    dets: list[VisionDetection] = []
    responses = data.get("responses", [])
    for r in responses:
        ts = r.get("timestamp")
        ts = _utc(datetime.fromisoformat(ts)) if ts else None
        for p in r.get("predictions", []):
            w, h = float(p.get("width", 0)), float(p.get("height", 0))
            x, y = float(p.get("x", 0)) - w / 2, float(p.get("y", 0)) - h / 2
            dets.append(
                VisionDetection(
                    frame_index=int(r["frame_index"]),
                    time_s=int(r["frame_index"]) / FPS,
                    timestamp=ts,
                    label=str(p.get("class", "unknown")),
                    confidence=min(1.0, max(0.0, float(p.get("confidence", 0)))),
                    bbox=(round(x), round(y), round(w), round(h)),
                    detector="roboflow",
                )
            )
    return dets, f"ok: {len(dets)} predictions on {len(responses)} sampled frames (cached {data.get('fetched_at')})"
