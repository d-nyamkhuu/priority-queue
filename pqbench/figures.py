"""The figures the README cites, from results/<scenario>/<variant>/repN/.

  fig_load              offered load over time, one lane per tenant plus the total
                        against capacity, from a recorded run's meta.json
  fig_priority_tiers    each class's p95 TTFT over time and when requests were
                        rejected, flow control on vs off
  fig_fairness          one tenant bursts: every tenant's p95 TTFT, on vs off
  fig_pd_detectors      pd-shapes: peak saturation per detector and the realtime
                        probe's p95 TTFT (needs metrics.csv)
  fig_admission         admission-tuning: realtime latency vs standard throughput
  fig_kv_transfer       KV-transfer probe before/after the UCX segment-size fix
                        (results/kv-transfer/summary.csv)

Run by `pqbench report`. Every p95 is computed per repeat, then the median
taken across repeats, the same way as summary.md.

One visual system, shared with the diagrams in docs/ ("navy + coral"):
  - white background, navy-black text, thin solid grid; IBM Plex Sans for words
    and IBM Plex Mono for data labels (vendored in pqbench/fonts, OFL);
  - two colours carry meaning, everything else is grey: navy is what is protected
    or primary (realtime, this setup, prefill), coral what takes the pressure
    (batch, decode); standard and baselines are slate; a darker brick red only
    marks rejected requests and the capacity line;
  - comparisons: this setup is a filled navy mark, the baseline a hollow slate
    one, so the two differ in shape as well as colour;
  - legends sit under the plot; latency counts served requests only, so figures
    that compare it also show the share served;
  - short labels only: longer explanations go in the README text under each figure.
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import colors as mcolors
from matplotlib import font_manager, patheffects
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from .data import Run, metric_series, runs
from .stats import percentile

# ----------------------------------------------------------------- style ---
# Checked with the dataviz palette validator on PAPER: navy, slate and coral stay
# apart for normal vision (all pairs, worst dE 15.4) and under protan/deutan
# simulation (worst 9.2), and all clear 3:1 against white. Navy and slate sit
# below the chroma floor on purpose: they are the neutrals coral stands out from.
PAPER, PANEL = "#ffffff", "#f1f4f7"
INK, INK2, INK3 = "#18283f", "#526173", "#8c98a5"
GRID, AXIS = "#e6eaef", "#c3cbd5"
NAVY, SLATE, CORAL, BRICK = "#2b4c7e", "#7d8ca0", "#d16e61", "#a8352b"
CLASS = {"realtime": NAVY, "standard": SLATE, "batch": CORAL}
C_ON = NAVY  # this setup / flow control on / after the fix: filled
C_OFF = SLATE  # baseline / flow control off / before the fix: hollow
C_PREFILL, C_DECODE = NAVY, CORAL
C_REJECT = BRICK

for _f in (Path(__file__).parent / "fonts").glob("*.ttf"):
    font_manager.fontManager.addfont(str(_f))
# DejaVu fills the few glyphs Plex lacks (e.g. ✗).
SANS = ["IBM Plex Sans", "DejaVu Sans"]
MONO = ["IBM Plex Mono", "DejaVu Sans Mono"]

WIDTH = 11.0  # inches; every figure has the same width so text renders at one size in the README
# Sized for the README's ~850 px column: an 11-inch figure is shown at about half size.
FS_TITLE, FS_PANEL, FS_VALUE, FS_NOTE = 17, 13.5, 11.5, 11

VARIANT_LABELS = {"requests": "Request count", "tokens": "Token count", "hybrid": "Hybrid"}

plt.rcParams.update(
    {
        "font.family": SANS,
        "font.sans-serif": SANS,
        "font.monospace": MONO,
        "font.size": 13,
        "text.color": INK,
        "axes.facecolor": PAPER,
        "axes.labelcolor": INK2,
        "axes.labelsize": 13,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.9,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titlesize": FS_PANEL,
        "axes.titleweight": "medium",
        "axes.titlelocation": "left",
        "axes.titlecolor": INK,
        "axes.titlepad": 10,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "grid.linestyle": "-",
        "xtick.color": INK2,
        "ytick.color": INK2,
        "xtick.labelsize": 11.5,
        "ytick.labelsize": 11.5,
        "xtick.major.size": 4,
        "ytick.major.size": 0,
        "xtick.major.width": 0.9,
        "xtick.direction": "out",
        "legend.frameon": False,
        "legend.fontsize": 12.5,
        "figure.facecolor": PAPER,
        "figure.dpi": 110,
        "savefig.dpi": 160,
        "savefig.facecolor": PAPER,
    }
)


def new_fig(height: float, ncols: int = 1, nrows: int = 1, **kw):
    return plt.subplots(nrows, ncols, figsize=(WIDTH, height), layout="constrained", **kw)


def header(fig, title: str) -> None:
    fig.suptitle(title, x=0.01, ha="left", fontsize=FS_TITLE, fontweight="semibold", color=INK)


def panel_title(ax, text: str) -> None:
    ax.set_title(text)


def legend(fig, handles, labels) -> None:
    fig.legend(handles, labels, loc="outside lower center", ncol=len(labels), handletextpad=0.5, columnspacing=2.2)


def save(fig, out: Path, name: str) -> None:
    fig.savefig(out / name, bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)


def value(ax, x, y, text, **kw) -> None:
    """A data label: numbers in the monospace face, one size and colour in every figure."""
    style = {"fontsize": FS_VALUE, "color": INK, "family": "monospace", "path_effects": halo(3)}
    ax.text(x, y, text, **{**style, **kw})


def note(ax, x, y, text, color=INK3, **kw) -> None:
    """A reference-line or shading label: small, muted, haloed so a line can't strike it through."""
    ax.text(x, y, text, fontsize=FS_NOTE, color=color, path_effects=halo(4), **kw)


def halo(width: float = 4.0):
    """A background-coloured outline, so a line or label stays legible over other marks."""
    return [patheffects.withStroke(linewidth=width, foreground=PAPER)]


def filled(ax, x, y, color=C_ON, size=130, marker="o", **kw):
    ax.scatter(x, y, s=size, color=color, marker=marker, zorder=4, edgecolor=PAPER, lw=1.5, **kw)


def hollow(ax, x, y, color=C_OFF, size=130, marker="o", **kw):
    ax.scatter(x, y, s=size, facecolor=PAPER, edgecolor=color, marker=marker, lw=2, zorder=4, **kw)


def dot(color, hollow_mark: bool = False, marker: str = "o"):
    return plt.Line2D(
        [],
        [],
        marker=marker,
        ls="",
        ms=10,
        mew=2 if hollow_mark else 0,
        color=color,
        mfc=PAPER if hollow_mark else color,
    )


def line(color, lw: float = 2.5, **kw):
    return plt.Line2D([], [], color=color, lw=lw, **kw)


# Shades for a class's 1st, 2nd, 3rd... tenant: + blends toward paper, - toward ink.
SHADES = (0.0, -0.3, 0.35, -0.5, 0.55)


def class_colors(objectives: dict[str, str | None]) -> dict[str, str]:
    """Tenant -> colour: its class colour, in a different shade for each tenant of the class."""
    out, count = {}, {}
    for t, obj in sorted(objectives.items()):
        i = count[obj] = count.get(obj, -1) + 1
        f = SHADES[i % len(SHADES)]
        rgb = np.array(mcolors.to_rgb(CLASS.get(obj, INK3)))
        paper = np.array(mcolors.to_rgb(PAPER))
        out[t] = mcolors.to_hex(rgb + (paper - rgb) * f if f > 0 else rgb * (1 + f))
    return out


# ------------------------------------------------------------------ data ---
def p95(x) -> float:
    """p95 in milliseconds of a series of seconds."""
    return percentile(x, 95) * 1000


def surge_p95_by_tenant(reps: list[Run]) -> pd.Series:
    """Median over repeats of each tenant's p95 TTFT (ms) in the surge."""
    rows = [
        (t, p95(g["ttft"])) for r in reps for t, g in r.req[(r.req["phase"] == "surge") & r.req["ok"]].groupby("tenant")
    ]
    return pd.DataFrame(rows, columns=["tenant", "p95_ms"]).groupby("tenant")["p95_ms"].median()


def class_p95(reps: list[Run], objective: str) -> float:
    """Median over repeats of the surge p95 TTFT (ms) of all served requests of one class."""
    sel = [r.req[(r.req["phase"] == "surge") & r.req["ok"] & (r.req["objective"] == objective)] for r in reps]
    return float(np.median([p95(g["ttft"]) for g in sel]))


def served_by_tenant(reps: list[Run]) -> pd.Series:
    """Median over repeats of each tenant's share of surge requests served."""
    shares = [r.req[r.req["phase"] == "surge"].groupby("tenant")["ok"].mean() for r in reps]
    return pd.concat(shares, axis=1).median(axis=1)


def served_share(reps: list[Run], objective: str) -> float:
    """Median over repeats of the share of one class's surge requests that were served."""
    return float(
        np.median(
            [r.req.loc[(r.req["phase"] == "surge") & (r.req["objective"] == objective), "ok"].mean() for r in reps]
        )
    )


def ms_label(v: float) -> str:
    return f"{v / 1000:,.1f} s" if v >= 1000 else f"{v:,.0f} ms"


def plain_log(ax, axis: str = "x") -> None:
    """Log axis labelled 100 ms / 1 s / 10 s instead of 10^2 notation."""
    a = ax.xaxis if axis == "x" else ax.yaxis
    a.set_major_locator(LogLocator(base=10, subs=(1.0, 3.0)))
    a.set_major_formatter(FuncFormatter(lambda v, _: ms_label(v).replace(".0 s", " s")))
    a.set_minor_formatter(NullFormatter())


# --------------------------------------------------------------- figures ---
# One traffic chart per scenario, shown in the README right above that scenario's results.
# admission-tuning stands in for consolidation, which replays the same traffic.
LOAD_TITLES = {
    "priority-tiers": "Traffic: batch tenant Bronze surges",
    "same-priority-fairness": "Traffic: realtime tenant A bursts",
    "pd-shapes": "Traffic: a prefill-heavy surge, then a decode-heavy surge",
    "admission-tuning": "Traffic: standard tenant C surges",
}


def tenant_label(name: str) -> str:
    """realtime-a -> Tenant A, platinum -> Platinum, realtime-probe -> probe."""
    head, _, tail = name.rpartition("-")
    if head and len(tail) == 1:
        return f"Tenant {tail.upper()}"
    if name == "realtime-probe":
        return "probe"
    return name if "-" in name else name.title()


def times(v: float) -> str:
    return f"{v:.2f}×".replace(".00×", "×")


def fig_load(root: Path, out: Path) -> None:
    """Offered load from a recorded run's meta.json (not the current scenario file), so
    later scenario edits can't change what a published chart shows. One lane per tenant,
    each rate as a fraction of its own request shape's capacity, and the total against it."""
    order = {"realtime": 0, "standard": 1, "batch": 2}
    for name, title in LOAD_TITLES.items():
        metas = sorted((root / name).glob("*/rep*/meta.json"))
        if not metas:
            continue
        meta = json.loads(metas[0].read_text())
        edges = np.cumsum([0] + [p["duration_s"] for p in meta["phases"]])
        names = [p["name"] for p in meta["phases"]]
        # Shapes match by token counts.
        shapes = {
            (s["prompt_tokens"], s["output_tokens"]): (k, s["capacity_rps"])
            for k, s in meta["calibration"]["shapes"].items()
        }
        used = set()
        tenants = []
        for t in meta["tenants"]:
            shape, cap = shapes[t["prompt_tokens"], t["output_tokens"]]
            used.add(shape)
            tenants.append((t["name"], t["objective"], np.array([t["rates_rps"][n] / cap for n in names])))
        tenants.sort(key=lambda t: order[t[1]])  # stable: scenario order within a class
        colors = class_colors({t[0]: t[1] for t in tenants})
        total = sum(t[2] for t in tenants)
        top = max(1.4, total.max() * 1.5)
        lane_top = max(t[2].max() for t in tenants) * 1.45
        fig, axes = plt.subplots(
            len(tenants) + 1,
            1,
            figsize=(WIDTH, 1.9 + 0.62 * len(tenants)),
            sharex=True,
            layout="constrained",
            gridspec_kw={"height_ratios": [2.6] + [1] * len(tenants)},
        )
        fig.get_layout_engine().set(h_pad=0.02, hspace=0.0)
        x = np.repeat(edges, 2)[1:-1]
        for ax in axes:
            for ph in meta["measure_phases"]:
                i = names.index(ph)
                ax.axvspan(edges[i], edges[i + 1], color=PANEL, zorder=0, lw=0)
        # Total against capacity.
        ax = axes[0]
        ax.fill_between(x, 0, np.repeat(total, 2), step=None, color=GRID, lw=0, zorder=1)
        ax.plot(x, np.repeat(total, 2), color=INK, lw=2, zorder=3)
        ax.axhline(1.0, color=C_REJECT, lw=1.2, ls=(0, (5, 3)), zorder=2)
        note(
            ax,
            edges[-1],
            1.0,
            "capacity" if len(used) == 1 else "1.0 = a shape's capacity",
            color=C_REJECT,
            ha="right",
            va="bottom",
        )
        for i in range(len(names)):
            # Above the step, or below it when the capacity line sits just above.
            below = 0 < 1.0 - total[i] < top * 0.15
            y = total[i] - top * 0.04 if below else total[i] + top * 0.03
            value(ax, (edges[i] + edges[i + 1]) / 2, y, times(total[i]), ha="center", va="top" if below else "bottom")
        for ph in meta["measure_phases"]:
            i = names.index(ph)
            note(ax, (edges[i] + edges[i + 1]) / 2, top, "measured", ha="center", va="top")
        ax.set_ylim(0, top)
        ax.set_yticks([0, 1])
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: times(v)))
        ax.set_ylabel("total" if len(used) == 1 else "total\n(per shape)", rotation=0, ha="right", va="center")
        # One lane per tenant, on a shared scale.
        for ax, (tname, obj, rate) in zip(axes[1:], tenants, strict=True):
            ax.fill_between(x, 0, np.repeat(rate, 2), color=colors[tname], lw=0, zorder=2)
            for i in range(len(names)):
                if rate[i] > 0 and (i == 0 or rate[i] != rate[i - 1]):
                    value(ax, edges[i] + 3, rate[i] + lane_top * 0.06, times(rate[i]), ha="left", va="bottom")
            ax.set_ylim(0, lane_top)
            ax.set_yticks([])
            ax.grid(False)
            ax.set_ylabel(f"{tenant_label(tname)}\n{obj}", rotation=0, ha="right", va="center", color=INK)
            ax.yaxis.label.set_fontstyle("normal")
        for ax in axes[:-1]:
            ax.tick_params(axis="x", length=0)
            ax.spines["bottom"].set_color(GRID)
        axes[-1].set_xlim(0, edges[-1])
        axes[-1].set_xlabel("seconds")
        header(fig, title)
        save(fig, out, f"fig_load_{name.replace('-', '_')}.png")


def ttft_timeline(r: Run, objective: str, bin_s: float = 10.0) -> pd.Series:
    """p95 TTFT (ms) of served requests per `bin_s` window of send time."""
    g = r.req[r.req["ok"] & (r.req["objective"] == objective)]
    # Published runs predate t_intended: bin those by the actual send time.
    b = (g["t_intended"].fillna(g["t_send"]) // bin_s) * bin_s + bin_s / 2
    return g.groupby(b)["ttft"].apply(lambda s: p95(s) if len(s) >= 5 else np.nan).dropna()


def fig_priority_tiers(root: Path, out: Path) -> None:
    """Each class's p95 TTFT per 10 s window, with a faint tick along the top for every
    rejected request, flow control on vs off."""
    on, off = runs(root, "priority-tiers", "hybrid"), runs(root, "priority-tiers", "no-flow-control")
    if not on or not off:
        return
    fig, axes = new_fig(3.9, ncols=2, sharey=True)
    fig.get_layout_engine().set(wspace=0.04)
    y_reject = 3.2e5
    for ax, (rs, title) in zip(axes, ((on, "Flow control on"), (off, "Flow control off")), strict=True):
        r = rs[0]
        df = r.req.assign(t=r.req["t_intended"].fillna(r.req["t_send"]))
        lo, hi = r.bounds["surge"]
        ax.axvspan(lo, hi, color=PANEL, zorder=0, lw=0)
        note(ax, (lo + hi) / 2, 1.6e5, "batch surge · 1.8× capacity", ha="center", va="center")
        for obj in ("standard", "batch", "realtime"):
            s = ttft_timeline(r, obj)
            ax.plot(s.index, s.values, color=CLASS[obj], lw=2, zorder=3, solid_capstyle="round")
        rej = df.loc[~df["ok"], "t"]
        ax.vlines(rej, y_reject / 1.18, y_reject * 1.18, color=C_REJECT, lw=0.5, alpha=0.12, zorder=2)
        ax.set_yscale("log")
        ax.set_ylim(70, 5e5)
        ax.set_yticks([100, 1000, 10000, 100000, y_reject], ["100 ms", "1 s", "10 s", "100 s", "rejected"])
        ax.minorticks_off()
        ax.set_xlim(0, sum(p["duration_s"] for p in r.meta["phases"]))
        ax.set_xlabel("seconds")
        # Title on top, the panel's two numbers on a line of their own beneath it.
        ax.set_title(title, pad=19)
        protected = max(class_p95(rs, "realtime"), class_p95(rs, "standard"))
        stats = f"realtime + standard p95 {ms_label(protected)}  ·  batch served {served_share(rs, 'batch'):.0%}"
        ax.text(0.0, 1.015, stats, transform=ax.transAxes, ha="left", va="bottom", fontsize=FS_NOTE, color=INK2)
    axes[0].set_ylabel("p95 TTFT")
    axes[0].get_yticklabels()[-1].set_color(C_REJECT)
    legend(fig, [line(CLASS[o], lw=2.4) for o in CLASS], list(CLASS))
    header(fig, "Priority: batch absorbs the overload")
    save(fig, out, "fig_priority_tiers.png")


def fig_fairness(root: Path, out: Path) -> None:
    rs_on = runs(root, "same-priority-fairness", "hybrid")
    rs_off = runs(root, "same-priority-fairness", "no-flow-control")
    if not rs_on or not rs_off:
        return
    on_v, off_v = surge_p95_by_tenant(rs_on), surge_p95_by_tenant(rs_off)
    on_s, off_s = served_by_tenant(rs_on), served_by_tenant(rs_off)
    # The tenant with the highest configured surge rate bursts.
    loud = max(rs_on[0].meta["tenants"], key=lambda t: t["rates_rps"]["surge"])["name"]
    tenants = [loud] + sorted(t for t in on_v.index if t != loud)
    fig, ax = new_fig(4.6)
    y = np.arange(len(tenants))[::-1]
    for yy, t in zip(y, tenants, strict=True):
        a, b = on_v.get(t, np.nan), off_v.get(t, np.nan)
        ax.annotate(
            "",
            (a * 1.08, yy),
            (b / 1.08, yy),
            arrowprops={"arrowstyle": "-|>", "color": AXIS, "lw": 1.6, "mutation_scale": 14},
            zorder=2,
        )
        hollow(ax, [b], [yy])
        filled(ax, [a], [yy])
        for v, share in ((a, on_s.get(t, np.nan)), (b, off_s.get(t, np.nan))):
            value(ax, v, yy + 0.16, f"{ms_label(v)}\n{share:.0%} served", ha="center", va="bottom")
    ax.set_yticks(y, [tenant_label(t) + ("\nbursts ~5×" if t == loud else "") for t in tenants])
    ax.tick_params(axis="y", labelsize=13, labelcolor=INK)
    ax.set_ylim(-0.5, len(tenants) - 0.05)
    ax.set_xscale("log")
    plain_log(ax, "x")
    ax.grid(axis="x", visible=True)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("p95 TTFT of served requests during the burst (log)")
    legend(
        fig, [dot(C_ON), dot(C_OFF, hollow_mark=True)], ["flow control on (round robin per tenant)", "flow control off"]
    )
    header(fig, "Fairness: three realtime tenants, one bursts; the others stay fast")
    save(fig, out, "fig_same_priority_fairness.png")


def fig_pd_detectors(root: Path, out: Path) -> None:
    rs = runs(root, "pd-shapes")
    if not rs or any(r.metrics.empty for r in rs):
        # metrics.csv is gitignored: on a fresh clone keep the committed figure.
        print("figures: fig_pd_detectors needs metrics.csv; keeping the existing files")
        return
    rows = []
    for r in rs:
        eff = metric_series(r.metrics, "llm_d_epp_flow_control_pool_saturation", "stage", "epp")["effective"]
        probe = r.req[(r.req["tenant"] == "realtime-probe") & r.req["ok"]]
        for ph in ("prefill-surge", "decode-surge"):
            lo, hi = r.bounds[ph]
            peak = float(eff[(eff.index >= lo) & (eff.index < hi)].max())
            rows.append(
                {
                    "variant": r.meta["variant"],
                    "phase": ph,
                    "peak": peak,
                    "probe_p95": p95(probe.loc[probe["phase"] == ph, "ttft"]),
                }
            )
    df = pd.DataFrame(rows).groupby(["variant", "phase"]).median().reset_index()
    fig, (a1, a2) = new_fig(4.8, ncols=2)
    fig.get_layout_engine().set(wspace=0.06)
    variants = [v for v in VARIANT_LABELS if v in set(df["variant"])]
    y = np.arange(len(variants))[::-1]
    phases = (
        ("prefill-surge", C_PREFILL, "o", "prefill-heavy surge"),
        ("decode-surge", C_DECODE, "D", "decode-heavy surge"),
    )
    for j, (ph, col, mk, _) in enumerate(phases):
        sub = df[df["phase"] == ph].set_index("variant").reindex(variants)
        ys = y + (0.5 - j) * 0.3
        filled(a1, sub["peak"], ys, color=col, marker=mk, size=120)
        filled(a2, sub["probe_p95"], ys, color=col, marker=mk, size=120)
        for yi, v in zip(ys, sub["peak"], strict=True):
            value(a1, v + 0.04, yi, f"{v:.2f}", va="center")
        for yi, v in zip(ys, sub["probe_p95"], strict=True):
            value(a2, v * 1.15, yi, ms_label(v), va="center")
    for ceiling, label in ((0.75, "standard\nceiling"), (1.0, "realtime\nceiling")):
        a1.axvline(ceiling, color=INK2, lw=1, ls=(0, (4, 3)), zorder=1)
        note(a1, ceiling, len(variants) - 0.45, label, ha="center", va="bottom", color=INK2)
    a1.set_xlim(0.4, 1.45)
    a1.set_xlabel("peak pool saturation")
    panel_title(a1, "What the detector saw")
    a2.set_xscale("log")
    plain_log(a2, "x")
    a2.set_xlim(400, 25000)
    a2.set_xlabel("probe p95 TTFT (log)")
    panel_title(a2, "What realtime felt")
    a1.set_yticks(y, [f"{VARIANT_LABELS[v]}\ndetector" for v in variants])
    a1.tick_params(axis="y", labelsize=13, labelcolor=INK)
    a2.set_yticks(y, [])
    for a in (a1, a2):
        a.set_ylim(-0.6, len(variants) - 0.05)
        a.grid(axis="y", visible=False)
        a.grid(axis="x", visible=True)
    legend(fig, [dot(c, marker=m) for _, c, m, _ in phases], [lab for _, _, _, lab in phases])
    header(fig, "P/D admission: hybrid kept probe p95 ≤ 1.1 s in both surges")
    save(fig, out, "fig_pd_detector_peaks.png")
    df.to_csv(out / "fig_pd_detector_peaks.csv", index=False)


def fig_admission(root: Path, out: Path) -> None:
    """One point per router setting on the trade-off it controls: realtime latency (y)
    against how much of the surging standard tenant's traffic still gets served (x)."""
    labels = {"hybrid": "Holdback (default)", "no-holdback": "No holdback", "cap-tight": "Request cap halved"}
    rows = []
    for v in labels:
        rs = runs(root, "admission-tuning", v)
        if rs:
            rows.append((v, class_p95(rs, "realtime"), 100 * served_share(rs, objective="standard")))
    if not rows:
        return
    fig, ax = new_fig(4.4)
    for v, rt, st in rows:
        (filled if v == "hybrid" else hollow)(ax, [st], [rt], size=160)
        ax.annotate(
            f"{labels[v]}", (st, rt), xytext=(13, 7), textcoords="offset points", va="bottom", fontsize=13, color=INK
        )
        ax.annotate(
            f"realtime {ms_label(rt)} · standard {st:.0f}% served",
            (st, rt),
            xytext=(13, 5),
            textcoords="offset points",
            va="top",
            fontsize=FS_VALUE,
            color=INK2,
            family="monospace",
        )
    ax.set_xlim(0, 80)
    ax.set_ylim(0, max(r[1] for r in rows) * 1.25)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f} ms"))
    ax.grid(axis="x", visible=True)
    ax.set_xlabel("standard tenant: share of its surge requests served")
    ax.set_ylabel("realtime p95 TTFT")
    ax.annotate(
        "better",
        xy=(0.97, 0.06),
        xytext=(0.88, 0.26),
        xycoords="axes fraction",
        textcoords="axes fraction",
        fontsize=FS_NOTE,
        color=INK3,
        ha="center",
        arrowprops={"arrowstyle": "-|>", "color": INK3, "lw": 1.2},
    )
    header(fig, "Admission tuning: holdback keeps realtime fastest, at the cost of standard traffic")
    save(fig, out, "fig_admission_tuning.png")


def fig_kv_transfer(root: Path, out: Path) -> None:
    """Three before/after slopes: each metric from UCX defaults (hollow) to 1 MiB segments (filled)."""
    src = root / "kv-transfer" / "summary.csv"
    if not src.exists():
        return
    df = pd.read_csv(src).set_index("setting")
    before, after = df.loc["baseline"], df.loc["ucx-seg-1m"]
    panels = [
        ("throughput_rps", "Throughput, higher is better", lambda v: f"{v:.1f} req/s"),
        ("ttft_p95_ms", "p95 TTFT, lower is better", ms_label),
        ("kv_transfer_mean_s", "Mean KV transfer, lower is better", lambda v: f"{v:.2f} s"),
    ]
    fig, axes = new_fig(3.8, ncols=3)
    fig.get_layout_engine().set(wspace=0.1)
    for ax, (col, title, fmt) in zip(axes, panels, strict=True):
        b, a = before[col], after[col]
        ax.plot([0, 1], [b, a], color=AXIS, lw=2, zorder=2)
        hollow(ax, [0], [b], size=150)
        filled(ax, [1], [a], size=150)
        value(ax, -0.09, b, fmt(b), ha="right", va="center", color=INK2)
        value(ax, 1.09, a, fmt(a), ha="left", va="center")
        ax.set_xticks([0, 1], ["UCX\ndefaults", "1 MiB\nsegments"])
        ax.tick_params(axis="x", labelsize=12.5, length=0)
        ax.set_xlim(-0.75, 1.75)
        ax.set_ylim(-max(a, b) * 0.08, max(a, b) * 1.15)
        ax.set_yticks([])
        ax.spines[["left", "bottom"]].set_visible(False)
        ax.grid(False)
        panel_title(ax, title)
    gain = after["throughput_rps"] / before["throughput_rps"]
    header(fig, f"KV transfer fix: larger UCX TCP segments, {gain:.1f}× pool throughput")
    save(fig, out, "fig_kv_transfer.png")


def fig_sweep(levels: pd.DataFrame, knees: dict[str, int], shapes: dict, out: Path) -> None:
    """The calibration sweep: one column per request shape, throughput (navy) and the
    latency it costs (coral) against concurrency, with the knee marked and labelled."""
    rows = (
        ("rps", "completed req/s", NAVY, lambda v: f"{v:.1f} req/s"),
        ("ttft_p95_ms", "p95 TTFT", CORAL, ms_label),
        ("tpot_p50_ms", "time per output\ntoken, p50 (ms)", CORAL, lambda v: f"{v:.0f} ms"),
    )
    groups = list(levels.groupby("shape", sort=False))
    fig, axes = plt.subplots(
        len(rows), len(groups), figsize=(WIDTH, 7.6), sharex="col", layout="constrained", squeeze=False
    )
    fig.get_layout_engine().set(h_pad=0.08, wspace=0.06)
    for j, (shape, g) in enumerate(groups):
        g = g.sort_values("concurrency")
        k = knees[shape]
        at = g[g["concurrency"] == k].iloc[0]
        for i, (col, label, color, fmt) in enumerate(rows):
            ax = axes[i, j]
            top = g[col].max() * 1.3
            ax.axvline(k, color=INK3, lw=1, ls=(0, (4, 3)), zorder=1)
            ax.plot(g["concurrency"], g[col], color=color, lw=2, marker="o", ms=4, zorder=3)
            filled(ax, [k], [at[col]], color=color, size=70)
            ax.text(
                0.98,
                0.97,
                f"at knee  {fmt(at[col])}",
                transform=ax.transAxes,
                ha="right",
                va="top",
                fontsize=FS_VALUE - 1,
                family="monospace",
                color=INK,
                bbox={"fc": PAPER, "ec": "none", "pad": 2},
                zorder=5,
            )
            ax.set_ylim(0, top)
            ax.yaxis.set_major_locator(plt.MaxNLocator(3))
            if col == "ttft_p95_ms":
                ax.yaxis.set_major_formatter(
                    FuncFormatter(lambda v, _: "0" if v == 0 else ms_label(v).replace(".0 s", " s"))
                )
            if j == 0:
                ax.set_ylabel(label)
        s = shapes[shape]
        panel_title(axes[0, j], f"{shape}\n{s['prompt_tokens']:,} in / {s['output_tokens']:,} out · knee {k}")
        axes[-1, j].set_xlabel("concurrent clients")
        axes[-1, j].set_xlim(0, g["concurrency"].max() * 1.05)
    header(fig, "Calibration sweep: where each request shape stops scaling")
    save(fig, out, "sweep.png")


FIGURES = (fig_load, fig_priority_tiers, fig_fairness, fig_pd_detectors, fig_admission, fig_kv_transfer)


def make_all(root: Path) -> None:
    out = root / "figures"
    out.mkdir(exist_ok=True)
    for f in FIGURES:
        f(root, out)
        print(f"figures: {f.__name__} done")
