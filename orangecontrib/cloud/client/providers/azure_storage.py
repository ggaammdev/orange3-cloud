"""The add-on's Azure storage account for files uploaded from this computer,
and the signed links through which the machine reads files in Azure Blob
Storage.

Local files reach a machine on Azure through one storage account in the
user's subscription, ``o3cloud<random suffix>`` (account names are public
and global, so the name has no subscription ID), in the resource group
``orange3-cloud`` and the machine's region, with the container
``orange3-cloud``. It is created only after the user agrees, tagged
``orange3-cloud=true``, with anonymous access and access with account keys
off: the add-on signs in with the user's Azure CLI credentials, which need
the role "Storage Blob Data Contributor" on the account even for its owner.
The add-on gives the user that role when it creates the account, and offers
to when it is missing; Azure takes a minute or two to apply a new role, and
the first upload waits for it. Files stay there for later sessions until
the user deletes them:

* **Names:** as on the other clouds, ``uploads/<key>/<file name>``
  (:func:`~orangecontrib.cloud.client.providers.uploads.object_name`). A file
  that is already there with the same size isn't uploaded again.
* **In blocks:** a file goes up as a block blob in blocks of 16 MiB (larger
  for files over about 800 GB, as a blob has at most 50,000 blocks), each with an
  MD5 checksum that Azure checks, and the blocks are committed at the end. A
  broken connection is retried; if the upload still stops (or Orange quits,
  or the user cancels), the upload's random block ID prefix is kept in the
  add-on's private folder, and the next upload of the same file sends only
  the blocks Azure doesn't have. Azure deletes uncommitted blocks after a
  week.
* **The machine reads** an uploaded file, or any file the user can read
  (``az://<account>/<container>/<path>``), through a link with a user
  delegation SAS: signed with a key that the user's sign-in gets from the
  account (kept for a day), read-only, for one blob, for an hour, made anew
  at each load (:meth:`AzureStorage.signed_link`). So the machine needs no
  Azure access of its own.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, BinaryIO, TypeVar

from orangecontrib.cloud.client.providers.azure_account import (
    MANAGEMENT_SCOPE,
    azure_call,
    azure_error,
)
from orangecontrib.cloud.client.providers.azure_arm import Arm, ArmError
from orangecontrib.cloud.client.providers.azure_setup import Permission, allowed
from orangecontrib.cloud.client.providers.azure_vm import AUTHORIZATION_API, TAG
from orangecontrib.cloud.client.providers.base import (
    ProviderError,
    StatusCallback,
    StorageMissingError,
    StoredFile,
    UploadProgress,
)
from orangecontrib.cloud.client.providers.uploads import (
    RETRY_DELAYS,
    UPLOAD_FOLDER,
    TransientError,
    UploadSessions,
    object_name,
    with_retries,
)

__all__ = [
    "ACCOUNT_PREFIX",
    "BLOB_ROLE",
    "BLOB_ROLE_ID",
    "CONTAINER",
    "LINK_SECONDS",
    "STORAGE_API",
    "AzBlob",
    "AzureStorage",
    "NotFoundError",
    "RoleMissingError",
    "account_body",
    "blob_service",
    "block_size",
    "is_addon_file",
    "new_account_name",
    "parse_az_uri",
    "principal",
]

log = logging.getLogger(__name__)
T = TypeVar("T")

#: ``make_blob_service(credential, account)``: an Azure ``BlobServiceClient``.
MakeBlobService = Callable[[Any, str], Any]

ACCOUNT_PREFIX = "o3cloud"
#: The container of the uploaded files in the add-on's account.
CONTAINER = "orange3-cloud"
#: Resource Manager API version of the storage requests.
STORAGE_API = "2025-01-01"
#: The built-in role that lets the user read and write the account's blobs.
BLOB_ROLE = "Storage Blob Data Contributor"
BLOB_ROLE_ID = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
#: What the add-on does with the blobs of its account (data actions).
DATA_ACTIONS = tuple(
    f"Microsoft.Storage/storageAccounts/blobServices/containers/blobs/{action}"
    for action in ("read", "write", "delete")
)
#: Bytes per block of an upload.
BLOCK_BYTES = 16 * 1024**2
#: Azure's largest number of blocks of one blob.
MAX_BLOCKS = 50_000
#: Seconds a signed link lives.
LINK_SECONDS = 3600
#: Seconds a user delegation key (which signs the links) lives (at most 7 days).
KEY_SECONDS = 24 * 3600
#: Links start this long ago, for clocks that are a little off.
CLOCK_SKEW = datetime.timedelta(minutes=5)
#: Seconds to wait for Azure to apply a role it just assigned, and between tries.
ROLE_WAIT = 5 * 60
ROLE_POLL = 10.0
#: Options of each try while waiting for a new role: one short request,
#: without the Azure library's retries (the wait tries again itself), so
#: giving up takes :data:`ROLE_WAIT` and not minutes more.
ROLE_TRY_OPTIONS = {"retry_total": 0, "connection_timeout": 10, "read_timeout": 15}
#: Seconds to wait for a new storage account.
CREATE_TIMEOUT = 5 * 60
#: The uploads in progress, in the add-on's private folder.
SESSIONS_FILE = "azure-uploads.json"

_ACCOUNT_NAME = re.compile(r"[a-z0-9]{3,24}")
_CONTAINER_NAME = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62}")
_TRANSIENT = {"ServerBusy", "OperationTimedOut", "InternalError"}
_NO_ROLE = {"AuthorizationPermissionMismatch", "AuthorizationFailure"}


class NotFoundError(ProviderError):
    """The blob or container doesn't exist."""


class RoleMissingError(ProviderError):
    """The user's sign-in may not do this with the account's blobs (no data
    role, or one Azure hasn't applied yet)."""


class UploadGoneError(ProviderError):
    """Azure no longer has the upload's blocks: start it again."""


def new_account_name() -> str:
    """A name for a new storage account: names are global, so it ends with
    a random suffix (lower-case letters and digits only)."""
    return ACCOUNT_PREFIX + secrets.token_hex(6)


@dataclass(frozen=True)
class AzBlob:
    """A blob in Azure Blob Storage."""

    account: str
    container: str
    name: str

    @property
    def uri(self) -> str:
        """``az://<account>/<container>/<name>``."""
        return f"az://{self.account}/{self.container}/{self.name}"


def parse_az_uri(uri: str) -> AzBlob:
    """The blob of ``az://<account>/<container>/<path>``."""
    rest = uri[5:] if uri.lower().startswith("az://") else ""
    account, _, rest = rest.partition("/")
    container, _, name = rest.partition("/")
    if not (_ACCOUNT_NAME.fullmatch(account) and _CONTAINER_NAME.fullmatch(container) and name):
        raise ProviderError(
            f"'{uri}' isn't an az://<storage account>/<container>/<path> URI "
            "(account and container names in lower case)."
        )
    return AzBlob(account, container, name)


def is_addon_file(uri: str) -> bool:
    """Whether ``uri`` is an uploaded file in the add-on's storage."""
    blob = parse_az_uri(uri)
    return (
        blob.account.startswith(ACCOUNT_PREFIX)
        and blob.container == CONTAINER
        and blob.name.startswith(UPLOAD_FOLDER)
    )


def block_size(size: int, minimum: int = BLOCK_BYTES) -> int:
    """The block size of an upload of ``size`` bytes: ``minimum``, or more
    (in whole MiB) for a file that needs more than Azure's 50,000 blocks."""
    mib = 1024**2
    needed = -(-size // MAX_BLOCKS)
    return minimum if needed <= minimum else -(-needed // mib) * mib


def account_body(region: str) -> dict[str, Any]:
    """The add-on's storage account in ``region``: standard, locally
    redundant, tagged, no anonymous access and no access with account keys
    (only Microsoft Entra sign-ins and the SAS links they sign)."""
    return {
        "location": region,
        "kind": "StorageV2",
        "sku": {"name": "Standard_LRS"},
        "tags": {TAG: "true"},
        "properties": {
            "accessTier": "Hot",
            "allowBlobPublicAccess": False,
            "allowSharedKeyAccess": False,
            "allowCrossTenantReplication": False,
            "defaultToOAuthAuthentication": True,
            "minimumTlsVersion": "TLS1_2",
            "supportsHttpsTrafficOnly": True,
        },
    }


def principal(credential: Any) -> tuple[str, str]:
    """``(object ID, principal type)`` of the signed-in user (or service
    principal), from the claims of its Resource Manager token."""
    token = azure_call("check who is signed in", lambda: credential.get_token(MANAGEMENT_SCOPE))
    token = token.token
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        object_id = str(claims["oid"])
    except (IndexError, KeyError, ValueError) as exc:
        raise ProviderError("Couldn't tell who is signed in to Azure.") from exc
    return object_id, "ServicePrincipal" if claims.get("idtyp") == "app" else "User"


def blob_service(credential: Any, account: str) -> Any:
    """An Azure ``BlobServiceClient`` of ``account`` with the user's
    credential: short timeouts and few retries (uploads retry by themselves)."""
    from azure.storage.blob import BlobServiceClient, ExponentialRetry

    return BlobServiceClient(
        f"https://{account}.blob.core.windows.net",
        credential=credential,
        retry_policy=ExponentialRetry(initial_backoff=1, increment_base=2, retry_total=2),
        connection_timeout=10,
        read_timeout=60,
    )


def _error(exc: Exception, what: str, account: str) -> ProviderError:
    """An error of the Azure Storage library as a :class:`ProviderError`."""
    from azure.core import exceptions as azure

    status = getattr(exc, "status_code", None) or 0
    code = str(getattr(exc, "error_code", "") or "")
    if isinstance(exc, (azure.ServiceRequestError, azure.ServiceResponseError)):
        return TransientError(f"Couldn't reach Azure to {what}.")
    if isinstance(exc, azure.ResourceNotFoundError) or status == 404:
        return NotFoundError(f"Couldn't {what}: it doesn't exist.")
    if code in _TRANSIENT or status >= 500:
        return TransientError(f"Azure couldn't {what} just now.")
    if code == "InvalidBlockList":
        return UploadGoneError(f"Couldn't {what}: Azure no longer has the upload.")
    if code in _NO_ROLE or (status == 403 and not isinstance(exc, azure.ClientAuthenticationError)):
        return RoleMissingError(
            f"Couldn't {what}: your Azure sign-in may not. Reading needs the role "
            f"“Storage Blob Data Reader” on the storage account {account} (uploads: "
            f"“{BLOB_ROLE}”). A role assigned in the last few minutes may not apply yet."
        )
    return azure_error(exc, what)


def _created(account: dict[str, Any]) -> str:
    """When a storage account (as Resource Manager lists it) was created."""
    return str((account.get("properties") or {}).get("creationTime", ""))


def _part_length(number: int, part: int, size: int) -> int:
    """Bytes in block ``number`` (from 1) of a file of ``size`` bytes."""
    return min(part, size - (number - 1) * part)


def _block_id(session: str, number: int) -> str:
    """The ID of block ``number`` of an upload (the same length for all)."""
    return f"{session}-{number:06d}"


def _block_number(session: str, block_id: str) -> int | None:
    """The number of a block of the upload ``session`` (``None``: another's)."""
    prefix, _, number = block_id.partition("-")
    return int(number) if prefix == session and number.isdigit() else None


class AzureStorage:
    """The add-on's storage account in a subscription: find or create it,
    give the user its data role, upload files, list and delete them; and
    links to the ``az://`` files the user can read.

    :param arm: Resource Manager calls in the subscription (with its
        ``credential``, which the blob calls use too).
    :param region: the machine's region, where a new account goes.
    :param make_blob_service: ``make_blob_service(credential, account)``,
        default :func:`blob_service` (tests give fakes).
    """

    #: Seconds to wait for Azure to apply a role it just assigned.
    role_wait = ROLE_WAIT
    role_poll = ROLE_POLL

    def __init__(
        self,
        arm: Arm,
        region: str,
        make_blob_service: MakeBlobService = blob_service,
        *,
        block_bytes: int = BLOCK_BYTES,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ):
        self.arm = arm
        self.region = region
        self._make_blob_service = make_blob_service
        self.block_bytes = block_bytes
        self.retry_delays = tuple(retry_delays)
        self.sessions = UploadSessions(file_name=SESSIONS_FILE)
        self._services: dict[str, Any] = {}
        #: Accounts whose role the user has: until when a refusal may mean
        #: that Azure hasn't applied it yet (0: it was there).
        self._roles: dict[str, float] = {}
        self._keys: dict[str, tuple[Any, datetime.datetime]] = {}
        self._containers: set[str] = set()

    # ------------------------------------------------------------------
    # Calls

    def _service(self, account: str) -> Any:
        if account not in self._services:
            self._services[account] = self._make_blob_service(self.arm.credential, account)
        return self._services[account]

    def _blob(self, target: AzBlob) -> Any:
        return self._service(target.account).get_blob_client(target.container, target.name)

    def _call(self, what: str, account: str, call: Callable[[], T]) -> T:
        """``call()`` (a blob call), with its errors as :class:`ProviderError`."""
        from azure.core.exceptions import AzureError

        try:
            return call()
        except AzureError as exc:
            raise _error(exc, what, account) from exc

    def _retry(self, what: str, account: str, call: Callable[[], T]) -> T:
        """``call()``, retried after broken connections."""
        return with_retries(partial(self._call, what, account, call), self.retry_delays, "Azure")

    # ------------------------------------------------------------------
    # The storage account

    def _account_path(self, name: str) -> str:
        return f"{self.arm.paths.group}/providers/Microsoft.Storage/storageAccounts/{name}"

    def find_account(self) -> str | None:
        """The add-on's storage account (named ``o3cloud…``, tagged
        ``orange3-cloud=true``, in the resource group ``orange3-cloud``):
        the oldest in the machine's region, else the oldest; ``None`` if
        there is none."""
        found = self.arm.list(
            f"{self.arm.paths.group}/providers/Microsoft.Storage/storageAccounts",
            "list the storage accounts",
            api=STORAGE_API,
        )
        ours = [
            a
            for a in found
            if a.get("name", "").startswith(ACCOUNT_PREFIX)
            and (a.get("tags") or {}).get(TAG) == "true"
            and (a.get("properties") or {}).get("provisioningState") == "Succeeded"
        ]
        ours.sort(key=lambda a: (a.get("location") != self.region, _created(a)))
        return ours[0]["name"] if ours else None

    def prepare(self, agreed: str | None = None, on_status: StatusCallback | None = None) -> str:
        """The add-on's account, ready for uploads by the user. Raises
        :class:`StorageMissingError` to ask the user first: to create it
        (then ``agreed`` is the new name), or to give the user its role
        (``agreed``: the account's name). ``on_status`` gets what it does."""
        status = on_status or (lambda text: None)
        account = self.find_account()
        if account is None:
            if not agreed:
                name = new_account_name()
                raise StorageMissingError(
                    "Files from this computer go to a storage account in your Azure "
                    f"subscription, and there is none yet. Create the storage account {name} "
                    f"in {self.region}, and give yourself the role “{BLOB_ROLE}” on it? "
                    "Azure takes a minute or two to apply the role, so the first upload "
                    "waits for it.",
                    name,
                )
            status(f"Creating the storage account {agreed}")
            return self.create_account(agreed)
        if not self.has_role(account):
            if agreed != account:
                raise StorageMissingError(
                    f"Files from this computer go to the storage account {account}, but your "
                    f"Azure sign-in may not write there. Give yourself the role “{BLOB_ROLE}” "
                    "on it? Azure takes a minute or two to apply it, so the upload waits.",
                    account,
                )
            status(f"Giving you the role “{BLOB_ROLE}” on the storage account {account}")
            self.assign_role(account)
        self._ensure_container(account)
        return account

    def _ensure_container(self, account: str) -> None:
        """Create the container of the uploaded files unless this storage
        made it already (creating it again changes nothing)."""
        if account not in self._containers:
            self.arm.put(
                f"{self._account_path(account)}/blobServices/default/containers/{CONTAINER}",
                {"properties": {"publicAccess": "None"}},
                f"create the container {CONTAINER}",
                api=STORAGE_API,
            )
            self._containers.add(account)

    def create_account(self, name: str) -> str:
        """Create the storage account ``name`` in the machine's region, its
        container and the user's role on it. Returns its name."""
        if not name.startswith(ACCOUNT_PREFIX) or not _ACCOUNT_NAME.fullmatch(name):
            raise ProviderError(f"The storage account name {name} isn't one of the add-on's.")
        what = f"create the storage account {name}"
        self.arm.ensure_group(self.region)
        path = self._account_path(name)
        self.arm.put(path, account_body(self.region), what, api=STORAGE_API)
        self._wait_created(path, what)
        self._ensure_container(name)
        self.assign_role(name)
        return name

    def _wait_created(self, path: str, what: str) -> None:
        """Wait until Azure has made the new account (it answers 202)."""
        deadline = time.monotonic() + CREATE_TIMEOUT
        while True:
            account = self.arm.get(path, what, api=STORAGE_API) or {}
            state = (account.get("properties") or {}).get("provisioningState", "")
            if state == "Succeeded":
                return
            if state in ("Failed", "Canceled"):
                raise ArmError(f"Couldn't {what}: Azure says {state}.")
            if time.monotonic() > deadline:
                raise ArmError(f"Couldn't {what} within {CREATE_TIMEOUT} s ({state or 'missing'}).")
            time.sleep(self.arm.poll_interval)

    def has_role(self, account: str) -> bool:
        """Whether the user may read, write and delete the account's blobs
        (from Resource Manager's list of the user's permissions on it)."""
        if account not in self._roles:
            reply = self.arm.request(
                "GET",
                f"{self._account_path(account)}/providers/Microsoft.Authorization/permissions",
                f"read your permissions on the storage account {account}",
                api=AUTHORIZATION_API,
            )
            permissions = [
                Permission(tuple(p.get("dataActions") or ()), tuple(p.get("notDataActions") or ()))
                for p in reply.get("value", [])
            ]
            if not all(allowed(action, permissions) for action in DATA_ACTIONS):
                return False
            self._roles[account] = 0.0
        return True

    def assign_role(self, account: str) -> None:
        """Give the signed-in user the role :data:`BLOB_ROLE` on the account."""
        object_id, kind = principal(self.arm.credential)
        scope = self._account_path(account)
        name = uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}#{object_id}")
        body = {
            "properties": {
                "roleDefinitionId": f"/subscriptions/{self.arm.paths.subscription}/providers/"
                f"Microsoft.Authorization/roleDefinitions/{BLOB_ROLE_ID}",
                "principalId": object_id,
                "principalType": kind,
            }
        }
        path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
        try:
            self.arm.put(path, body, f"give you the role “{BLOB_ROLE}”", api=AUTHORIZATION_API)
        except ArmError as exc:
            if exc.code != "RoleAssignmentExists":
                raise
        self._roles[account] = time.monotonic() + self.role_wait

    def _once_role_applies(
        self,
        account: str,
        call: Callable[..., T],
        report: Callable[[], None],
        on_status: StatusCallback,
    ) -> T:
        """``call()``; while the role assigned just now doesn't apply yet,
        tried again every :attr:`role_poll` seconds, each time once
        (``call(**ROLE_TRY_OPTIONS)``). ``report()`` may raise to stop;
        ``on_status`` says that it waits."""
        options: dict[str, Any] = {}
        started = time.monotonic()
        while True:
            try:
                result = call(**options)
            except RoleMissingError:
                if time.monotonic() > self._roles.get(account, 0.0):
                    raise
            else:
                if options:
                    on_status("Uploading the file to Azure storage")
                return result
            log.info("Waiting for Azure to apply the role %s on %s", BLOB_ROLE, account)
            options = ROLE_TRY_OPTIONS
            on_status(
                f"Waiting for Azure to apply your new role on the storage account {account} "
                f"({time.monotonic() - started:.0f} s; it can take a few minutes)"
            )
            report()
            time.sleep(self.role_poll)

    # ------------------------------------------------------------------
    # Files

    def files(self, account: str) -> list[StoredFile]:
        """The uploaded files, newest first."""
        container = self._service(account).get_container_client(CONTAINER)
        blobs = self._call(
            "list the uploaded files",
            account,
            lambda: list(container.list_blobs(name_starts_with=UPLOAD_FOLDER)),
        )
        files = [
            StoredFile(
                uri=AzBlob(account, CONTAINER, b.name).uri,
                name=b.name.rsplit("/", 1)[-1],
                size_bytes=int(b.size or 0),
                updated=b.last_modified.isoformat(timespec="seconds") if b.last_modified else "",
            )
            for b in blobs
        ]
        return sorted(files, key=lambda f: f.updated, reverse=True)

    def delete(self, uris: Sequence[str]) -> None:
        """Delete uploaded files (files already gone are skipped)."""
        for uri in uris:
            target = parse_az_uri(uri)
            with contextlib.suppress(NotFoundError):  # gone already
                self._call("delete the file", target.account, self._blob(target).delete_blob)
            self.sessions.drop(uri)

    def head(self, uri: str, **options: Any) -> Any:
        """The properties of the blob ``uri``, read with the user's
        credentials; raises :class:`ProviderError` if they can't read it.
        ``options`` go to the Azure library's call (e.g. ``retry_total``)."""
        target = parse_az_uri(uri)
        blob = self._blob(target)
        return self._call(
            f"read {uri}", target.account, partial(blob.get_blob_properties, **options)
        )

    def read_head(self, uri: str, length: int) -> tuple[int, bytes]:
        """``(size, first length bytes)`` of the blob ``uri``."""
        target = parse_az_uri(uri)
        size = int(self.head(uri).size or 0)
        if not length or not size:
            return size, b""
        blob = self._blob(target)
        return size, self._call(
            f"read {uri}",
            target.account,
            lambda: blob.download_blob(offset=0, length=min(length, size)).readall(),
        )

    def signed_link(self, uri: str) -> str:
        """A link with which the machine reads the blob ``uri`` for
        :data:`LINK_SECONDS`: a read-only user delegation SAS. Raises
        :class:`ProviderError` if the user can't read the blob."""
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        target = parse_az_uri(uri)
        self.head(uri)
        now = datetime.datetime.now(datetime.timezone.utc)
        sas = generate_blob_sas(
            target.account,
            target.container,
            target.name,
            user_delegation_key=self._delegation_key(target.account, now),
            permission=BlobSasPermissions(read=True),
            start=now - CLOCK_SKEW,
            expiry=now + datetime.timedelta(seconds=LINK_SECONDS),
            protocol="https",
        )
        return f"{self._blob(target).url}?{sas}"

    def _delegation_key(self, account: str, now: datetime.datetime) -> Any:
        """The account's user delegation key, kept while it outlives a new link."""
        key, expiry = self._keys.get(account, (None, now))
        if key is None or expiry - now < datetime.timedelta(seconds=2 * LINK_SECONDS):
            expiry = now + datetime.timedelta(seconds=KEY_SECONDS)
            service = self._service(account)
            key = self._call(
                f"get a key to sign links to the storage account {account}",
                account,
                lambda: service.get_user_delegation_key(now - CLOCK_SKEW, expiry),
            )
            self._keys[account] = (key, expiry)
        return key

    # ------------------------------------------------------------------
    # Uploads

    def upload(
        self,
        account: str,
        path: str | os.PathLike,
        on_progress: UploadProgress | None = None,
        on_status: StatusCallback | None = None,
    ) -> str:
        """Upload a local file to the account (or find it there) and return
        its ``az://`` URI. ``on_progress(sent, total)`` may raise to stop;
        the upload then continues next time. ``on_status`` gets the wait
        for a new role."""
        report = on_progress or (lambda sent, total: None)
        stat = os.stat(path)
        size, mtime = stat.st_size, stat.st_mtime_ns
        target = AzBlob(account, CONTAINER, object_name(path, size, mtime))
        uploaded = self._once_role_applies(
            account,
            partial(self._uploaded, target, size),
            partial(report, 0, size),
            on_status or (lambda text: None),
        )
        if uploaded:
            self.sessions.drop(target.uri)
        else:
            with open(path, "rb") as f:
                if size <= self.block_bytes:
                    self._put(f, target, size, report)
                else:
                    self._send_blocks(f, target, (size, mtime), report)
        report(size, size)
        return target.uri

    def _uploaded(self, target: AzBlob, size: int, **options: Any) -> bool:
        try:
            existing = self.head(target.uri, **options)
        except NotFoundError:
            return False
        return int(existing.size or -1) == size

    def _put(self, f: BinaryIO, target: AzBlob, size: int, report: UploadProgress) -> None:
        """A small file, in one request."""
        report(0, size)
        data, blob = f.read(), self._blob(target)
        self._retry(
            "upload the file",
            target.account,
            partial(blob.upload_blob, data, overwrite=True, validate_content=True),
        )

    def _send_blocks(
        self, f: BinaryIO, target: AzBlob, file: tuple[int, int], report: UploadProgress
    ) -> None:
        """Send the blocks Azure doesn't have yet, then commit them."""
        size = file[0]
        part = block_size(size, self.block_bytes)
        count = -(-size // part)
        session, done = self._resume(target, file, part)
        blob = self._blob(target)
        for number in range(1, count + 1):
            if number in done:
                continue
            report(sum(_part_length(n, part, size) for n in done), size)
            f.seek((number - 1) * part)
            data = f.read(part)
            stage = partial(
                blob.stage_block, _block_id(session, number), data, len(data), validate_content=True
            )
            self._retry("upload the file", target.account, stage)
            done.add(number)
        self._commit(target, session, count, size)
        self.sessions.drop(target.uri)

    def _resume(self, target: AzBlob, file: tuple[int, int], part: int) -> tuple[str, set[int]]:
        """(the upload's block ID prefix, the blocks Azure has): the
        remembered upload of this file, else a new one."""
        size, mtime = file
        session = self.sessions.get(target.uri, size, mtime)
        if session is None:
            session = secrets.token_hex(4)
            self.sessions.put(target.uri, size, mtime, session)
            return session, set()
        blob = self._blob(target)
        try:
            _, uncommitted = self._retry(
                "check the upload", target.account, partial(blob.get_block_list, "uncommitted")
            )
        except NotFoundError:  # no blocks (any more)
            uncommitted = []
        done = {
            number
            for block in uncommitted
            if (number := _block_number(session, block.id)) is not None
            and block.size == _part_length(number, part, size)
        }
        log.info("Resuming the upload of %s after %s blocks", target.uri, len(done))
        return session, done

    def _commit(self, target: AzBlob, session: str, count: int, size: int) -> None:
        """Join the blocks into the blob."""
        from azure.storage.blob import BlobBlock, ContentSettings

        blocks = [BlobBlock(_block_id(session, n)) for n in range(1, count + 1)]
        blob = self._blob(target)
        commit = partial(
            blob.commit_block_list,
            blocks,
            content_settings=ContentSettings(content_type="application/octet-stream"),
        )
        try:
            self._retry("finish the upload", target.account, commit)
        except UploadGoneError:
            self.sessions.drop(target.uri)
            if not self._uploaded(target, size):
                raise ProviderError(
                    "The upload expired on Azure. Load again to start it again."
                ) from None
