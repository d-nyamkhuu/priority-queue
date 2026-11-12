"""Analysis of the load generator's output, run locally on a copied results tree.

  pqbench calibrate DIR   sweep -> knee per request shape -> calibration.json + sweep.png
  pqbench report DIR      every scenario/variant/rep -> CSV tables, a timeline
                          per run, summary.md, and the figures the docs cite
                          (figures.py)

The CSVs are long-format and keep every repeat, so new charts can be made
later without rerunning on GPUs.
"""

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import figures
from .data import Run, load_requests, metric_series, runs
from .stats import percentile

PRIORITY_CLASS = {"100": "realtime", "0": "standard", "-10": "batch"}
STAGE_COLORS = {"prefill": figures.C_PREFILL, "decode": figures.C_DECODE, "effective": figures.INK}
# TTFT SLO for the rejection-aware "within SLO" and goodput metrics. Neither the
# scenarios nor the objectives define one, so this is the report's choice: 2 s sits
# above every request shape's calibrated p99 TTFT at the knee (prefill-heavy
# 1.28 s), so a request that misses it waited in a queue.
TTFT_SLO_S = 2.0
# Priority holdback ceilings below 1.0 in deploy/router/epp.yaml, drawn on the
# timeline's saturation panel except for the variants that delete the policy.
HOLDBACK_CEILINGS = {"0": 0.75, "-10": 0.5}
NO_HOLDBACK_VARIANTS = {"no-holdback", "no-flow-control"}


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def perf_stats(ok: pd.DataFrame, dur: float) -> dict[str, float]:
    """Throughput over `dur` seconds and latency percentiles for successful requests.

    Pass only served requests: rejected ones have no TTFT, so these percentiles
    describe the survivors. tenant_stats adds the rejection-aware SLO metric.
    TPOT is time per output token, from token counts; decode_tps is a
    request's tokens/s after its first token.
    """
    ms = 1000.0
    out = {
        "served_rps": len(ok) / dur,
        "input_tps": ok["input_tokens"].sum() / dur,
        "output_tps": ok["output_tokens"].sum() / dur,
        "total_tps": (ok["input_tokens"].sum() + ok["output_tokens"].sum()) / dur,
        "prompt_tokens_mean": ok["input_tokens"].mean(),
    }
    for q in (50, 95, 99):
        out[f"ttft_p{q}_ms"] = percentile(ok["ttft"], q) * ms
        out[f"tpot_p{q}_ms"] = percentile(ok["tpot"], q) * ms
    out["e2e_p95_s"] = percentile(ok["e2e"], 95)
    out["decode_tps_p50"] = percentile(ok["decode_tps"], 50)
    return out


# ------------------------------------------------------------- calibrate ---
def knee(levels: pd.DataFrame, frac: float = 0.9) -> pd.Series:
    """Smallest concurrency that reaches `frac` of the peak throughput."""
    peak = levels["rps"].max()
    return levels[levels["rps"] >= frac * peak].sort_values("concurrency").iloc[0]


def calibrate(d: Path) -> None:
    meta = json.loads((d / "meta.json").read_text())
    df = load_requests(d / "requests.jsonl")
    measured = df[df["phase"] == "measure"]
    rows = []
    for level in meta["levels"]:
        hold = level["measure_end"] - level["measure_start"]
        sel = measured[(measured["shape"] == level["shape"]) & (measured["concurrency"] == level["concurrency"])]
        ok = sel[sel["ok"]]
        rows.append(
            {
                "shape": level["shape"],
                "concurrency": level["concurrency"],
                "requests": len(sel),
                "ok": len(ok),
                **perf_stats(ok, hold),
            }
        )
    levels = pd.DataFrame(rows).rename(columns={"served_rps": "rps"})
    levels.to_csv(d / "sweep_summary.csv", index=False)

    shapes = {}
    for shape, g in levels.groupby("shape"):
        k = knee(g)
        shapes[shape] = {
            **meta["shapes"][shape],
            "knee_concurrency": int(k["concurrency"]),
            "capacity_rps": round(float(k["rps"]), 3),
            "peak_rps": round(float(g["rps"].max()), 3),
            "peak_total_tps": round(float(g["total_tps"].max()), 1),
            "peak_output_tps": round(float(g["output_tps"].max()), 1),
            "prompt_tokens_mean": round(float(k["prompt_tokens_mean"]), 1),
            # What the pool delivers at the knee: throughput and the latency it costs.
            "at_knee": {
                key: round(float(v), 3)
                for key, v in k.drop(["shape", "concurrency", "requests", "ok", "rps", "prompt_tokens_mean"]).items()
            },
        }
    # Detector limits from the sweep knees:
    #   maxConcurrency      ~ in-flight requests at the default-shape knee
    #   maxTokenConcurrency ~ in-flight prompt tokens at the prefill-heavy knee
    if not {"default", "prefill-heavy"} <= shapes.keys():
        sys.exit("calibration needs the default and prefill-heavy shapes in the sweep")
    ph = shapes["prefill-heavy"]
    calibration = {
        "max_concurrency": int(shapes["default"]["knee_concurrency"]),
        "max_token_concurrency": int(round(ph["knee_concurrency"] * ph["prompt_tokens_mean"], -3)),
        "shapes": shapes,
        "method": "knee = smallest concurrency reaching 90% of peak req/s in a closed-loop sweep",
    }
    (d / "calibration.json").write_text(json.dumps(calibration, indent=2))
    print(json.dumps(calibration, indent=2))

    knees = {shape: v["knee_concurrency"] for shape, v in shapes.items()}
    figures.fig_sweep(levels, knees, meta["shapes"], d)


# ---------------------------------------------------------------- report ---
def tenant_stats(df: pd.DataFrame, bounds: dict[str, tuple[float, float]]) -> pd.DataFrame:
    rows = []
    for (tenant, phase), g in df.groupby(["tenant", "phase"]):
        ok = g[g["ok"]]
        # Rates are over the phase's configured duration (open-loop arrivals),
        # not the span of send times, which inflates sparse tenants.
        lo, hi = bounds[phase]
        dur = hi - lo
        perf = perf_stats(ok, dur)
        # Rejection-aware: share of everything sent that was served within the
        # TTFT SLO. The latency percentiles above only see served requests.
        # Goodput counts only those requests; served_rps counts every HTTP 200.
        in_slo = int((ok["ttft"] <= TTFT_SLO_S).sum())
        rows.append(
            {
                "tenant": tenant,
                "phase": phase,
                "objective": g["objective"].iloc[0],
                "sent": len(g),
                "ok": len(ok),
                "success_rate": len(ok) / len(g),
                "rejected_4xx_5xx": int((g["status"] >= 400).sum()),
                "client_errors": int((g["status"] < 0).sum()),
                "ttft_slo_ms": TTFT_SLO_S * 1000,
                "within_slo": in_slo,
                "slo_attainment": in_slo / len(g),
                "goodput_rps": in_slo / dur,
                "send_lag_p99_ms": percentile(g["send_lag"], 99) * 1000,
                **perf,
            }
        )
    return pd.DataFrame(rows)


def peak_saturation(metrics: pd.DataFrame, lo: float, hi: float) -> dict[str, float]:
    s = metric_series(metrics, "llm_d_epp_flow_control_pool_saturation", "stage", "epp")
    return {f"peak_saturation_{k}": float(v[(v.index >= lo) & (v.index < hi)].max()) for k, v in s.items()}


def shade_phases(ax, bounds, measure):
    for name, (lo, hi) in bounds.items():
        if name in measure:
            ax.axvspan(lo, hi, color=figures.PANEL, lw=0)


def key(ax, ncol: int = 1) -> None:
    """A framed legend over the lines, or a note when the panel has no series (flow control off)."""
    if ax.get_legend_handles_labels()[0]:
        ax.legend(
            loc="upper left",
            fontsize=10,
            ncol=ncol,
            frameon=True,
            framealpha=0.9,
            facecolor=figures.PAPER,
            edgecolor="none",
        )
    else:
        ax.text(
            0.5,
            0.5,
            "no EPP flow-control metrics (flow control off)",
            transform=ax.transAxes,
            ha="center",
            color=figures.INK3,
        )


def timeline(r: Run, out: Path) -> None:
    df, metrics, meta = r.req, r.metrics, r.meta
    colors = figures.class_colors(df.groupby("tenant")["objective"].first().to_dict())
    tenants = sorted(colors)
    # Every 10 s bin of the run, so a bin without requests breaks a latency line
    # (NaN) and shows as zero throughput, instead of being bridged.
    bins = np.arange(0, max(df["t_end"].max(), max(hi for _, hi in r.bounds.values())) + 10, 10.0)
    fig, axes = plt.subplots(5, 1, figsize=(14, 17), sharex=True)

    ax = axes[0]
    for t in tenants:
        g = df[(df["tenant"] == t) & df["ok"]]
        ax.scatter(g["t_send"], g["ttft"] * 1000, s=4, alpha=0.25, color=colors[t])
        p95 = g.groupby(g["t_send"] // 10 * 10)["ttft"].agg(lambda x: percentile(x, 95)).reindex(bins) * 1000
        ax.plot(bins + 5, p95.values, color=colors[t], lw=2, label=f"{t} (p95 / 10s, served)")
    ax.set_yscale("log")
    ax.set_ylabel("TTFT (ms, log)")
    key(ax, ncol=2)
    ax.set_title(f"{meta['scenario']} / {meta['variant']} / rep {meta['rep']}  (shaded = measured phase)")

    ax = axes[1]
    for t in tenants:
        g = df[df["tenant"] == t]
        ok = g[g["ok"]]
        sent = g.groupby(g["t_send"] // 10 * 10).size().reindex(bins, fill_value=0) / 10
        okc = ok.groupby(ok["t_end"] // 10 * 10).size().reindex(bins, fill_value=0) / 10
        ax.plot(bins + 5, sent.values, ls=":", color=colors[t])
        ax.plot(bins + 5, okc.values, color=colors[t], label=t)
    ax.set_ylabel("req/s (dotted sent, solid completed)")
    key(ax, ncol=3)

    ax = axes[2]
    for prio, s in sorted(
        metric_series(metrics, "llm_d_epp_flow_control_queue_size", "priority", "epp").items(),
        key=lambda kv: -int(kv[0] or 0),
    ):
        cls = PRIORITY_CLASS.get(prio)
        ax.plot(s.index, s.values, color=figures.CLASS.get(cls, figures.INK3), label=f"{cls or 'priority'} ({prio})")
    ax.set_ylabel("EPP queued requests")
    key(ax)

    ax = axes[3]
    for stage, s in metric_series(metrics, "llm_d_epp_flow_control_pool_saturation", "stage", "epp").items():
        ax.plot(s.index, s.values, color=STAGE_COLORS.get(stage), label=f"saturation: {stage}")
    ax.axhline(1.0, color=figures.C_REJECT, ls="--", lw=1)
    for prio, ceil in ({} if meta["variant"] in NO_HOLDBACK_VARIANTS else HOLDBACK_CEILINGS).items():
        ax.axhline(ceil, color=figures.INK3, ls=":", lw=1)
        ax.text(0, ceil, f" ceiling p{prio}", fontsize=9, va="bottom", color=figures.INK2)
    ax.set_ylabel("pool saturation")
    key(ax)

    ax = axes[4]
    for src in ("prefill", "decode"):
        for name, ls in (("vllm:num_requests_running", "-"), ("vllm:num_requests_waiting", ":")):
            s = metric_series(metrics, name, None, src).get("all")
            if s is not None:
                ax.plot(s.index, s.values, ls=ls, color=STAGE_COLORS[src], label=f"{src} {name.split('_', 2)[-1]}")
    ax.set_ylabel("vLLM requests")
    ax.set_xlabel("seconds since run start")
    key(ax, ncol=2)

    for ax in axes:
        shade_phases(ax, r.bounds, meta["measure_phases"])
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


def report(root: Path) -> None:
    slo_ms = f"{TTFT_SLO_S * 1000:,.0f}"
    md = [
        "# Benchmark summary",
        "",
        (
            "Measured-phase statistics per tenant, each the median across valid repeats (n = number of repeats). "
            "TTFT and E2E are measured from each request's scheduled send time when the run recorded one "
            "(`t_intended`), otherwise from the actual send. TTFT, TPOT, decode and E2E "
            "percentiles count served requests only (HTTP 200): rejected requests have no TTFT. *within SLO* is "
            f"the share of all sent requests that were served with TTFT <= {slo_ms} ms, so rejections count "
            "against it. *served req/s* counts every served request; *goodput req/s* counts only those within "
            "the SLO."
        ),
        "",
    ]
    for scen_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "calibration"):
        rs = runs(root, scen_dir.name)
        if not rs:
            continue
        measure = rs[0].meta["measure_phases"]
        per_run = []
        for r in rs:
            st = tenant_stats(r.req, r.bounds)
            st["variant"], st["rep"] = r.meta["variant"], r.meta["rep"]
            per_run.append(st)
        runs_df = pd.concat(per_run)
        runs_df.to_csv(scen_dir / "runs.csv", index=False)
        agg = (
            runs_df[runs_df["phase"].isin(measure)]
            .groupby(["variant", "phase", "tenant", "objective"])
            .agg(
                repeats=("rep", "nunique"),
                ttft_p50_ms_median=("ttft_p50_ms", "median"),
                ttft_p95_ms_median=("ttft_p95_ms", "median"),
                tpot_p95_ms_median=("tpot_p95_ms", "median"),
                decode_tps_p50_median=("decode_tps_p50", "median"),
                e2e_p95_s_median=("e2e_p95_s", "median"),
                output_tps_median=("output_tps", "median"),
                success_rate_median=("success_rate", "median"),
                rejected_total=("rejected_4xx_5xx", "sum"),
                slo_attainment_median=("slo_attainment", "median"),
                served_rps_median=("served_rps", "median"),
                goodput_rps_median=("goodput_rps", "median"),
                send_lag_p99_ms_max=("send_lag_p99_ms", "max"),
            )
            .reset_index()
        )
        agg.to_csv(scen_dir / "summary.csv", index=False)

        # metrics.csv is gitignored: on a fresh clone keep the committed
        # timeline.png and saturation.csv rather than blank them.
        sat_csv = scen_dir / "saturation.csv"
        for r in rs:
            if not r.metrics.empty:
                timeline(r, r.dir / "timeline.png")
        if all(not r.metrics.empty for r in rs):
            sat_rows = [
                {
                    "variant": r.meta["variant"],
                    "rep": r.meta["rep"],
                    "phase": ph,
                    **peak_saturation(r.metrics, *r.bounds[ph]),
                }
                for r in rs
                for ph in measure
            ]
            sat = (
                pd.DataFrame(sat_rows)
                .groupby(["variant", "phase"])
                .median(numeric_only=True)
                .drop(columns="rep")
                .reset_index()
            )
            sat.to_csv(sat_csv, index=False)
        else:
            warn(f"{scen_dir.name}: some runs have no metrics.csv; keeping their timeline.png and {sat_csv.name}")
            sat = pd.read_csv(sat_csv) if sat_csv.exists() else pd.DataFrame()

        md += [f"## {scen_dir.name}", ""]
        for ph in measure:
            a = agg[agg["phase"] == ph]
            md += [
                f"**{ph}**",
                "",
                (
                    "| variant | tenant | objective | p95 TTFT ms (served only) | p50 TTFT ms | p95 TPOT ms "
                    "| decode tok/s p50 | p95 E2E s | success | within SLO | served req/s | goodput req/s "
                    "| output tok/s | n |"
                ),
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
            for _, r in a.sort_values(["variant", "tenant"]).iterrows():
                md.append(
                    f"| {r.variant} | {r.tenant} | {r.objective} | {r.ttft_p95_ms_median:,.0f} | "
                    f"{r.ttft_p50_ms_median:,.0f} | "
                    f"{r.tpot_p95_ms_median:,.1f} | {r.decode_tps_p50_median:,.1f} | {r.e2e_p95_s_median:,.2f} | "
                    f"{r.success_rate_median:.1%} | {r.slo_attainment_median:.1%} | "
                    f"{r.served_rps_median:.2f} | {r.goodput_rps_median:.2f} | {r.output_tps_median:,.0f} | "
                    f"{r.repeats} |"
                )
            md.append("")
        if not sat.empty:
            md += [
                "Peak EPP pool saturation in the measured phase (median over repeats):",
                "",
                sat.set_index(["variant", "phase"]).round(3).to_markdown(),
                "",
            ]
    (root / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))

    figures.make_all(root)
