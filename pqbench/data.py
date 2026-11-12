"""Loading a results tree: one place for the readers that analyze.py and
figures.py both use, so the tables and the figures see the same numbers.

Layout (written by `pqbench run`):
  <root>/<scenario>/<variant>/repN/{requests.jsonl, metrics.csv, meta.json, DONE}
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd


def load_requests(path: Path) -> pd.DataFrame:
    """requests.jsonl as a DataFrame with derived per-request columns.

    Latency is measured from the scheduled send time when the record has one
    (open-loop runs): `ttft` and `e2e` include any delay between when the
    request was due and when the generator actually sent it, so a slow
    generator can't hide queueing (coordinated omission). `ttft_from_send`
    keeps the raw value. Closed-loop sweep records have no schedule and are
    unchanged.
    """
    df = pd.read_json(path, lines=True)
    # Absent when no request got a usage block, and in sweep records (no schedule).
    for col in ("prompt_tokens", "completion_tokens", "t_intended"):
        if col not in df:
            df[col] = np.nan
    df["ok"] = df["status"] == 200
    df["ttft_from_send"] = df["ttft"]
    df["send_lag"] = (df["t_send"] - df["t_intended"]).clip(lower=0).fillna(0.0)
    df["ttft"] += df["send_lag"]
    df["e2e"] += df["send_lag"]
    # Token counts come from vLLM's usage block. Fall back to the target prompt
    # length and to the chunk count if a stream ended before usage arrived.
    df["input_tokens"] = df["prompt_tokens"].fillna(df["target_prompt_tokens"])
    df["output_tokens"] = df["completion_tokens"].fillna(df["chunks"])
    # TPOT from token counts, not chunk gaps: under load vLLM can stream several
    # tokens per chunk, so gaps between chunks overstate the per-token time.
    df["tpot"] = ((df["e2e"] - df["ttft"]) / (df["output_tokens"] - 1)).where(df["output_tokens"] > 1)
    df["decode_tps"] = 1.0 / df["tpot"]
    return df


def phase_bounds(meta: dict) -> dict[str, tuple[float, float]]:
    """Phase name -> (start, end) in seconds since run start."""
    out, t = {}, 0.0
    for p in meta["phases"]:
        out[p["name"]] = (t, t + p["duration_s"])
        t += p["duration_s"]
    return out


def load_metrics(path: Path) -> pd.DataFrame:
    """metrics.csv in long format; empty when absent (it is gitignored, so a fresh clone has none)."""
    if not path.exists():
        return pd.DataFrame(columns=["t", "source", "name", "labels", "value"])
    metrics = pd.read_csv(path)
    metrics["labels"] = metrics["labels"].map(json.loads)
    return metrics


def metric_series(metrics: pd.DataFrame, name: str, by: str | None, source: str) -> dict[str, pd.Series]:
    """One time series per value of label `by` (or a single "all"), summed over other labels."""
    sel = metrics[(metrics["name"] == name) & (metrics["source"] == source)]
    key = sel["labels"].map(lambda lab: lab.get(by, "")) if by else pd.Series("all", index=sel.index)
    return {str(k): g.groupby("t")["value"].sum() for k, g in sel.assign(key=key).groupby("key")}


class Run:
    """One finished repeat: meta, requests and (if present) the 1 Hz metrics."""

    def __init__(self, d: Path):
        self.dir = d
        self.meta = json.loads((d / "meta.json").read_text())
        self.req = load_requests(d / "requests.jsonl")
        self.metrics = load_metrics(d / "metrics.csv")
        self.bounds = phase_bounds(self.meta)


def runs(root: Path, scenario: str, variant: str = "*") -> list[Run]:
    """Finished repeats (those with a DONE marker) of a scenario, of one variant or all."""
    return [Run(done.parent) for done in sorted((root / scenario).glob(f"{variant}/rep*/DONE"))]
