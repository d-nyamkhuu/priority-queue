# Findings: llm-d flow control on a P/D-disaggregated pool (2 × RTX PRO 6000)

Measurements and issues found running llm-d v1.0.0 flow control (priority
bands, per-tenant fairness, stage-aware admission) in front of Qwen3.5-9B, served by one
prefill and one decode vLLM pod on separate GPU nodes.

- Run: 2026-11-08 UTC, one repeat of 11 scenario runs. The raw data is under
  [`results/`](../results) and the tables are in [`results/summary.md`](../results/summary.md).
- Figures: [`results/figures/`](../results/figures).
- Numbers are from a single repeat. Differences of a few tens of milliseconds between
  variants are within run-to-run noise. The large gaps (hundreds of ms versus tens of
  seconds) are not.

## Contents

1. [Setup](#1-setup)
2. [Headline results](#2-headline-results)
3. [Pool capacity (calibration)](#3-pool-capacity-calibration)
4. [KV transfer between prefill and decode](#4-kv-transfer-between-prefill-and-decode)
5. [Flow-control behaviours seen in the runs](#5-flow-control-behaviours-seen-in-the-runs)
6. [Serving Qwen3.5 with P/D on vLLM v0.31](#6-serving-qwen35-with-pd-on-vllm-v031)
7. [Open items](#7-open-items)

---

## 1. Setup

| | |
|---|---|
| Cluster | Managed Kubernetes 1.35.7, Cilium (pod MTU 1450) |
| CPU node | 1 × 8 vCPU / 32 GB: router (EPP + Envoy), Prometheus, Grafana, load generator, results |
| GPU nodes | 2 × 1-GPU VMs (24 vCPU, 218 GB): RTX PRO 6000 Blackwell Server Edition (96 GB), driver 580.173.02, CUDA 13.0. One runs prefill, the other decode. No RDMA. |
| llm-d | v1.0.0: `llm-d-router-standalone` chart, EPP, disagg sidecar. GAIE v1.6.2, LWS 1.0.0 (DisaggregatedSet) |
| Engine | vLLM v0.31.0, NIXL 1.4.1 (bundled UCX), `NixlConnector` (pull), UCX over TCP |
| Model | `Qwen/Qwen3.5-9B`, TP 1, `--max-model-len 32768`, `--max-num-seqs 128`, `--max-num-batched-tokens 8192`, `--gpu-memory-utilization 0.90`, prefix caching off |
| Router | Flow control with bands 100 / 0 / −10, round-robin fairness, FCFS ordering, hybrid concurrency detector (`maxConcurrency 64`, `maxTokenConcurrency 30000`, headroom 0.1), priority holdback 1.0 / 0.75 / 0.5, request TTL 90 s |
| Bench | 11 runs, phases warmup 30 s / surge 120 s (measured) / cooldown 60 s. pd-shapes runs 30 / 120 / 30 / 120 / 45 s. Clients don't retry 429s. |
| GPU time | ≈ 4 h 10 min with both GPU nodes up (≈ 8.3 GPU-node-hours), including bring-up, fixes, calibration, diagnostics and the bench (≈ 1 h 20 min) |

## 2. Headline results

All values are surge-phase p95 TTFT unless noted.

| Scenario | What it tests | Flow control on | Flow control off / other variants | Outcome |
|---|---|---|---|---|
| priority-tiers | Bronze (batch) surges to ~1.8× capacity | Platinum 352 ms, Gold 358 ms, Silver 352 ms. Bronze 89.6 s (13.5% served) | Platinum 996 ms, Gold 984 ms, Silver 1,002 ms. Bronze 929 ms, but **59% dropped** with 429 | Protected tiers stay at idle-level TTFT (~250 ms p50). Without flow control they are ~3× slower. |
| consolidation | Two realtime tenants share the pool with a surging standard tenant | Realtime A 360 ms, B 361 ms. Standard C 45.4 s | not run (see [Open items](#7-open-items)) | Realtime keeps its priority. |
| same-priority-fairness | Three realtime tenants; A bursts ~5× | Peers B 537 ms, C 507 ms. Burster A 35.6 s | B 77.7 s, C 78.6 s, A 78.2 s | The burster can't starve its peers' latency, but ~⅓ of the peers' requests were rejected at admission ([§5.2](#52-admission-is-per-band-not-per-tenant)). |
| pd-shapes | Prefill-heavy, then decode-heavy surge; realtime probe p95 | Hybrid: 1.09 s in the prefill-heavy surge, 0.70 s in the decode-heavy surge | Requests detector: 6.65 s / 2.28 s. Tokens detector: 1.06 s / 8.70 s | Hybrid kept probe p95 ≤ 1.1 s in both measured surges ([fig](../results/figures/fig_pd_detector_peaks.png)). |
| admission-tuning | Holdback and cap size, with consolidation traffic | Hybrid with holdback: realtime 419 ms, standard 47.7 s (39% served) | No holdback: realtime 489–567 ms, standard 34.4 s (50% served). cap-tight (0.5×): realtime 578–600 ms, standard 90.6 s (10% served) | Holdback protects realtime at the cost of standard throughput. A cap that is too tight hurts realtime too ([§5.4](#54-a-cap-below-the-top-tiers-own-demand-hurts-the-top-tier)). |

## 3. Pool capacity (calibration)

Closed-loop sweep with the detector limits opened up. The knee is the smallest concurrency
that reaches 90% of peak req/s. Source: [`results/calibration/`](../results/calibration)
([plot](../results/calibration/sweep.png)).

| Shape (in / out tokens) | Knee | Capacity at knee | Peak | p95 TTFT at knee | p50 TPOT at knee | What limits it |
|---|---:|---|---:|---:|---:|---|
| default (512 / 128) | 64 | 23.9 req/s, 14.9k tok/s | 26.4 req/s | 481 ms | 18.1 ms | **KV transfer** (§4). Prefill tensor cores ~28–34% busy at the plateau. |
| prefill-heavy (4096 / 32) | 8 | 6.1 req/s, 23.2k tok/s | 6.2 req/s | 1,150 ms | 13.1 ms | Prefill compute (tensor cores 76% busy). Throughput *falls* past c=16, from 6.2 to 5.6 req/s at c=64. |
| decode-heavy (256 / 1024) | 96 | 4.3 req/s, 4.4k out tok/s | 4.7 req/s | 758 ms | 21.9 ms | Decode batch. TPOT grows linearly from 13 ms (c=8) to 26 ms (c=160). |

- Detector limits come from these knees: `maxConcurrency = 64` (the default knee) and
  `maxTokenConcurrency = 30000` (prefill-heavy knee × mean prompt).
- The earlier sweep, before the UCX fix in §4.2, plateaued at **~5 req/s** for the default
  shape. It was discarded.
- vLLM streamed exactly one token per SSE chunk (`tok_per_chunk = 1.0`), so chunk ITL and
  token-based TPOT agree here.

## 4. KV transfer between prefill and decode

KV transfer, not the GPUs, set the pool's capacity for short prompts. The fixes and
diagnostics below took it from **2.7 Gbit/s to ~14 Gbit/s in production**, plus a
measured path to ~25 Gbit/s. vLLM reports the transfers on the decode side as
`vllm:nixl_xfer_time_seconds` and `vllm:nixl_bytes_transferred`; dividing their rates gives
mean transfer time and Gbit/s (both are on the Grafana dashboard).

### 4.1 Every request moves ~69 MB of KV, however short the prompt

- **Model layout.** Qwen3.5-9B has 32 layers: 24 Gated DeltaNet (Mamba-style) and 8 full
  attention.
- **Page size.** vLLM's hybrid KV manager gives every layer the same page size. Each Mamba
  layer's state is a 2 MiB fp32 SSM state plus 48 KiB of conv state per request, so vLLM
  raised the attention block size to **528 tokens** to match.
- **Result.** One page per layer × 32 layers ≈ 2.06 MiB × 32 ≈ **69 MB** for any prompt up
  to 528 tokens. Measured: 68.8 MB per transfer.
- **Comparison.** An attention-only model of similar size would move ~16 MB for a 512-token
  prompt.

So transfer load scales with request count rather than prompt length. That's why the
request-count detector captures it.

### 4.2 UCX's default TCP segments capped transfers at 2.7 Gbit/s (fixed)

- **Symptom.** The default shape plateaued at 4.8–5.0 req/s with p95 TTFT climbing to
  19.5 s. Both GPUs were mostly idle (4–42% utilization). The decode pod had ~40 requests
  waiting on remote KV, each taking ~9 s to transfer.
- **Cause.** Without RDMA, the decode side's NIXL READ is a GET that UCX emulates over TCP,
  in segment-sized fragments (8 KiB TX / 64 KiB RX by default) staged through host memory.
- **Fix.** `UCX_TCP_TX_SEG_SIZE=1m` and `UCX_TCP_RX_SEG_SIZE=1m` on both pods, as in
  [smg-project/smg#4276](https://github.com/smg-project/smg/pull/4276). Commit `05dfd0b`.

| 48 clients, default shape, 60 s | Before | After |
|---|---:|---:|
| Throughput | 4.87 req/s | **20.42 req/s** |
| p95 TTFT | 9,576 ms | **356 ms** |
| Mean NIXL transfer | 7.22 s | **0.14 s** |

### 4.3 Staging KV in host memory OOM-kills the decode pod

- **What was tried.** `kv_buffer_device: cpu`, to let UCX send plain host buffers.
- **What happened.** vLLM pins a host copy of the **entire** KV cache (~67 GiB here). The
  decode engine was OOMKilled (exit 137) under the 160 Gi pod limit right after allocating
  KV.
- **Status.** Reverted (commit `6eb73d7`). It would need a much smaller KV cache to fit.

### 4.4 The remaining limit is one receive thread (~15–17 Gbit/s)

After the fix in §4.2, the default shape still plateaued at ~26 req/s, with KV moving at
~11–14 Gbit/s. The link itself has far more headroom:

| Pod-to-pod iperf3 (prefill → decode) | Gbit/s |
|---|---:|
| 1 stream | 16.7 |
| 4 streams | 73.2 |
| 8 streams | 121.5 |

Evidence gathered from the live pods without restarting anything:

- **Socket state** (`ss -tinm`):
  - All KV goes over **one** TCP connection (decode engine → prefill `:45101`). It had
    carried 1.58 TB.
  - The decode end shows **~62,000 receiver drops**, with its receive buffer at the 6 MB
    kernel maximum, so the receiving process wasn't draining the socket fast enough.
  - The sender was window-limited only 0.1% of the time. Most retransmits were spurious
    (DSACK) or caused by reordering.
  - Minimum RTT 0.05 ms, rising to 5.5 ms under load (data queueing at the receiver).
- **NIXL configuration.**
  - The UCX plugin in NIXL 1.4.1 defaults to `num_workers = 1`.
  - vLLM v0.31 passes only `num_threads = 4`.
  - Four more UCX connections existed, but each had only completed a handshake.
- **Microbenchmark.** A NIXL microbenchmark run inside the live vLLM containers pulled
  33 × 2 MiB GPU READs shaped like a KV pull.

| UCX workers / threads | 1 in flight | 8 in flight | 32 in flight | Threads doing work |
|---|---:|---:|---:|---|
| 1 / 4 (as vLLM runs today) | 14.7 | 16.6 | 14.7 | 1 |
| 1 / 0 | 15.2 | 16.2 | 15.6 | 1 |
| 2 / 4 | 15.7 | 16.3 | 15.2 | 1 |
| 4 / 4 | 14.3 | 15.7 | 14.9 | 1 |
| 8 / 8 | 17.7 | 15.0 | 15.9 | 1 (18 TCP connections, still one busy thread) |

Values are in Gbit/s. In every configuration exactly one transport thread on the receiving
side sat at ~99.5% CPU while the rest idled. The one-thread ceiling (15–17 Gbit/s) matches
a single iperf3 stream: one core's worth of TCP receive on these VMs. vLLM reaches about
85% of it.

**Root cause.**

- NIXL's UCX thread pool splits a transfer across its threads (each with its own UCX worker
  and connection) only when the transfer has at least
  `max(num_threads, split_batch_size)` descriptors.
- `split_batch_size` defaults to **1024** (4096 with SGL). See
  [ai-dynamo/nixl#2329](https://github.com/ai-dynamo/nixl/pull/2329) and
  [#2523](https://github.com/ai-dynamo/nixl/pull/2523).
- A Qwen3.5 KV pull here has about **33** descriptors, so it never splits.

**Measured fix** (microbenchmark only, not yet applied to vLLM):

| UCX workers / threads, extra param | 1 in flight | 8 in flight | Threads doing work |
|---|---:|---:|---|
| 1 / 4 (baseline) | 15.4 | 13.8 | 1 |
| 1 / 4, `split_batch_size=4` | 15.9 | **24.8** | 5 (55–99% each) |
| 4 / 4, `split_batch_size=4` | 17.2 | 22.5 | 5 |
| 1 / 8, `split_batch_size=8` | 20.0 | **25.7** | 7 |

Values are in Gbit/s.

- With 8 in flight, mean transfer time drops from 0.32 s to 0.18 s.
- The next ceiling is ~25 Gbit/s: one thread, probably NIXL's main progress or notification
  path, still sits at ~99%.

**How to apply it.**

- vLLM v0.31 cannot pass `split_batch_size`.
- [vllm-project/vllm#62905](https://github.com/vllm-project/vllm/pull/62905) adds a
  `ucx_backend_params` key for this. It merged to `main` on 2026-10-29, but the v0.33.0
  release notes don't mention it.
- vLLM v0.33.0 requires NIXL 1.5. NIXL 1.5 adds per-transfer UCX worker selection, but
  vLLM doesn't use it, and none of v0.33's KV-connector changes target this limit.
- A ~15-line `sitecustomize` hook (mounted from a ConfigMap) could inject the parameter
  into vLLM v0.31. The image already ships Ubuntu's own
  `/usr/lib/python3.12/sitecustomize.py`, so the hook must load that first.
- Applying the hook changes pool capacity, so it needs a recalibration.

### 4.5 How much the transfer limit distorts the results

| Sweep level | Mean KV transfer | KV rate |
|---|---:|---:|
| default, c=4 (idle) | 0.085 s | 0.9 Gbit/s |
| default, c=64 (router limit) | **0.138 s** | 13.0 Gbit/s |
| default, c=128 | **2.12 s** | 14.2 Gbit/s |
| prefill-heavy, c=16 | 1.14 s | 7.0 Gbit/s |

- **Priority and fairness results: little distortion.** With flow control on, the router
  admits work only up to the calibrated limits. There KV transfer adds ~50 ms over idle,
  against a p95 TTFT of ~480 ms.
- **Absolute capacity: specific to this deployment.** The default-shape capacity (~26 req/s)
  is set by the transport, not by llm-d or the GPUs.
- **Runs past the limit look worse than they would on a better-networked pool.** Past
  c≈96–128, KV transfers wait in UCX's priority-blind first-come-first-served queue for
  ~2 s, beneath vLLM's own queue. That affects flow control off, loose caps, and the
  overloaded tenants' tails.
- **Long prompts near the token limit.** Their TTFT includes ~1 s of transfer queueing.

## 5. Flow-control behaviours seen in the runs

### 5.1 Without flow control, sheddable traffic is dropped, not queued

In priority-tiers with flow control off, 1,924 of 3,273 Bronze surge requests (59%) got
**429 `system saturated, sheddable request dropped`**. The EPP still sheds negative-priority
requests when the pool is saturated, even with the flow-control gate off. So "flow control
off" means *batch is thrown away, and everyone else shares vLLM's FIFO queue at ~1 s p95*.
It does not mean *everyone queues together*.

### 5.2 Admission is per band, not per tenant

- **What happened.** In same-priority-fairness with flow control on, about a third of
  **every** tenant's requests got **429 `request rejected pre-queue: queue at capacity`**.
  That's A: 1,281 / 3,844, B: 236 / 708, C: 247 / 694.
- **Why.** A's 5× burst kept the shared realtime band at `maxRequests: 512`, and a full band
  rejects new requests from any tenant.
  - Round-robin fairness orders dispatch among *admitted* requests. That's why B and C's
    admitted requests ran at ~0.5 s p95 while A's took 35.6 s.
  - Admission capacity is shared within the band.
- **Documentation.** llm-d's docs describe this behaviour: limits are global and per band;
  a full band "drops [requests] immediately (HTTP 429) regardless of that band's priority".
  Clients are expected to retry with backoff, as `llm-d-async` does, and the load generator
  here doesn't retry.
- **Implication.** Size each band for its expected burst (here roughly rate × TTL ≈ 1,500
  requests). Otherwise well-behaved tenants share the 429s. Per-tenant admission limits
  don't exist in v1.0.

### 5.3 The request TTL caps the tail of overloaded tenants

Several overloaded tenants show p95 at ~89–91 s:
- Bronze in priority-tiers
- standard-c under cap-tight
- prefill-heavy and decode-heavy under the requests and hybrid detectors

That's the 90 s `defaultRequestTTL`: requests still queued at 90 s are evicted with 429
`request TTL expired`. Those p95s are TTL-capped waits, not service latencies. Read them
together with the success column in [`results/summary.md`](../results/summary.md).

### 5.4 A cap below the top tier's own demand hurts the top tier

- **cap-tight** halves `maxConcurrency` to 32 in-flight requests.
- The two realtime tenants alone need about that much: ~12 req/s × ~2.6 s end to end.
- So realtime queued at the router too, and its p95 rose from 419 ms (hybrid) to
  578–600 ms.
- Standard-c was mostly evicted (10% served).

A cap can only protect a tier whose own demand fits under it.

### 5.5 Holdback trades low-priority throughput for realtime headroom

| admission-tuning | Realtime p95 | Standard-c p95 | Standard-c served |
|---|---:|---:|---:|
| hybrid (holdback 1.0 / 0.75 / 0.5) | 419 ms | 47.7 s | 39% (1,391 requests) |
| no-holdback | 489–567 ms | 34.4 s | 50% (1,824 requests) |

### 5.6 Detectors: hybrid protected the probe in both measured surges

| pd-shapes detector | Peak saturation, prefill / decode surge | Realtime probe p95, prefill / decode surge |
|---|---|---|
| requests (in-flight count) | 0.81 / 0.88 | **6.65 s** / 2.28 s |
| tokens (in-flight prompt tokens) | 1.07 / 0.93 | 1.06 s / **8.70 s** |
| hybrid | 1.06 / 1.02 | **1.09 s / 0.70 s** |

- **Observation:** hybrid kept probe p95 at 1.09 s and 0.70 s. Request-only admission
  performed worse during the prefill surge; token-only admission during the decode surge.
- **Thresholds:** surging traffic is standard (ceiling 0.75); the realtime probe has ceiling
  1.0. All reported peaks exceed 0.75. A peak below 1.0 therefore does not establish that
  standard traffic was never held back. The policy compares saturation with each band's
  own ceiling ([upstream policy](https://github.com/llm-d/llm-d-router/blob/v1.0.0/pkg/epp/framework/plugins/flowcontrol/usagelimits/priorityholdback/README.md)).
- **Interpretation:** the latency pattern is consistent with request counts underrepresenting
  long-prompt work and prompt-token counts underrepresenting long generations. Peak values
  alone do not establish the timing or duration of dispatch blocking, or prove its cause.

## 6. Serving Qwen3.5 with P/D on vLLM v0.31

- **`VLLM_SSM_CONV_STATE_LAYOUT=DS` is required.** Without it, both engines fail at KV-cache
  init with `AssertionError: 3-read Mamba conv transfer requires DS conv state layout`. The
  upstream P/D guide doesn't set it, because its model has no Mamba layers.
- **Other required flags:** `--no-disable-hybrid-kv-cache-manager` and
  `--language-model-only`, with the same TP and block size on both roles. vLLM picks the
  528-token attention block automatically (§4.1).
- **Startup.** The engine takes ~3–4 min to start (weights already cached on the node). After
  a node restart it took ~7 min, because torch.compile ran again.
- **Readiness probe.** The decode pod's startup probe polls every 30 s, so the pod shows
  1/2 Ready for up to 30 s after vLLM logs `Application startup complete`.

## 7. Open items

1. **Apply `split_batch_size` in vLLM.** Use a `sitecustomize` hook, or a vLLM build with
   `ucx_backend_params`. Then recalibrate and rerun. Expected: a higher default-shape
   capacity and less transfer queueing in overloaded runs.
2. **Fairness with a band sized for the burst.** Set realtime `maxRequests` to ~1,500, or
   make clients retry 429s with backoff. This shows whether peers stop sharing the 429s.
   About 8 min of GPU time, best done in the same session as item 1.
3. **More repeats** if any single-run result needs confirming (`pqbench run --repeats N`).
4. **Consolidation without flow control.** Dropped from this run because no figure uses it.
   Add it back for a direct on/off comparison.
