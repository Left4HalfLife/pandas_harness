"""Network-disabled analysis worker. Never give this service application secrets."""

import importlib
import io
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from harness.results import encode_result  # noqa: E402


def execute(input_path, output_path):
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    payload = json.loads(Path(input_path).read_text())
    with tempfile.TemporaryDirectory(prefix="analysis-") as directory:
        os.chdir(directory)
        frame = pd.read_json(io.StringIO(payload["frame"]), orient="table")
        environment = {"pd": pd, "np": np, "plt": plt, "dfs": [frame], "df": frame}
        for dependency in payload["dependencies"]:
            module = importlib.import_module(dependency["module"])
            name = dependency["name"]
            environment[dependency["alias"]] = (
                getattr(module, name) if name != dependency["module"] else module
            )
        code = re.sub(
            r"""(['"])([^'"]*\.png)\1""",
            lambda match: repr(str(Path(directory) / "chart.png")),
            payload["code"],
        )
        exec(compile(code, "<analysis>", "exec"), environment)
        result = encode_result(environment.get("result"), directory)
    Path(output_path).write_text(json.dumps(result, allow_nan=False))


def serve():
    root = Path(os.getenv("JOB_DIR", "/jobs"))
    root.mkdir(exist_ok=True)
    print("Analysis worker ready", flush=True)
    while True:
        Path("/tmp/worker-heartbeat").touch()
        for source in sorted(root.glob("job-*/input.json")):
            claimed = source.with_name("running.json")
            try:
                source.rename(claimed)
            except FileNotFoundError:
                continue
            output = source.with_name("output.tmp")
            process = subprocess.Popen(
                [sys.executable, __file__, "execute", str(claimed), str(output)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": "/tmp", "MPLCONFIGDIR": "/tmp/matplotlib",
                    "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                },
            )
            failed = False
            try:
                failed = process.wait(timeout=30) != 0
            except subprocess.TimeoutExpired:
                failed = True
            finally:
                # Also stop descendants left behind by generated code.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            if not source.parent.exists():
                continue
            if failed or not output.exists():
                output.write_text(json.dumps({"error": "Analysis execution failed"}))
                print("Analysis job failed", flush=True)
            output.rename(source.with_name("output.json"))
        time.sleep(0.2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "execute":
        execute(sys.argv[2], sys.argv[3])
    else:
        serve()
