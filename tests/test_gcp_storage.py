"""The project's storage bucket and uploads, with Cloud Storage replaced by
a fake: creating the bucket with the machine's read access, resumable
uploads that survive a broken connection and an interrupted upload (they
continue where they stopped), reusing an uploaded file, listing and deleting
files, and the memory estimate of ``gs://`` files. Cloud File asks before
creating the bucket and loads the uploaded file on the in-process machine;
the "Uploaded files" dialog deletes files.

``test_real_bucket`` uploads a small file to the test project's bucket; it
runs only with ``ORANGE3_CLOUD_GCP_VM_TESTS=1`` and
``ORANGE3_CLOUD_GCP_PROJECT``/``_ZONE`` set (never in CI).
"""

import copy
import gzip
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from AnyQt.QtWidgets import QApplication
from conftest import SPEC, https_server
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_cloud_file import write_csv
from test_widget_base import SharedMachineProvider

from orangecontrib.cloud.client.providers import gcp_account, gcp_storage_api
from orangecontrib.cloud.client.providers.base import (
    MachineHandle,
    ProviderError,
    StorageMissingError,
    StoredFile,
)
from orangecontrib.cloud.client.providers.gcp import OwnGCPProvider
from orangecontrib.cloud.client.providers.gcp_compute import ComputeApi
from orangecontrib.cloud.client.providers.gcp_storage import (
    READER_ROLE,
    ProjectStorage,
    bucket_prefix,
    new_bucket_name,
)
from orangecontrib.cloud.client.providers.gcp_storage_api import (
    NotFoundError,
    StorageApi,
    TransientError,
    UploadExpiredError,
)
from orangecontrib.cloud.client.providers.uploads import UploadSessions, object_name
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.estimate import estimate_stored
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.widgets.owcloudfile import OWCloudFile

PROJECT, ZONE = "my-project", "europe-west1-b"
SERVICE_ACCOUNT = "123-compute@developer.gserviceaccount.com"


class FakeStorage(StorageApi):
    """Buckets in memory, with resumable upload sessions."""

    def __init__(self):
        self.buckets = {}  # name -> {"meta", "policy", "objects": {name: bytes}}
        self.sessions = {}  # url -> {"bucket", "name", "size", "data"}
        self.fail_chunks = set()  # numbers of chunk calls that break the connection
        self.chunk_calls = []  # (url, offset) of each chunk call
        self.policy_sets = 0

    def list_buckets(self, project, prefix):
        return [b["meta"] for name, b in self.buckets.items() if name.startswith(prefix)]

    def create_bucket(self, project, body):
        meta = {**body, "timeCreated": "2026-11-19T10:00:00Z"}
        policy = {"bindings": [{"role": "roles/storage.legacyBucketOwner", "members": ["x"]}]}
        self.buckets[body["name"]] = {"meta": meta, "policy": policy, "objects": {}}
        return meta

    def _bucket(self, bucket):
        if bucket not in self.buckets:
            raise NotFoundError("no bucket")
        return self.buckets[bucket]

    def get_policy(self, bucket):
        return copy.deepcopy(self._bucket(bucket)["policy"])

    def set_policy(self, bucket, policy):
        self.policy_sets += 1
        self._bucket(bucket)["policy"] = copy.deepcopy(policy)

    def _meta(self, name, data):
        return {"name": name, "size": str(len(data)), "updated": "2026-11-19T11:00:00Z"}

    def get_object(self, bucket, name):
        objects = self._bucket(bucket)["objects"]
        if name not in objects:
            raise NotFoundError("no object")
        return self._meta(name, objects[name])

    def list_objects(self, bucket, prefix):
        objects = self._bucket(bucket)["objects"]
        return [self._meta(n, d) for n, d in objects.items() if n.startswith(prefix)]

    def delete_object(self, bucket, name):
        self.get_object(bucket, name)
        del self._bucket(bucket)["objects"][name]

    def read_object(self, bucket, name, length):
        self.get_object(bucket, name)
        return self._bucket(bucket)["objects"][name][:length]

    def start_upload(self, bucket, name, size):
        url = f"https://upload.example/{len(self.sessions) + len(self.chunk_calls)}"
        self.sessions[url] = {"bucket": bucket, "name": name, "size": size, "data": bytearray()}
        return url

    def _state(self, url):
        if url not in self.sessions:
            raise UploadExpiredError("expired")
        session = self.sessions[url]
        if len(session["data"]) < session["size"]:
            return len(session["data"])
        objects = self._bucket(session["bucket"])["objects"]
        objects[session["name"]] = bytes(session["data"])
        return self._meta(session["name"], session["data"])

    def upload_chunk(self, session_url, data, offset, size):
        self.chunk_calls.append((session_url, offset))
        session = self.sessions.get(session_url)
        if session is not None and offset == len(session["data"]):
            session["data"] += data
        if len(self.chunk_calls) in self.fail_chunks:  # arrived, but the reply was lost
            raise TransientError("connection reset")
        return self._state(session_url)

    def upload_offset(self, session_url, size):
        return self._state(session_url)


class Compute(ComputeApi):
    def default_service_account(self, project):
        return SERVICE_ACCOUNT


def storage_for(api, tmp_path, chunk=1000):
    sessions = UploadSessions(tmp_path / "sessions.json")
    return ProjectStorage(PROJECT, api, sessions=sessions, chunk_bytes=chunk, retry_delays=(0, 0))


def test_bucket_upload_resume_and_delete(tmp_path):
    api = FakeStorage()
    storage = storage_for(api, tmp_path)
    assert storage.find_bucket() is None
    name = new_bucket_name(PROJECT)
    assert name.startswith(bucket_prefix(PROJECT)) and len(name) <= 63
    with pytest.raises(ProviderError):
        storage.create_bucket("someone-elses-bucket", "europe-west1")
    storage.create_bucket(name, "europe-west1")
    body = api.buckets[name]["meta"]
    assert body["location"] == "europe-west1" and body["labels"] == {"orange3-cloud": "true"}
    assert body["iamConfiguration"]["uniformBucketLevelAccess"] == {"enabled": True}
    assert body["iamConfiguration"]["publicAccessPrevention"] == "enforced"
    assert storage.find_bucket() == name

    # The machine's service account may read the files; asking again changes nothing.
    member = f"serviceAccount:{SERVICE_ACCOUNT}"
    storage.allow_reader(name, member)
    storage.allow_reader(name, member)
    readers = [b for b in api.buckets[name]["policy"]["bindings"] if b["role"] == READER_ROLE]
    assert readers == [{"role": READER_ROLE, "members": [member]}] and api.policy_sets == 1

    # A broken connection is retried from where Google's copy ends.
    path = tmp_path / "data.csv"
    path.write_bytes(os.urandom(10_500))
    api.fail_chunks = {3}
    progress = []
    uri = storage.upload(name, path, lambda sent, total: progress.append((sent, total)))
    stat = path.stat()
    assert uri == f"gs://{name}/{object_name(path, stat.st_size, stat.st_mtime_ns)}"
    assert uri.endswith("/data.csv") and "/uploads/" in uri
    assert api.buckets[name]["objects"][uri.split("/", 3)[3]] == path.read_bytes()
    assert progress[-1] == (10_500, 10_500)
    assert [offset for _, offset in api.chunk_calls] == [i * 1000 for i in range(11)]

    # The same file isn't uploaded again.
    api.chunk_calls.clear()
    assert storage.upload(name, path) == uri and api.chunk_calls == []

    # An upload stopped midway (cancelled, Orange quit, no connection)
    # continues where it stopped, in a new process.
    other = tmp_path / "other.csv.gz"
    other.write_bytes(os.urandom(7_200))

    def stop_at_half(sent, total):
        if sent >= 4000:
            raise KeyboardInterrupt("killed")

    with pytest.raises(KeyboardInterrupt):
        storage.upload(name, other, stop_at_half)
    assert len(UploadSessions(tmp_path / "sessions.json")._read()) == 1
    api.chunk_calls.clear()
    again = storage_for(api, tmp_path)
    other_uri = again.upload(name, other)
    assert [offset for _, offset in api.chunk_calls] == [4000, 5000, 6000, 7000]
    assert api.buckets[name]["objects"][other_uri.split("/", 3)[3]] == other.read_bytes()
    assert UploadSessions(tmp_path / "sessions.json")._read() == {}

    # Connection gone for good: a clear error, and the upload can continue later.
    third = tmp_path / "third.csv"
    third.write_bytes(os.urandom(3000))
    api.fail_chunks = set(range(len(api.chunk_calls) + 1, len(api.chunk_calls) + 50))
    api.upload_offset = lambda url, size: (_ for _ in ()).throw(TransientError("down"))
    with pytest.raises(ProviderError, match="Load again to continue"):
        storage.upload(name, third)
    del api.upload_offset
    api.fail_chunks = set()
    storage.upload(name, third)

    # An expired session starts again from the first byte.
    fourth = tmp_path / "fourth.csv"
    fourth.write_bytes(b"a,b\n1,2\n")
    st = fourth.stat()
    fourth_uri = f"gs://{name}/{object_name(fourth, st.st_size, st.st_mtime_ns)}"
    storage.sessions.put(fourth_uri, st.st_size, st.st_mtime_ns, "https://upload.example/gone")
    assert storage.upload(name, fourth) == fourth_uri

    files = storage.files(name)
    assert sorted(f.name for f in files) == ["data.csv", "fourth.csv", "other.csv.gz", "third.csv"]
    assert {f.uri: f.size_bytes for f in files}[uri] == 10_500
    storage.delete([uri, other_uri])
    storage.delete([uri])  # already gone
    assert sorted(f.name for f in storage.files(name)) == ["fourth.csv", "third.csv"]


def test_provider_stages_files(tmp_path):
    provider = OwnGCPProvider(project=PROJECT, zone=ZONE, api=Compute())
    provider.storage_api = api = FakeStorage()
    handle = MachineHandle(
        "gcp", "m1", "orange3-cloud-m1", "", "", "", extra={"project": PROJECT, "zone": ZONE}
    )
    path = tmp_path / "data.csv"
    write_csv(path, 50)
    assert provider.stores_uploads and provider.stored_files(handle) == []
    with pytest.raises(StorageMissingError) as missing:
        provider.stage_file(handle, path)
    bucket = missing.value.bucket
    assert bucket.startswith(f"orange3-cloud-{PROJECT}-") and bucket in str(missing.value)
    assert api.buckets == {}  # nothing created without the user's OK
    sent = []
    uri = provider.stage_file(
        handle, path, on_progress=lambda s, t: sent.append(s), create_bucket=bucket
    )
    assert uri.startswith(f"gs://{bucket}/uploads/") and sent[-1] == path.stat().st_size
    assert api.buckets[bucket]["meta"]["location"] == "europe-west1"
    members = [m for b in api.buckets[bucket]["policy"]["bindings"] for m in b["members"]]
    assert f"serviceAccount:{SERVICE_ACCOUNT}" in members
    assert provider.stage_file(handle, path) == uri  # the bucket exists now
    assert [f.uri for f in provider.stored_files(handle)] == [uri]
    with pytest.raises(ProviderError):
        provider.delete_stored_files(["gs://someone-elses-bucket/data.csv"])
    provider.delete_stored_files([uri])
    assert provider.stored_files(handle) == []


def test_estimate_of_gs_files(tmp_path, monkeypatch):
    api = FakeStorage()
    api.create_bucket(PROJECT, {"name": "b"})
    write_csv(tmp_path / "data.csv", 20_000)
    text = (tmp_path / "data.csv").read_bytes()
    api.buckets["b"]["objects"]["data.csv"] = text
    api.buckets["b"]["objects"]["data.csv.gz"] = gzip.compress(text)
    monkeypatch.setattr(gcp_account, "load_credentials", lambda: (None, ""))
    monkeypatch.setattr(gcp_storage_api, "GoogleStorageApi", lambda credentials, timeout: api)
    read_head = OwnGCPProvider(project=PROJECT).read_stored_head
    plain = estimate_stored("gs://b/data.csv", read_head)
    assert plain.file_bytes == len(text) and plain.columns == 5
    assert abs(plain.rows - 20_000) / 20_000 < 0.05
    zipped = estimate_stored("gs://b/data.csv.gz", read_head)
    assert zipped.gzip and zipped.columns == 5
    with pytest.raises(NotFoundError):
        estimate_stored("gs://b/missing.csv", read_head)


class StoringProvider(SharedMachineProvider):
    """The shared test machine, with a "bucket" folder for uploads (the
    in-process machine loads its files by path)."""

    stores_uploads = True

    def __init__(self, url, cert, folder):
        super().__init__(url, cert)
        self.folder = folder
        self.bucket = None

    def stage_file(self, handle, path, *, on_progress=None, create_bucket=None, on_status=None):
        if self.bucket is None:
            if not create_bucket:
                raise StorageMissingError("Create the bucket test-bucket?", "test-bucket")
            self.bucket = self.folder / create_bucket
            self.bucket.mkdir()
        target = self.bucket / Path(path).name
        shutil.copy(path, target)
        on_progress(target.stat().st_size, target.stat().st_size)
        return str(target)

    def stored_files(self, handle=None):
        files = sorted(self.bucket.iterdir()) if self.bucket else []
        return [StoredFile(str(p), p.name, p.stat().st_size, "2026-11-19T12:00") for p in files]

    def delete_stored_files(self, uris):
        for uri in uris:
            Path(uri).unlink()


class TestCloudFileOnGoogleCloud(base.WidgetTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(
            token="widget-test-token", machine="test", data_dir=folder / "machine",
            worker_threads=2,
        )  # fmt: skip
        url, cert, _ = cls.tear_down_stack.enter_context(
            https_server(create_machine_app(config), folder)
        )
        cls.url, cls.cert = url, cert
        cls.csv = folder / "data.csv"
        write_csv(cls.csv, 500)

    def setUp(self):
        super().setUp()
        self.state = Path(tempfile.mkdtemp())
        self.provider = StoringProvider(self.url, self.cert, self.state)
        self.session = CloudSession(self.provider, store=HandleStore(self.state / "m.json"))
        self._previous, CloudSession._instance = CloudSession._instance, self.session
        self.session.start_machine(spec=SPEC, wait=True)
        self.process_events()

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        shutil.rmtree(self.state, ignore_errors=True)

    def wait_for(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline
            QApplication.processEvents()
            time.sleep(0.02)

    def test_asks_before_creating_the_bucket(self):
        w = self.create_widget(OWCloudFile)
        assert not w.files_button.isHidden()
        asked = []
        w.confirm_create_bucket = lambda message: asked.append(message) or False
        w.set_local_path(str(self.csv))
        assert w.load()
        self.wait_until_finished(w)
        assert asked == ["Create the bucket test-bucket?"] and self.provider.bucket is None
        assert "need storage on" in str(w.Error.source_error)
        assert self.get_output(w.Outputs.data, widget=w) is None

        w.confirm_create_bucket = lambda message: asked.append(message) or True
        w.Error.source_error.clear()
        assert w.load()
        self.wait_until_finished(w)
        self.wait_for(lambda: w.task is None and w.data is not None, timeout=30)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert self.provider.bucket == self.state / "test-bucket"
        data = self.get_output(w.Outputs.data, widget=w)
        assert data.ref.n_rows == 500
        assert f"Uploaded to {self.state / 'test-bucket' / 'data.csv'}" in w.info_label.text()

        # The uploaded files can be deleted.
        dialog = w.show_uploaded_files()
        self.wait_for(lambda: dialog.files is not None and not dialog.busy)
        assert [f.name for f in dialog.files] == ["data.csv"] and dialog.list.count() == 1
        assert not dialog.delete_button.isEnabled()
        dialog.set_checked([dialog.files[0].uri])
        assert dialog.delete_button.isEnabled()
        dialog.confirm_delete = lambda names: names == ["data.csv"]
        dialog.delete_checked()
        self.wait_for(lambda: dialog.files == [] and not dialog.busy)
        assert "No files" in dialog.label.text()
        assert not (self.state / "test-bucket" / "data.csv").exists()
        dialog.close()


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_GCP_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_GCP_PROJECT"),
    reason="uploads to the test project's bucket; set ORANGE3_CLOUD_GCP_VM_TESTS=1",
)
def test_real_bucket(tmp_path):
    project = os.environ["ORANGE3_CLOUD_GCP_PROJECT"]
    zone = os.environ["ORANGE3_CLOUD_GCP_ZONE"]
    provider = OwnGCPProvider(project=project, zone=zone)
    handle = MachineHandle("gcp", "t", "t", "", "", "", extra={"project": project, "zone": zone})
    path = tmp_path / "smoke.csv"
    write_csv(path, 1000)
    try:
        uri = provider.stage_file(handle, path)
    except StorageMissingError as missing:
        uri = provider.stage_file(handle, path, create_bucket=missing.bucket)
    try:
        assert uri in [f.uri for f in provider.stored_files(handle)]
        estimate = estimate_stored(uri, provider.read_stored_head)
        assert estimate.columns == 5 and estimate.file_bytes == path.stat().st_size
    finally:
        provider.delete_stored_files([uri])
    assert uri not in [f.uri for f in provider.stored_files(handle)]
