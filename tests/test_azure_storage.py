"""The Azure subscription's storage account and uploads, with Resource
Manager and Blob Storage replaced by fakes (each blob call is checked
against the signature of the Azure Storage library's own method): the
account is created after the user agrees (tagged, no anonymous or account
key access, with the container and the user's data role), a role is offered
when the user lacks it and the first upload waits until Azure applies it,
uploads go up in blocks that survive a broken connection and an interrupted
upload (they continue after the blocks Azure has), an uploaded file is
reused, files are listed and deleted, and the machine reads ``az://`` files
through user delegation SAS links. Cloud File uploads a local file and loads
it on the in-process machine through such a link, loads an ``az://`` URL,
and its "Uploaded files…" dialog survives being deleted while it lists.

``test_real_storage`` uploads a small file to the test subscription's
storage account and reads it back through a signed link; it runs only with
``ORANGE3_CLOUD_AZURE_VM_TESTS=1`` and ``ORANGE3_CLOUD_AZURE_SUBSCRIPTION``/
``_REGION`` set (never in CI).
"""

import base64
import contextlib
import datetime
import http.server
import inspect
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest
from AnyQt.QtWidgets import QApplication
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError
from azure.storage.blob import (
    BlobBlock,
    BlobClient,
    BlobProperties,
    BlobServiceClient,
    ContainerClient,
    UserDelegationKey,
)
from conftest import SPEC, https_server
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_azure_provider import GROUP, REGION, SUBSCRIPTION, FakeAzure, Response, failed
from test_cloud_file import write_csv
from test_widget_base import SharedMachineProvider

from orangecontrib.cloud.client.providers.azure import AzureProvider
from orangecontrib.cloud.client.providers.azure_storage import (
    BLOB_ROLE_ID,
    ROLE_TRY_OPTIONS,
    AzureStorage,
    block_size,
)
from orangecontrib.cloud.client.providers.base import (
    MachineHandle,
    ProviderError,
    StorageMissingError,
)
from orangecontrib.cloud.client.providers.uploads import UploadSessions, object_name
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.estimate import estimate_stored
from orangecontrib.cloud.gui.uploaded_files import UploadedFilesDialog
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings
from orangecontrib.cloud.widgets.owcloudfile import OWCloudFile

USER = "11111111-2222-3333-4444-555555555555"
ACCOUNTS = f"{GROUP}/providers/Microsoft.Storage/storageAccounts"
DATA_ACTIONS = ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/*"]


class FakeCredential:
    """The Azure CLI's sign-in: a token whose claims name the user."""

    def get_token(self, *scopes, **kwargs):
        claims = base64.urlsafe_b64encode(json.dumps({"oid": USER, "idtyp": "user"}).encode())
        return AccessToken(f"e30.{claims.decode().rstrip('=')}.sig", int(time.time()) + 3600)


class FakeSubscription(FakeAzure):
    """The resource group with storage accounts: a new account takes two
    reads to appear, and the user's permissions on it come from their role
    assignments there."""

    def __init__(self, folder):
        super().__init__(folder)
        self.unknown_principals = 0
        self.creating = {}

    def arm(self, subscription):
        arm = super().arm(subscription)
        arm.credential = FakeCredential()
        return arm

    def send(self, request):
        path = urlsplit(request.url).path
        if path == ACCOUNTS:
            accounts = [
                r
                for p, r in self.resources.items()
                if p.rsplit("/", 1)[0] == ACCOUNTS and p not in self.creating
            ]
            return Response(200, {"value": accounts})
        if path.endswith("/providers/Microsoft.Authorization/permissions"):
            return self.permissions(path.split("/providers/Microsoft.Authorization/")[0])
        if path.rsplit("/", 1)[0] == ACCOUNTS and path in self.creating:
            self.calls.append((request.method, path))
            self.creating[path] -= 1
            if self.creating[path] < 0:
                del self.creating[path]
                return super().send(request)
            creating = {"properties": {"provisioningState": "ResolvingDNS"}}
            return (
                failed(404, "ResourceNotFound") if self.creating[path] else Response(200, creating)
            )
        reply = super().send(request)
        if request.method == "PUT" and path.rsplit("/", 1)[0] == ACCOUNTS:
            self.creating[path] = 2  # Azure answers 202 and makes it meanwhile
            return Response(202)
        return reply

    def permissions(self, scope):
        roles = [
            r
            for p, r in self.resources.items()
            if p.startswith(f"{scope}/providers/Microsoft.Authorization/roleAssignments/")
            and r["properties"]["principalId"] == USER
            and r["properties"]["roleDefinitionId"].endswith(BLOB_ROLE_ID)
        ]
        data = DATA_ACTIONS if roles else []
        return Response(200, {"value": [{"actions": ["*"], "dataActions": data}]})

    def blob_role(self, account):
        return [
            r
            for p, r in self.resources.items()
            if p.startswith(f"{ACCOUNTS}/{account}/providers/Microsoft.Authorization/")
        ]


def storage_error(status, code):
    kind = ResourceNotFoundError if status == 404 else HttpResponseError
    error = kind(message=f"{code} (fake)")
    error.status_code, error.error_code = status, code
    return error


class FakeBlobs:
    """Blob Storage in memory, and a web server that answers SAS links."""

    def __init__(self):
        self.blobs = {}  # (account, container, name) -> bytes
        self.modified = {}
        self.blocks = {}  # (account, container, name) -> {block id: bytes}
        self.calls, self.block_calls, self.broken_calls, self.link_reads = [], [], set(), []
        self.options = []  # the options of each get_blob_properties
        #: Refuse this many calls, as before Azure applies a new role.
        self.refusing = 0
        blobs = self

        class Links(http.server.BaseHTTPRequestHandler):
            def answer(self, body):
                parts = urlsplit(self.path)
                key = tuple(unquote(parts.path).lstrip("/").split("/", 2))
                query = parse_qs(parts.query)
                data = blobs.blobs.get(key)
                if data is None or not {"sig", "skoid", "se"} <= set(query) or query["sp"] != ["r"]:
                    self.send_response(403)
                    self.end_headers()
                    return
                if body:
                    blobs.link_reads.append(key[2])
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if body:
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.wfile.write(data)

            def do_HEAD(self):  # noqa: N802
                self.answer(False)

            def do_GET(self):  # noqa: N802
                self.answer(True)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Links)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.web_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def service(self, credential, account):
        assert isinstance(credential, FakeCredential)
        return _Client(self, BlobServiceClient, account)

    def close(self):
        self.server.shutdown()

    def call(self, kind, method, args, kwargs):
        """Check the call against the library's method, then count it."""
        inspect.signature(getattr(kind, method)).bind(None, *args, **kwargs)
        self.calls.append(method)
        if method == "get_blob_properties":
            self.options.append(kwargs)
        if len(self.calls) in self.broken_calls:
            raise ServiceRequestError("Connection broken (fake)")
        if self.refusing:
            self.refusing -= 1
            raise storage_error(403, "AuthorizationPermissionMismatch")


class _Client:
    """A blob service, container or blob client of the fake."""

    def __init__(self, blobs, kind, account, container=None, name=None):
        self.fake, self.kind = blobs, kind
        self.account, self.container, self.name = account, container, name

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.fake.call(self.kind, method, args, kwargs)
            return getattr(self, f"_{method}")(*args, **kwargs)

        return call

    @property
    def key(self):
        return self.account, self.container, self.name

    @property
    def url(self):
        return f"{self.fake.web_url}/{self.account}/{self.container}/{quote(self.name)}"

    # The service and the container

    def get_blob_client(self, container, blob):
        return _Client(self.fake, BlobClient, self.account, container, blob)

    def get_container_client(self, container):
        return _Client(self.fake, ContainerClient, self.account, container)

    def _get_user_delegation_key(self, key_start_time, key_expiry_time):
        assert key_expiry_time - key_start_time <= datetime.timedelta(days=7)
        key = UserDelegationKey()
        key.signed_oid, key.signed_tid, key.signed_service = USER, "tenant", "b"
        key.signed_start = key_start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_expiry = key_expiry_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_version, key.value = "2025-01-05", base64.b64encode(b"k" * 32).decode()
        return key

    def _list_blobs(self, name_starts_with):
        return iter(
            self._properties(name, data)
            for (account, container, name), data in self.fake.blobs.items()
            if (account, container) == (self.account, self.container)
            and name.startswith(name_starts_with)
        )

    def _properties(self, name, data):
        properties = BlobProperties()
        properties.name, properties.size = name, len(data)
        properties.last_modified = self.fake.modified[(self.account, self.container, name)]
        return properties

    # Blobs

    def _store(self, data):
        self.fake.blobs[self.key] = bytes(data)
        self.fake.modified[self.key] = datetime.datetime.now(datetime.timezone.utc)
        self.fake.blocks.pop(self.key, None)

    def _get_blob_properties(self, **options):
        if self.key not in self.fake.blobs:
            raise storage_error(404, "BlobNotFound")
        return self._properties(self.name, self.fake.blobs[self.key])

    def _download_blob(self, offset, length):
        data = self._get_blob_properties() and self.fake.blobs[self.key]
        return _Download(data[offset : offset + length])

    def _upload_blob(self, data, overwrite, validate_content):
        assert overwrite and validate_content
        self._store(data)

    def _delete_blob(self):
        if self.fake.blobs.pop(self.key, None) is None:
            raise storage_error(404, "BlobNotFound")

    def _stage_block(self, block_id, data, length, validate_content):
        assert validate_content and length == len(data)
        blocks = self.fake.blocks.setdefault(self.key, {})
        assert all(len(b) == len(block_id) for b in blocks), "block IDs of different lengths"
        blocks[block_id] = bytes(data)
        self.fake.block_calls.append(int(block_id.rsplit("-", 1)[1]))

    def _get_block_list(self, block_list_type):
        assert block_list_type == "uncommitted"
        if self.key not in self.fake.blocks:
            raise storage_error(404, "BlobNotFound")
        listed = []
        for block_id, data in self.fake.blocks[self.key].items():
            block = BlobBlock(block_id)
            block.size = len(data)
            listed.append(block)
        return [], listed

    def _commit_block_list(self, block_list, content_settings):
        blocks = self.fake.blocks.get(self.key, {})
        if any(b.id not in blocks for b in block_list):
            raise storage_error(400, "InvalidBlockList")
        assert content_settings.content_type == "application/octet-stream"
        self._store(b"".join(blocks[b.id] for b in block_list))


class _Download:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


@pytest.fixture
def azure(tmp_path, monkeypatch):
    monkeypatch.setenv("ORANGE3_CLOUD_PUBLIC_IP", "93.184.216.34")
    providers = {"azure": ProviderSettings(SUBSCRIPTION, REGION)}
    CloudSettings(provider="azure", providers=providers).save()
    subscription, blobs = FakeSubscription(tmp_path), FakeBlobs()
    yield subscription, blobs
    blobs.close()
    subscription.close()


def storage_for(azure, block=1000):
    subscription, blobs = azure
    arm = subscription.arm(SUBSCRIPTION)
    arm.poll_interval = 0
    storage = AzureStorage(arm, REGION, blobs.service, block_bytes=block, retry_delays=(0, 0))
    storage.role_poll = 0
    return storage


def test_account_upload_resume_and_delete(azure, tmp_path):
    subscription, blobs = azure
    storage = storage_for(azure)
    assert storage.find_account() is None
    with pytest.raises(StorageMissingError) as missing:
        storage.prepare()
    name = missing.value.bucket
    assert name.startswith("o3cloud") and len(name) == 19 and name.isalnum() and name.islower()
    assert REGION in str(missing.value) and "Storage Blob Data Contributor" in str(missing.value)
    assert subscription.resources == {}  # nothing created without the user's OK
    with pytest.raises(ProviderError, match="isn't one of the add-on's"):
        storage.create_account("someoneelses")

    # Created with its container and the user's role, which Azure applies a
    # little later: the first upload waits for it.
    assert storage.prepare(name) == name
    account = subscription.resources[f"{ACCOUNTS}/{name}"]
    assert (account["location"], account["kind"]) == (REGION, "StorageV2")
    assert account["tags"] == {"orange3-cloud": "true"}
    properties = account["properties"]
    assert properties["allowBlobPublicAccess"] is properties["allowSharedKeyAccess"] is False
    container = f"{ACCOUNTS}/{name}/blobServices/default/containers/orange3-cloud"
    assert subscription.resources[container]["properties"]["publicAccess"] == "None"
    (role,) = subscription.blob_role(name)
    assert role["scope"] == f"{ACCOUNTS}/{name}"
    assert role["properties"]["roleDefinitionId"].endswith(f"/roleDefinitions/{BLOB_ROLE_ID}")
    assert (role["properties"]["principalId"], role["properties"]["principalType"]) == (
        USER,
        "User",
    )

    # In blocks; a broken connection is retried.
    path = tmp_path / "data.csv"
    path.write_bytes(os.urandom(10_500))
    blobs.refusing = 2
    blobs.broken_calls = {len(blobs.calls) + 8}
    progress, statuses = [], []
    uri = storage.upload(
        name, path, lambda sent, total: progress.append((sent, total)), statuses.append
    )
    # While waiting for the role: a status, and each try once, quickly.
    assert statuses[0].startswith(
        f"Waiting for Azure to apply your new role on the storage account {name} ("
    )
    assert statuses[-1] == "Uploading the file to Azure storage"
    assert blobs.options[:3] == [{}, ROLE_TRY_OPTIONS, ROLE_TRY_OPTIONS]
    stat = path.stat()
    key = object_name(path, stat.st_size, stat.st_mtime_ns)
    assert uri == f"az://{name}/orange3-cloud/{key}" and key.endswith("/data.csv")
    assert blobs.blobs[(name, "orange3-cloud", key)] == path.read_bytes()
    assert progress[0] == (0, 10_500) and progress[-1] == (10_500, 10_500)
    assert blobs.block_calls == list(range(1, 12)) and blobs.calls.count("stage_block") == 12
    assert storage.find_account() == name  # and the same next time

    # The same file isn't uploaded again.
    blobs.block_calls.clear()
    assert storage.upload(name, path) == uri and blobs.block_calls == []

    # An upload stopped midway (cancelled, Orange quit, no connection)
    # continues after the blocks Azure has, in a new process.
    other = tmp_path / "other.csv.gz"
    other.write_bytes(os.urandom(7_200))

    def stop_at_half(sent, total):
        if sent >= 4000:
            raise KeyboardInterrupt("killed")

    with pytest.raises(KeyboardInterrupt):
        storage.upload(name, other, stop_at_half)
    assert len(UploadSessions(storage.sessions.path)._read()) == 1
    blobs.block_calls.clear()
    fresh = storage_for(azure)
    assert fresh.prepare() == name
    other_uri = fresh.upload(name, other)
    assert blobs.block_calls == [5, 6, 7, 8]
    assert blobs.blobs[(name, "orange3-cloud", other_uri.split("/", 4)[4])] == other.read_bytes()
    assert UploadSessions(storage.sessions.path)._read() == {}

    # The connection gone for good: a clear error, and the upload continues later.
    third = tmp_path / "third.csv"
    third.write_bytes(os.urandom(3000))
    blobs.broken_calls = set(range(len(blobs.calls) + 3, len(blobs.calls) + 50))
    with pytest.raises(ProviderError, match="Load again to continue"):
        storage.upload(name, third)
    blobs.broken_calls = set()
    third_uri = storage.upload(name, third)
    assert blobs.blobs[(name, "orange3-cloud", third_uri.split("/", 4)[4])] == third.read_bytes()

    # A small file goes in one request.
    small = tmp_path / "small.csv"
    small.write_bytes(b"a,b\n1,2\n")
    small_uri = storage.upload(name, small)
    assert blobs.blobs[(name, "orange3-cloud", small_uri.split("/", 4)[4])] == b"a,b\n1,2\n"

    files = storage.files(name)
    names = ["data.csv", "other.csv.gz", "small.csv", "third.csv"]
    assert sorted(f.name for f in files) == names
    assert {f.uri: f.size_bytes for f in files}[uri] == 10_500
    storage.delete([uri, other_uri])
    storage.delete([uri])  # already gone
    assert sorted(f.name for f in storage.files(name)) == names[2:]
    assert block_size(10**9) == 16 * 1024**2 and block_size(10**12) == 20 * 1024**2

    # Without the role (e.g. another user of the subscription), the user is
    # asked before getting it.
    for found in [p for p in subscription.resources if "/roleAssignments/" in p]:
        del subscription.resources[found]
    other_user = storage_for(azure)
    with pytest.raises(StorageMissingError, match=f"storage account {name}") as missing:
        other_user.prepare()
    assert missing.value.bucket == name and subscription.blob_role(name) == []
    assert other_user.prepare(name) == name and len(subscription.blob_role(name)) == 1


def test_provider_stages_and_signs(azure, tmp_path):
    subscription, blobs = azure
    provider = AzureProvider(make_arm=subscription.arm, make_blob_service=blobs.service)
    provider.poll_interval = 0
    where = {"subscription": SUBSCRIPTION, "region": REGION}
    handle = MachineHandle("azure", "m1", "orange3-cloud-m1", "", "", "", extra=where)
    path = tmp_path / "data.csv"
    write_csv(path, 50)
    assert provider.stores_uploads and provider.stored_files(handle) == []
    assert provider.storage_examples() == ["az://account/container/data.csv"]
    with pytest.raises(StorageMissingError) as missing:
        provider.stage_file(handle, path)
    name = missing.value.bucket
    sent = []
    uri = provider.stage_file(
        handle, path, on_progress=lambda s, t: sent.append(s), create_bucket=name
    )
    assert uri.startswith(f"az://{name}/orange3-cloud/uploads/") and sent[-1] == path.stat().st_size
    assert provider.stage_file(handle, path) == uri  # the account exists now
    assert [f.uri for f in provider.stored_files(handle)] == [uri]

    # The machine gets a read-only user delegation SAS link; other
    # locations stay as they are.
    link = provider.machine_location(uri, handle)
    query = parse_qs(urlsplit(link).query)
    assert link.startswith(f"{blobs.web_url}/{name}/orange3-cloud/uploads/")
    assert query["sp"] == ["r"] and query["skoid"] == [USER] and query["spr"] == ["https"]
    expiry = datetime.datetime.fromisoformat(query["se"][0].replace("Z", "+00:00"))
    left = expiry - datetime.datetime.now(datetime.timezone.utc)
    assert datetime.timedelta(minutes=59) < left <= datetime.timedelta(hours=1)
    assert urllib.request.urlopen(link).read() == path.read_bytes()
    provider.machine_location(uri, handle)  # a new link at each load, with the same key
    assert blobs.calls.count("get_user_delegation_key") == 1
    web = "https://example.com/data.csv"
    assert provider.machine_location(web, handle) == web
    with pytest.raises(ProviderError, match="doesn't exist"):
        provider.machine_location(f"az://{name}/orange3-cloud/missing.csv", handle)
    with pytest.raises(ProviderError, match="isn't an az://"):
        provider.machine_location(f"az://{name}/missing.csv", handle)

    # The memory estimate reads the file's start with the user's credentials.
    estimate = estimate_stored(uri, provider.read_stored_head)
    assert estimate.columns == 5 and estimate.file_bytes == path.stat().st_size

    with pytest.raises(ProviderError, match="isn't in the storage account"):
        provider.delete_stored_files(["az://someoneelses/data/data.csv"])
    provider.delete_stored_files([uri])
    assert provider.stored_files(handle) == []


class AzureMachine(SharedMachineProvider):
    """The shared test machine, with the Azure provider's storage on fakes."""

    id = "azure"
    label = "Azure"
    stores_uploads = True
    storage_schemes = ("az://",)

    def __init__(self, url, cert, subscription, blobs):
        super().__init__(url, cert)
        self.azure = AzureProvider(make_arm=subscription.arm, make_blob_service=blobs.service)
        self.azure.poll_interval = 0
        self.azure.storage(SUBSCRIPTION, REGION).block_bytes = 4000  # several blocks

    def stage_file(self, handle, path, **kwargs):
        return self.azure.stage_file(None, path, **kwargs)

    def machine_location(self, location, handle=None):
        return self.azure.machine_location(location)

    def read_stored_head(self, uri, length):
        return self.azure.read_stored_head(uri, length)

    def stored_files(self, handle=None):
        return self.azure.stored_files()

    def delete_stored_files(self, uris):
        self.azure.delete_stored_files(uris)

    def storage_examples(self):
        return self.azure.storage_examples()


class TestCloudFileOnAzure(base.WidgetTest):
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
        providers = {"azure": ProviderSettings(SUBSCRIPTION, REGION)}
        CloudSettings(provider="azure", providers=providers).save()
        self.state = Path(tempfile.mkdtemp())
        self.subscription, self.blobs = FakeSubscription(self.state), FakeBlobs()
        self.provider = AzureMachine(self.url, self.cert, self.subscription, self.blobs)
        self.session = CloudSession(self.provider, store=HandleStore(self.state / "m.json"))
        self._previous, CloudSession._instance = CloudSession._instance, self.session
        self.session.start_machine(spec=SPEC, wait=True)
        self.process_events()

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        self.blobs.close()
        self.subscription.close()
        shutil.rmtree(self.state, ignore_errors=True)

    def wait_for(self, condition, timeout=30):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline
            QApplication.processEvents()
            time.sleep(0.02)

    def load(self, w):
        assert w.load() or w.task is not None
        self.wait_until_finished(w, timeout=60000)
        self.wait_for(lambda: w.task is None)

    def test_upload_and_load_from_azure(self):
        w = self.create_widget(OWCloudFile)
        assert "az://account/container/data.csv" in w.url_edit.placeholderText()
        asked = []
        w.confirm_create_bucket = lambda message: asked.append(message) or True
        w.set_local_path(str(self.csv))
        self.load(w)
        self.wait_for(lambda: w.data is not None)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        ((account, container, key),) = self.blobs.blobs
        assert len(asked) == 1 and account in asked[0]
        assert self.get_output(w.Outputs.data, widget=w).ref.n_rows == 500
        assert set(self.blobs.link_reads) == {key}  # the machine read it with a signed link
        uri = f"az://{account}/{container}/{key}"
        assert f"Uploaded to {uri}." in w.info_label.text()
        assert w.table_info.source == f"{self.blobs.web_url}/{account}/{container}/{key}"

        # The user's own az:// file, and one that doesn't exist.
        w.set_url(uri)
        self.load(w)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.estimate is not None and w.estimate.columns == 5
        assert self.get_output(w.Outputs.data, widget=w).ref.n_rows == 500
        w.set_url(f"az://{account}/{container}/missing.csv")
        self.load(w)
        assert "doesn't exist" in str(w.Error.cloud_error)

        # The uploaded files can be deleted.
        dialog = w.show_uploaded_files()
        self.wait_for(lambda: dialog.files is not None and not dialog.busy)
        assert [f.name for f in dialog.files] == ["data.csv"]
        dialog.set_checked([dialog.files[0].uri])
        dialog.confirm_delete = lambda names: names == ["data.csv"]
        dialog.delete_checked()
        self.wait_for(lambda: dialog.files == [] and not dialog.busy)
        assert self.blobs.blobs == {}
        dialog.close()

    def test_dialog_deleted_while_listing(self):
        """A listing that ends after its dialog was deleted (closed while
        Azure is slow) leaves the deleted dialog alone."""
        from AnyQt.sip import delete

        release, errors = threading.Event(), []
        listing = self.provider.stored_files
        self.provider.stored_files = lambda handle=None: release.wait(5) and listing(handle)
        hook, threading.excepthook = threading.excepthook, errors.append
        try:
            dialog = UploadedFilesDialog(self.provider, None)
            (thread,) = [t for t in threading.enumerate() if t.name == "orange3-cloud-uploads"]
            delete(dialog)
            release.set()
            thread.join(5)
            for _ in range(5):
                QApplication.processEvents()
        finally:
            threading.excepthook = hook
        assert errors == []


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AZURE_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AZURE_SUBSCRIPTION"),
    reason="uploads to the test subscription's storage; set ORANGE3_CLOUD_AZURE_VM_TESTS=1",
)
def test_real_storage(tmp_path):
    subscription = os.environ["ORANGE3_CLOUD_AZURE_SUBSCRIPTION"]
    region = os.environ["ORANGE3_CLOUD_AZURE_REGION"]
    providers = {"azure": ProviderSettings(subscription, region)}
    CloudSettings(provider="azure", providers=providers).save()
    provider = AzureProvider()
    where = {"subscription": subscription, "region": region}
    handle = MachineHandle("azure", "t", "t", "", "", "", extra=where)
    path = tmp_path / "smoke.csv"
    write_csv(path, 1000)
    try:
        uri = provider.stage_file(handle, path)
    except StorageMissingError as missing:
        print(missing)
        uri = provider.stage_file(handle, path, create_bucket=missing.bucket)
    try:
        assert uri in [f.uri for f in provider.stored_files(handle)]
        estimate = estimate_stored(uri, provider.read_stored_head)
        assert estimate.columns == 5 and estimate.file_bytes == path.stat().st_size
        link = provider.machine_location(uri, handle)
        assert ".blob.core.windows.net/orange3-cloud/uploads/" in link and "skoid=" in link
        assert urllib.request.urlopen(link, timeout=30).read() == path.read_bytes()
    finally:
        provider.delete_stored_files([uri])
    assert uri not in [f.uri for f in provider.stored_files(handle)]
    # The account: no anonymous access, no account keys.
    arm = provider.arm(subscription)
    account = uri.split("/")[2]
    path = f"{arm.paths.group}/providers/Microsoft.Storage/storageAccounts/{account}"
    found = arm.get(path, "read the storage account", api="2025-01-01")
    assert found["properties"]["allowBlobPublicAccess"] is False
    assert found["properties"]["allowSharedKeyAccess"] is False
