"""End to end: pqbench run / sweep against a fake pool on localhost, then the analysis."""

import asyncio
import json

import httpx
import pytest
import yaml

from pqbench import analyze, loadgen

from .fake_pool import FakePool

SCENARIO = {
    "name": "tiny",
    "variants": ["a", "b"],
    "phases": [{"name": "warmup", "duration_s": 0.5}, {"name": "surge", "duration_s": 1.5}],
    "measure_phases": ["surge"],
    "tenants": [
        {"name": "rt", "objective": "realtime", "rate": {"warmup": 0.4, "surge": 1.0}},
        {"name": "bulk", "objective": "batch", "rate": {"surge": 0.6}},
    ],
}


@pytest.fixture
def ep(monkeypatch):
    monkeypatch.setattr(loadgen, "SETTLE_S", 0.0)
    with FakePool() as pool:
        yield loadgen.Endpoints(pool.url, f"{pool.url}/metrics", pool.url)


def test_run_is_paired_across_variants_and_reported(tmp_path, ep):
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(SCENARIO))
    (tmp_path / "cal.json").write_text(json.dumps({"shapes": {"default": {"capacity_rps": 10.0}}}))
    out = tmp_path / "results"
    recs = {}
    for v in ("a", "b"):
        asyncio.run(loadgen.run(ep, tmp_path / "s.yaml", v, "m", tmp_path / "cal.json", out))
        lines = (out / "tiny" / v / "rep1" / "requests.jsonl").read_text().splitlines()
        recs[v] = [json.loads(line) for line in lines]
    # Same seed: both variants replay the same arrival schedule.
    assert [(r["tenant"], r["t_intended"]) for r in recs["a"]] == [(r["tenant"], r["t_intended"]) for r in recs["b"]]
    assert {r["status"] for r in recs["a"] if r["tenant"] == "rt"} == {200}
    assert {r["status"] for r in recs["a"] if r["tenant"] == "bulk"} == {429}

    # EPP sampled directly, each vLLM pod from Prometheus.
    sources = {
        line.split(",")[1] for line in (out / "tiny" / "a" / "rep1" / "metrics.csv").read_text().splitlines()[1:]
    }
    assert sources == {"epp", "prefill", "decode"}

    analyze.report(out)
    assert "| a | rt | realtime |" in (out / "summary.md").read_text()


def test_sweep_and_calibrate(tmp_path, ep):
    out = tmp_path / "calibration"
    asyncio.run(loadgen.sweep(ep, "m", out, levels="default:2,default:4,prefill-heavy:2", hold_s=0.6, warmup_s=0.2))
    analyze.calibrate(out)
    assert json.loads((out / "calibration.json").read_text())["shapes"]["default"]["capacity_rps"] > 0


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ('data: {"choices": [{"text": "x"}]}\n\ndata: [DONE]\n\n', 200),
        ('data: {"choices": [{"text": "x"}]}\n\n', -1),  # no [DONE]
        ('data: {"error": {"message": "engine failed"}}\n\ndata: [DONE]\n\n', -1),
    ],
)
def test_send_fails_incomplete_streams(body, status):
    async def go():
        out = []
        tenant = loadgen.Tenant("t", "realtime", "t", 1, 1, {})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))) as c:
            await loadgen.send(c, "http://pool", "m", tenant, "p", "surge", 0.0, out)
        return out[0]

    assert asyncio.run(go())["status"] == status
