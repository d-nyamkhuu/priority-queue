"""Multi-tenant load generator for llm-d flow control on a P/D pool.

Talks only HTTP: the router's OpenAI-compatible endpoint, the EPP's /metrics
page and the Prometheus that scrapes the vLLM pods (see `Endpoints`), so it
runs anywhere that can reach them: on a laptop through `kubectl port-forward`,
or in a pod next to the router. Two modes,
exposed as `pqbench run` and `pqbench sweep`:

  run    Open-loop (Poisson) traffic from several tenants. Each tenant has its
         own objective (priority) and fairness id, and its rate changes by
         phase (warmup -> surge -> cooldown).
  sweep  Closed-loop concurrency sweep with flow control effectively off,
         used to calibrate capacity per request shape.

Every repeat writes to its own directory:
  requests.jsonl    one line per request: tenant, phase, send time (and, for
                    open-loop runs, the scheduled send time `t_intended`), TTFT,
                    E2E, HTTP status, token counts; `timed_out` / `incomplete`
                    flag requests that hit the total timeout or were still in
                    flight at the end (status -1)
  metrics.csv       1 Hz samples of the EPP queue and saturation gauges (scraped
                    directly: one EPP), then every vLLM pod's running/waiting
                    counts, fetched from Prometheus after the run (long format)
  meta.json         scenario, variant, seed, timing, tenants and their rates
  DONE              written last; a rerun skips repeats that have it
"""

import asyncio
import csv
import json
import random
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import yaml

from .stats import percentile


@dataclass(frozen=True)
class Endpoints:
    """Every URL the generator talks to (defaults and PQ_* variables: cli.py)."""

    router: str
    epp_metrics: str
    prometheus: str


SETTLE_S = 10.0  # idle time between runs
# Open-loop runs keep up to a few thousand streams open (queued requests hold
# their connection).
LIMITS = httpx.Limits(max_connections=4096, max_keepalive_connections=1024)

# Request shapes used by calibration and scenarios. Scenario rates are given
# as fractions of the calibrated capacity of a shape.
SHAPES = {
    "default": {"prompt_tokens": 512, "output_tokens": 128},
    "prefill-heavy": {"prompt_tokens": 4096, "output_tokens": 32},
    "decode-heavy": {"prompt_tokens": 256, "output_tokens": 1024},
}

# The series the analysis plots and wait_quiet() checks: the EPP's, scraped
# from its /metrics page (everything else there is skipped), and vLLM's, read
# per pod from Prometheus, where the pd-pool PodMonitors label them by role.
SATURATION = "llm_d_epp_flow_control_pool_saturation"
METRICS = ("llm_d_epp_flow_control_queue_size", SATURATION)
VLLM_METRICS = ("vllm:num_requests_running", "vllm:num_requests_waiting")
ROLES = 'llm_d_ai_role=~"prefill|decode"'
PROM_STEP = 1.0  # seconds; the chart's monitoring.interval

# Common English words. With the Qwen tokenizer they average ~1.1 tokens each
# once joined with spaces, so make_prompt() uses tokens / 1.1 words (a 512-token
# target measured 496 prompt tokens on average, uuid prefix included).
WORDS = (
    "the of and to in is that for it as was with be by on not he this are or his from at which but have an "
    "they you were her she there one all we their been has would when who will more no if out so said what up "
    "its about into than them can only other new some could time these two may then do first any my now such "
    "like our over man me even most made after also did many before must through back years where much your way "
    "well down should because each just those people how too little state good very make world still own see men "
    "work long get here between both life being under never day same another know while last might us great old "
    "year off come since against go came right used take three himself few house use during without again place "
    "around however home small found thought went say part once general high upon school every does got united "
    "left number course war until always away something fact though water less public put think almost hand enough"
).split()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def make_prompt(tokens: int, rng: random.Random) -> str:
    # A unique prefix per request keeps any prefix cache from helping.
    n = max(8, int(tokens / 1.1))
    return f"[{uuid.UUID(int=rng.getrandbits(128)).hex}] " + " ".join(rng.choices(WORDS, k=n))


def http_client(timeout: float) -> httpx.AsyncClient:
    # httpx's timeout is per read/write; send() also bounds each request in total.
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), limits=LIMITS)


def write_results(d: Path, records: list[dict], meta: dict) -> None:
    with (d / "requests.jsonl").open("w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)
    (d / "meta.json").write_text(json.dumps(meta, indent=2))


def start_dir(d: Path) -> bool:
    """False if `d` is finished (has DONE); otherwise empties it for a fresh run."""
    if (d / "DONE").exists():
        log(f"{d} already done; delete it to re-run")
        return False
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return True


def mark_done(d: Path) -> None:
    (d / "DONE").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


# --------------------------------------------------------------- requests ---
@dataclass
class Tenant:
    name: str
    objective: str | None
    fairness_id: str
    prompt_tokens: int
    output_tokens: int
    rates_rps: dict[str, float]  # phase -> requests/s


async def send(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    tenant: Tenant,
    prompt: str,
    phase: str,
    t0: float,
    out: list,
    timeout: float = 600.0,
    **extra,
) -> None:
    """Sends one streaming completion and appends its record to `out`.

    httpx timeouts apply per read, so a stream that keeps trickling chunks
    never trips them; `timeout` bounds the whole request. A request that hits
    it is recorded with status -1 and `timed_out: true`; one cancelled while
    in flight (end of run) with status -1 and `incomplete: true` before the
    cancellation propagates. SSE errors, a missing [DONE], empty output and a
    wrong output length also give status -1; `http_status` keeps the HTTP code.
    """
    headers = {"x-llm-d-inference-fairness-id": tenant.fairness_id}
    if tenant.objective:
        headers["x-llm-d-inference-objective"] = tenant.objective
    body = {
        "model": model,
        "prompt": prompt,
        "max_tokens": tenant.output_tokens,
        "min_tokens": tenant.output_tokens,
        "ignore_eos": True,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    rec = {
        "tenant": tenant.name,
        "objective": tenant.objective,
        "fairness_id": tenant.fairness_id,
        "phase": phase,
        "target_prompt_tokens": tenant.prompt_tokens,
        "target_output_tokens": tenant.output_tokens,
        **extra,
        "status": -1,
    }
    start = time.perf_counter()
    rec["t_send"], rec["wall_send"] = start - t0, time.time()
    first, chunks, complete = None, 0, False
    try:
        async with (
            asyncio.timeout(timeout),
            client.stream("POST", f"{url}/v1/completions", json=body, headers=headers) as r,
        ):
            rec["http_status"] = r.status_code
            if r.status_code != 200:
                rec["status"] = r.status_code
                rec["error_body"] = (await r.aread()).decode(errors="replace")[:300]
                return
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                if line == "data: [DONE]":
                    complete = True
                    break
                ev = json.loads(line[6:])
                if "error" in ev:
                    raise ValueError(f"stream error: {json.dumps(ev['error'])[:250]}")
                if usage := ev.get("usage"):
                    rec["prompt_tokens"], rec["completion_tokens"] = usage["prompt_tokens"], usage["completion_tokens"]
                if ev.get("choices") and ev["choices"][0].get("text"):
                    first = first or time.perf_counter()
                    chunks += 1
            if not complete:
                rec["incomplete"] = True
                raise ValueError("stream ended without [DONE]")
            if first is None:
                raise ValueError("stream completed without any output text")
            # min_tokens == max_tokens and ignore_eos fix the output length. Chunks
            # are not tokens, so only usage (when sent) can check it.
            if rec.get("completion_tokens", tenant.output_tokens) != tenant.output_tokens:
                rec["incomplete"] = True
                raise ValueError(f"expected {tenant.output_tokens} output tokens, got {rec['completion_tokens']}")
            rec["status"] = 200
    except TimeoutError:  # asyncio.timeout's; httpx's per-read timeouts are httpx.TimeoutException
        rec["timed_out"] = True
        rec["error"] = f"TimeoutError: no complete response within {timeout:g}s"
    except asyncio.CancelledError:
        rec["incomplete"] = True
        rec["error"] = "CancelledError: still in flight when the run ended"
        raise
    except Exception as e:  # httpx timeouts, resets (e.g. a node restart), bad streams
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        end = time.perf_counter()
        rec["stream_complete"] = complete
        rec["t_end"], rec["e2e"] = end - t0, end - start
        rec["ttft"] = first - start if first else None
        rec["chunks"] = chunks
        out.append(rec)


# ---------------------------------------------------------------- sampler ---
SAMPLE = re.compile(r"^([^\s{]+)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> list[tuple[str, dict, float]]:
    """The METRICS samples of a Prometheus text page, as (name, labels, value)."""
    rows = []
    for line in text.splitlines():
        if line.startswith(METRICS) and (m := SAMPLE.match(line)) and m[1] in METRICS and m[3] != "NaN":
            rows.append((m[1], dict(LABEL.findall(m[2] or "")), float(m[3])))
    return rows


async def scrape(client: httpx.AsyncClient, url: str) -> list[tuple[str, dict, float]]:
    r = await client.get(url)
    r.raise_for_status()
    return parse_metrics(r.text)


async def sample(path: Path, t0: float, url: str) -> None:
    """Scrapes the EPP once a second into `path` until cancelled."""
    down = False
    async with httpx.AsyncClient(timeout=2.0) as client:
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "source", "name", "labels", "value"])
            while True:
                tick = time.time()
                try:
                    rows = await scrape(client, url)
                except Exception as e:
                    rows = []
                    if not down:
                        log(f"sampler: EPP scrape failed: {e!r}"[:150])
                    down = True
                else:
                    if down:
                        log("sampler: EPP scrape recovered")
                    down = False
                for n, labels, v in rows:
                    w.writerow([f"{tick - t0:.2f}", "epp", n, json.dumps(labels, sort_keys=True), v])
                f.flush()
                await asyncio.sleep(1.0 - (time.time() - tick))


async def prom(client: httpx.AsyncClient, url: str, api: str, **params) -> list[dict]:
    """One Prometheus HTTP API call (`query` or `query_range`): its result list."""
    r = await client.get(f"{url}/api/v1/{api}", params=params)
    r.raise_for_status()
    return r.json()["data"]["result"]


async def append_engine_metrics(path: Path, url: str, t0: float, t1: float) -> None:
    """Appends each vLLM pod's VLLM_METRICS between wall times t0 and t1, from Prometheus."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            for name in VLLM_METRICS:
                query = f"sum by (llm_d_ai_role, pod) ({name}{{{ROLES}}})"
                start = t0
                while start < t1:  # Prometheus caps a range query at 11,000 points
                    end = min(t1, start + 10_000 * PROM_STEP)
                    for s in await prom(client, url, "query_range", query=query, start=start, end=end, step=PROM_STEP):
                        role, pod = s["metric"]["llm_d_ai_role"], json.dumps({"pod": s["metric"]["pod"]})
                        w.writerows([f"{float(ts) - t0:.2f}", role, name, pod, v] for ts, v in s["values"])
                    start = end + PROM_STEP


async def stop(sampler: asyncio.Task) -> None:
    sampler.cancel()
    await asyncio.gather(sampler, return_exceptions=True)


async def wait_quiet(ep: Endpoints, timeout_s: float = 600) -> None:
    """Waits until every engine is idle and the EPP queues are empty, so runs don't overlap."""
    deadline = time.time() + timeout_s
    query = f'sum({{__name__=~"{"|".join(VLLM_METRICS)}",{ROLES}}})'
    async with httpx.AsyncClient(timeout=3.0) as client:
        while time.time() < deadline:
            try:
                epp = await scrape(client, ep.epp_metrics)
                engines = await prom(client, ep.prometheus, "query", query=query)
            except Exception as e:
                log(f"waiting for the EPP /metrics and Prometheus: {type(e).__name__}")
            else:
                if not engines:
                    log("waiting for vLLM series in Prometheus (is pd-pool's monitoring.enabled on?)")
                    await asyncio.sleep(10)
                    continue
                busy = sum(v for n, _, v in epp if n != SATURATION) + float(engines[0]["value"][1])
                if busy == 0:
                    await asyncio.sleep(SETTLE_S)
                    return
                log(f"waiting for the pool to drain (busy={busy:.0f})")
            await asyncio.sleep(10)
    log("pool did not drain in time; continuing anyway")


def log_summary(records: list[dict], phases: list[str]) -> None:
    for phase in phases:
        by: dict[str, list[float]] = {}
        for r in records:
            if r["phase"] == phase and r["status"] == 200:
                by.setdefault(r["tenant"], []).append(r["ttft"])
        for tenant, xs in sorted(by.items()):
            log(
                f"  {phase:>10} {tenant:<18} n={len(xs):5d}  p50 TTFT={percentile(xs, 50) * 1000:8.0f} ms  "
                f"p95={percentile(xs, 95) * 1000:8.0f} ms"
            )
    # How late requests left versus their schedule. A large p99 means the
    # generator, not the router, was the bottleneck.
    lags = [(r["t_send"] - r["t_intended"]) * 1000 for r in records]
    log(
        f"send lag vs schedule: p50 {percentile(lags, 50):.1f} ms, p99 {percentile(lags, 99):.1f} ms, max {max(lags):.1f} ms"
    )


# ------------------------------------------------------------------- run ---
def tenant_rng(seed: int, scenario: str, rep: int, tenant: str) -> random.Random:
    """Arrivals and prompts per tenant. The variant is left out on purpose, so
    every variant of a scenario replays the same arrival sequence (a paired
    comparison); each repeat gets its own."""
    return random.Random(f"{seed}/{scenario}/rep{rep}/{tenant}")


def build_tenants(scenario: dict, calibration: dict) -> list[Tenant]:
    """Scenario tenants with rates in req/s. `rate` is a fraction of the shape's
    calibrated capacity: one number for every phase, or a phase -> number map."""
    phases = [p["name"] for p in scenario["phases"]]
    tenants = []
    for t in scenario["tenants"]:
        shape = t.get("shape", "default")
        cap = calibration["shapes"][shape]["capacity_rps"]
        rate = t["rate"] if isinstance(t["rate"], dict) else dict.fromkeys(phases, t["rate"])
        rates = {ph: cap * rate.get(ph, 0.0) for ph in phases}
        tenants.append(
            Tenant(t["name"], t["objective"], t.get("fairness_id", t["name"]), **SHAPES[shape], rates_rps=rates)
        )
    return tenants


async def run_once(
    ep: Endpoints, scenario: dict, variant: str, rep: int, model: str, calibration: dict, seed: int, outdir: Path
) -> None:
    tenants = build_tenants(scenario, calibration)
    phases = scenario["phases"]
    timeout = float(scenario.get("request_timeout_s", 600))
    log(
        f"{scenario['name']} / {variant} / rep {rep}: {sum(p['duration_s'] for p in phases)}s, tenants: "
        + ", ".join(f"{t.name}({t.objective},{ {k: round(v, 2) for k, v in t.rates_rps.items()} })" for t in tenants)
    )
    await wait_quiet(ep)
    records: list[dict] = []
    tasks: set[asyncio.Task] = set()

    async def arrivals(client: httpx.AsyncClient, tenant: Tenant) -> None:
        """Open-loop Poisson arrivals on an absolute schedule.

        Arrival times are drawn ahead of time (next_t += Exp(rate)) and the loop
        sleeps until each one, so event-loop and scheduling overhead delays a send
        without stretching the gaps after it: a late loop catches up instead of
        lowering the rate. The rate is piecewise constant per phase; by the
        memoryless property, restarting the draw at each phase boundary keeps the
        process exactly Poisson within every phase. Each record carries the
        intended send time (`t_intended`) next to the actual one (`t_send`); the
        analysis measures latency from `t_intended`, so a late send is charged to
        the request instead of disappearing from the numbers.
        """
        rng = tenant_rng(seed, scenario["name"], rep, tenant.name)
        end = 0.0
        for ph in phases:
            next_t, end = end, end + ph["duration_s"]
            rate = tenant.rates_rps[ph["name"]]
            while rate > 0 and (next_t := next_t + rng.expovariate(rate)) < end:
                await asyncio.sleep(t0 + next_t - time.perf_counter())
                prompt = make_prompt(tenant.prompt_tokens, rng)
                task = asyncio.create_task(
                    send(client, ep.router, model, tenant, prompt, ph["name"], t0, records, timeout, t_intended=next_t)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            # Hold until the phase ends (also for rate-0 phases), so the next
            # phase's schedule starts at its own boundary.
            await asyncio.sleep(t0 + end - time.perf_counter())

    t0, wall0 = time.perf_counter(), time.time()
    sampler = asyncio.create_task(sample(outdir / "metrics.csv", wall0, ep.epp_metrics))
    async with http_client(timeout) as client:
        await asyncio.gather(*(arrivals(client, t) for t in tenants))
        log(f"traffic stopped; waiting for {len(tasks)} in-flight requests")
        if tasks:
            # Every request has its own deadline, so this grace period is only a
            # backstop. Stragglers are cancelled; send() records them as incomplete.
            _, pending = await asyncio.wait(list(tasks), timeout=timeout + 30)
            if pending:
                log(f"cancelling {len(pending)} requests still in flight; recorded as incomplete")
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
    wall1 = time.time()
    await asyncio.sleep(5)
    await stop(sampler)
    await append_engine_metrics(outdir / "metrics.csv", ep.prometheus, wall0, time.time())

    meta = {
        "scenario": scenario["name"],
        "variant": variant,
        "rep": rep,
        "model": model,
        "seed": seed,
        "start": wall0,
        "end": wall1,
        "phases": phases,
        "measure_phases": scenario["measure_phases"],
        "tenants": [asdict(t) for t in tenants],
        "calibration": calibration,
        "requests": len(records),
    }
    write_results(outdir, sorted(records, key=lambda r: r["t_send"]), meta)
    log_summary(records, scenario["measure_phases"])


async def run(
    ep: Endpoints,
    scenario_path: Path,
    variant: str,
    model: str,
    calibration_path: Path,
    out: Path,
    repeats: int = 1,
    seed: int = 0,
) -> None:
    """Runs `repeats` repeats of one scenario against whatever router variant is deployed.

    Resumable: repeats with a DONE marker are skipped, so an interrupted run
    picks up where it stopped.
    """
    if not calibration_path.is_file():
        sys.exit(f"{calibration_path} is missing: run `pqbench sweep` and `pqbench calibrate` first (see README)")
    scenario = yaml.safe_load(scenario_path.read_text())
    calibration = json.loads(calibration_path.read_text())
    for rep in range(1, repeats + 1):
        d = out / scenario["name"] / variant / f"rep{rep}"
        if start_dir(d):
            await run_once(ep, scenario, variant, rep, model, calibration, seed, d)
            mark_done(d)


# ----------------------------------------------------------------- sweep ---
SWEEP_LEVELS = {
    "default": [4, 8, 16, 32, 48, 64, 96, 128, 160],
    "prefill-heavy": [2, 4, 8, 16, 24, 32, 48, 64],
    "decode-heavy": [8, 16, 32, 64, 96, 128, 160],
}


def sweep_levels(spec: str | None) -> dict[str, list[int]]:
    """--levels "default:48,prefill-heavy:16" runs only those levels (quick A/B probes)."""
    if not spec:
        return SWEEP_LEVELS
    out: dict[str, list[int]] = {}
    for item in spec.split(","):
        shape, c = item.split(":")
        if shape not in SHAPES:
            sys.exit(f"unknown shape {shape!r}; choose from {sorted(SHAPES)}")
        out.setdefault(shape, []).append(int(c))
    return out


async def closed_loop(
    client, ep: Endpoints, model: str, tenant: Tenant, level: dict, t0: float, rng, out: list
) -> None:
    """One sweep worker: back-to-back requests until the level ends."""
    while (now := time.perf_counter() - t0) < level["measure_end"]:
        phase = "warmup" if now < level["measure_start"] else "measure"
        prompt = make_prompt(tenant.prompt_tokens, rng)
        await send(
            client,
            ep.router,
            model,
            tenant,
            prompt,
            phase,
            t0,
            out,
            shape=level["shape"],
            concurrency=level["concurrency"],
        )


async def sweep(
    ep: Endpoints,
    model: str,
    out: Path,
    levels: str | None = None,
    hold_s: float = 60,
    warmup_s: float = 15,
    seed: int = 0,
) -> None:
    """Closed-loop sweep: for each shape and concurrency level, `c` workers send
    back-to-back requests for warmup_s + hold_s. Run it with the router's
    `calibrate` variant (limits opened up) so it measures the raw pool."""
    if not start_dir(out):
        return
    t0, wall0 = time.perf_counter(), time.time()
    sampler = asyncio.create_task(sample(out / "metrics.csv", wall0, ep.epp_metrics))
    records: list[dict] = []
    levels_meta = []
    async with http_client(600.0) as client:
        for shape, concurrencies in sweep_levels(levels).items():
            tenant = Tenant(f"sweep-{shape}", None, "calibration", **SHAPES[shape], rates_rps={})
            hold = hold_s * (1.5 if shape == "decode-heavy" else 1.0)
            for c in concurrencies:
                await wait_quiet(ep)
                start = time.perf_counter() - t0
                level = {
                    "shape": shape,
                    "concurrency": c,
                    "start": start,
                    "measure_start": start + warmup_s,
                    "measure_end": start + warmup_s + hold,
                }
                done: list[dict] = []
                rngs = [random.Random(f"{seed}/sweep/{shape}/c{c}/w{i}") for i in range(c)]
                await asyncio.gather(*(closed_loop(client, ep, model, tenant, level, t0, rng, done) for rng in rngs))
                ttft = [r["ttft"] for r in done if r["phase"] == "measure" and r["status"] == 200]
                log(
                    f"sweep {shape:<14} c={c:4d}  {len(ttft) / hold:6.2f} req/s  p95 TTFT {percentile(ttft, 95) * 1000:7.0f} ms"
                )
                records += done
                levels_meta.append(level)
    wall1 = time.time()
    await stop(sampler)
    await append_engine_metrics(out / "metrics.csv", ep.prometheus, wall0, time.time())
    meta = {
        "start": wall0,
        "end": wall1,
        "model": model,
        "seed": seed,
        "shapes": SHAPES,
        "levels": levels_meta,
        "hold_s": hold_s,
        "warmup_s": warmup_s,
    }
    write_results(out, records, meta)
    mark_done(out)
