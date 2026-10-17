"""TRI (Toxics Release Inventory) loader and matching to ECHO facilities.

TRI is annual, self-reported data (2024 here). It is context only and cannot match a live event.
Matching uses the FRS id first; otherwise a nearby TRI site with a similar name is a low-confidence match.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import pandas as pd
from pyproj import Transformer

from ..config import DISTANCE_CRS, SOURCE_CRS, get_settings
from ..schemas import TriRelease
from .echo import cached_frame, clean_str, opt_float

TRI_COLUMNS = [
    "trifd", "frs_id", "name", "latitude", "longitude", "chemical", "year", "water_release", "unit",
]
# Stripped TRI column name -> output column.
_TRI_SOURCE = {
    "TRIFD": "trifd",
    "FRS ID": "frs_id",
    "FACILITY NAME": "name",
    "LATITUDE": "latitude",
    "LONGITUDE": "longitude",
    "CHEMICAL": "chemical",
    "YEAR": "year",
    "5.3 - WATER": "water_release",
    "UNIT OF MEASURE": "unit",
}
# Original column names in the 2024 national file, cited on every TriRelease.
TRI_SOURCE_FIELDS = ["37. CHEMICAL", "1. YEAR", "53. 5.3 - WATER", "50. UNIT OF MEASURE"]

_NAME_STOPWORDS = frozenset(
    "INC LLC CO CORP CORPORATION COMPANY LP LTD THE PLANT FACILITY".split()
)
_PREFIX = re.compile(r"^\d+\.\s+")

MatchKind = Literal["frs_id", "proximity_name"]
MatchConfidence = Literal["high", "low"]


def _strip_prefix(col: str) -> str:
    return _PREFIX.sub("", col.strip())


def _build_tri(src: Path) -> pd.DataFrame:
    raw = pd.read_csv(src, dtype=str, encoding="latin-1", keep_default_na=False)
    raw.columns = [_strip_prefix(c) for c in raw.columns]
    raw = raw[raw["ST"].str.strip() == "VA"]
    df = pd.DataFrame({new: clean_str(raw[old]) for old, new in _TRI_SOURCE.items()})
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce").astype("float64")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce").astype("float64")
    df["year"] = pd.to_numeric(df["year"], errors="raise").astype("int64")
    df["water_release"] = opt_float(df["water_release"])
    return df.reset_index(drop=True)[TRI_COLUMNS]


def load_tri(path: str | Path | None = None, cache_dir: str | Path | None = None) -> pd.DataFrame:
    """Virginia TRI rows: trifd, frs_id, name, latitude, longitude, chemical, year, water_release, unit."""
    if path is None or cache_dir is None:
        s = get_settings()
        path = path if path is not None else s.tri_csv_path
        cache_dir = cache_dir if cache_dir is not None else s.cache_dir
    df = cached_frame(Path(path), Path(cache_dir), "tri_va", _build_tri)
    for c in ("trifd", "frs_id", "name", "chemical", "unit"):
        df[c] = df[c].astype("object").where(df[c].notna(), None)
    df["water_release"] = pd.Series(
        [None if pd.isna(v) else float(v) for v in df["water_release"]], index=df.index, dtype="object"
    )
    df["year"] = df["year"].astype("int64")
    return df


# ---- name similarity -------------------------------------------------------------------------
def _name_tokens(name: str | None) -> set[str]:
    text = (name or "").upper()
    text = re.sub(r"['.]", "", text)  # "INC." -> "INC", "SMITH'S" -> "SMITHS"
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return {t for t in text.split() if t not in _NAME_STOPWORDS}


def name_similarity(a: str, b: str) -> float:
    """Jaccard similarity of normalized name tokens, 0..1 (0 when either name has no tokens)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---- matching --------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _transformer() -> Transformer:
    return Transformer.from_crs(SOURCE_CRS, DISTANCE_CRS, always_xy=True)


def _releases(rows: pd.DataFrame) -> list[TriRelease]:
    out = [
        TriRelease(
            chemical=r.chemical or "",
            year=int(r.year),
            water_release=None if r.water_release is None or pd.isna(r.water_release) else float(r.water_release),
            unit=r.unit or "",
            source_fields=list(TRI_SOURCE_FIELDS),
        )
        for r in rows.itertuples(index=False)
    ]
    out.sort(key=lambda t: (t.water_release is None, -(t.water_release or 0.0), t.chemical, t.year))
    return out


def match_tri(
    candidate_frs_id: str | None,
    candidate_name: str,
    candidate_lat: float,
    candidate_lon: float,
    tri: pd.DataFrame,
    echo_frs_ids: set[str],
    max_distance_m: float = 1000.0,
) -> tuple[list[TriRelease], MatchKind | None, MatchConfidence | None]:
    """TRI releases for one ECHO facility, with how they were matched and the match confidence."""
    if tri is None or tri.empty:
        return [], None, None

    frs = (candidate_frs_id or "").strip()
    if frs:
        rows = tri[tri["frs_id"] == frs]
        if not rows.empty:
            return _releases(rows), "frs_id", "high"

    # Fallback: only TRI sites not already tied to an ECHO facility by FRS id.
    pool = tri[~tri["frs_id"].isin(echo_frs_ids) & tri["latitude"].notna() & tri["longitude"].notna()]
    if pool.empty or candidate_lat is None or candidate_lon is None:
        return [], None, None
    sites = pool.drop_duplicates("trifd")[["trifd", "name", "latitude", "longitude"]]
    tf = _transformer()
    cx, cy = tf.transform(float(candidate_lon), float(candidate_lat))
    xs, ys = tf.transform(sites["longitude"].to_numpy(), sites["latitude"].to_numpy())
    dist = ((xs - cx) ** 2 + (ys - cy) ** 2) ** 0.5

    best: tuple[float, str] | None = None
    for trifd, name, d in zip(sites["trifd"], sites["name"], dist):
        if d <= max_distance_m and name_similarity(candidate_name, name) >= 0.5:
            if best is None or (d, trifd) < best:
                best = (float(d), trifd)
    if best is None:
        return [], None, None
    return _releases(pool[pool["trifd"] == best[1]]), "proximity_name", "low"
