"""Run a demo case end to end offline and print the final IncidentResponse JSON.

    python scripts/run_scenario.py [--case spill|storm] [--out path]

Uses cached local data only. Agents use Groq when GROQ_API_KEY and GROQ_MODEL are set, otherwise
the deterministic fallback; incident memory uses Backboard when BACKBOARD_API_KEY is set. Either
way the exit code is 0. Agent and memory modes go to stderr.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.agents.memory import IncidentMemory  # noqa: E402
from backend.app.agents.orchestrator import run_chain_detailed  # noqa: E402
from backend.app.config import ConfigError, get_settings  # noqa: E402
from backend.app.simulation.scenario import run_spill_scenario, run_storm_scenario  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=("spill", "storm"), default="spill")
    parser.add_argument("--out", type=Path, default=None, help="also write the JSON to this file")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
        scenario = run_spill_scenario() if args.case == "spill" else run_storm_scenario()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    response, reasons = run_chain_detailed(scenario, memory_store=IncidentMemory.from_settings(settings))

    for name, mode in response.agent_modes.items():
        reason = reasons.get(name)
        print(f"{name}: {mode}" + (f" ({reason})" if mode == "fallback" and reason else ""), file=sys.stderr)
    print(f"memory: {response.memory_mode}" + (f" ({response.memory_reason})" if response.memory_reason else "")
          + f"; related past events: {len(response.report.related_past_events)}", file=sys.stderr)
    if response.report.guardrail_notes:
        print(f"guardrail notes: {len(response.report.guardrail_notes)}", file=sys.stderr)

    text = response.model_dump_json(indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
