import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from harness import Workspace
from harness.analysis import analyze, run_job
from harness.results import validate_result

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("code,kind,expected", [
    ("result = {'type': 'number', 'value': dfs[0]['sales'].sum()}", "text", "50"),
    ("result = {'type': 'dataframe', 'value': dfs[0].groupby('region').sum().reset_index()}", "table", None),
    ("import matplotlib.pyplot as plt\n"
     "dfs[0].plot.bar(x='region', y='sales')\n"
     "plt.savefig('/tmp/example.png')\n"
     "result = {'type': 'plot', 'value': '/tmp/example.png'}", "image", None),
])
def test_real_pandasai_generation_and_worker(tmp_path, provider, code, kind, expected):
    provider["code"] = code
    settings = Workspace(endpoint=provider["url"], key="isolated-secret", model="test-model")
    stop = threading.Event()
    failures = []

    def worker():
        while not stop.is_set():
            for source in tmp_path.glob("job-*/input.json"):
                claimed = source.with_name("claimed.json")
                source.rename(claimed)
                assert "isolated-secret" not in claimed.read_text()
                result = subprocess.run(
                    [sys.executable, str(ROOT / "worker.py"), "execute",
                     str(claimed), str(source.with_name("output.tmp"))],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode:
                    failures.append(result.stderr)
                    source.with_name("output.tmp").write_text(json.dumps({"error": result.stderr}))
                source.with_name("output.tmp").rename(source.with_name("output.json"))
            time.sleep(0.02)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        result = analyze(
            pd.DataFrame({"region": ["North", "South"], "sales": [20, 30]}),
            "Analyze the sales data", [], settings, tmp_path,
        )
        assert not failures, "\n".join(failures)
        assert result["type"] == kind
        if expected:
            assert result["value"] == expected
        if kind == "table":
            assert result["value"]["data"] == [["North", 20], ["South", 30]]
        assert list(tmp_path.iterdir()) == []
    finally:
        stop.set()
        thread.join()
    assert not failures, "\n".join(failures)


def test_worker_timeout_cleans_job(tmp_path):
    with pytest.raises(TimeoutError):
        run_job("result = 1", [], pd.DataFrame({"a": [1]}), tmp_path, timeout=0.1)
    assert list(tmp_path.iterdir()) == []


def test_provider_cannot_echo_key_into_pandasai_logs(tmp_path, provider, capsys, caplog):
    provider["status"] = 401
    settings = Workspace(endpoint=provider["url"], key="must-not-log-this-key", model="test-model")
    with pytest.raises(RuntimeError, match="generate"):
        analyze(pd.DataFrame({"sales": [1]}), "Total sales?", [], settings, tmp_path)
    captured = capsys.readouterr()
    assert "must-not-log-this-key" not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("result", [
    {"type": "html", "value": "<script>alert(1)</script>"},
    {"type": "image", "value": "bm90IGEgcG5n"},
    {"type": "table", "value": {"columns": ["a"], "data": [[1, 2]]}},
    {"type": "table", "value": {"columns": ["a"], "data": [[{}]]}},
    {"type": "table", "value": {"columns": ["a"], "data": [[float("inf")]]}},
])
def test_invalid_worker_output_is_rejected(result):
    with pytest.raises((ValueError, OSError)):
        validate_result(result)
