"""pqbench: benchmark llm-d flow control on a prefill/decode pool over HTTP.

Load generation (needs HTTP access to the router, its /metrics and Prometheus):
  pqbench sweep        closed-loop concurrency sweep, for calibration
  pqbench run          one scenario against the router variant deployed now

Analysis (runs anywhere, on a copied results tree; needs pqbench[analysis]):
  pqbench calibrate    sweep -> calibration.json (pool capacity per shape)
  pqbench report       tables, summary.md and figures for every scenario

  pqbench scenarios    list the bundled scenarios

pqbench never touches Kubernetes: switching router variants is a separate
`helm upgrade` (see README). Endpoint URLs default to the local ports the
README's `kubectl port-forward` commands open; override them with flags or PQ_*
variables.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml

from . import SCENARIOS, __version__, loadgen

# Results root for the load-generation commands (default ./results).
OUT = Path(os.environ.get("PQ_OUT", "results"))
# Endpoints field, environment variable, default, help.
ENDPOINTS = (
    ("router", "PQ_ROUTER", "http://localhost:8080", "llm-d router base URL"),
    ("epp_metrics", "PQ_EPP_METRICS", "http://localhost:9090/metrics", "EPP /metrics URL"),
    ("prometheus", "PQ_PROMETHEUS", "http://localhost:9091", "Prometheus that scrapes the vLLM pods"),
)


def scenario_names() -> list[str]:
    return sorted(p.stem for p in SCENARIOS.glob("*.yaml"))


def resolve_scenario(name_or_path: str) -> Path:
    """A path to a scenario YAML, or a bundled scenario name (e.g. priority-tiers)."""
    for p in (Path(name_or_path), SCENARIOS / f"{name_or_path}.yaml"):
        if p.is_file():
            return p
    sys.exit(f"no scenario file or bundled scenario {name_or_path!r}; bundled: {', '.join(scenario_names())}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pqbench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--version", action="version", version=f"pqbench {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    load = argparse.ArgumentParser(add_help=False)
    g = load.add_argument_group("endpoints (flag > PQ_* env var > default)")
    for field, var, default, help in ENDPOINTS:
        g.add_argument(
            f"--{field.replace('_', '-')}",
            default=os.environ.get(var, default),
            help=f"{help} [{var}] (default: %(default)s)",
        )
    load.add_argument(
        "--model",
        default=os.environ.get("PQ_MODEL", "Qwen/Qwen3.5-9B"),
        help="served model name [PQ_MODEL] (default: %(default)s)",
    )
    load.add_argument(
        "--seed",
        type=int,
        default=0,
        help="seed for arrivals and prompts; variants share it, so they replay the same arrivals",
    )

    s = sub.add_parser("sweep", parents=[load], help="closed-loop calibration sweep")
    s.add_argument("--out", type=Path, default=OUT / "calibration", help="output dir (default: %(default)s)")
    s.add_argument("--hold-s", type=float, default=60, help="measured seconds per level (decode-heavy x1.5)")
    s.add_argument("--warmup-s", type=float, default=15, help="unmeasured seconds per level")
    s.add_argument("--levels", help='only these levels, e.g. "default:48,prefill-heavy:16" (quick A/B probes)')

    r = sub.add_parser("run", parents=[load], help="run one scenario against the deployed router variant")
    r.add_argument("--scenario", required=True, help=f"bundled name ({', '.join(scenario_names())}) or a YAML path")
    r.add_argument(
        "--variant",
        required=True,
        help="name of the router variant deployed now; a label for the results, nothing is switched",
    )
    r.add_argument(
        "--calibration",
        type=Path,
        default=Path(os.environ.get("PQ_CALIBRATION", "results/calibration/calibration.json")),
        help="calibration.json from `pqbench calibrate` [PQ_CALIBRATION] (default: %(default)s)",
    )
    r.add_argument("--out", type=Path, default=OUT, help="results root (default: %(default)s)")
    r.add_argument("--repeats", "-n", type=int, default=1, help="repeats; tables report the median (default: 1)")

    sub.add_parser("calibrate", help="sweep results -> calibration.json, sweep.png, sweep_summary.csv").add_argument(
        "dir", type=Path, nargs="?", default=Path("results/calibration")
    )
    sub.add_parser("report", help="summary.md, CSVs, timelines and figures for a results tree").add_argument(
        "dir", type=Path, nargs="?", default=Path("results")
    )
    sub.add_parser("scenarios", help="list the bundled scenarios")
    return ap


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    if a.cmd in ("calibrate", "report"):
        try:
            from . import analyze
        except ModuleNotFoundError:
            sys.exit("this command needs the analysis extras: pip install 'pqbench[analysis]'")
        getattr(analyze, a.cmd)(a.dir)
    elif a.cmd == "scenarios":
        for n in scenario_names():
            print(f"{n:<24} variants: {', '.join(yaml.safe_load(resolve_scenario(n).read_text())['variants'])}")
    else:
        ep = loadgen.Endpoints(**{field: getattr(a, field) for field, *_ in ENDPOINTS})
        if a.cmd == "sweep":
            job = loadgen.sweep(ep, a.model, a.out, a.levels, a.hold_s, a.warmup_s, a.seed)
        else:
            job = loadgen.run(
                ep, resolve_scenario(a.scenario), a.variant, a.model, a.calibration, a.out, a.repeats, a.seed
            )
        asyncio.run(job)
