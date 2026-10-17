"""Send sampled frames of the SIMULATED clips to the hosted Roboflow model and cache the responses.

LIVE NETWORK. Run once to create backend/fixtures/vision/roboflow_<kind>.json; the pipeline only reads those.
The model detects trash and people, not plumes: results are supporting context only.

Usage:
  python scripts/run_debris_detection.py [--kinds spill glare shadow algae] [--sample-every 20] [--from-footage]
    --from-footage  read the rendered MP4s from SIMULATED_FOOTAGE_DIR instead of rendering in memory
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import ConfigError, load_settings  # noqa: E402
from backend.app.detection.vision import fetch_debris, load_debris, read_clip  # noqa: E402
from backend.app.simulation.plume_renderer import NEGATIVE_KINDS, render_clip  # noqa: E402


def _frames(kind: str, settings, from_footage: bool):
    if from_footage:
        matches = sorted(Path(settings.simulated_footage_dir).glob(f"{kind}_*_seed{settings.random_seed}.mp4"))
        if not matches:
            raise FileNotFoundError(f"no rendered {kind} clip in {settings.simulated_footage_dir}")
        frames, timestamps, _ = read_clip(matches[-1])
        return frames, timestamps
    clip = render_clip(kind, scenario_timestamp=settings.scenario_timestamp, seed=settings.random_seed)
    return clip.frames, clip.timestamps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kinds", nargs="+", default=["spill", *NEGATIVE_KINDS])
    ap.add_argument("--sample-every", type=int, default=20)
    ap.add_argument("--from-footage", action="store_true")
    args = ap.parse_args()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Roboflow model: {settings.roboflow_model_id} (key {'set' if settings.roboflow_api_key else 'not set'})")
    print("SIMULATED clips; the model detects trash/people, not plumes (supporting context only).")
    for kind in args.kinds:
        frames, timestamps = _frames(kind, settings, args.from_footage)
        path = fetch_debris(frames, timestamps, kind, sample_every=args.sample_every, settings=settings)
        dets, status = load_debris(kind)
        classes = sorted({d.label for d in dets})
        print(f"  {kind}: {status}; classes: {classes or 'none'} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
