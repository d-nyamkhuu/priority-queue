import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")
def test_roles_share_one_weight_cache_and_size_gpus_from_tp():
    chart = Path(__file__).parents[1] / "charts" / "pd-pool"
    args = ["--set", "decode.replicas=2,decode.tensorParallelSize=4"]
    out = subprocess.check_output(["helm", "template", "t", str(chart), *args], text=True)
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert [d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim"] == ["t-model-cache"]
    roles = {r["name"]: r["spec"] for r in next(d for d in docs if d["kind"] == "DisaggregatedSet")["spec"]["roles"]}
    for role, gpus in (("prefill", "1"), ("decode", "4")):
        pod = roles[role]["leaderWorkerTemplate"]["workerTemplate"]["spec"]
        assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "t-model-cache"
        assert pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == gpus
        assert f"--tensor-parallel-size={gpus}" in pod["containers"][0]["args"]
