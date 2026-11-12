# Benchmark summary

Measured-phase statistics per tenant, each the median across valid repeats (n = number of repeats). TTFT and E2E are measured from each request's scheduled send time when the run recorded one (`t_intended`), otherwise from the actual send. TTFT, TPOT, decode and E2E percentiles count served requests only (HTTP 200): rejected requests have no TTFT. *within SLO* is the share of all sent requests that were served with TTFT <= 2,000 ms, so rejections count against it. *served req/s* counts every served request; *goodput req/s* counts only those within the SLO.

## admission-tuning

**surge**

| variant | tenant | objective | p95 TTFT ms (served only) | p50 TTFT ms | p95 TPOT ms | decode tok/s p50 | p95 E2E s | success | within SLO | served req/s | goodput req/s | output tok/s | n |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cap-tight | realtime-a | realtime | 600 | 253 | 15.4 | 68.0 | 2.50 | 100.0% | 100.0% | 6.22 | 6.22 | 796 | 1 |
| cap-tight | realtime-b | realtime | 578 | 254 | 15.4 | 68.0 | 2.46 | 100.0% | 100.0% | 6.06 | 6.06 | 775 | 1 |
| cap-tight | standard-c | standard | 90,572 | 86,537 | 15.1 | 69.3 | 92.40 | 10.2% | 0.0% | 3.04 | 0.00 | 389 | 1 |
| hybrid | realtime-a | realtime | 419 | 262 | 18.9 | 61.0 | 2.73 | 100.0% | 100.0% | 6.19 | 6.19 | 793 | 1 |
| hybrid | realtime-b | realtime | 419 | 261 | 19.1 | 60.9 | 2.73 | 100.0% | 100.0% | 6.09 | 6.09 | 780 | 1 |
| hybrid | standard-c | standard | 47,716 | 43,644 | 19.3 | 61.1 | 49.82 | 39.0% | 0.6% | 11.59 | 0.17 | 1,484 | 1 |
| no-holdback | realtime-a | realtime | 489 | 320 | 22.0 | 55.6 | 3.18 | 100.0% | 100.0% | 6.33 | 6.33 | 811 | 1 |
| no-holdback | realtime-b | realtime | 567 | 328 | 22.3 | 55.6 | 3.16 | 100.0% | 100.0% | 6.05 | 6.05 | 774 | 1 |
| no-holdback | standard-c | standard | 34,430 | 31,208 | 20.8 | 55.8 | 36.70 | 49.7% | 1.0% | 15.20 | 0.32 | 1,946 | 1 |

Peak EPP pool saturation in the measured phase (median over repeats):

|                          |   peak_saturation_decode |   peak_saturation_effective |   peak_saturation_prefill |
|:-------------------------|-------------------------:|----------------------------:|--------------------------:|
| ('cap-tight', 'surge')   |                    1     |                       1     |                     0.352 |
| ('hybrid', 'surge')      |                    0.875 |                       0.875 |                     0.634 |
| ('no-holdback', 'surge') |                    1     |                       1     |                     0.88  |

## consolidation

**surge**

| variant | tenant | objective | p95 TTFT ms (served only) | p50 TTFT ms | p95 TPOT ms | decode tok/s p50 | p95 E2E s | success | within SLO | served req/s | goodput req/s | output tok/s | n |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid | realtime-a | realtime | 360 | 261 | 17.9 | 61.5 | 2.57 | 100.0% | 100.0% | 5.97 | 5.97 | 764 | 1 |
| hybrid | realtime-b | realtime | 361 | 264 | 18.0 | 61.5 | 2.59 | 100.0% | 100.0% | 5.75 | 5.75 | 736 | 1 |
| hybrid | standard-c | standard | 45,395 | 41,177 | 17.4 | 61.5 | 47.46 | 41.6% | 0.7% | 12.45 | 0.21 | 1,594 | 1 |

Peak EPP pool saturation in the measured phase (median over repeats):

|                     |   peak_saturation_decode |   peak_saturation_effective |   peak_saturation_prefill |
|:--------------------|-------------------------:|----------------------------:|--------------------------:|
| ('hybrid', 'surge') |                    0.922 |                       0.922 |                     0.422 |

## pd-shapes

**prefill-surge**

| variant | tenant | objective | p95 TTFT ms (served only) | p50 TTFT ms | p95 TPOT ms | decode tok/s p50 | p95 E2E s | success | within SLO | served req/s | goodput req/s | output tok/s | n |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid | prefill-heavy | standard | 88,690 | 36,402 | 16.7 | 78.4 | 89.21 | 86.7% | 1.5% | 8.45 | 0.14 | 270 | 1 |
| hybrid | realtime-probe | realtime | 1,090 | 827 | 13.1 | 78.3 | 2.72 | 100.0% | 100.0% | 2.38 | 2.38 | 305 | 1 |
| requests | prefill-heavy | standard | 89,926 | 44,859 | 16.7 | 78.0 | 90.44 | 84.3% | 1.7% | 8.20 | 0.17 | 262 | 1 |
| requests | realtime-probe | realtime | 6,652 | 5,296 | 14.0 | 77.4 | 8.33 | 100.0% | 2.1% | 2.41 | 0.05 | 308 | 1 |
| tokens | prefill-heavy | standard | 71,629 | 39,234 | 25.5 | 78.3 | 72.42 | 94.0% | 2.8% | 8.93 | 0.27 | 286 | 1 |
| tokens | realtime-probe | realtime | 1,058 | 842 | 13.6 | 78.6 | 2.71 | 100.0% | 100.0% | 2.53 | 2.53 | 324 | 1 |

**decode-surge**

| variant | tenant | objective | p95 TTFT ms (served only) | p50 TTFT ms | p95 TPOT ms | decode tok/s p50 | p95 E2E s | success | within SLO | served req/s | goodput req/s | output tok/s | n |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid | decode-heavy | standard | 90,392 | 71,273 | 16.8 | 59.8 | 107.49 | 65.2% | 3.3% | 4.43 | 0.23 | 4,540 | 1 |
| hybrid | realtime-probe | realtime | 701 | 214 | 17.0 | 59.7 | 2.73 | 100.0% | 100.0% | 2.33 | 2.33 | 298 | 1 |
| requests | decode-heavy | standard | 90,367 | 71,035 | 16.8 | 59.8 | 107.41 | 63.7% | 0.0% | 4.37 | 0.00 | 4,471 | 1 |
| requests | realtime-probe | realtime | 2,282 | 248 | 17.0 | 59.8 | 4.30 | 100.0% | 94.0% | 2.38 | 2.23 | 304 | 1 |
| tokens | decode-heavy | standard | 52,582 | 29,497 | 25.7 | 40.1 | 75.76 | 100.0% | 1.9% | 6.97 | 0.13 | 7,142 | 1 |
| tokens | realtime-probe | realtime | 8,702 | 3,704 | 26.5 | 40.1 | 11.89 | 100.0% | 36.9% | 2.17 | 0.80 | 277 | 1 |

Peak EPP pool saturation in the measured phase (median over repeats):

|                               |   peak_saturation_decode |   peak_saturation_effective |   peak_saturation_prefill |
|:------------------------------|-------------------------:|----------------------------:|--------------------------:|
| ('hybrid', 'decode-surge')    |                    1.021 |                       1.021 |                     1.021 |
| ('hybrid', 'prefill-surge')   |                    1.056 |                       1.056 |                     1.056 |
| ('requests', 'decode-surge')  |                    0.875 |                       0.875 |                     0.688 |
| ('requests', 'prefill-surge') |                    0.812 |                       0.812 |                     0.797 |
| ('tokens', 'decode-surge')    |                    0.933 |                       0.933 |                     0.933 |
| ('tokens', 'prefill-surge')   |                    1.074 |                       1.074 |                     1.074 |

## priority-tiers

**surge**

| variant | tenant | objective | p95 TTFT ms (served only) | p50 TTFT ms | p95 TPOT ms | decode tok/s p50 | p95 E2E s | success | within SLO | served req/s | goodput req/s | output tok/s | n |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid | bronze | batch | 89,639 | 79,167 | 16.4 | 66.2 | 91.55 | 13.5% | 0.0% | 3.81 | 0.00 | 487 | 1 |
| hybrid | gold | realtime | 358 | 254 | 16.7 | 65.4 | 2.41 | 100.0% | 100.0% | 3.52 | 3.52 | 450 | 1 |
| hybrid | platinum | realtime | 352 | 254 | 16.9 | 65.3 | 2.42 | 100.0% | 100.0% | 3.95 | 3.95 | 506 | 1 |
| hybrid | silver | standard | 352 | 247 | 16.9 | 65.3 | 2.42 | 100.0% | 100.0% | 6.75 | 6.75 | 864 | 1 |
| no-flow-control | bronze | batch | 929 | 556 | 22.1 | 54.0 | 3.95 | 41.2% | 41.2% | 11.24 | 11.24 | 1,439 | 1 |
| no-flow-control | gold | realtime | 984 | 608 | 23.8 | 53.8 | 4.10 | 100.0% | 100.0% | 3.62 | 3.62 | 463 | 1 |
| no-flow-control | platinum | realtime | 996 | 606 | 21.1 | 54.2 | 3.82 | 100.0% | 100.0% | 3.61 | 3.61 | 462 | 1 |
| no-flow-control | silver | standard | 1,002 | 608 | 27.0 | 53.6 | 4.19 | 100.0% | 100.0% | 7.08 | 7.08 | 906 | 1 |

Peak EPP pool saturation in the measured phase (median over repeats):

|                              |   peak_saturation_decode |   peak_saturation_effective |   peak_saturation_prefill |
|:-----------------------------|-------------------------:|----------------------------:|--------------------------:|
| ('hybrid', 'surge')          |                    0.844 |                       0.844 |                     0.317 |
| ('no-flow-control', 'surge') |                  nan     |                     nan     |                   nan     |

## same-priority-fairness

**surge**

| variant | tenant | objective | p95 TTFT ms (served only) | p50 TTFT ms | p95 TPOT ms | decode tok/s p50 | p95 E2E s | success | within SLO | served req/s | goodput req/s | output tok/s | n |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid | realtime-a | realtime | 35,628 | 28,528 | 20.4 | 56.1 | 38.17 | 66.7% | 1.0% | 21.36 | 0.33 | 2,734 | 1 |
| hybrid | realtime-b | realtime | 537 | 363 | 19.4 | 56.4 | 2.90 | 66.7% | 66.7% | 3.93 | 3.93 | 503 | 1 |
| hybrid | realtime-c | realtime | 507 | 357 | 19.5 | 56.1 | 2.86 | 64.4% | 64.4% | 3.73 | 3.73 | 477 | 1 |
| no-flow-control | realtime-a | realtime | 78,185 | 45,953 | 39.2 | 28.0 | 82.93 | 100.0% | 1.8% | 30.02 | 0.55 | 3,843 | 1 |
| no-flow-control | realtime-b | realtime | 77,737 | 44,853 | 39.2 | 28.0 | 82.51 | 100.0% | 0.9% | 6.23 | 0.06 | 798 | 1 |
| no-flow-control | realtime-c | realtime | 78,629 | 48,903 | 39.5 | 28.0 | 83.31 | 100.0% | 0.6% | 5.92 | 0.03 | 757 | 1 |

Peak EPP pool saturation in the measured phase (median over repeats):

|                              |   peak_saturation_decode |   peak_saturation_effective |   peak_saturation_prefill |
|:-----------------------------|-------------------------:|----------------------------:|--------------------------:|
| ('hybrid', 'surge')          |                        1 |                           1 |                     0.493 |
| ('no-flow-control', 'surge') |                      nan |                         nan |                   nan     |

