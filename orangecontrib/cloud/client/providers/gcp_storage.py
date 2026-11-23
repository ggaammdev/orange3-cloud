"""The project's storage bucket for files uploaded from this computer.

Local files reach a machine on Google Cloud through one bucket in the user's
project, ``orange3-cloud-<project>-<suffix>``, created only after the user
agrees. Files stay there for later sessions until the user deletes them:

* **Names:** ``uploads/<key>/<file name>``, where the key comes from the
  file's path, size and modification time. A file that is already in the
  bucket with the same size isn't uploaded again.
* **Resumable:** files go up in chunks through Google's resumable uploads.
  A broken connection is retried; if the upload still stops (or Orange
  quits, or the user cancels), the session URL is kept in the add-on's
  private folder, and the next upload of the same file continues where it
  stopped.
* **Access:** the bucket has uniform access, no public access and no soft
  delete (deleted files are gone, and cost nothing). The machine's service
  account gets read access (``roles/storage.objectViewer``) on it.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable, Sequence
from functools import partial
from typing import BinaryIO, TypeVar

from orangecontrib.cloud.client.providers.base import (
    ProviderError,
    StoredFile,
    UploadProgress,
)
from orangecontrib.cloud.client.providers.gcp_storage_api import (
    NotFoundError,
    StorageApi,
    UploadExpiredError,
)
from orangecontrib.cloud.client.providers.gcp_vm import LABEL
from orangecontrib.cloud.client.providers.uploads import (
    RETRY_DELAYS,
    UPLOAD_FOLDER,
    UploadSessions,
    object_name,
    with_retries,
)

__all__ = [
    "BUCKET_PREFIX",
    "READER_ROLE",
    "SESSIONS_FILE",
    "ProjectStorage",
    "bucket_prefix",
    "new_bucket_name",
    "parse_gs_uri",
    "read_object_head",
]

log = logging.getLogger(__name__)
T = TypeVar("T")

BUCKET_PREFIX = "orange3-cloud-"
#: The role the machine's service account gets on the bucket.
READER_ROLE = "roles/storage.objectViewer"
#: Bytes per request (a multiple of 256 KiB, as Google requires).
CHUNK_BYTES = 16 * 1024**2
#: The upload sessions in progress, in the add-on's private folder.
SESSIONS_FILE = "gcs-uploads.json"


def bucket_prefix(project: str) -> str:
    """The start of the name of the project's bucket."""
    return f"{BUCKET_PREFIX}{project}-"


def new_bucket_name(project: str) -> str:
    """A name for a new bucket (bucket names are global, so it ends with a
    random suffix)."""
    return bucket_prefix(project) + secrets.token_hex(3)


def bucket_body(name: str, location: str) -> dict:
    """The ``buckets.insert`` request for the project's bucket."""
    return {
        "name": name,
        "location": location,
        "storageClass": "STANDARD",
        "labels": {LABEL: "true"},
        "iamConfiguration": {
            "uniformBucketLevelAccess": {"enabled": True},
            "publicAccessPrevention": "enforced",
        },
        "softDeletePolicy": {"retentionDurationSeconds": 0},
    }


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """``(bucket, object name)`` of ``gs://bucket/name``."""
    if not uri.startswith("gs://") or "/" not in uri[5:]:
        raise ValueError(f"'{uri}' isn't a gs://bucket/file URI.")
    bucket, name = uri[5:].split("/", 1)
    if not bucket or not name:
        raise ValueError(f"'{uri}' isn't a gs://bucket/file URI.")
    return bucket, name


class ProjectStorage:
    """The bucket of one project: find or create it, upload files, list and
    delete them.

    :param api: the Cloud Storage calls.
    :param sessions: where unfinished uploads are remembered.
    """

    def __init__(
        self,
        project: str,
        api: StorageApi,
        *,
        sessions: UploadSessions | None = None,
        chunk_bytes: int = CHUNK_BYTES,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ):
        self.project = project
        self.api = api
        self.sessions = sessions or UploadSessions(file_name=SESSIONS_FILE)
        self.chunk_bytes = chunk_bytes
        self.retry_delays = tuple(retry_delays)

    # ------------------------------------------------------------------
    # The bucket

    def find_bucket(self) -> str | None:
        """The name of the project's bucket, or ``None`` if there is none."""
        buckets = self.api.list_buckets(self.project, bucket_prefix(self.project))
        labelled = [b for b in buckets if (b.get("labels") or {}).get(LABEL) == "true"]
        found = sorted(labelled or buckets, key=lambda b: b.get("timeCreated", ""))
        return found[0]["name"] if found else None

    def create_bucket(self, name: str, location: str) -> str:
        """Create the bucket ``name`` in ``location`` (a region); returns its name."""
        if not name.startswith(bucket_prefix(self.project)):
            raise ProviderError(f"The bucket name {name} isn't one for project {self.project}.")
        self.api.create_bucket(self.project, bucket_body(name, location))
        return name

    def allow_reader(self, bucket: str, member: str) -> None:
        """Give ``member`` (e.g. ``serviceAccount:…``) read access to the
        bucket's files, unless it has it already."""
        policy = self.api.get_policy(bucket)
        bindings = policy.setdefault("bindings", [])
        binding = next((b for b in bindings if b.get("role") == READER_ROLE), None)
        if binding is None:
            binding = {"role": READER_ROLE, "members": []}
            bindings.append(binding)
        if member in binding.setdefault("members", []):
            return
        binding["members"].append(member)
        self.api.set_policy(bucket, policy)

    # ------------------------------------------------------------------
    # Files

    def files(self, bucket: str) -> list[StoredFile]:
        """The uploaded files, newest first."""
        objects = self.api.list_objects(bucket, UPLOAD_FOLDER)
        files = [
            StoredFile(
                uri=f"gs://{bucket}/{o['name']}",
                name=o["name"].rsplit("/", 1)[-1],
                size_bytes=int(o.get("size", 0)),
                updated=o.get("updated", ""),
            )
            for o in objects
        ]
        return sorted(files, key=lambda f: f.updated, reverse=True)

    def delete(self, uris: Sequence[str]) -> None:
        """Delete uploaded files (files already gone are skipped)."""
        for uri in uris:
            bucket, name = parse_gs_uri(uri)
            try:
                self.api.delete_object(bucket, name)
            except NotFoundError:
                log.info("%s was already deleted", uri)
            self.sessions.drop(uri)

    def upload(
        self, bucket: str, path: str | os.PathLike, on_progress: UploadProgress | None = None
    ) -> str:
        """Upload a local file to the bucket (or find it there) and return
        its ``gs://`` URI. ``on_progress(sent, total)`` may raise to stop;
        the upload then continues next time."""
        report = on_progress or (lambda sent, total: None)
        stat = os.stat(path)
        size, mtime = stat.st_size, stat.st_mtime_ns
        name = object_name(path, size, mtime)
        uri = f"gs://{bucket}/{name}"
        if self._uploaded(bucket, name, size):
            self.sessions.drop(uri)
            report(size, size)
            return uri
        with open(path, "rb") as f:
            self._send(f, bucket, name, (size, mtime), report)
        return uri

    def _uploaded(self, bucket: str, name: str, size: int) -> bool:
        try:
            existing = self.api.get_object(bucket, name)
        except NotFoundError:
            return False
        return int(existing.get("size", -1)) == size

    def _session(
        self, bucket: str, name: str, file: tuple[int, int], *, fresh: bool = False
    ) -> tuple[str, int | dict]:
        """(session URL, bytes Google has or the finished object): the
        remembered session of this file, else a new one."""
        uri = f"gs://{bucket}/{name}"
        size, mtime = file
        session_url = None if fresh else self.sessions.get(uri, size, mtime)
        if session_url is not None:
            try:
                state = self._with_retries(lambda: self.api.upload_offset(session_url, size))
            except UploadExpiredError:
                log.info("The upload session of %s expired; starting again", uri)
            else:
                log.info("Resuming the upload of %s at byte %s of %s", uri, state, size)
                return session_url, state
        session_url = self._with_retries(lambda: self.api.start_upload(bucket, name, size))
        self.sessions.put(uri, size, mtime, session_url)
        return session_url, 0

    def _send(
        self, f: BinaryIO, bucket: str, name: str, file: tuple[int, int], report: UploadProgress
    ) -> None:
        """Send the file's chunks from where Google's copy ends."""
        uri, size = f"gs://{bucket}/{name}", file[0]
        session_url, state = self._session(bucket, name, file)
        restarted = False
        while not isinstance(state, dict):
            report(state, size)
            f.seek(state)
            data = f.read(self.chunk_bytes)
            offset = state
            try:
                state = self._with_retries(
                    partial(self.api.upload_chunk, session_url, data, offset, size),
                    partial(self.api.upload_offset, session_url, size),
                )
            except UploadExpiredError:
                if restarted:
                    raise
                restarted = True
                session_url, state = self._session(bucket, name, file, fresh=True)
        self.sessions.drop(uri)
        report(size, size)

    def _with_retries(self, call: Callable[[], T], check: Callable[[], T] | None = None) -> T:
        """``call()``, retried after broken connections; with ``check``,
        each retry asks it first (how much of the upload arrived)."""
        return with_retries(call, self.retry_delays, "Google Cloud", check)


def read_object_head(uri: str, length: int, api: StorageApi | None = None) -> tuple[int, bytes]:
    """``(size, first bytes)`` of a ``gs://`` file, read with the user's
    Google sign-in (for the memory estimate before loading)."""
    bucket, name = parse_gs_uri(uri)
    if api is None:
        from orangecontrib.cloud.client.providers.gcp_account import load_credentials
        from orangecontrib.cloud.client.providers.gcp_storage_api import GoogleStorageApi

        api = GoogleStorageApi(load_credentials()[0], timeout=30)
    size = int(api.get_object(bucket, name).get("size", 0))
    return size, api.read_object(bucket, name, length) if length else b""
