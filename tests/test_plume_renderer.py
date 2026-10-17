"""Tests for backend.app.simulation.plume_renderer, written from the step 6 contract.

Clips are rendered once per module with a reduced frame count. The count still covers the plume
start: LEAD_IN is 10 min = 20 frames at 30 s per frame.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import cv2
import numpy as np
import pytest
from pyproj import Transformer

from backend.app.config import DISTANCE_CRS, PROJECT_ROOT, SOURCE_CRS, ConfigError
from backend.app.simulation import plume_renderer as pr

REACH_ID = 10000500083697
FIXTURE = PROJECT_ROOT / "backend" / "fixtures" / "reach" / f"reach_{REACH_ID}.geojson"
SCENARIO = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
SEED = 7
N = 120  # 1 h of scenario time; the plume starts at frame 20
KINDS = ("spill", "glare", "shadow", "algae")
# Detector range from the contract, repeated here so these tests do not depend on vision.py.
HSV_LOWER = np.array((140, 100, 80), dtype=np.uint8)
HSV_UPPER = np.array((175, 255, 255), dtype=np.uint8)


def dye(frame: np.ndarray) -> np.ndarray:
    return cv2.inRange(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV), HSV_LOWER, HSV_UPPER) > 0


def render(kind: str, seed: int = SEED, n_frames: int = N) -> pr.Clip:
    try:
        return pr.render_clip(kind, scenario_timestamp=SCENARIO, seed=seed,
                              line_lonlat=pr.load_reach_line(REACH_ID), n_frames=n_frames)
    except ConfigError:
        pytest.skip("no .env")


@lru_cache(maxsize=None)
def _cached(kind: str) -> pr.Clip:
    return render(kind)


@pytest.fixture(scope="module")
def clips() -> dict[str, pr.Clip]:
    return {k: _cached(k) for k in KINDS}


# ---- constants -------------------------------------------------------------------------------
def test_shared_constants():
    assert pr.DYE_BGR == (180, 40, 230) and pr.WATER_BGR == (90, 80, 40)
    assert pr.FRAME_SIZE == (640, 480) and pr.FPS == 10 and pr.FRAME_STEP_S == 30
    assert pr.N_FRAMES == 240 and pr.LEAD_IN == timedelta(minutes=10)
    assert tuple(pr.NEGATIVE_KINDS) == ("glare", "shadow", "algae")
    assert N * pr.FRAME_STEP_S > pr.LEAD_IN.total_seconds()


def test_dye_colour_inside_detector_range_and_water_outside():
    assert dye(np.full((4, 4, 3), pr.DYE_BGR, np.uint8)).all()
    blend = np.round(0.6 * np.array(pr.DYE_BGR) + 0.4 * np.array(pr.WATER_BGR)).astype(np.uint8)
    assert dye(np.full((4, 4, 3), blend, np.uint8)).all()
    assert not dye(np.full((4, 4, 3), pr.WATER_BGR, np.uint8)).any()


# ---- reach fixture and projection ------------------------------------------------------------
def test_reach_fixture_is_committed_geojson():
    assert FIXTURE.exists(), FIXTURE
    feat = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert feat["type"] == "Feature" and feat["geometry"]["type"] == "LineString"
    assert int(feat["properties"]["nhdplusid"]) == REACH_ID
    assert "NHDPlus" in feat["properties"]["source"]
    line = pr.load_reach_line(REACH_ID)
    assert len(line) >= 2
    assert [tuple(c[:2]) for c in feat["geometry"]["coordinates"]] == [tuple(c) for c in line]
    for lon, lat in line:
        assert -78.6 < lon < -78.0 and 38.8 < lat < 39.2  # near Strasburg, VA


def _check_projection(lonlat, margin):
    px = pr.project_to_pixels(lonlat, size=pr.FRAME_SIZE, margin=margin)
    w, h = pr.FRAME_SIZE
    assert isinstance(px, np.ndarray) and px.dtype == np.int32 and px.shape == (len(lonlat), 2)
    assert px[:, 0].min() >= margin - 1 and px[:, 0].max() <= w - margin
    assert px[:, 1].min() >= margin - 1 and px[:, 1].max() <= h - margin
    # Uniform scale: pixel distance / metre distance is the same for every pair of points.
    t = Transformer.from_crs(SOURCE_CRS, DISTANCE_CRS, always_xy=True)
    m = np.array([t.transform(lon, lat) for lon, lat in lonlat])
    ratios = []
    for i in range(len(lonlat)):
        for j in range(i + 1, len(lonlat)):
            dm = np.hypot(*(m[i] - m[j]))
            dp = np.hypot(*(px[i] - px[j]).astype(float))
            if dp > 100:
                ratios.append(dp / dm)
    assert ratios and max(ratios) / min(ratios) < 1.03
    # The binding dimension fills the space inside the margin.
    fill = max(np.ptp(px[:, 0]) / (w - 2 * margin), np.ptp(px[:, 1]) / (h - 2 * margin))
    assert fill > 0.9
    return px


@pytest.mark.parametrize("margin", [40, 10])
def test_project_to_pixels_keeps_margin_and_aspect(margin):
    # East-west line: about 4.3 km wide and 1.1 km tall.
    line = [(-78.40, 38.95), (-78.38, 38.96), (-78.37, 38.952), (-78.36, 38.958), (-78.35, 38.955)]
    px = _check_projection(line, margin)
    assert np.ptp(px[:, 0]) > np.ptp(px[:, 1])  # wide stays wide


def test_project_real_reach_line():
    _check_projection(pr.load_reach_line(REACH_ID), 40)


# ---- Clip fields -----------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_clip_fields(clips, kind):
    clip = clips[kind]
    w, h = pr.FRAME_SIZE
    assert clip.kind == kind and clip.seed == SEED
    assert len(clip.frames) == N and len(clip.timestamps) == N
    for f in clip.frames:
        assert f.shape == (h, w, 3) and f.dtype == np.uint8
    start = SCENARIO - pr.LEAD_IN
    assert clip.timestamps[0] == start
    for i, ts in enumerate(clip.timestamps):
        assert ts.utcoffset() == timedelta(0)
        assert ts == start + timedelta(seconds=i * pr.FRAME_STEP_S)
    assert clip.river_mask.shape == (h, w) and clip.river_mask.dtype == bool
    assert 0.01 < clip.river_mask.mean() < 0.5
    assert clip.plume_start == (SCENARIO if kind == "spill" else None)


def test_spill_no_dye_before_plume_start(clips):
    clip = clips["spill"]
    before = [f for f, t in zip(clip.frames, clip.timestamps) if t < clip.plume_start]
    assert len(before) == 20
    assert all(dye(f).sum() == 0 for f in before)


def test_spill_dye_appears_and_grows(clips):
    clip = clips["spill"]
    i0 = clip.timestamps.index(clip.plume_start)
    area = [int(dye(f).sum()) for f in clip.frames]
    assert max(area[i0:i0 + 6]) > 0
    assert area[i0 + 40] > area[i0 + 5] > 0
    assert area[-1] > area[i0 + 5]
    # The tail end of the clip has more dye than the first minutes after release.
    assert np.mean(area[-20:]) > np.mean(area[i0:i0 + 20])


def test_spill_dye_confined_to_river(clips):
    clip = clips["spill"]
    for f in clip.frames:
        d = dye(f)
        outside = int((d & ~clip.river_mask).sum())
        assert outside <= max(5, 0.01 * int(d.sum())), outside


@pytest.mark.parametrize("kind", pr.NEGATIVE_KINDS)
def test_negatives_have_no_dye(clips, kind):
    assert all(dye(f).sum() == 0 for f in clips[kind].frames)


def _river_hsv(clip: pr.Clip) -> list[np.ndarray]:
    return [cv2.cvtColor(f, cv2.COLOR_BGR2HSV)[clip.river_mask] for f in clip.frames]


def test_negative_kinds_show_their_lookalike(clips):
    water_v = max(pr.WATER_BGR)
    glare = sum(int(((p[:, 2] > 200) & (p[:, 1] < 80)).sum()) for p in _river_hsv(clips["glare"]))
    assert glare > 100  # bright, low-saturation patches on the water
    shadow = sum(int((p[:, 2] < 0.7 * water_v).sum()) for p in _river_hsv(clips["shadow"]))
    spill_dark = sum(int((p[:, 2] < 0.7 * water_v).sum()) for p in _river_hsv(clips["spill"]))
    assert shadow > 100 and shadow > 2 * spill_dark  # darkened water
    algae = sum(int(((p[:, 0] >= 15) & (p[:, 0] <= 85) & (p[:, 1] > 60)).sum())
                for p in _river_hsv(clips["algae"]))
    assert algae > 100  # green/brown mats


@pytest.mark.parametrize("kind", KINDS)
def test_watermark_text_is_burned_in_and_not_dye(clips, kind):
    f = clips[kind].frames[0]
    hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
    bright_grey = (hsv[..., 2] > 180) & (hsv[..., 1] < 60)
    assert bright_grey.sum() > 300
    assert dye(f).sum() == 0


def test_frames_differ_between_kinds_and_seeds(clips):
    assert not np.array_equal(clips["spill"].frames[0], clips["glare"].frames[-1])
    other = render("spill", seed=SEED + 1, n_frames=24)
    assert any(not np.array_equal(a, b) for a, b in zip(other.frames, clips["spill"].frames))


# ---- determinism -----------------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_rendering_is_deterministic(kind):
    a = render(kind, n_frames=30)
    b = render(kind, n_frames=30)
    assert len(a.frames) == len(b.frames) == 30
    assert all(x.tobytes() == y.tobytes() for x, y in zip(a.frames, b.frames))
    assert a.timestamps == b.timestamps and np.array_equal(a.river_mask, b.river_mask)


# ---- write_clip ------------------------------------------------------------------------------
def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_write_clip_mp4_and_sidecar(clips, tmp_path):
    clip = clips["spill"]
    path = pr.write_clip(clip, out_dir=tmp_path)
    assert path.parent == tmp_path and path.suffix == ".mp4" and path.exists()
    assert re.fullmatch(rf"spill_\d{{8}}T\d{{4}}Z_seed{SEED}\.mp4", path.name), path.name
    cap = cv2.VideoCapture(str(path))
    try:
        assert cap.isOpened()
        assert cap.get(cv2.CAP_PROP_FPS) == pytest.approx(pr.FPS, abs=0.5)
        count = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            assert frame.shape == clip.frames[0].shape
            count += 1
    finally:
        cap.release()
    assert count == N

    side = path.with_suffix(".json")
    assert side.exists()
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta["simulated"] is True
    assert meta["kind"] == "spill" and meta["fps"] == pr.FPS and meta["frame_step_s"] == pr.FRAME_STEP_S
    assert meta["n_frames"] == N and meta["seed"] == SEED
    assert "SIMULATED" in meta["note"] and "not real" in meta["note"]
    assert _parse(meta["plume_start"]) == SCENARIO
    # An explicit line_lonlat was passed, so the reach id may be unknown (null) here.
    reach_keys = [k for k in meta if "reach" in k]
    assert reach_keys and meta[reach_keys[0]] in (None, REACH_ID, str(REACH_ID))
    stamps = [v for k, v in meta.items() if isinstance(v, str) and k != "plume_start" and re.match(r"\d{4}-\d\d-\d\dT", v)]
    parsed = {_parse(s) for s in stamps}
    assert clip.timestamps[0] in parsed and clip.timestamps[-1] in parsed


def test_write_negative_clip_has_no_plume_start(clips, tmp_path):
    path = pr.write_clip(clips["algae"], out_dir=tmp_path)
    assert path.name.startswith("algae_")
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["plume_start"] is None and meta["simulated"] is True and meta["kind"] == "algae"
