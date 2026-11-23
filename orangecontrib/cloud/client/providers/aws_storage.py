"""The add-on's S3 bucket for files uploaded from this computer, and the
signed links through which the machine reads files in S3.

Local files reach a machine on AWS through one bucket in the user's account,
``orange3-cloud-<random suffix>`` (bucket names are public, so the name
doesn't contain the account ID). It is created in the machine's region only
after the user agrees, tagged ``orange3-cloud=true``, with all public access
blocked, and found again by its name and tag. Files stay there for later
sessions until the user deletes them:

* **Names:** as on Google Cloud, ``uploads/<key>/<file name>``
  (:func:`~orangecontrib.cloud.client.providers.uploads.object_name`). A file
  that is already in the bucket with the same size isn't uploaded again.
* **In parts:** a file goes up as an S3 multipart upload in parts of 16 MiB
  (larger for files over 160 GB, as S3 allows 10,000 parts), each with a
  CRC32 checksum that S3 checks. A broken connection is retried; if the
  upload still stops (or Orange quits, or the user cancels), its upload ID
  is kept in the add-on's private folder, and the next upload of the same
  file sends only the parts S3 doesn't have. The bucket's lifecycle rule
  deletes the parts of uploads left unfinished for 7 days.
* **The machine reads** an uploaded file, or any ``s3://`` file the user can
  read, through a link signed with the user's credentials and made anew at
  each load (:meth:`S3Storage.signed_link`), so it needs no AWS access of
  its own. A link signed with temporary credentials (``aws login``, IAM
  Identity Center) works only as long as they do, 5 to 15 minutes, so the
  machine copies the file to its disk with one request before reading it.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, BinaryIO, TypeVar

from orangecontrib.cloud.client.providers.aws_account import aws_error, error_code
from orangecontrib.cloud.client.providers.aws_vm import TAG
from orangecontrib.cloud.client.providers.base import ProviderError, StoredFile, UploadProgress
from orangecontrib.cloud.client.providers.uploads import (
    RETRY_DELAYS,
    UPLOAD_FOLDER,
    TransientError,
    UploadSessions,
    object_name,
    with_retries,
)

__all__ = [
    "BUCKET_PREFIX",
    "LIFECYCLE",
    "LINK_SECONDS",
    "PUBLIC_ACCESS_BLOCK",
    "NotFoundError",
    "S3Storage",
    "UploadGoneError",
    "bucket_request",
    "new_bucket_name",
    "parse_s3_uri",
    "part_size",
]

log = logging.getLogger(__name__)
T = TypeVar("T")

#: ``make_client(profile, service, region)``: a ``boto3`` client.
MakeClient = Callable[[str, str, str], Any]

BUCKET_PREFIX = "orange3-cloud-"
#: Bytes per part of an upload (S3 wants at least 5 MiB, except the last).
PART_BYTES = 16 * 1024**2
#: S3's largest number of parts of one upload.
MAX_PARTS = 10_000
#: Seconds a signed link lives at most (temporary credentials end it earlier).
LINK_SECONDS = 3600
#: Days after which the bucket deletes the parts of an unfinished upload.
UNFINISHED_UPLOAD_DAYS = 7
#: The uploads in progress, in the add-on's private folder.
SESSIONS_FILE = "s3-uploads.json"
#: The checksum S3 checks for each part.
CHECKSUM = "CRC32"

#: The add-on's bucket blocks all public access.
PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}
#: The bucket's lifecycle: parts of unfinished uploads cost storage, so
#: they are deleted after a week.
LIFECYCLE = {
    "Rules": [
        {
            "ID": "orange3-cloud-unfinished-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": UPLOAD_FOLDER},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": UNFINISHED_UPLOAD_DAYS},
        }
    ]
}

_NOT_FOUND = {"NoSuchBucket", "NoSuchKey", "NoSuchTagSet", "NotFound", "404"}
_TRANSIENT = {"RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable"}


class NotFoundError(ProviderError):
    """The bucket, file or tags don't exist."""


class UploadGoneError(ProviderError):
    """The multipart upload doesn't exist (any more): start it again."""


def new_bucket_name() -> str:
    """A name for a new bucket: bucket names are global, so it ends with a
    random suffix."""
    return BUCKET_PREFIX + secrets.token_hex(6)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """``(bucket, key)`` of ``s3://bucket/key``."""
    bucket, _, key = uri[5:].partition("/") if uri.lower().startswith("s3://") else ("", "", "")
    if not bucket or not key:
        raise ProviderError(f"'{uri}' isn't an s3://bucket/file URI.")
    return bucket, key


def part_size(size: int, minimum: int = PART_BYTES) -> int:
    """The part size of an upload of ``size`` bytes: ``minimum``, or more
    (in whole MiB) for a file that needs more than S3's 10,000 parts."""
    mib = 1024**2
    needed = -(-size // MAX_PARTS)
    return minimum if needed <= minimum else -(-needed // mib) * mib


def bucket_request(name: str, region: str) -> dict[str, Any]:
    """The ``CreateBucket`` request of the add-on's bucket in ``region``,
    tagged ``orange3-cloud=true`` (us-east-1 takes no location)."""
    config: dict[str, Any] = {"Tags": [{"Key": TAG, "Value": "true"}]}
    if region != "us-east-1":
        config["LocationConstraint"] = region
    return {
        "Bucket": name,
        "CreateBucketConfiguration": config,
        "ObjectOwnership": "BucketOwnerEnforced",
    }


@dataclass(frozen=True)
class S3Object:
    """A file in S3, and the region its bucket is in."""

    region: str
    bucket: str
    key: str

    @property
    def uri(self) -> str:
        """``s3://bucket/key``."""
        return f"s3://{self.bucket}/{self.key}"


def _region_of(reply: dict[str, Any]) -> str:
    """The bucket's region in a ``HeadBucket`` answer (also in a refusal)."""
    headers = reply.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    return reply.get("BucketRegion") or headers.get("x-amz-bucket-region", "")


def _iso(value: Any) -> str:
    """A time from ``boto3`` as ISO 8601 text."""
    return value.isoformat(timespec="seconds") if hasattr(value, "isoformat") else str(value or "")


def _part_length(number: int, part: int, size: int) -> int:
    """Bytes in part ``number`` (from 1) of a file of ``size`` bytes."""
    return min(part, size - (number - 1) * part)


def _completed(part: dict[str, Any]) -> dict[str, Any]:
    """A part as ``CompleteMultipartUpload`` lists it."""
    listed = {"PartNumber": part["PartNumber"], "ETag": part["ETag"]}
    if part.get("ChecksumCRC32"):
        listed["ChecksumCRC32"] = part["ChecksumCRC32"]
    return listed


class S3Storage:
    """The add-on's bucket in the AWS account of ``profile``: find or create
    it, upload files, list and delete them; and links to the ``s3://`` files
    the user can read.

    :param make_client: ``make_client(profile, "s3", region)``, a ``boto3``
        S3 client.
    :param region: the machine's region, where a new bucket goes.
    """

    def __init__(
        self,
        make_client: MakeClient,
        profile: str,
        region: str,
        *,
        part_bytes: int = PART_BYTES,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ):
        self._make_client = make_client
        self.profile = profile
        self.region = region
        self.part_bytes = part_bytes
        self.retry_delays = tuple(retry_delays)
        self.sessions = UploadSessions(file_name=SESSIONS_FILE)
        self._regions: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Calls

    def _call(self, region: str | None, operation: str, what: str, **kwargs: Any) -> Any:
        """``operation`` of the S3 client in ``region`` (default: the
        machine's), with its errors as :class:`ProviderError`."""
        from botocore import exceptions as boto

        try:
            api = self._make_client(self.profile, "s3", region or self.region)
            return getattr(api, operation)(**kwargs)
        except boto.ClientError as exc:
            raise self._error(exc, what) from exc
        except (boto.ConnectionError, boto.HTTPClientError) as exc:
            raise TransientError(f"Couldn't reach AWS to {what}.") from exc
        except boto.BotoCoreError as exc:
            raise aws_error(exc, what, self.profile) from exc

    def _error(self, exc: Exception, what: str) -> ProviderError:
        code = error_code(exc)
        status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in _NOT_FOUND:
            return NotFoundError(f"Couldn't {what}: it doesn't exist.")
        if code == "NoSuchUpload":
            return UploadGoneError(f"Couldn't {what}: the upload is gone.")
        if code in _TRANSIENT or (status or 0) >= 500:
            return TransientError(f"AWS couldn't {what} just now.")
        if code == "403":  # HEAD requests have no error code
            return ProviderError(f'Couldn\'t {what}: the profile "{self.profile}" may not.')
        return aws_error(exc, what, self.profile)

    def _retry(self, call: Callable[[], T]) -> T:
        """``call()``, retried after broken connections."""
        return with_retries(call, self.retry_delays, "AWS")

    # ------------------------------------------------------------------
    # The bucket

    def find_bucket(self) -> str | None:
        """The add-on's bucket (named ``orange3-cloud-…`` and tagged
        ``orange3-cloud=true``): the oldest in the machine's region, else
        the oldest; ``None`` if there is none."""
        listed: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"Prefix": BUCKET_PREFIX, "MaxBuckets": 1000}
        while True:
            reply = self._call(None, "list_buckets", "list the storage buckets", **kwargs)
            listed += reply.get("Buckets", [])
            if not reply.get("ContinuationToken"):
                break
            kwargs["ContinuationToken"] = reply["ContinuationToken"]
        for bucket in listed:
            if bucket.get("BucketRegion"):
                self._regions[bucket["Name"]] = bucket["BucketRegion"]
        ours = [b for b in listed if self._tagged(b["Name"])]
        ours.sort(key=lambda b: (self._regions[b["Name"]] != self.region, str(b["CreationDate"])))
        return ours[0]["Name"] if ours else None

    def _tagged(self, bucket: str) -> bool:
        """Whether the bucket has the add-on's tag."""
        try:
            reply = self._call(
                self.bucket_region(bucket),
                "get_bucket_tagging",
                "read the bucket's tags",
                Bucket=bucket,
            )
        except NotFoundError:  # no tags, or the bucket is gone
            return False
        return {"Key": TAG, "Value": "true"} in reply.get("TagSet", [])

    def create_bucket(self, name: str) -> str:
        """Create the bucket ``name`` in the machine's region: tagged, all
        public access blocked, and the parts of unfinished uploads deleted
        after a week. Returns its name."""
        if not name.startswith(BUCKET_PREFIX):
            raise ProviderError(f"The bucket name {name} isn't one of the add-on's.")
        what = "create the storage bucket"
        self._call(None, "create_bucket", what, **bucket_request(name, self.region))
        self._regions[name] = self.region
        self._call(
            None,
            "put_public_access_block",
            what,
            Bucket=name,
            PublicAccessBlockConfiguration=PUBLIC_ACCESS_BLOCK,
        )
        self._call(
            None,
            "put_bucket_lifecycle_configuration",
            what,
            Bucket=name,
            LifecycleConfiguration=LIFECYCLE,
        )
        return name

    def bucket_region(self, bucket: str) -> str:
        """The region of ``bucket`` (S3 tells it even to those who may not
        read the bucket itself)."""
        if bucket not in self._regions:
            from botocore.exceptions import ClientError

            try:
                reply = self._call(None, "head_bucket", f"find the bucket {bucket}", Bucket=bucket)
            except ProviderError as exc:
                cause = exc.__cause__
                reply = cause.response if isinstance(cause, ClientError) else {}
                if not _region_of(reply):
                    raise
            self._regions[bucket] = _region_of(reply) or self.region
        return self._regions[bucket]

    def _object(self, uri: str) -> S3Object:
        bucket, key = parse_s3_uri(uri)
        return S3Object(self.bucket_region(bucket), bucket, key)

    # ------------------------------------------------------------------
    # Files

    def files(self, bucket: str) -> list[StoredFile]:
        """The uploaded files, newest first."""
        region = self.bucket_region(bucket)
        objects: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": UPLOAD_FOLDER}
        while True:
            reply = self._call(region, "list_objects_v2", "list the uploaded files", **kwargs)
            objects += reply.get("Contents", [])
            if not reply.get("IsTruncated"):
                break
            kwargs["ContinuationToken"] = reply["NextContinuationToken"]
        files = [
            StoredFile(
                uri=f"s3://{bucket}/{o['Key']}",
                name=o["Key"].rsplit("/", 1)[-1],
                size_bytes=int(o.get("Size", 0)),
                updated=_iso(o.get("LastModified")),
            )
            for o in objects
        ]
        return sorted(files, key=lambda f: f.updated, reverse=True)

    def delete(self, uris: Sequence[str]) -> None:
        """Delete uploaded files (files already gone are skipped)."""
        for uri in uris:
            target = self._object(uri)
            self._call(
                target.region, "delete_object", "delete the file", Bucket=target.bucket,
                Key=target.key,
            )  # fmt: skip
            self.sessions.drop(uri)

    def head(self, uri: str) -> dict[str, Any]:
        """The metadata of the ``s3://`` file ``uri``, read with the user's
        credentials; raises :class:`ProviderError` if they can't read it."""
        target = self._object(uri)
        return self._call(
            target.region, "head_object", f"read {uri}", Bucket=target.bucket, Key=target.key
        )

    def read_head(self, uri: str, length: int) -> tuple[int, bytes]:
        """``(size, first length bytes)`` of the ``s3://`` file ``uri``."""
        target = self._object(uri)
        size = int(self.head(uri).get("ContentLength", 0))
        if not length or not size:
            return size, b""
        reply = self._call(
            target.region, "get_object", f"read {uri}", Bucket=target.bucket, Key=target.key,
            Range=f"bytes=0-{length - 1}",
        )  # fmt: skip
        return size, reply["Body"].read()

    def signed_link(self, uri: str) -> str:
        """A link with which the machine reads the ``s3://`` file ``uri``,
        signed with the user's credentials for :data:`LINK_SECONDS` (or until
        temporary credentials end). Raises :class:`ProviderError` if the
        user can't read the file."""
        target = self._object(uri)
        self.head(uri)
        return self._call(
            target.region,
            "generate_presigned_url",
            f"sign a link to {uri}",
            ClientMethod="get_object",
            Params={"Bucket": target.bucket, "Key": target.key},
            ExpiresIn=LINK_SECONDS,
        )

    # ------------------------------------------------------------------
    # Uploads

    def upload(
        self, bucket: str, path: str | os.PathLike, on_progress: UploadProgress | None = None
    ) -> str:
        """Upload a local file to the bucket (or find it there) and return
        its ``s3://`` URI. ``on_progress(sent, total)`` may raise to stop;
        the upload then continues next time."""
        report = on_progress or (lambda sent, total: None)
        stat = os.stat(path)
        size, mtime = stat.st_size, stat.st_mtime_ns
        target = S3Object(self.bucket_region(bucket), bucket, object_name(path, size, mtime))
        if self._uploaded(target, size):
            self.sessions.drop(target.uri)
            report(size, size)
            return target.uri
        with open(path, "rb") as f:
            if size <= self.part_bytes:
                self._put(f, target, size, report)
            else:
                self._send_parts(f, target, (size, mtime), report)
        report(size, size)
        return target.uri

    def _uploaded(self, target: S3Object, size: int) -> bool:
        try:
            existing = self.head(target.uri)
        except NotFoundError:
            return False
        return int(existing.get("ContentLength", -1)) == size

    def _put(self, f: BinaryIO, target: S3Object, size: int, report: UploadProgress) -> None:
        """A small file, in one request."""
        report(0, size)
        data = f.read()
        self._retry(
            partial(
                self._call, target.region, "put_object", "upload the file", Bucket=target.bucket,
                Key=target.key, Body=data, ChecksumAlgorithm=CHECKSUM,
            )
        )  # fmt: skip

    def _send_parts(
        self, f: BinaryIO, target: S3Object, file: tuple[int, int], report: UploadProgress
    ) -> None:
        """Send the parts S3 doesn't have yet, then finish the upload."""
        size = file[0]
        part = part_size(size, self.part_bytes)
        upload_id, done = self._multipart(target, file, part)
        for number in range(1, -(-size // part) + 1):
            if number in done:
                continue
            report(sum(_part_length(n, part, size) for n in done), size)
            f.seek((number - 1) * part)
            send = partial(self._upload_part, target, upload_id, number, f.read(part))
            try:
                done[number] = self._retry(send)
            except UploadGoneError:
                self.sessions.drop(target.uri)
                raise ProviderError(
                    "The upload was cancelled on AWS. Load again to start it again."
                ) from None
        self._complete(target, upload_id, done, size)
        self.sessions.drop(target.uri)

    def _multipart(
        self, target: S3Object, file: tuple[int, int], part: int
    ) -> tuple[str, dict[int, dict[str, Any]]]:
        """(upload ID, the parts S3 has): the remembered upload of this
        file, else a new one."""
        size, mtime = file
        upload_id = self.sessions.get(target.uri, size, mtime)
        if upload_id is not None:
            try:
                parts = self._retry(partial(self._list_parts, target, upload_id))
            except UploadGoneError:
                log.info("The upload of %s is gone; starting again", target.uri)
            else:
                if all(p["Size"] == _part_length(p["PartNumber"], part, size) for p in parts):
                    log.info("Resuming the upload of %s after %s parts", target.uri, len(parts))
                    return upload_id, {p["PartNumber"]: _completed(p) for p in parts}
                log.info("The parts of %s have another size; starting again", target.uri)
        reply = self._retry(
            partial(
                self._call, target.region, "create_multipart_upload", "start the upload",
                Bucket=target.bucket, Key=target.key, ChecksumAlgorithm=CHECKSUM,
                ContentType="application/octet-stream",
            )
        )  # fmt: skip
        self.sessions.put(target.uri, size, mtime, reply["UploadId"])
        return reply["UploadId"], {}

    def _list_parts(self, target: S3Object, upload_id: str) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"Bucket": target.bucket, "Key": target.key, "UploadId": upload_id}
        while True:
            reply = self._call(target.region, "list_parts", "check the upload", **kwargs)
            parts += reply.get("Parts", [])
            if not reply.get("IsTruncated"):
                return parts
            kwargs["PartNumberMarker"] = reply["NextPartNumberMarker"]

    def _upload_part(
        self, target: S3Object, upload_id: str, number: int, data: bytes
    ) -> dict[str, Any]:
        reply = self._call(
            target.region, "upload_part", "upload the file", Bucket=target.bucket,
            Key=target.key, UploadId=upload_id, PartNumber=number, Body=data,
            ChecksumAlgorithm=CHECKSUM,
        )  # fmt: skip
        return _completed({"PartNumber": number, **reply})

    def _complete(
        self, target: S3Object, upload_id: str, done: dict[int, dict[str, Any]], size: int
    ) -> None:
        """Join the parts into the file."""
        parts = [done[n] for n in sorted(done)]
        try:
            self._retry(
                partial(
                    self._call, target.region, "complete_multipart_upload", "finish the upload",
                    Bucket=target.bucket, Key=target.key, UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            )  # fmt: skip
        except UploadGoneError:
            # A retry after S3 finished it (and the answer was lost) finds no upload.
            if not self._uploaded(target, size):
                raise
