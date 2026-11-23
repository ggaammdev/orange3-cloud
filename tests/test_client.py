"""The API client against the machine's server, run in-process over HTTPS.

The server runs with uvicorn in a thread, on a random port, with a fresh
self-signed certificate, so the tests also cover the client's TLS pinning.
"""

import os
import time

import httpx
import pytest
from conftest import https_server
from Orange.data import Table

from orangecontrib.cloud.client.api import (
    CloudAuthError,
    CloudCancelled,
    CloudClient,
    CloudConnectionError,
    CloudError,
    CloudNotFoundError,
    CloudWorkerCrashedError,
)
from orangecontrib.cloud.client.providers.base import self_signed_certificate
from orangecontrib.cloud.protocol.errors import ErrorCode, ErrorResponse
from orangecontrib.cloud.protocol.jobs import JobKind, JobState
from orangecontrib.cloud.protocol.wire import API_VERSION
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app

TOKEN = "test-token"


@pytest.fixture(scope="module")
def machine(tmp_path_factory):
    """The machine's server over HTTPS: yields (base URL, certificate PEM)."""
    folder = tmp_path_factory.mktemp("machine")
    config = ServerConfig(token=TOKEN, data_dir=folder, worker_threads=2, debug_operations=True)
    with https_server(create_machine_app(config), folder) as (url, cert, _):
        yield url, cert


@pytest.fixture
def client(machine):
    url, cert = machine
    with CloudClient(url, TOKEN, cert) as client:
        yield client


def test_info(client):
    info = client.info()
    assert info.api_version == API_VERSION
    assert info.cpu_count >= 1
    assert "orange3-cloud" in info.packages
    assert "test-token" not in repr(client)


def test_tls_trusts_only_the_machines_certificate(machine):
    url, cert = machine
    other_cert, _ = self_signed_certificate()
    with (
        CloudClient(url, TOKEN, other_cert, retries=0) as other,
        pytest.raises(CloudConnectionError, match="certificate"),
    ):
        other.info()
    # The hostname is checked too: this certificate isn't for "machine.test".
    with (
        CloudClient(url, TOKEN, cert, server_hostname="machine.test", retries=0) as other,
        pytest.raises(CloudConnectionError, match="certificate"),
    ):
        other.info()
    with pytest.raises(ValueError):
        CloudClient(url, TOKEN)  # https:// without a certificate
    with pytest.raises(ValueError):
        CloudClient(url.replace("https", "http"), TOKEN)


def test_wrong_token(machine):
    url, cert = machine
    with CloudClient(url, "wrong", cert) as other, pytest.raises(CloudAuthError) as e:
        other.info()
    assert e.value.code == ErrorCode.UNAUTHORIZED
    assert e.value.status_code == 401


def test_sleep_job_with_progress(client):
    job = client.submit_job(JobKind.SLEEP, params={"seconds": 1.2, "steps": 3})
    seen = []
    result = client.wait_for_job(
        job, on_progress=lambda f, text: seen.append((f, text)), max_poll_interval=0.3
    )
    assert result.values == {"seconds": 1.2, "steps": 3}
    assert seen[-1] == (1.0, "Step 3 of 3")
    assert any(0 < f < 1 and text.startswith("Step") for f, text in seen), seen
    assert len(seen) == len(set(seen))  # called only on change


def test_cancel(client):
    job = client.submit_job("sleep", params={"seconds": 60, "steps": 600})
    started = time.monotonic()
    with pytest.raises(CloudCancelled):
        client.wait_for_job(job, should_cancel=lambda: time.monotonic() - started > 0.5)
    assert client.job_info(job.id).state == JobState.CANCELLED
    with pytest.raises(CloudCancelled):
        client.job_result(job.id)


def test_failing_job_raises_remote_traceback(client):
    job = client.submit_job("sleep", params={"seconds": 0, "fail": True})
    with pytest.raises(CloudError) as e:
        client.wait_for_job(job)
    assert e.value.code == ErrorCode.JOB_FAILED
    assert "RuntimeError: The sleep job failed on request." in e.value.remote_traceback
    with pytest.raises(CloudNotFoundError):
        client.job_info("no-such-job")


def test_objects_round_trip(client):
    obj = {"weights": list(range(10))}
    result = client.wait_for_job(client.submit_job("echo", objects={"thing": obj}))
    stored = result.objects["thing"]
    assert client.get_object(stored.id) == obj
    assert client.delete_object(stored.id).deleted
    with pytest.raises(CloudNotFoundError):
        client.get_object(stored.id)


class _CutStream(httpx.SyncByteStream):
    """A response body that breaks off after ``limit`` bytes."""

    def __init__(self, stream, limit: int):
        self.stream, self.limit = stream, limit

    def __iter__(self):
        sent = 0
        for chunk in self.stream:
            if sent + len(chunk) > self.limit:
                yield chunk[: self.limit - sent]
                raise httpx.ReadError("connection cut by the test")
            sent += len(chunk)
            yield chunk

    def close(self):
        self.stream.close()


class _CuttingTransport(httpx.BaseTransport):
    """Cuts the first ``cuts`` object downloads after ``limit`` bytes."""

    def __init__(self, inner: httpx.BaseTransport, limit: int, cuts: int):
        self.inner, self.limit, self.cuts = inner, limit, cuts
        self.ranges: list[str | None] = []

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        if request.method == "GET" and "/objects/" in request.url.path:
            self.ranges.append(request.headers.get("range"))
            if self.cuts:
                self.cuts -= 1
                response.stream = _CutStream(response.stream, self.limit)
        return response


def test_object_download_continues_after_a_break(client):
    """A broken download continues where it stopped, with the same file."""
    obj = {"blob": os.urandom(3_000_000)}
    stored = client.wait_for_job(client.submit_job("echo", objects={"thing": obj})).objects["thing"]
    cutting = _CuttingTransport(client._http._transport, limit=1_000_000, cuts=2)
    client._http._transport = cutting
    progress = []
    assert client.get_object(stored.id, lambda got, total: progress.append((got, total))) == obj
    assert cutting.ranges == [None, "bytes=1000000-", "bytes=2000000-"]
    assert progress[-1] == (stored.size_bytes, stored.size_bytes)
    # A connection that breaks without new bytes gives up after the retries.
    client.retries, client.retry_backoff = 1, 0.01
    cutting.cuts, cutting.limit = 5, 0
    with pytest.raises(CloudConnectionError, match="closed the connection"):
        client.get_object(stored.id)
    assert len(cutting.ranges) == 3 + 2  # the second download: one try and one retry
    client.delete_object(stored.id)


@pytest.mark.slow
def test_slow_object_download(client):
    """A download read slower than the machine sends it isn't cut off. A
    pickle sent as one response body was: uvicorn hands it to the TLS
    transport at once, closes the idle-looking connection after its
    keep-alive timeout (5 s), and asyncio drops what is still unsent after
    its TLS shutdown timeout (30 s)."""
    obj = {"blob": os.urandom(50_000_000)}
    stored = client.wait_for_job(client.submit_job("echo", objects={"thing": obj})).objects["thing"]
    started = time.monotonic()

    def slowly(received, total):  # about 1 MB/s, so it takes about 50 s
        time.sleep(max(0.0, started + received / 1e6 - time.monotonic()))

    client.retries = 0  # no second try
    assert client.get_object(stored.id, slowly) == obj
    assert time.monotonic() - started > 40
    client.delete_object(stored.id)


def test_table_round_trip(client, tmp_path):
    csv = tmp_path / "small.csv"
    csv.write_text("x,y,label\n" + "".join(f"{i},{i * 0.5},{'ab'[i % 2]}\n" for i in range(50)))
    sent = []
    upload = client.upload(csv, on_progress=lambda n, total: sent.append((n, total)))
    assert upload.size_bytes == csv.stat().st_size
    assert sent[-1] == (upload.size_bytes, upload.size_bytes)

    job = client.create_table(upload_id=upload.id, name="small")
    result = client.wait_for_job(job, max_poll_interval=0.3)
    (table,) = result.tables.values()
    assert table.n_rows == 50
    assert [t.id for t in client.list_tables()] == [table.id]
    assert client.table_info(table.id).name == "small"
    domain = client.table_domain(table.id)
    assert [v.name for v in domain.variables] == ["x", "y", "label"]
    page = client.table_rows(table.id, 5, 15)
    assert isinstance(page, Table)
    assert len(page) == 10 and page.domain == domain
    assert list(page.X[:, 0]) == list(range(5, 15))
    top = client.table_rows(table.id, 0, 3, sort="y", descending=True)
    assert list(top.X[:, 0]) == [49, 48, 47]
    assert client.delete_table(table.id).deleted
    with pytest.raises(CloudNotFoundError):
        client.table_info(table.id)


def test_worker_crash_error_class():
    error = ErrorResponse(code=ErrorCode.WORKER_CRASHED, message="crashed", remote_traceback="log")
    ex = CloudError.from_response(error, 500)
    assert isinstance(ex, CloudWorkerCrashedError)
    assert (ex.code, ex.remote_traceback) == (ErrorCode.WORKER_CRASHED, "log")
