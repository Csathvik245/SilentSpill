"""Tests for backend.app.detection.vision, written from the step 6 contract.

No network: Roboflow debris tests use temporary cache files, a monkeypatched DEBRIS_CACHE_DIR
and a fake session.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from types import SimpleNamespace

import numpy as np
import pytest
import requests
from pydantic import SecretStr

from backend.app.config import ConfigError
from backend.app.detection import vision
from backend.app.schemas import VisionFrame, VisionSummary
from backend.app.simulation import plume_renderer as pr

REACH_ID = 10000500083697
SCENARIO = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
SEED = 7
N = 60  # covers the plume start at frame 20
W, H = pr.FRAME_SIZE
FAKE_KEY = "rf-fake-key-0123456789"
MODEL = "illegal-dumping-detection-dd6wk-3t0jf/1"
API = "https://serverless.roboflow.com"


def water() -> np.ndarray:
    return np.full((H, W, 3), pr.WATER_BGR, np.uint8)


def patch(color, x=300, y=200, size=60, base=None) -> np.ndarray:
    f = water() if base is None else base.copy()
    f[y:y + size, x:x + size] = color
    return f


def blend(alpha: float = 0.6) -> tuple[int, int, int]:
    c = alpha * np.array(pr.DYE_BGR) + (1 - alpha) * np.array(pr.WATER_BGR)
    return tuple(int(v) for v in np.round(c))


def band_mask() -> np.ndarray:
    m = np.zeros((H, W), bool)
    m[200:280, :] = True  # 80 * 640 = 51,200 river pixels
    return m


# ---- constants -------------------------------------------------------------------------------
def test_constants():
    assert tuple(vision.HSV_LOWER) == (140, 100, 80) and tuple(vision.HSV_UPPER) == (175, 255, 255)
    assert vision.MIN_AREA_FRACTION == 0.002 and vision.FULL_CONFIDENCE_AREA == 0.03


# ---- plume_mask / analyze_frame on synthetic frames ------------------------------------------
@pytest.mark.parametrize("color", [pr.DYE_BGR, blend(0.6)], ids=["pure", "blend60"])
def test_dye_detected(color):
    f = patch(color)
    m = vision.plume_mask(f)
    assert m.dtype == bool and m.shape == (H, W)
    assert m.sum() == pytest.approx(3600, rel=0.05)
    assert not m[:200].any() and not m[260:].any()
    vf = vision.analyze_frame(f, 3, SCENARIO, None)
    assert isinstance(vf, VisionFrame)
    assert vf.frame_index == 3 and vf.timestamp == SCENARIO and vf.plume_detected is True
    assert vf.area_fraction == pytest.approx(3600 / (W * H), rel=0.05)
    assert vf.confidence == pytest.approx(min(1.0, vf.area_fraction / vision.FULL_CONFIDENCE_AREA))


LOOKALIKES = {
    "algae_green": (40, 140, 60),
    "algae_brown": (30, 80, 110),
    "glare_white": (245, 245, 240),
    "glare_pale_yellow": (200, 240, 250),
    "shadow": (45, 40, 20),
    "water": pr.WATER_BGR,
}


@pytest.mark.parametrize("name", list(LOOKALIKES))
def test_lookalikes_not_detected(name):
    f = patch(LOOKALIKES[name], size=200, x=200, y=100)
    assert vision.plume_mask(f).sum() == 0
    vf = vision.analyze_frame(f, 0, SCENARIO, None)
    assert vf.plume_detected is False and vf.confidence == 0.0


def test_river_mask_limits_and_normalises():
    river = band_mask()
    inside = patch(pr.DYE_BGR, x=300, y=220, size=40)  # 1,600 px / 51,200 = 0.03125
    m = vision.plume_mask(inside, river)
    assert m.sum() == pytest.approx(1600, rel=0.05) and not (m & ~river).any()
    vf = vision.analyze_frame(inside, 0, SCENARIO, river)
    assert vf.area_fraction == pytest.approx(1600 / river.sum(), rel=0.05)
    assert vf.plume_detected and vf.confidence == 1.0  # capped at 1

    outside = patch(pr.DYE_BGR, x=300, y=20, size=60)  # above the band
    assert vision.plume_mask(outside, river).sum() == 0
    vf = vision.analyze_frame(outside, 0, SCENARIO, river)
    assert vf.plume_detected is False and vf.area_fraction == 0.0 and vf.confidence == 0.0


def test_confidence_formula_and_min_area():
    river = band_mask()
    n_river = int(river.sum())
    # 0.01 of the river -> detected, confidence 0.01 / 0.03
    side = int(round((0.01 * n_river) ** 0.5))
    vf = vision.analyze_frame(patch(pr.DYE_BGR, x=100, y=210, size=side), 0, SCENARIO, river)
    assert vf.plume_detected
    assert vf.confidence == pytest.approx(vf.area_fraction / 0.03)
    assert vf.confidence == pytest.approx(side * side / n_river / 0.03, rel=0.05)
    # Below MIN_AREA_FRACTION (8x8 = 64 px = 0.00125) -> not detected, confidence 0
    vf = vision.analyze_frame(patch(pr.DYE_BGR, x=100, y=210, size=8), 0, SCENARIO, river)
    assert vf.plume_detected is False and vf.confidence == 0.0
    assert vf.area_fraction < vision.MIN_AREA_FRACTION


# ---- analyze_frames --------------------------------------------------------------------------
def test_analyze_frames_first_last_and_detections():
    t0 = SCENARIO
    frames = [water(), water(), patch(pr.DYE_BGR), patch(blend(0.6)), water()]
    stamps = [t0 + timedelta(seconds=30 * i) for i in range(5)]
    s = vision.analyze_frames(frames, stamps, None, clip_name="synthetic", clip_kind="spill")
    assert isinstance(s, VisionSummary)
    assert s.clip == "synthetic" and s.clip_kind == "spill" and s.frames_analyzed == 5
    assert [f.plume_detected for f in s.frames] == [False, False, True, True, False]
    assert [f.frame_index for f in s.frames] == list(range(5))
    assert [f.timestamp for f in s.frames] == stamps
    assert s.first_plume == stamps[2] and s.last_plume == stamps[3]
    assert [d.frame_index for d in s.plume_detections] == [2, 3]
    for d in s.plume_detections:
        assert d.label == "plume" and d.detector == "hsv"
        assert d.time_s == pytest.approx(d.frame_index / pr.FPS)
        assert d.timestamp == stamps[d.frame_index]
        assert d.confidence == pytest.approx(s.frames[d.frame_index].confidence)
        assert d.area_fraction == pytest.approx(s.frames[d.frame_index].area_fraction)
        assert d.bbox is not None and len(d.bbox) == 4
        assert abs(d.bbox[0] - 300) <= 1 and abs(d.bbox[1] - 200) <= 1
    assert s.debris == [] and s.debris_status == "not run"
    assert s.simulated is True and s.requires_human_review is True


def test_analyze_frames_without_plume():
    frames = [patch(LOOKALIKES["algae_green"]), patch(LOOKALIKES["glare_white"]), water()]
    stamps = [SCENARIO + timedelta(seconds=30 * i) for i in range(3)]
    s = vision.analyze_frames(frames, stamps, band_mask(), clip_name="neg", clip_kind="algae",
                              debris=[], debris_status="skipped: test")
    assert s.first_plume is None and s.last_plume is None and s.plume_detections == []
    assert s.clip_kind == "algae" and s.debris_status == "skipped: test"


def test_analyze_frames_estimates_river_without_mask():
    # A river band of water over grey land; dye inside the band is found without a river_mask.
    f = np.full((H, W, 3), (110, 120, 120), np.uint8)
    f[200:280] = pr.WATER_BGR
    f[220:260, 300:360] = pr.DYE_BGR
    s = vision.analyze_frames([f], [SCENARIO])
    assert s.first_plume == SCENARIO and len(s.plume_detections) == 1


# ---- rendered clips --------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _clip(kind: str) -> pr.Clip:
    return pr.render_clip(kind, scenario_timestamp=SCENARIO, seed=SEED,
                          line_lonlat=pr.load_reach_line(REACH_ID), n_frames=N)


@pytest.fixture(scope="module")
def clips():
    try:
        return {k: _clip(k) for k in ("spill",) + tuple(pr.NEGATIVE_KINDS)}
    except ConfigError:
        pytest.skip("no .env")


def test_watermark_ignored_on_first_spill_frame(clips):
    clip = clips["spill"]
    f0 = clip.frames[0]
    assert vision.plume_mask(f0).sum() == 0  # whole frame, watermark included
    assert vision.analyze_frame(f0, 0, clip.timestamps[0], clip.river_mask).plume_detected is False


def test_spill_clip_detected_after_plume_start(clips):
    clip = clips["spill"]
    s = vision.analyze_frames(clip.frames, clip.timestamps, clip.river_mask, "spill", "spill")
    assert s.frames_analyzed == N
    assert s.first_plume is not None
    assert clip.plume_start <= s.first_plume <= clip.plume_start + timedelta(minutes=10)
    assert s.last_plume == clip.timestamps[-1]
    assert all(not f.plume_detected for f in s.frames if f.timestamp < clip.plume_start)
    assert max(d.confidence for d in s.plume_detections) > 0


@pytest.mark.parametrize("kind", pr.NEGATIVE_KINDS)
def test_negative_clips_have_no_plume(clips, kind):
    clip = clips[kind]
    s = vision.analyze_frames(clip.frames, clip.timestamps, clip.river_mask, kind, kind)
    assert s.plume_detections == [] and s.first_plume is None and s.last_plume is None
    # Also without the river mask (the whole frame, text and land included)
    s = vision.analyze_frames(clip.frames[::10], clip.timestamps[::10], None, kind, kind)
    assert s.plume_detections == []


def test_read_clip_roundtrip(clips, tmp_path):
    clip = clips["spill"]
    path = pr.write_clip(clip, out_dir=tmp_path)
    frames, stamps, meta = vision.read_clip(path)
    assert len(frames) == N and frames[0].shape == (H, W, 3) and frames[0].dtype == np.uint8
    assert stamps == clip.timestamps
    assert meta["simulated"] is True and meta["kind"] == "spill"


# ---- debris cache (load_debris) --------------------------------------------------------------
@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "vision_cache"
    d.mkdir()
    monkeypatch.setattr(vision, "DEBRIS_CACHE_DIR", d)
    return d


def _write_cache(d, name, body):
    (d / f"roboflow_{name}.json").write_text(json.dumps(body), encoding="utf-8")


def test_load_debris_missing_cache(cache_dir):
    dets, status = vision.load_debris("spill")
    assert dets == []
    assert status == "skipped: no cached Roboflow response (run scripts/run_debris_detection.py)"


def test_load_debris_ok(cache_dir):
    pred = {"class": "trash", "confidence": 0.81, "x": 100.0, "y": 120.0, "width": 40.0, "height": 30.0}
    _write_cache(cache_dir, "spill", {
        "model_id": MODEL, "status": "ok", "reason": "", "fetched_at": "2026-10-17T12:00:00Z",
        "responses": [
            {"frame_index": 0, "timestamp": "2026-10-09T19:50:00Z", "predictions": [pred]},
            {"frame_index": 20, "timestamp": "2026-10-09T20:00:00Z",
             "predictions": [dict(pred, confidence=0.4), dict(pred, **{"class": "person"})]},
        ],
    })
    dets, status = vision.load_debris("spill")
    assert status.startswith("ok: 3 predictions on 2 sampled frames") and "2026-10-17T12:00:00Z" in status
    assert len(dets) == 3
    assert [d.label for d in dets] == ["trash", "trash", "person"]
    assert [d.frame_index for d in dets] == [0, 20, 20]
    assert [d.confidence for d in dets] == pytest.approx([0.81, 0.4, 0.81])
    for d in dets:
        assert d.detector == "roboflow" and d.bbox is not None and len(d.bbox) == 4


@pytest.mark.parametrize("status,reason", [("skipped", "ROBOFLOW_API_KEY not set"),
                                            ("failed", "HTTP 503 from Roboflow")])
def test_load_debris_skipped_or_failed(cache_dir, status, reason):
    _write_cache(cache_dir, "glare", {"model_id": MODEL, "status": status, "reason": reason,
                                      "fetched_at": "2026-10-17T12:00:00Z", "responses": []})
    assert vision.load_debris("glare") == ([], f"{status}: {reason}")


# ---- fetch_debris with a fake session (no network) -------------------------------------------
class FakeResponse:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.headers: dict = {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeSession:
    """Records every call; replays the responses (repeating the last). Items may be exceptions."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def _call(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def request(self, method, url, **kw):
        return self._call(method, url, **kw)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(vision, "_sleep", lambda s: None, raising=False)


def fake_settings(key: str | None = FAKE_KEY):
    return SimpleNamespace(roboflow_api_key=SecretStr(key) if key is not None else None,
                           roboflow_model_id=MODEL, roboflow_api_url=API, random_seed=SEED,
                           simulation_mode=True)


def small_clip(n: int = 41):
    frames = [water() for _ in range(n)]
    stamps = [SCENARIO + timedelta(seconds=30 * i) for i in range(n)]
    return frames, stamps


def assert_key_only_in_auth_header(session: FakeSession):
    for c in session.calls:
        assert FAKE_KEY not in c["url"]
        assert FAKE_KEY not in json.dumps(c.get("params") or {}, default=str)
        assert FAKE_KEY not in str(c.get("data", "")) and FAKE_KEY not in str(c.get("json", ""))
        headers = dict(c.get("headers") or {})
        for k, v in headers.items():
            if k.lower() != "authorization":
                assert FAKE_KEY not in str(v)


def test_fetch_debris_ok_writes_cache_without_key(cache_dir, no_sleep):
    preds = {"predictions": [{"class": "trash", "confidence": 0.7, "x": 50, "y": 60, "width": 20,
                              "height": 10, "class_id": 0, "detection_id": "abc"}]}
    session = FakeSession([FakeResponse(200, preds)])
    frames, stamps = small_clip(41)
    path = vision.fetch_debris(frames, stamps, "spill", sample_every=20, settings=fake_settings(),
                               session=session)
    assert path == cache_dir / "roboflow_spill.json" and path.exists()
    assert len(session.calls) == 3
    for c in session.calls:
        assert c["method"] == "POST"
        assert c["url"] == f"{API}/{MODEL}"
        headers = dict(c.get("headers") or {})
        assert headers.get("Authorization") == f"Bearer {FAKE_KEY}"
        assert "x-www-form-urlencoded" in headers.get("Content-Type", "")
        assert c.get("timeout") is not None
    assert_key_only_in_auth_header(session)

    text = path.read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    body = json.loads(text)
    assert body["status"] == "ok" and body["model_id"] == MODEL and body["fetched_at"]
    assert [r["frame_index"] for r in body["responses"]] == [0, 20, 40]
    assert body["responses"][0]["predictions"][0]["class"] == "trash"

    dets, status = vision.load_debris("spill")
    assert len(dets) == 3 and status.startswith("ok: 3 predictions on 3 sampled frames")


def test_fetch_debris_missing_key_is_skipped(cache_dir, no_sleep):
    session = FakeSession([FakeResponse(200, {"predictions": []})])
    frames, stamps = small_clip(5)
    path = vision.fetch_debris(frames, stamps, "glare", settings=fake_settings(None), session=session)
    assert session.calls == []
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["status"] == "skipped" and body["reason"] == "ROBOFLOW_API_KEY not set"
    assert vision.load_debris("glare") == ([], "skipped: ROBOFLOW_API_KEY not set")


@pytest.mark.parametrize("responses", [
    [requests.ConnectionError("boom")],
    [FakeResponse(503, {"error": "unavailable"})],
    [FakeResponse(401, {"error": "unauthorized"})],
], ids=["connection_error", "http_503", "http_401"])
def test_fetch_debris_failure_never_raises_or_leaks_key(cache_dir, no_sleep, responses):
    session = FakeSession(responses)
    frames, stamps = small_clip(21)
    path = vision.fetch_debris(frames, stamps, "shadow", sample_every=20, settings=fake_settings(),
                               session=session)
    assert session.calls  # it tried
    assert_key_only_in_auth_header(session)
    text = path.read_text(encoding="utf-8")
    assert FAKE_KEY not in text
    body = json.loads(text)
    assert body["status"] == "failed" and body["reason"] and FAKE_KEY not in body["reason"]
    dets, status = vision.load_debris("shadow")
    assert dets == [] and status.startswith("failed: ") and FAKE_KEY not in status
