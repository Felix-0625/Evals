import json
import os
import subprocess
import sys

import psutil
import pytest

from evals_app.fixtures import fixture_plan
from evals_app.service import Service


def test_native_proxy_rejects_a_foreign_process_before_opening_http(tmp_path, monkeypatch):
    service = Service(tmp_path, background=False)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        plan = fixture_plan()
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="exp")
        experiment_id = created["id"]
        job = tmp_path / "owned-job.json"
        job.write_text("{}", "utf-8")
        job.with_suffix(".identity.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "create_time": psutil.Process().create_time(),
                    "job_file": str(job.resolve()),
                }
            ),
            "utf-8",
        )
        service.runtime.processes[experiment_id] = process
        service.runtime.patch(
            experiment_id,
            worker={
                "pid": process.pid,
                "create_time": psutil.Process(process.pid).create_time(),
                "job_file": str(job.resolve()),
            },
        )
        monkeypatch.setattr(
            "evals_app.runtime.httpx.Client",
            lambda **kwargs: pytest.fail("HTTP opened for a foreign process"),
        )
        assert service.runtime.discover(experiment_id) is None
        with pytest.raises(RuntimeError, match="owned worker"):
            service.runtime.native(experiment_id, "cancel")
        assert psutil.Process().is_running()
    finally:
        service.runtime.reap(process)
        service.runtime.processes.clear()
        service.close()
