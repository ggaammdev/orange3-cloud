import asyncio
import json
import time
from importlib.metadata import version

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from orangecontrib.cloud.protocol.errors import ErrorCode, ErrorResponse
from orangecontrib.cloud.protocol.jobs import JobInfo, JobState
from orangecontrib.cloud.protocol.machine import InfoResponse, ShutdownResponse
from orangecontrib.cloud.protocol.wire import API_VERSION, JSON_CONTENT_TYPE, SPEC_PART
from orangecontrib.cloud.server.app import (
    IDLE_TIMEOUT,
    MAX_LIFETIME,
    ServerConfig,
    ServerState,
    cancel_unattended_jobs,
    create_app,
    create_machine_app,
    unattended_message,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def failing_router() -> APIRouter:
    router = APIRouter()

    @router.get("/boom")
    def boom():
        raise ValueError("kaboom")

    @router.get("/number")
    def number(n: int):
        return {"n": n}

    return router


@pytest.fixture
def shutdowns():
    return []


@pytest.fixture
def client(shutdowns):
    config = ServerConfig(token=TOKEN, on_shutdown=lambda: shutdowns.append(1))
    app = create_app(config, routers=[failing_router()])
    with TestClient(app) as client:
        yield client


def test_info_with_token(client):
    r = client.get("/v1/info", headers=AUTH)
    assert r.status_code == 200
    info = InfoResponse.model_validate(r.json())
    assert info.api_version == API_VERSION
    assert info.server_version == version("orange3-cloud")
    assert info.packages["orange3-cloud"] == info.server_version
    assert info.cpu_count >= 1
    assert 0 < info.memory_free_bytes <= info.memory_total_bytes
    assert info.uptime_seconds >= 0


def test_info_with_missing_data_dir(tmp_path):
    config = ServerConfig(token=TOKEN, data_dir=tmp_path / "not" / "yet")
    assert TestClient(create_app(config)).get("/v1/info", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}],
    ids=["missing", "wrong", "no-scheme"],
)
def test_info_without_token(client, headers):
    for path in ("/v1/info", "/v1/boom"):
        r = client.get(path, headers=headers)
        assert r.status_code == 401
        assert ErrorResponse.model_validate(r.json()).code == ErrorCode.UNAUTHORIZED


def test_unexpected_error_has_traceback(client):
    r = client.get("/v1/boom", headers=AUTH)
    assert r.status_code == 500
    error = ErrorResponse.model_validate(r.json())
    assert error.code == ErrorCode.INTERNAL
    assert "ValueError: kaboom" in error.remote_traceback


def test_protocol_errors(client):
    r = client.get("/v1/nothing", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    r = client.get("/v1/number", params={"n": "x"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


def test_shutdown_calls_hook(client, shutdowns):
    r = client.post("/v1/shutdown", headers=AUTH)
    assert r.status_code == 200
    assert ShutdownResponse.model_validate(r.json()).shutting_down
    assert shutdowns == [1]


def test_main_reads_token_from_env(monkeypatch):
    from orangecontrib.cloud.server.__main__ import TOKEN_ENV, parse_args

    monkeypatch.setenv(TOKEN_ENV, "from-env")
    assert parse_args([]).token == "from-env"
    assert parse_args(["--token", "cli"]).token == "cli"


def test_main_reads_install_failures(tmp_path):
    from orangecontrib.cloud.server.__main__ import parse_args, read_install_failures

    path = tmp_path / "failures.json"
    path.write_text('[{"name": "foo", "version": "1.0", "error": "no wheel"}]')
    args = parse_args(["--token", "t", "--install-failures", str(path), "--idle-timeout", "60"])
    assert args.idle_timeout == 60
    assert read_install_failures(args.install_failures)[0].name == "foo"
    assert read_install_failures(None) == []


# Idle shutdown and maximum lifetime


def wait_for(condition, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def start_sleep(client, seconds):
    spec = {"kind": "sleep", "params": {"seconds": seconds, "steps": max(1, int(seconds * 10))}}
    files = {SPEC_PART: (None, json.dumps(spec), JSON_CONTENT_TYPE)}
    response = client.post("/v1/jobs", headers=AUTH, files=files)
    assert response.status_code == 202, response.text
    return JobInfo.model_validate(response.json()).id


def test_idle_shutdown_waits_for_jobs():
    """Jobs keep the machine running while Orange is in touch: GET /v1/info,
    the heartbeat, is enough, but doesn't count as activity after them."""
    reasons = []
    config = ServerConfig(token=TOKEN, idle_timeout_seconds=1.0, worker_threads=1)
    config.on_shutdown = lambda: reasons.append(app.state.server.shutdown_reason)
    app = create_machine_app(config)
    with TestClient(app) as client:
        info = InfoResponse.model_validate(client.get("/v1/info", headers=AUTH).json())
        assert info.idle_timeout_seconds == 1.0 and 0 < info.idle_time_left_seconds <= 1.0
        assert info.lifetime_left_seconds is None
        assert info.time_left_seconds == info.idle_time_left_seconds

        job_id = start_sleep(client, 2.5)
        end = time.monotonic() + 2.0  # longer than the idle timeout, with only GET /v1/info
        while time.monotonic() < end:
            info = InfoResponse.model_validate(client.get("/v1/info", headers=AUTH).json())
            time.sleep(0.2)
        assert reasons == [] and info.jobs_running + info.jobs_queued == 1
        assert info.idle_time_left_seconds is None and info.time_left_seconds is None

        # The job ends, then the server stops.
        assert wait_for(lambda: client.get("/v1/info", headers=AUTH) and reasons, 5)
        assert reasons == [IDLE_TIMEOUT]
        assert app.state.server.job_manager.info(job_id).state == JobState.DONE


def test_jobs_cancelled_when_orange_is_silent():
    """Without any request for the idle timeout (Orange crashed), the machine
    cancels the jobs, and the idle shutdown follows after another timeout."""
    stopped = []
    config = ServerConfig(token=TOKEN, idle_timeout_seconds=1.0, worker_threads=1)
    config.on_shutdown = lambda: stopped.append((time.time(), state.shutdown_reason))
    app = create_machine_app(config)
    with TestClient(app) as client:
        state = app.state.server
        jobs = state.job_manager
        job_id = start_sleep(client, 60)
        # Watched in this process: a request would be Orange in touch.
        assert wait_for(lambda: jobs.info(job_id).state.finished, 5)
        job = jobs.info(job_id)
        assert stopped == [] and state.silent_seconds >= 1.0
        assert wait_for(lambda: stopped, 5)
    assert job.state == JobState.CANCELLED and job.error.code == ErrorCode.CANCELLED
    assert job.error.message == (
        "The machine cancelled the job: Orange hadn't been in touch for 1 second."
    )
    ((when, reason),) = stopped
    assert reason == IDLE_TIMEOUT and when - job.finished_at.timestamp() >= 0.8


def test_no_cancelling_without_an_idle_timeout_or_while_in_touch():
    class Jobs:
        cancelled = []

        def unfinished(self):
            return ["j-1"]

        def cancel(self, job_id, message):
            self.cancelled.append((job_id, message))

    def cancel(**limits):
        return asyncio.run(cancel_unattended_jobs(ServerConfig(token=TOKEN, **limits), state))

    state = ServerState(job_manager=Jobs())
    state.last_contact -= 3600
    assert cancel(idle_timeout_seconds=0) == cancel(max_lifetime_seconds=60) == []
    state.record_contact()
    assert cancel(idle_timeout_seconds=60) == [] and Jobs.cancelled == []
    state.last_contact -= 3600
    assert cancel(idle_timeout_seconds=3600) == ["j-1"]
    assert Jobs.cancelled == [("j-1", unattended_message(3600))]
    assert unattended_message(3600).endswith("in touch for 1 hour.")
    assert unattended_message(90).endswith("in touch for 90 seconds.")


def test_max_lifetime_despite_activity():
    reasons = []
    config = ServerConfig(token=TOKEN, idle_timeout_seconds=10, max_lifetime_seconds=1.0)
    config.on_shutdown = lambda: reasons.append(app.state.server.shutdown_reason)
    app = create_app(config)
    with TestClient(app) as client:
        start = time.monotonic()
        while not reasons and time.monotonic() - start < 3:
            client.get("/v1/nothing", headers=AUTH)  # activity
            time.sleep(0.05)
        info = InfoResponse.model_validate(client.get("/v1/info", headers=AUTH).json())
    assert reasons == [MAX_LIFETIME]
    assert 1.0 <= time.monotonic() - start < 2.0
    assert info.lifetime_left_seconds == 0 and info.idle_time_left_seconds > 8


def test_main_exit_codes():
    from orangecontrib.cloud.server import __main__ as m

    assert m.EXIT_CODES == {IDLE_TIMEOUT: 3, MAX_LIFETIME: 4}
    args = m.parse_args(["--token", "t"])
    assert (args.idle_timeout, args.max_lifetime) == (1800, 43200)
