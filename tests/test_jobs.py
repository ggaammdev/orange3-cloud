import json
import pickle
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from orangecontrib.cloud.protocol.errors import ErrorCode
from orangecontrib.cloud.protocol.jobs import JobInfo, JobKind, JobResult, JobState
from orangecontrib.cloud.protocol.pickles import PICKLE_PROTOCOL
from orangecontrib.cloud.protocol.wire import (
    JSON_CONTENT_TYPE,
    PAYLOAD_HEADER,
    PAYLOAD_PICKLE,
    PICKLE_CONTENT_TYPE,
    SPEC_PART,
)
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PICKLE_HEADERS = {PAYLOAD_HEADER: PAYLOAD_PICKLE}


@pytest.fixture(scope="module")
def client():
    # One small cluster for the whole module; the out-of-memory and crash
    # tests run last because they restart the worker.
    config = ServerConfig(
        token=TOKEN,
        worker_threads=2,
        worker_memory_bytes=512 * 1024**2,
        debug_operations=True,
    )
    with TestClient(create_machine_app(config)) as client:
        yield client


def post_job(client, spec, files=None):
    files = {SPEC_PART: (None, json.dumps(spec), JSON_CONTENT_TYPE), **(files or {})}
    return client.post("/v1/jobs", headers=AUTH, files=files)


def start_job(client, kind, params=None, inputs=None, parts=None):
    files = {
        name: (
            name,
            pickle.dumps(obj, protocol=PICKLE_PROTOCOL),
            PICKLE_CONTENT_TYPE,
            PICKLE_HEADERS,
        )
        for name, obj in (parts or {}).items()
    }
    spec = {"kind": kind, "params": params or {}, "inputs": inputs or {}, "parts": list(files)}
    r = post_job(client, spec, files)
    assert r.status_code == 202, r.text
    return JobInfo.model_validate(r.json())


def wait(client, job_id, timeout=30, seen=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = JobInfo.model_validate(client.get(f"/v1/jobs/{job_id}", headers=AUTH).json())
        if seen is not None:
            seen.append(info)
        if info.state.finished:
            return info
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} didn't finish in {timeout} s")


def test_sleep_reports_progress(client):
    job = start_job(client, "sleep", {"seconds": 1.0, "steps": 5})
    assert job.state == JobState.QUEUED
    seen = []
    info = wait(client, job.id, seen=seen)
    assert info.state == JobState.DONE
    assert info.progress == 1.0
    assert any(
        s.state == JobState.RUNNING and 0 < s.progress < 1 and s.status.startswith("Step")
        for s in seen
    )
    result = JobResult.model_validate(client.get(f"/v1/jobs/{job.id}/result", headers=AUTH).json())
    assert result.values == {"seconds": 1.0, "steps": 5}


def test_sleep_can_be_cancelled(client):
    job = start_job(client, "sleep", {"seconds": 60, "steps": 600})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        info = JobInfo.model_validate(client.get(f"/v1/jobs/{job.id}", headers=AUTH).json())
        if info.progress > 0:
            break
        time.sleep(0.05)
    assert info.state == JobState.RUNNING
    assert client.get("/v1/info", headers=AUTH).json()["jobs_running"] == 1

    r = client.delete(f"/v1/jobs/{job.id}", headers=AUTH)
    assert r.status_code == 200
    assert JobInfo.model_validate(r.json()).state == JobState.CANCELLED
    info = wait(client, job.id)
    assert info.state == JobState.CANCELLED
    assert info.error.code == ErrorCode.CANCELLED
    r = client.get(f"/v1/jobs/{job.id}/result", headers=AUTH)
    assert r.status_code == 409
    assert r.json()["code"] == "cancelled"
    assert client.get("/v1/info", headers=AUTH).json()["jobs_running"] == 0


def test_failing_operation_returns_traceback(client):
    info = wait(client, start_job(client, "sleep", {"seconds": 0, "fail": True}).id)
    assert info.state == JobState.FAILED
    assert info.error.code == ErrorCode.JOB_FAILED
    assert "RuntimeError: The sleep job failed on request." in info.error.remote_traceback
    assert "sleep_operation" in info.error.remote_traceback
    r = client.get(f"/v1/jobs/{info.id}/result", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["code"] == "job_failed"
    assert "RuntimeError" in r.json()["remote_traceback"]


def test_pickle_parts_and_objects(client):
    obj = {"weights": list(range(100))}
    info = wait(client, start_job(client, "echo", {"a": 1}, parts={"thing": obj}).id)
    assert info.state == JobState.DONE
    result = JobResult.model_validate(client.get(f"/v1/jobs/{info.id}/result", headers=AUTH).json())
    stored = result.objects["thing"]
    assert stored.type == "builtins.dict"
    assert stored.size_bytes == len(pickle.dumps(obj, protocol=PICKLE_PROTOCOL))

    r = client.get(f"/v1/objects/{stored.id}", headers=AUTH)
    assert r.status_code == 200
    assert r.headers[PAYLOAD_HEADER] == PAYLOAD_PICKLE
    assert pickle.loads(r.content) == obj
    assert client.delete(f"/v1/objects/{stored.id}", headers=AUTH).json()["deleted"]
    assert client.get(f"/v1/objects/{stored.id}", headers=AUTH).status_code == 404

    # Parts without the payload header aren't unpickled.
    spec = {"kind": "echo", "parts": ["thing"]}
    r = post_job(client, spec, {"thing": ("thing", b"not a pickle", PICKLE_CONTENT_TYPE)})
    assert r.status_code == 400
    # Unknown kinds and table-only kinds are refused.
    for kind in ("nothing", "load_table"):
        assert post_job(client, {"kind": kind}).status_code == 400


def test_every_job_kind_is_registered():
    """The job system registers every operation module of ``server/ops/``
    when it starts, so every job kind of the API has an operation."""
    from orangecontrib.cloud.server import tables  # noqa: F401 (registers load_table)
    from orangecontrib.cloud.server.ops import registration
    from orangecontrib.cloud.server.worker import OPERATIONS

    registration.register_operations()
    assert {kind.value for kind in JobKind} <= set(OPERATIONS)
    folder = Path(registration.__file__).parent
    modules = {path.stem for path in folder.glob("*.py") if "@operation(" in path.read_text()}
    assert modules == set(registration.OPERATION_MODULES)


def test_object_files_are_written_once_without_leftover_locks(tmp_path, monkeypatch):
    """Parallel downloads of an object pickle it once, and its lock goes
    when no download uses it; parallel first requests make one ObjectFiles."""
    from orangecontrib.cloud.server import objects

    writes = []

    class Jobs:
        def object_info(self, object_id):
            return None

        def run_on_worker(self, func, object_id, path):
            writes.append(object_id)
            time.sleep(0.05)
            Path(path).write_bytes(b"pickle")
            return True

    files = objects.ObjectFiles(tmp_path)
    with ThreadPoolExecutor(8) as pool:
        paths = set(pool.map(lambda _: files.get(Jobs(), "o-1"), range(8)))
    assert paths == {files.path("o-1")} and writes == ["o-1"]
    assert files._object_locks == {}
    files.forget("o-1")
    assert not files.path("o-1").exists() and files._object_locks == {}

    class SlowObjectFiles(objects.ObjectFiles):
        def __init__(self, folder):
            time.sleep(0.05)
            super().__init__(folder)

    monkeypatch.setattr(objects, "ObjectFiles", SlowObjectFiles)
    config = ServerConfig(token=TOKEN, data_dir=tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=config)))
    with ThreadPoolExecutor(8) as pool:
        made = list(pool.map(lambda _: objects.get_object_files(request), range(8)))
    assert all(files is made[0] for files in made)


def test_out_of_memory(client):
    kept = wait(client, start_job(client, "echo", parts={"thing": 1}).id)
    kept_id = client.get(f"/v1/jobs/{kept.id}/result", headers=AUTH).json()["objects"]["thing"]

    # 2 GiB in a worker limited to 512 MiB: Dask's nanny restarts the worker.
    job = start_job(client, "memory_hog", {"bytes": 2 * 1024**3})
    info = wait(client, job.id, timeout=60)
    assert info.state == JobState.FAILED
    assert info.error.code == ErrorCode.OUT_OF_MEMORY
    assert info.error.message == "The machine ran out of memory. Choose a larger machine size."
    assert "went over its memory limit" in info.error.remote_traceback
    r = client.get(f"/v1/jobs/{job.id}/result", headers=AUTH)
    assert r.status_code == 507
    assert r.json()["code"] == "out_of_memory"

    # The server is still up, the worker restarted, and its objects are gone.
    assert client.get("/v1/info", headers=AUTH).status_code == 200
    assert client.get(f"/v1/objects/{kept_id['id']}", headers=AUTH).status_code == 404
    info = wait(client, start_job(client, "sleep", {"seconds": 0.1, "steps": 1}).id, timeout=60)
    assert info.state == JobState.DONE


def test_worker_crash(client):
    # A segfault isn't out of memory: the job says the worker crashed, with
    # the signal and the worker's traceback (from faulthandler).
    info = wait(client, start_job(client, "crash_worker").id, timeout=60)
    assert info.state == JobState.FAILED
    assert info.error.code == ErrorCode.WORKER_CRASHED
    assert "SIGSEGV" in info.error.message
    assert "crash_worker_operation" in info.error.remote_traceback
    r = client.get(f"/v1/jobs/{info.id}/result", headers=AUTH)
    assert r.status_code == 500
    assert r.json()["code"] == "worker_crashed"

    # The server is still up and the restarted worker runs new jobs.
    assert client.get("/v1/info", headers=AUTH).status_code == 200
    info = wait(client, start_job(client, "sleep", {"seconds": 0.1, "steps": 1}).id, timeout=60)
    assert info.state == JobState.DONE
