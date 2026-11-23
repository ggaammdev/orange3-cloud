"""The AWS account's S3 bucket and uploads, with S3 replaced by a fake whose
requests are checked against botocore's own model: the bucket is created
after the user agrees (tagged, public access blocked, unfinished uploads
cleaned up), uploads go up in parts that survive a broken connection and an
interrupted upload (they continue after the parts S3 has), an uploaded file
is reused, files are listed and deleted, and the machine reads ``s3://``
files through signed links. Cloud File uploads a local file and loads it on
the in-process machine, which copies the signed link's file to its disk
(the fake's links refuse HEAD requests, as S3's do), and loads an ``s3://``
URL too.

``test_real_bucket`` uploads a small file to the test account's bucket and
reads it back through a signed link; it runs only with
``ORANGE3_CLOUD_AWS_VM_TESTS=1`` and ``ORANGE3_CLOUD_AWS_PROFILE``/
``_REGION`` set (never in CI).
"""

import base64
import contextlib
import datetime
import http.server
import itertools
import os
import shutil
import tempfile
import threading
import time
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import botocore.session
import pytest
from AnyQt.QtWidgets import QApplication
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.validate import ParamValidator
from conftest import SPEC, https_server
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_cloud_file import write_csv
from test_widget_base import SharedMachineProvider

from orangecontrib.cloud.client.providers.aws import AWSProvider
from orangecontrib.cloud.client.providers.aws_storage import (
    LIFECYCLE,
    PUBLIC_ACCESS_BLOCK,
    S3Storage,
    part_size,
)
from orangecontrib.cloud.client.providers.base import (
    MachineHandle,
    ProviderError,
    StorageMissingError,
)
from orangecontrib.cloud.client.providers.uploads import UploadSessions, object_name
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.estimate import estimate_stored
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.settings import CloudSettings, ProviderSettings
from orangecontrib.cloud.widgets.owcloudfile import OWCloudFile

PROFILE, REGION = "orange3-cloud", "eu-west-1"
_MODEL = botocore.session.get_session().get_service_model("s3")
_OPERATIONS = {botocore.xform_name(name): name for name in _MODEL.operation_names}


def error(code, operation, status=400, headers=None):
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers or {}},
        },
        operation,
    )


def crc32(data):
    return base64.b64encode(zlib.crc32(data).to_bytes(4, "big")).decode()


class FakeClient:
    """A boto3 S3 client in ``region``: checks each request against
    botocore's model, then lets the fake S3 answer it."""

    def __init__(self, s3, region):
        self.s3, self.region = s3, region

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        assert ClientMethod == "get_object" and ExpiresIn == 3600
        bucket = self.s3.buckets[Params["Bucket"]]
        assert bucket["region"] == self.region  # signed for the bucket's region
        return f"{self.s3.web_url}/{Params['Bucket']}/{Params['Key']}?X-Amz-Signature=sig"

    def __getattr__(self, method):
        operation = _OPERATIONS[method]

        def call(**params):
            shape = _MODEL.operation_model(operation).input_shape
            report = ParamValidator().validate(params, shape)
            assert not report.has_errors(), report.generate_report()
            self.s3.calls.append(method)
            if len(self.s3.calls) in self.s3.broken_calls:
                raise EndpointConnectionError(endpoint_url="https://s3.amazonaws.com")
            return getattr(self.s3, method)(self.region, **params)

        return call


class FakeS3:
    """The buckets of one account in memory, and a web server that answers
    their signed links (GET only, as S3's links)."""

    def __init__(self):
        self.buckets = {}  # name -> {"region", "tags", "objects": {key: bytes}, ...}
        self.uploads = {}  # upload ID -> {"bucket", "key", "parts": {number: bytes}}
        self.calls, self.broken_calls, self.part_calls = [], set(), []
        self._ids = itertools.count(1)
        s3 = self

        class Links(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):  # noqa: N802 - S3 refuses HEAD with a link signed for GET
                self.send_response(403)
                self.end_headers()

            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                bucket, key = unquote(parts.path).lstrip("/").split("/", 1)
                data = s3.buckets.get(bucket, {}).get("objects", {}).get(key)
                if data is None or "X-Amz-Signature" not in parse_qs(parts.query):
                    self.send_response(403)
                    self.end_headers()
                    return
                s3.link_reads.append(key)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(data)  # fsspec reads only the size of the first answer

            def log_message(self, *args):
                pass

        self.link_reads = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Links)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.web_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def client(self, profile, service, region):
        assert (profile, service) == (PROFILE, "s3")
        return FakeClient(self, region)

    def close(self):
        self.server.shutdown()

    def _bucket(self, region, name, operation):
        if name not in self.buckets:
            raise error("NoSuchBucket", operation, 404)
        bucket = self.buckets[name]
        assert bucket["region"] == region, f"{operation} for {name} sent to {region}"
        return bucket

    # Buckets

    def list_buckets(self, region, Prefix, MaxBuckets, ContinuationToken=None):
        return {
            "Buckets": [
                {"Name": n, "CreationDate": b["created"], "BucketRegion": b["region"]}
                for n, b in self.buckets.items()
                if n.startswith(Prefix)
            ]
        }

    def create_bucket(self, region, Bucket, CreateBucketConfiguration, ObjectOwnership):
        location = CreateBucketConfiguration.get("LocationConstraint", "us-east-1")
        assert location == region
        created = datetime.datetime.now(datetime.timezone.utc)
        self.buckets[Bucket] = {
            "region": region,
            "tags": CreateBucketConfiguration.get("Tags"),
            "ownership": ObjectOwnership,
            "created": created,
            "objects": {},
            "modified": {},
        }
        return {"Location": f"/{Bucket}"}

    def put_public_access_block(self, region, Bucket, PublicAccessBlockConfiguration):
        self._bucket(region, Bucket, "PutPublicAccessBlock")["public"] = (
            PublicAccessBlockConfiguration
        )

    def put_bucket_lifecycle_configuration(self, region, Bucket, LifecycleConfiguration):
        self._bucket(region, Bucket, "PutBucketLifecycleConfiguration")["lifecycle"] = (
            LifecycleConfiguration
        )
        return {}

    def get_bucket_tagging(self, region, Bucket):
        tags = self._bucket(region, Bucket, "GetBucketTagging")["tags"]
        if not tags:
            raise error("NoSuchTagSet", "GetBucketTagging", 404)
        return {"TagSet": tags}

    def head_bucket(self, region, Bucket):
        if Bucket not in self.buckets:
            raise error("404", "HeadBucket", 404)
        bucket_region = self.buckets[Bucket]["region"]
        if bucket_region != region:  # S3 tells the region in its refusal
            headers = {"x-amz-bucket-region": bucket_region}
            raise error("301", "HeadBucket", 301, headers)
        return {"BucketRegion": bucket_region}

    # Files

    def _store(self, bucket, key, data):
        bucket["objects"][key] = data
        bucket["modified"][key] = datetime.datetime.now(datetime.timezone.utc)

    def head_object(self, region, Bucket, Key):
        objects = self._bucket(region, Bucket, "HeadObject")["objects"]
        if Key not in objects:
            raise error("404", "HeadObject", 404)
        return {"ContentLength": len(objects[Key])}

    def get_object(self, region, Bucket, Key, Range):
        data = self._bucket(region, Bucket, "GetObject")["objects"][Key]
        start, stop = (int(x) for x in Range.removeprefix("bytes=").split("-"))
        return {"Body": _Body(data[start : stop + 1])}

    def put_object(self, region, Bucket, Key, Body, ChecksumAlgorithm):
        self._store(self._bucket(region, Bucket, "PutObject"), Key, bytes(Body))
        return {"ETag": '"etag"', "ChecksumCRC32": crc32(Body)}

    def list_objects_v2(self, region, Bucket, Prefix):
        bucket = self._bucket(region, Bucket, "ListObjectsV2")
        contents = [
            {"Key": k, "Size": len(v), "LastModified": bucket["modified"][k]}
            for k, v in bucket["objects"].items()
            if k.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def delete_object(self, region, Bucket, Key):
        bucket = self._bucket(region, Bucket, "DeleteObject")
        bucket["objects"].pop(Key, None)
        return {}

    # Multipart uploads

    def create_multipart_upload(self, region, Bucket, Key, ChecksumAlgorithm, ContentType):
        self._bucket(region, Bucket, "CreateMultipartUpload")
        upload_id = f"upload-{next(self._ids)}"
        self.uploads[upload_id] = {"bucket": Bucket, "key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def _upload(self, upload_id, operation):
        if upload_id not in self.uploads:
            raise error("NoSuchUpload", operation, 404)
        return self.uploads[upload_id]

    def upload_part(self, region, **request):
        assert request["ChecksumAlgorithm"] == "CRC32"
        self._bucket(region, request["Bucket"], "UploadPart")
        number, body = request["PartNumber"], bytes(request["Body"])
        self.part_calls.append(number)
        self._upload(request["UploadId"], "UploadPart")["parts"][number] = body
        return {"ETag": f'"e{number}"', "ChecksumCRC32": crc32(body)}

    def list_parts(self, region, Bucket, Key, UploadId):
        parts = self._upload(UploadId, "ListParts")["parts"]
        listed = [
            {"PartNumber": n, "ETag": f'"e{n}"', "Size": len(d), "ChecksumCRC32": crc32(d)}
            for n, d in sorted(parts.items())
        ]
        return {"Parts": listed, "IsTruncated": False}

    def complete_multipart_upload(self, region, Bucket, Key, UploadId, MultipartUpload):
        upload = self._upload(UploadId, "CompleteMultipartUpload")
        parts = upload["parts"]
        listed = MultipartUpload["Parts"]
        assert [p["PartNumber"] for p in listed] == sorted(parts)
        assert all(p["ChecksumCRC32"] == crc32(parts[p["PartNumber"]]) for p in listed)
        data = b"".join(parts[n] for n in sorted(parts))
        self._store(self._bucket(region, Bucket, "CompleteMultipartUpload"), Key, data)
        del self.uploads[UploadId]
        return {"ETag": '"done"'}


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


@pytest.fixture
def s3():
    fake = FakeS3()
    yield fake
    fake.close()


def storage_for(s3, part=1000):
    return S3Storage(s3.client, PROFILE, REGION, part_bytes=part, retry_delays=(0, 0))


def test_bucket_upload_resume_and_delete(s3, tmp_path):
    storage = storage_for(s3)
    assert storage.find_bucket() is None
    s3.buckets["orange3-cloud-someone"] = {"region": REGION, "tags": None, "created": 0}
    assert storage.find_bucket() is None  # not tagged: not the add-on's
    del s3.buckets["orange3-cloud-someone"]
    with pytest.raises(ProviderError):
        storage.create_bucket("someone-elses-bucket")
    name = storage.create_bucket("orange3-cloud-0123456789ab")
    bucket = s3.buckets[name]
    assert bucket["tags"] == [{"Key": "orange3-cloud", "Value": "true"}]
    assert bucket["public"] == PUBLIC_ACCESS_BLOCK and all(PUBLIC_ACCESS_BLOCK.values())
    assert bucket["lifecycle"] == LIFECYCLE and bucket["ownership"] == "BucketOwnerEnforced"
    assert S3Storage(s3.client, PROFILE, "us-east-1").find_bucket() == name

    # In parts; a broken connection is retried.
    path = tmp_path / "data.csv"
    path.write_bytes(os.urandom(10_500))
    s3.broken_calls = {len(s3.calls) + 6}
    progress = []
    uri = storage.upload(name, path, lambda sent, total: progress.append((sent, total)))
    stat = path.stat()
    assert uri == f"s3://{name}/{object_name(path, stat.st_size, stat.st_mtime_ns)}"
    assert uri.endswith("/data.csv") and "/uploads/" in uri
    assert bucket["objects"][uri.split("/", 3)[3]] == path.read_bytes()
    assert progress[0] == (0, 10_500) and progress[-1] == (10_500, 10_500)
    assert s3.part_calls == list(range(1, 12)) and s3.calls.count("upload_part") == 12

    # The same file isn't uploaded again.
    s3.part_calls.clear()
    assert storage.upload(name, path) == uri and s3.part_calls == []

    # An upload stopped midway (cancelled, Orange quit, no connection)
    # continues after the parts S3 has, in a new process.
    other = tmp_path / "other.csv.gz"
    other.write_bytes(os.urandom(7_200))

    def stop_at_half(sent, total):
        if sent >= 4000:
            raise KeyboardInterrupt("killed")

    with pytest.raises(KeyboardInterrupt):
        storage.upload(name, other, stop_at_half)
    assert len(UploadSessions(storage.sessions.path)._read()) == 1
    s3.part_calls.clear()
    other_uri = storage_for(s3).upload(name, other)
    assert s3.part_calls == [5, 6, 7, 8]
    assert bucket["objects"][other_uri.split("/", 3)[3]] == other.read_bytes()
    assert UploadSessions(storage.sessions.path)._read() == {}

    # The connection gone for good: a clear error, and the upload continues later.
    third = tmp_path / "third.csv"
    third.write_bytes(os.urandom(3000))
    s3.broken_calls = set(range(len(s3.calls) + 3, len(s3.calls) + 50))
    with pytest.raises(ProviderError, match="Load again to continue"):
        storage.upload(name, third)
    s3.broken_calls = set()
    third_uri = storage.upload(name, third)
    assert bucket["objects"][third_uri.split("/", 3)[3]] == third.read_bytes()

    # An upload that S3 no longer has starts again; a small file goes in one request.
    fourth = tmp_path / "fourth.csv"
    fourth.write_bytes(os.urandom(2500))
    st = fourth.stat()
    fourth_uri = f"s3://{name}/{object_name(fourth, st.st_size, st.st_mtime_ns)}"
    storage.sessions.put(fourth_uri, st.st_size, st.st_mtime_ns, "upload-gone")
    s3.part_calls.clear()
    assert storage.upload(name, fourth) == fourth_uri and s3.part_calls == [1, 2, 3]
    small = tmp_path / "small.csv"
    small.write_bytes(b"a,b\n1,2\n")
    small_uri = storage.upload(name, small)
    assert bucket["objects"][small_uri.split("/", 3)[3]] == b"a,b\n1,2\n"

    files = storage.files(name)
    names = ["data.csv", "fourth.csv", "other.csv.gz", "small.csv", "third.csv"]
    assert sorted(f.name for f in files) == names
    assert {f.uri: f.size_bytes for f in files}[uri] == 10_500
    storage.delete([uri, other_uri])
    storage.delete([uri])  # already gone
    assert sorted(f.name for f in storage.files(name)) == names[1:2] + names[3:]
    assert part_size(10**9) == 16 * 1024**2 and part_size(10**12) == 96 * 1024**2


def test_provider_stages_and_signs(s3, tmp_path):
    CloudSettings(provider="aws", providers={"aws": ProviderSettings(PROFILE, REGION)}).save()
    provider = AWSProvider(make_client=s3.client)
    handle = MachineHandle(
        "aws", "m1", "orange3-cloud-m1", "", "", "", extra={"profile": PROFILE, "region": REGION}
    )
    path = tmp_path / "data.csv"
    write_csv(path, 50)
    assert provider.stores_uploads and provider.stored_files(handle) == []
    with pytest.raises(StorageMissingError) as missing:
        provider.stage_file(handle, path)
    name = missing.value.bucket
    assert name.startswith("orange3-cloud-") and len(name) == len("orange3-cloud-") + 12
    assert name in str(missing.value) and REGION in str(missing.value)
    assert s3.buckets == {}  # nothing created without the user's OK
    sent = []
    uri = provider.stage_file(
        handle, path, on_progress=lambda s, t: sent.append(s), create_bucket=name
    )
    assert uri.startswith(f"s3://{name}/uploads/") and sent[-1] == path.stat().st_size
    assert s3.buckets[name]["region"] == REGION
    assert provider.stage_file(handle, path) == uri  # the bucket exists now
    assert [f.uri for f in provider.stored_files(handle)] == [uri]

    # The machine gets a signed link; other locations stay as they are.
    link = provider.machine_location(uri, handle)
    assert link.startswith(f"{s3.web_url}/{name}/uploads/") and "X-Amz-Signature" in link
    assert urllib.request.urlopen(link).read() == path.read_bytes()
    web = "https://example.com/data.csv"
    assert provider.machine_location(web, handle) == web
    with pytest.raises(ProviderError, match="doesn't exist"):
        provider.machine_location(f"s3://{name}/missing.csv", handle)

    # The memory estimate reads the file's start with the user's credentials.
    estimate = estimate_stored(uri, provider.read_stored_head)
    assert estimate.columns == 5 and estimate.file_bytes == path.stat().st_size

    with pytest.raises(ProviderError):
        provider.delete_stored_files(["s3://someone-elses-bucket/data.csv"])
    provider.delete_stored_files([uri])
    assert provider.stored_files(handle) == []


class S3Machine(SharedMachineProvider):
    """The shared test machine, with the AWS provider's storage on a fake S3."""

    id = "aws"
    label = "AWS"
    stores_uploads = True
    storage_schemes = ("s3://",)

    def __init__(self, url, cert, s3):
        super().__init__(url, cert)
        self.aws = AWSProvider(make_client=s3.client)
        self.aws.storage(PROFILE, REGION).part_bytes = 4000  # several parts

    def stage_file(self, handle, path, **kwargs):
        return self.aws.stage_file(None, path, **kwargs)

    def machine_location(self, location, handle=None):
        return self.aws.machine_location(location)

    def read_stored_head(self, uri, length):
        return self.aws.read_stored_head(uri, length)

    def stored_files(self, handle=None):
        return self.aws.stored_files()

    def delete_stored_files(self, uris):
        self.aws.delete_stored_files(uris)


class TestCloudFileOnAws(base.WidgetTest):
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
        CloudSettings(provider="aws", providers={"aws": ProviderSettings(PROFILE, REGION)}).save()
        self.state = Path(tempfile.mkdtemp())
        self.s3 = FakeS3()
        self.provider = S3Machine(self.url, self.cert, self.s3)
        self.session = CloudSession(self.provider, store=HandleStore(self.state / "m.json"))
        self._previous, CloudSession._instance = CloudSession._instance, self.session
        self.session.start_machine(spec=SPEC, wait=True)
        self.process_events()

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        self.s3.close()
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

    def test_upload_and_load_from_s3(self):
        w = self.create_widget(OWCloudFile)
        asked = []
        w.confirm_create_bucket = lambda message: asked.append(message) or True
        w.set_local_path(str(self.csv))
        self.load(w)
        self.wait_for(lambda: w.data is not None)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        (bucket,) = self.s3.buckets
        assert len(asked) == 1 and bucket in asked[0]
        assert self.get_output(w.Outputs.data, widget=w).ref.n_rows == 500
        (key,) = self.s3.buckets[bucket]["objects"]
        assert set(self.s3.link_reads) == {key}  # the machine read it with a signed link
        assert f"Uploaded to s3://{bucket}/{key}." in w.info_label.text()
        assert w.table_info.source == f"{self.s3.web_url}/{bucket}/{key}"  # no signature

        # The user's own s3:// file, and one that doesn't exist.
        w.set_url(f"s3://{bucket}/{key}")
        self.load(w)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        assert w.estimate is not None and w.estimate.columns == 5
        assert self.get_output(w.Outputs.data, widget=w).ref.n_rows == 500
        w.set_url(f"s3://{bucket}/missing.csv")
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
        assert self.s3.buckets[bucket]["objects"] == {}
        dialog.close()


@pytest.mark.skipif(
    os.environ.get("ORANGE3_CLOUD_AWS_VM_TESTS") != "1"
    or not os.environ.get("ORANGE3_CLOUD_AWS_PROFILE"),
    reason="uploads to the test account's bucket; set ORANGE3_CLOUD_AWS_VM_TESTS=1",
)
def test_real_bucket(tmp_path):
    profile = os.environ["ORANGE3_CLOUD_AWS_PROFILE"]
    region = os.environ["ORANGE3_CLOUD_AWS_REGION"]
    CloudSettings(provider="aws", providers={"aws": ProviderSettings(profile, region)}).save()
    provider = AWSProvider()
    where = {"profile": profile, "region": region}
    handle = MachineHandle("aws", "t", "t", "", "", "", extra=where)
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
        link = provider.machine_location(uri, handle)
        assert f".s3.{region}.amazonaws.com/" in link and "X-Amz-Signature=" in link
        assert urllib.request.urlopen(link, timeout=30).read() == path.read_bytes()
    finally:
        provider.delete_stored_files([uri])
    assert uri not in [f.uri for f in provider.stored_files(handle)]
