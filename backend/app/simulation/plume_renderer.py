"""Synthetic top-down camera clips of the demo reach: a dye plume (spill) and three look-alikes.

The river is drawn along the real NHDPlus HR flowline of the demo reach (committed as a small
GeoJSON fixture), projected to DISTANCE_CRS and scaled into the frame. Everything else (land,
water texture, dye, glare, cloud shadow, algae) is synthetic and seeded. Every frame carries a
"SIMULATED" watermark and a UTC timestamp; nothing here is real imagery.

Orientation: NHDPlus HR flowlines are digitized upstream -> downstream. For the demo reach this was
checked against the flow table: its first vertex is the last vertex of both upstream neighbours and
its last vertex is the first vertex of the downstream neighbour (10000500066141).
`export_reach_geometry` re-checks this and reverses the line if it is ever digitized the other way,
so the fixture is always ordered upstream -> downstream and the plume follows coordinate order.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from ..config import DISTANCE_CRS, FEET_TO_METERS, PROJECT_ROOT, SOURCE_CRS, get_settings

DYE_BGR = (180, 40, 230)  # rhodamine-like magenta dye; OpenCV HSV ~ (158, 211, 230)
WATER_BGR = (90, 80, 40)  # dark teal river water
FRAME_SIZE = (640, 480)  # width, height
FPS = 10
FRAME_STEP_S = 30  # scenario seconds per frame
N_FRAMES = 240  # 24 s of video = 2 h of scenario time
LEAD_IN = timedelta(minutes=10)  # clip starts 10 min before the scenario timestamp
NEGATIVE_KINDS = ("glare", "shadow", "algae")
KINDS = ("spill",) + NEGATIVE_KINDS

REACH_FIXTURE_DIR = PROJECT_ROOT / "backend" / "fixtures" / "reach"
DEFAULT_REACH_ID = 10000500083697
REACH_FIXTURE = REACH_FIXTURE_DIR / f"reach_{DEFAULT_REACH_ID}.geojson"
GEOMETRY_SOURCE = "NHDPlus HR EPA snapshot 2022"
# EROM mean annual velocity of the demo reach (va, read as ft/s); used if the fixture lacks it.
DEFAULT_VELOCITY_FPS = 0.96159656
SIDECAR_NOTE = (
    "SIMULATED footage rendered on a synthetic river drawn along the NHDPlus flowline; not real imagery"
)

RIVER_WIDTH_M = 40.0  # drawn channel width, matching the scale bar (about 11 px at ~3.5 m/px)
BANK_PX = 4.0  # sand/mud band outside the water
SOURCE_FRACTION = 0.06  # plume source position along the drawn reach
DYE_MAX_ALPHA = 0.78
LONG_DISPERSION_M2S = 2.0  # longitudinal dispersion coefficient
LATERAL_MIX_M = 250.0  # e-folding distance for the plume to spread across the channel

_KIND_SALT = {k: i + 1 for i, k in enumerate(KINDS)}


def _fixture_path(nhdplusid: int, out_dir: Path | None = None) -> Path:
    return Path(out_dir or REACH_FIXTURE_DIR) / f"reach_{int(nhdplusid)}.geojson"


def _line_coords(geom) -> list[tuple[float, float]]:
    if geom.geom_type == "MultiLineString":
        coords = [c for part in geom.geoms for c in part.coords]
    else:
        coords = list(geom.coords)
    return [(float(c[0]), float(c[1])) for c in coords]


def export_reach_geometry(nhdplusid: int, gpkg_path=None, out_dir=None) -> Path:
    """Write the reach flowline (upstream -> downstream) as a small GeoJSON Feature fixture."""
    from ..data.nhd_graph import load_graph, reach_geometries

    settings = get_settings()
    gpkg = gpkg_path or settings.nhdplus_gpkg_path
    nid = int(nhdplusid)
    props: dict = {"nhdplusid": nid}
    downstream: list[int] = []
    try:
        G = load_graph(gpkg_path=gpkg, cache_dir=settings.cache_dir)
        if nid in G:
            node = G.nodes[nid]
            if node.get("gnis_name"):
                props["gnis_name"] = node["gnis_name"]
            props["length_km"] = node.get("length_km")
            props["velocity_fps"] = node.get("velocity_fps")
            downstream = [int(n) for n in G.successors(nid)]
    except Exception:  # graph unavailable: export the geometry without the extra attributes
        pass

    gdf = reach_geometries(gpkg, [nid] + downstream)
    rows = {int(r.nhdplusid): r.geometry for r in gdf.itertuples()}
    if nid not in rows:
        raise KeyError(f"reach {nid} not found in {gpkg}")
    coords = _line_coords(rows[nid])

    orientation = "as digitized (upstream -> downstream assumed; no downstream neighbour to check)"
    if downstream and downstream[0] in rows:
        start = np.array(_line_coords(rows[downstream[0]])[0])
        d_first = float(np.hypot(*(np.array(coords[0]) - start)))
        d_last = float(np.hypot(*(np.array(coords[-1]) - start)))
        if d_first < d_last:
            coords = coords[::-1]
            orientation = f"reversed so the line ends at the start of downstream reach {downstream[0]}"
        else:
            orientation = f"as digitized; last vertex meets downstream reach {downstream[0]}"
    props["orientation"] = "upstream -> downstream: " + orientation
    props["crs"] = SOURCE_CRS
    props["source"] = GEOMETRY_SOURCE

    feature = {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "LineString", "coordinates": [[round(x, 8), round(y, 8)] for x, y in coords]},
    }
    path = _fixture_path(nid, out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(feature, indent=1) + "\n", encoding="utf-8")
    return path


def _load_feature(nhdplusid: int | None = None) -> dict:
    nid = int(nhdplusid) if nhdplusid is not None else int(get_settings().reach_nhdplusid)
    path = _fixture_path(nid)
    if not path.exists():
        raise FileNotFoundError(f"reach fixture missing: {path} (run export_reach_geometry({nid}))")
    return json.loads(path.read_text(encoding="utf-8"))


def load_reach_line(nhdplusid: int | None = None) -> list[tuple[float, float]]:
    """Lon/lat coordinates (upstream -> downstream) of the reach from the committed fixture."""
    return [(float(x), float(y)) for x, y in _load_feature(nhdplusid)["geometry"]["coordinates"]]


def _project_float(lonlat, size=FRAME_SIZE, margin: int = 40) -> tuple[np.ndarray, float]:
    """Float pixel coords (north up) and metres per pixel."""
    from pyproj import Transformer

    tr = Transformer.from_crs(SOURCE_CRS, DISTANCE_CRS, always_xy=True)
    lon, lat = np.asarray(lonlat, dtype=float).T
    x, y = tr.transform(lon, lat)
    x, y = np.asarray(x), np.asarray(y)
    w, h = size
    span_x = max(float(x.max() - x.min()), 1e-6)
    span_y = max(float(y.max() - y.min()), 1e-6)
    px_per_m = min((w - 2 * margin) / span_x, (h - 2 * margin) / span_y)
    cx, cy = (x.max() + x.min()) / 2, (y.max() + y.min()) / 2
    px = w / 2 + (x - cx) * px_per_m
    py = h / 2 - (y - cy) * px_per_m
    return np.column_stack([px, py]), 1.0 / px_per_m


def project_to_pixels(lonlat: list[tuple[float, float]], size=FRAME_SIZE, margin=40) -> np.ndarray:
    """Project to DISTANCE_CRS and scale uniformly into the frame; int32 Nx2 pixel coords."""
    pts, _ = _project_float(lonlat, size, margin)
    return np.round(pts).astype(np.int32)


@dataclass(frozen=True)
class Clip:
    kind: str  # "spill" or one of NEGATIVE_KINDS
    frames: list[np.ndarray]  # BGR uint8 (H, W, 3)
    timestamps: list[datetime]  # tz-aware UTC per frame
    river_mask: np.ndarray  # bool (H, W)
    seed: int
    plume_start: datetime | None  # scenario timestamp for spill, None for negatives
    reach_nhdplusid: int | None = None
    meters_per_pixel: float | None = None


# ---------------------------------------------------------------- scene geometry


@dataclass
class _Scene:
    mpp: float
    half_width: float  # river half width in px
    length_px: float
    river: np.ndarray  # bool mask
    ry: np.ndarray  # river pixel rows
    rx: np.ndarray  # river pixel cols
    s: np.ndarray  # along-river distance (px) per river pixel
    n: np.ndarray  # signed lateral offset (px) per river pixel
    background: np.ndarray  # float32 (H, W, 3) land + banks + still water


def _noise(rng: np.random.Generator, shape: tuple[int, int], cells: tuple[int, int]) -> np.ndarray:
    """Smooth value noise in [0, 1] of the given (h, w) shape from a coarse random grid."""
    coarse = rng.random(cells).astype(np.float32)
    return cv2.resize(coarse, (shape[1], shape[0]), interpolation=cv2.INTER_CUBIC).clip(0, 1)


def _fbm(rng: np.random.Generator, shape: tuple[int, int], base: int = 4, octaves: int = 4) -> np.ndarray:
    h, w = shape
    out = np.zeros(shape, np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        k = base * 2**o
        out += amp * _noise(rng, shape, (max(2, k * h // w), k))
        total += amp
        amp *= 0.5
    return out / total


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _build_scene(line_lonlat, seed: int) -> _Scene:
    w, h = FRAME_SIZE
    pts, mpp = _project_float(line_lonlat)
    hw = 0.5 * RIVER_WIDTH_M / mpp
    seg = np.diff(pts, axis=0)
    seg_len = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    length = float(cum[-1])
    s_dense = np.arange(0.0, length, 0.5)
    dense = np.column_stack([np.interp(s_dense, cum, pts[:, 0]), np.interp(s_dense, cum, pts[:, 1])])
    tang = np.gradient(dense, axis=0)
    tang /= np.maximum(np.hypot(tang[:, 0], tang[:, 1]), 1e-9)[:, None]

    # Nearest centreline sample for every pixel via a labelled distance transform.
    ix = np.clip(np.round(dense[:, 0]).astype(int), 0, w - 1)
    iy = np.clip(np.round(dense[:, 1]).astype(int), 0, h - 1)
    src = np.full((h, w), 255, np.uint8)
    src[iy, ix] = 0
    _, labels = cv2.distanceTransformWithLabels(src, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    lab2idx = np.zeros(int(labels.max()) + 1, np.int64)
    lab2idx[labels[iy, ix]] = np.arange(len(dense))
    idx = lab2idx[labels]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx = xx - dense[idx, 0]
    dy = yy - dense[idx, 1]
    n_all = (dx * -tang[idx, 1] + dy * tang[idx, 0]).astype(np.float32)
    dist = np.hypot(dx, dy).astype(np.float32)

    rng = np.random.default_rng([int(seed), 0])
    # Land: rotated agricultural parcels with fbm texture, darker riparian trees near the banks.
    parcels = rng.integers(0, 5, (7, 9)).astype(np.float32)
    parcels = cv2.resize(parcels, (w * 2, h * 2), interpolation=cv2.INTER_NEAREST)
    rot = cv2.getRotationMatrix2D((w, h), float(rng.uniform(-35, 35)), 1.0)
    parcels = cv2.warpAffine(parcels, rot, (w * 2, h * 2), flags=cv2.INTER_NEAREST)[h // 2 : h // 2 + h, w // 2 : w // 2 + w]
    palette = np.array(
        [[52, 98, 70], [60, 120, 92], [70, 128, 128], [58, 92, 104], [48, 110, 84]], np.float32
    )  # BGR greens, pale crop, tilled brown
    land = palette[parcels.astype(int)]
    land = cv2.GaussianBlur(land, (0, 0), 1.2)
    tex = _fbm(rng, (h, w), base=6, octaves=4)
    grain = rng.random((h, w)).astype(np.float32)
    land *= (0.78 + 0.4 * tex + 0.08 * (grain - 0.5))[..., None]
    # Parcel edges as faint field boundaries.
    edges = cv2.Laplacian(parcels, cv2.CV_32F) != 0
    land[edges] *= 0.82

    trees = _fbm(rng, (h, w), base=16, octaves=3)
    tree_zone = 1.0 - _smoothstep(hw + BANK_PX + 4, hw + BANK_PX + 26, dist)
    tree_a = _smoothstep(0.45, 0.6, trees * 0.6 + tree_zone * 0.55)
    canopy = np.array([34, 70, 40], np.float32) * (0.7 + 0.6 * trees)[..., None]
    land = land * (1 - tree_a[..., None]) + canopy * tree_a[..., None]

    bank = np.array([110, 140, 150], np.float32) * (0.85 + 0.3 * tex)[..., None]  # sand/mud
    water = np.array(WATER_BGR, np.float32)
    shallow = np.array([96, 96, 62], np.float32)
    wet = _smoothstep(hw + 1.0, hw - 0.5, dist)  # 1 in the channel
    bank_a = _smoothstep(hw + BANK_PX + 2.0, hw + BANK_PX - 1.0, dist)
    edge = _smoothstep(hw * 0.6, hw, dist)[..., None]
    water_img = water * (1 - 0.5 * edge) + shallow * (0.5 * edge)
    water_img = water_img * (0.94 + 0.12 * tex[..., None])
    img = land * (1 - bank_a[..., None]) + bank * bank_a[..., None]
    img = img * (1 - wet[..., None]) + water_img * wet[..., None]

    river = dist <= hw
    ry, rx = np.nonzero(river)
    return _Scene(
        mpp=mpp,
        half_width=hw,
        length_px=length,
        river=river,
        ry=ry,
        rx=rx,
        s=s_dense[idx[ry, rx]].astype(np.float32),
        n=n_all[ry, rx],
        background=np.clip(img, 0, 255).astype(np.float32),
    )


# ---------------------------------------------------------------- overlays


_OVERLAY_CACHE: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}


def _static_overlay(mpp: float, header: str) -> tuple[np.ndarray, np.ndarray]:
    """(alpha (H,W), colour (H,W,3)) for the watermark, header, scale bar and footer backing."""
    key = (round(mpp, 6), header)
    if key in _OVERLAY_CACHE:
        return _OVERLAY_CACHE[key]
    w, h = FRAME_SIZE
    color = np.zeros((h, w, 3), np.float32)

    # Large diagonal watermark, white at ~30%.
    wm = np.zeros((h, w), np.uint8)
    text, font, scale, thick = "SIMULATED", cv2.FONT_HERSHEY_DUPLEX, 2.9, 7
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    cv2.putText(wm, text, ((w - tw) // 2, (h + th) // 2), font, scale, 255, thick, cv2.LINE_AA)
    wm = cv2.warpAffine(wm, cv2.getRotationMatrix2D((w / 2, h / 2), 18, 1.0), (w, h))
    alpha = wm.astype(np.float32) / 255 * 0.30
    color[alpha > 0] = 255

    # Footer and header backing bars (black, 55%).
    for y0, y1 in ((h - 30, h), (0, 24)):
        alpha[y0:y1] = 0.55
        color[y0:y1] = 0
    hdr = np.zeros((h, w), np.uint8)
    cv2.putText(hdr, header, (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42, 255, 1, cv2.LINE_AA)
    # Scale bar: 100 m, bottom right above the footer.
    bar_px = int(round(100.0 / mpp))
    x1, y = w - 16, h - 42
    cv2.rectangle(hdr, (x1 - bar_px, y), (x1, y + 3), 255, -1)
    cv2.putText(hdr, "100 m", (x1 - bar_px, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, 255, 1, cv2.LINE_AA)
    a = hdr.astype(np.float32) / 255
    sel = a > 0
    color[sel] = color[sel] * (1 - a[sel, None]) + np.float32(225) * a[sel, None]
    alpha[sel] = np.maximum(alpha[sel], a[sel] * 0.95)
    _OVERLAY_CACHE[key] = (alpha, color)
    return alpha, color


def _footer(frame: np.ndarray, ts: datetime) -> None:
    text = f"{ts.strftime('%Y-%m-%d %H:%M:%S')}Z   |   SIMULATED - not real footage"
    cv2.putText(frame, text, (10, FRAME_SIZE[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (235, 235, 235), 1, cv2.LINE_AA)


# ---------------------------------------------------------------- per-kind effects


def _dye_alpha(sc: _Scene, tex: np.ndarray, tau_s: float, u_px: float, s0: float, n0: float) -> np.ndarray:
    """Dye opacity per river pixel for a continuous release that started tau_s seconds ago."""
    if tau_s <= 0:
        return np.zeros_like(sc.s)
    d = sc.s - s0
    front = u_px * tau_s
    sigma_l = math.sqrt(2 * LONG_DISPERSION_M2S * tau_s) / sc.mpp + 3.0
    along = 0.5 * (1 - np.tanh((d - front) / sigma_l)) * _smoothstep(-6.0, 2.0, d)
    mix = 1 - np.exp(-np.maximum(d, 0) * sc.mpp / LATERAL_MIX_M)
    centre = n0 * (1 - mix)
    sig_n = 1.5 + max(sc.half_width * 0.85 - 1.5, 0.0) * mix
    lateral = np.exp(-((sc.n - centre) ** 2) / (2 * sig_n**2))
    amp = 1 - 0.3 * mix
    ramp = min(1.0, tau_s / 120.0)  # the release builds up over the first two minutes
    return np.clip(DYE_MAX_ALPHA * ramp * amp * along * lateral * (0.72 + 0.5 * tex), 0, DYE_MAX_ALPHA)


def _advected_tex(field: np.ndarray, sc: _Scene, shift_px: float, n_scale: float = 1.0) -> np.ndarray:
    """Sample an (S, N) noise field at river coords moved downstream by shift_px."""
    S, N = field.shape
    si = np.mod((sc.s - shift_px).astype(np.int64), S)
    ni = np.clip((sc.n * n_scale + N / 2).astype(np.int64), 0, N - 1)
    return field[si, ni]


def render_clip(
    kind: str = "spill",
    scenario_timestamp: datetime | None = None,
    seed: int | None = None,
    line_lonlat=None,
    n_frames: int = N_FRAMES,
) -> Clip:
    """Render one seeded, deterministic simulated clip ("spill" or a look-alike)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    settings = get_settings()
    ts0 = scenario_timestamp or settings.scenario_timestamp
    ts0 = ts0.replace(tzinfo=timezone.utc) if ts0.tzinfo is None else ts0.astimezone(timezone.utc)
    seed = int(settings.random_seed if seed is None else seed)

    reach_id: int | None = None
    velocity_fps = DEFAULT_VELOCITY_FPS
    header = "Synthetic river along an NHDPlus flowline"
    if line_lonlat is None:
        feat = _load_feature()
        props = feat["properties"]
        line_lonlat = [tuple(c) for c in feat["geometry"]["coordinates"]]
        reach_id = int(props["nhdplusid"])
        velocity_fps = float(props.get("velocity_fps") or DEFAULT_VELOCITY_FPS)
        header = f"Synthetic river along NHDPlus {reach_id}" + (
            f" ({props['gnis_name']})" if props.get("gnis_name") else ""
        )

    sc = _build_scene(line_lonlat, seed)
    w, h = FRAME_SIZE
    overlay_a, overlay_c = _static_overlay(sc.mpp, header)
    keep3 = np.repeat((1 - overlay_a)[..., None], 3, axis=2)
    add3 = overlay_c * overlay_a[..., None]
    base_u8 = np.clip(sc.background * keep3 + add3, 0, 255).astype(np.uint8)
    rf = sc.ry * w + sc.rx  # flat river pixel indices
    r_keep = keep3.reshape(-1, 3)[rf]
    r_add = add3.reshape(-1, 3)[rf]

    rng = np.random.default_rng([seed, _KIND_SALT[kind]])
    u_px = velocity_fps * FEET_TO_METERS / sc.mpp  # px per scenario second
    clip_start = ts0 - LEAD_IN
    timestamps = [clip_start + timedelta(seconds=i * FRAME_STEP_S) for i in range(n_frames)]

    river_bg = sc.background.reshape(-1, 3)[rf]
    # Textures in (along, across) river coords, stretched in the flow direction.
    S = int(sc.length_px) + 64
    flow_tex = cv2.resize(rng.random((max(4, S // 40), 6)).astype(np.float32), (64, S), interpolation=cv2.INTER_CUBIC)
    flow_tex = (flow_tex - flow_tex.min()) / max(float(np.ptp(flow_tex)), 1e-6)
    ripple_tex = cv2.resize(rng.random((max(4, S // 5), 20)).astype(np.float32), (64, S), interpolation=cv2.INTER_CUBIC)
    ripple_tex = (ripple_tex - 0.5).clip(-0.6, 0.6)
    rp = rng.uniform(0, 2 * np.pi, len(rf)).astype(np.float32) * 0.15 + 0.07 * sc.n

    dye = np.array(DYE_BGR, np.float32)
    s0 = SOURCE_FRACTION * sc.length_px
    n0 = -0.55 * sc.half_width  # outfall on one bank

    if kind == "glare":
        # Glare spots sit on the channel (river coords) and drift slowly downstream.
        n_spots = 5
        g_s = rng.uniform(0.05, 0.9, n_spots) * sc.length_px
        g_n = rng.uniform(-0.5, 0.5, n_spots) * sc.half_width
        g_rad = rng.uniform(14, 30, n_spots)
        glare_c = np.array([215, 242, 252], np.float32)  # pale yellow-white, low saturation
    elif kind == "shadow":
        cloud = _fbm(rng, (h * 2, w * 3), base=9, octaves=3)
        cloud = (cloud - cloud.min()) / max(float(np.ptp(cloud)), 1e-6)
        cloud = _smoothstep(0.52, 0.66, cloud)
        c_dir = rng.uniform(1.6, 2.4)  # px per frame, mostly along +x
        c_off = rng.uniform(0, h)
    elif kind == "algae":
        n_mats = 9
        m_s = rng.uniform(-0.1, 0.8, n_mats) * sc.length_px
        m_n = rng.uniform(-0.6, 0.6, n_mats) * sc.half_width
        m_rs = rng.uniform(10, 26, n_mats)
        m_rn = rng.uniform(0.3, 0.6, n_mats) * sc.half_width
        m_col = np.array([[52, 128, 92], [40, 104, 110], [46, 118, 100]], np.float32)  # green / olive-brown
        m_pick = rng.integers(0, len(m_col), n_mats)
        algae_tex = cv2.resize(rng.random((max(4, S // 8), 12)).astype(np.float32), (64, S), interpolation=cv2.INTER_CUBIC)

    frames: list[np.ndarray] = []
    for i, ts in enumerate(timestamps):
        t_s = i * FRAME_STEP_S
        # Flow-aligned streaks drifting downstream plus a faint shimmer.
        ripple = 16.0 * _advected_tex(ripple_tex, sc, u_px * t_s, n_scale=1.6) + 2.5 * np.sin(
            0.25 * sc.s - 0.8 * i + rp
        )
        water = river_bg + ripple[:, None]

        if kind == "spill":
            tau = (ts - ts0).total_seconds()
            if tau > 0:
                tex = _advected_tex(flow_tex, sc, u_px * tau * 0.8, n_scale=2.0)
                a = _dye_alpha(sc, tex, tau, u_px, s0, n0)[:, None]
                water = water * (1 - a) + dye * a
        elif kind == "glare":
            inten = np.zeros_like(sc.s)
            for k in range(n_spots):
                cs = np.mod(g_s[k] + 0.5 * u_px * t_s, sc.length_px)
                r2 = ((sc.s - cs) / g_rad[k]) ** 2 + ((sc.n - g_n[k]) / max(sc.half_width, 1.0)) ** 2
                inten += np.exp(-r2 / 2).astype(np.float32)
            sparkle = rng.random(sc.s.shape).astype(np.float32)
            a = np.clip(inten * (0.25 + 0.75 * (sparkle > 0.55) * sparkle), 0, 0.85)[:, None]
            water = water * (1 - a) + glare_c * a
        elif kind == "algae":
            a = np.zeros_like(sc.s)
            col = np.zeros((len(sc.s), 3), np.float32)
            tex = _advected_tex(algae_tex, sc, u_px * t_s * 0.95, n_scale=2.0)
            for k in range(n_mats):
                cs = m_s[k] + u_px * t_s * 0.95
                e = ((sc.s - cs) / m_rs[k]) ** 2 + ((sc.n - m_n[k]) / m_rn[k]) ** 2 + 0.9 * (tex - 0.5)
                ak = (1 - _smoothstep(0.6, 1.0, e)) * 0.85
                upd = ak > a
                a = np.where(upd, ak, a)
                col[upd] = m_col[m_pick[k]] * (0.8 + 0.4 * tex[upd, None])
            water = water * (1 - a[:, None]) + col * a[:, None]

        if kind == "shadow":
            frame = sc.background.copy()
            frame.reshape(-1, 3)[rf] = water
            x0 = int(i * c_dir) % (w * 2)
            y0 = int(c_off + i * 0.4) % h
            shade = np.ascontiguousarray(1 - 0.58 * cloud[y0 : y0 + h, x0 : x0 + w])
            frame = cv2.multiply(frame, cv2.merge([shade, shade, shade]))
            out = cv2.convertScaleAbs(cv2.add(cv2.multiply(frame, keep3), add3))
        else:
            out = base_u8.copy()
            out.reshape(-1, 3)[rf] = np.clip(water * r_keep + r_add, 0, 255).astype(np.uint8)
        _footer(out, ts)
        frames.append(out)

    return Clip(
        kind=kind,
        frames=frames,
        timestamps=timestamps,
        river_mask=sc.river.copy(),
        seed=seed,
        plume_start=ts0 if kind == "spill" else None,
        reach_nhdplusid=reach_id,
        meters_per_pixel=sc.mpp,
    )


def _iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_clip(clip: Clip, out_dir=None) -> Path:
    """Write the clip as MP4 (mp4v) plus a JSON sidecar with the same stem; returns the MP4 path."""
    out = Path(out_dir) if out_dir is not None else get_settings().simulated_footage_dir
    out.mkdir(parents=True, exist_ok=True)
    scenario_ts = clip.timestamps[0] + LEAD_IN
    stem = f"{clip.kind}_{scenario_ts.strftime('%Y%m%dT%H%MZ')}_seed{clip.seed}"
    mp4 = out / f"{stem}.mp4"
    w, h = FRAME_SIZE
    writer = cv2.VideoWriter(str(mp4), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"could not open video writer for {mp4}")
    try:
        for f in clip.frames:
            writer.write(f)
    finally:
        writer.release()
    sidecar = {
        "kind": clip.kind,
        "fps": FPS,
        "frame_step_s": FRAME_STEP_S,
        "n_frames": len(clip.frames),
        "frame_size": list(FRAME_SIZE),
        "first_timestamp": _iso(clip.timestamps[0]),
        "last_timestamp": _iso(clip.timestamps[-1]),
        "scenario_timestamp": _iso(scenario_ts),
        "plume_start": _iso(clip.plume_start),
        "seed": clip.seed,
        "reach_nhdplusid": clip.reach_nhdplusid,
        "meters_per_pixel": clip.meters_per_pixel,
        "simulated": True,
        "note": SIDECAR_NOTE,
    }
    mp4.with_suffix(".json").write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    return mp4


def render_all(scenario_timestamp=None, seed=None, out_dir=None) -> dict[str, Path]:
    """Render and write the spill clip and the three look-alike clips; kind -> MP4 path."""
    return {k: write_clip(render_clip(k, scenario_timestamp, seed), out_dir) for k in KINDS}
