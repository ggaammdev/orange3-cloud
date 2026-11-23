"""The user's Azure account: the Azure CLI, signing in, the subscriptions and
the regions the user can choose, and the sizes a region offers there.

The add-on signs in with the Azure CLI: the sign-in button runs ``az login``,
which opens Microsoft's sign-in page in the browser (:func:`sign_in`,
through the provider's ``sign_in`` and the session). The subscriptions come
from the CLI's own list (``az account list``, which reads the CLI's files,
no network), and the credentials from ``azure.identity``'s
``AzureCliCredential`` for the chosen subscription's tenant
(:func:`credential`), which asks ``az`` for each token. The add-on never
sees or stores the user's password.

Every function here may be slow (``az``, the network, or a login shell to
find ``az``), so the settings dialog calls them off the GUI thread. They
raise :class:`~orangecontrib.cloud.client.providers.base.ProviderError` (or
:class:`NotSignedInError`) with a message for the user. The Azure libraries
are imported only when needed.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from orangecontrib.cloud.client.providers.azure_vm import DEFAULT_REGION, MACHINE_TYPES
from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.cli_tools import find_tool, run_sign_in

__all__ = [
    "AZURE_CLI_INSTALL_URL",
    "MANAGEMENT_SCOPE",
    "SIGN_IN_BUTTON",
    "AzureRegion",
    "AzureSubscription",
    "CachedCredential",
    "NotSignedInError",
    "az_env",
    "azure_call",
    "azure_error",
    "check_sign_in",
    "compute_client",
    "credential",
    "default_location",
    "find_az",
    "list_regions",
    "list_subscriptions",
    "offered_sizes",
    "pick_region",
    "pick_subscription",
    "resource_client",
    "sign_in",
    "sign_in_command",
    "sizes_offered",
    "subscription_client",
]

AZURE_CLI_INSTALL_URL = "https://learn.microsoft.com/cli/azure/install-azure-cli"
#: The sign-in button's text, which messages refer to.
SIGN_IN_BUTTON = "Sign in to Azure"
#: The scope of tokens for Azure Resource Manager.
MANAGEMENT_SCOPE = "https://management.azure.com/.default"

_T = TypeVar("_T")


class NotSignedInError(ProviderError):
    """The Azure CLI has no sign-in, or it no longer works."""


# ----------------------------------------------------------------------
# The Azure CLI


def _known_az_paths() -> list[Path]:
    """Where the Azure CLI's installers and package managers put ``az``."""
    if sys.platform == "win32":
        bases = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)")]
        return [
            Path(b) / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / "az.cmd" for b in bases if b
        ]
    return [
        Path("/opt/homebrew/bin/az"),
        Path("/usr/local/bin/az"),
        Path("/usr/bin/az"),
        Path("/snap/bin/az"),
        Path.home() / ".local" / "bin" / "az",
    ]


def find_az(*, login_shell: bool = True) -> str | None:
    """The path of the ``az`` tool, or ``None`` if it isn't installed.

    Looks on ``PATH``, in the usual install locations and, unless
    ``login_shell`` is false, on the PATH of the user's login shell.
    """
    names = ("az.cmd", "az") if sys.platform == "win32" else ("az",)
    return find_tool(names, _known_az_paths(), login_shell=login_shell)


def az_env() -> dict[str, str]:
    """The environment for ``az``: no Python warnings (the CLI's own Python
    prints some), no colours, and no question on the console after
    ``az login``."""
    return dict(
        os.environ,
        PYTHONWARNINGS="ignore",
        AZURE_CORE_NO_COLOR="true",
        AZURE_CORE_LOGIN_EXPERIENCE_V2="off",
    )


_WARNING_LINE = re.compile(r"\b\w*Warning: ")


def _cli_message(text: str, limit: int = 400) -> str:
    """The useful part of ``az``'s error output: without Python's warnings
    (and the source lines they quote), on one line."""
    lines, skip_indented = [], False
    for line in text.splitlines():
        if _WARNING_LINE.search(line):
            skip_indented = True
            continue
        if skip_indented and line.startswith((" ", "\t")):
            continue
        skip_indented = False
        if line.strip():
            lines.append(line.strip())
    message = " ".join(lines)
    return message if len(message) <= limit else message[: limit - 1] + "…"


def _run_az(az: str, args: Sequence[str], what: str, timeout: float = 60) -> str:
    """Run ``az`` with ``args`` and return its output; raises
    :class:`ProviderError` (saying it couldn't ``what``) if it fails."""
    try:
        result = subprocess.run(
            [az, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=az_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProviderError(
            f"Couldn't run az to {what} ({exc}). Is the Azure CLI installed?"
        ) from exc
    if result.returncode != 0:
        raise ProviderError(f"Couldn't {what}: {_cli_message(result.stderr) or result.returncode}")
    return result.stdout


@dataclass(frozen=True)
class AzureSubscription:
    """A subscription the Azure CLI's sign-in can use."""

    id: str
    name: str
    tenant_id: str = ""
    #: ``Enabled``, ``Warned``, ``PastDue``, ``Disabled`` or ``Deleted``.
    state: str = "Enabled"
    #: The Azure CLI's default subscription.
    is_default: bool = False
    #: Who the CLI signed in, e.g. ``ana@example.com``.
    user: str = ""
    #: The subscription's offer, e.g. ``PayAsYouGo_2014-09-01`` or
    #: ``FreeTrial_2014-09-01`` (``""``: unknown; only the API gives it).
    quota_id: str = ""

    @property
    def label(self) -> str:
        """The name, as the user sees it in the Azure portal."""
        return self.name or self.id


def list_subscriptions(az: str) -> list[AzureSubscription]:
    """The subscriptions of the Azure CLI's sign-in (``az account list``,
    from the CLI's files: no network), sorted by name. Empty when nobody is
    signed in."""
    output = _run_az(
        az,
        ["account", "list", "--output", "json", "--only-show-errors"],
        "list the Azure CLI's subscriptions",
    )
    try:
        entries = json.loads(output or "[]")
    except ValueError as exc:
        raise ProviderError(f"Couldn't read the Azure CLI's subscriptions: {exc}") from exc
    subscriptions = [
        AzureSubscription(
            id=str(entry.get("id", "")),
            name=str(entry.get("name", "")),
            tenant_id=str(entry.get("tenantId", "")),
            state=str(entry.get("state", "")),
            is_default=bool(entry.get("isDefault")),
            user=str((entry.get("user") or {}).get("name", "")),
        )
        for entry in entries
        if isinstance(entry, dict) and entry.get("id")
    ]
    return sorted(subscriptions, key=lambda s: (s.label.lower(), s.id))


def pick_subscription(
    subscriptions: Sequence[AzureSubscription], *preferred: str
) -> AzureSubscription | None:
    """The first of ``preferred`` (IDs) among ``subscriptions``, else the
    Azure CLI's default, else the first enabled one, else the first
    (``None`` if there are none)."""
    by_id = {s.id: s for s in subscriptions}
    for subscription_id in preferred:
        if subscription_id in by_id:
            return by_id[subscription_id]
    for subscription in subscriptions:
        if subscription.is_default:
            return subscription
    enabled = [s for s in subscriptions if s.state == "Enabled"]
    return (enabled or list(subscriptions) or [None])[0]


def default_location() -> str:
    """The Azure CLI's default location (``az config set
    defaults.location=…``), or ``""``. Reads the CLI's configuration file
    (``AZURE_CONFIG_DIR`` if set); no ``az`` call."""
    folder = os.environ.get("AZURE_CONFIG_DIR") or str(Path.home() / ".azure")
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(Path(folder) / "config", encoding="utf-8")
    except (OSError, configparser.Error):
        return ""
    return config.get("defaults", "location", fallback="").strip()


def sign_in_command(az: str) -> list[str]:
    """The command that signs the user in: it opens Microsoft's sign-in page
    in the browser and saves the sign-in in the Azure CLI."""
    return [az, "login"]


def sign_in(az: str, cancel: threading.Event | None = None, *, poll: float = 0.2) -> str:
    """Run :func:`sign_in_command` and wait until it ends; return its output.

    The user signs in in the browser, which can take minutes, so call it off
    the GUI thread. Setting ``cancel`` kills the process and raises
    :class:`~orangecontrib.cloud.client.providers.base.SignInCancelledError`.
    Raises :class:`ProviderError` if ``az`` can't start or doesn't finish.
    """
    return run_sign_in(
        sign_in_command(az),
        cancel,
        tool="az",
        install_hint="Is the Azure CLI installed?",
        poll=poll,
        env=az_env(),
    )


# ----------------------------------------------------------------------
# Credentials


class CachedCredential:
    """A credential that keeps each token until shortly before it expires.

    ``AzureCliCredential`` runs ``az`` (about a second) for every token, and
    every Azure client would ask for its own; this one asks once, and one
    request at a time.
    """

    def __init__(self, credential: Any, margin: float = 300):
        self.credential = credential
        self._margin = margin
        self._tokens: dict[tuple, Any] = {}
        self._lock = threading.Lock()

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        """A token for ``scopes`` (``azure.core``'s ``TokenCredential``)."""
        if kwargs.get("claims"):
            return self.credential.get_token(*scopes, **kwargs)
        key = (scopes, kwargs.get("tenant_id"))
        with self._lock:
            token = self._tokens.get(key)
            if token is None or token.expires_on - time.time() < self._margin:
                token = self._tokens[key] = self.credential.get_token(*scopes, **kwargs)
            return token

    def close(self) -> None:
        """Nothing to close (``azure.core``'s clients call it)."""


def _put_on_path(az: str) -> None:
    """Make ``az`` findable for ``AzureCliCredential``, which looks only on
    ``PATH``: Orange started from the Dock or the Start menu doesn't get the
    PATH of the user's shell. Appends the tool's folder, once."""
    import shutil

    if shutil.which("az") is None:
        folder = str(Path(az).parent)
        os.environ["PATH"] = os.pathsep.join(p for p in (os.environ.get("PATH", ""), folder) if p)


def credential(az: str | None, subscription: AzureSubscription | None = None) -> CachedCredential:
    """The Azure CLI's credentials (``AzureCliCredential``) for the tenant of
    ``subscription``. Tokens are read when a call needs them (see
    :func:`check_sign_in`)."""
    from azure.identity import AzureCliCredential

    if az:
        _put_on_path(az)
    tenant = subscription.tenant_id if subscription else ""
    return CachedCredential(AzureCliCredential(tenant_id=tenant, process_timeout=30))


def check_sign_in(cred: Any) -> None:
    """Get a token for Azure Resource Manager; raises
    :class:`NotSignedInError` if the sign-in is missing or has expired."""
    azure_call("check your Azure sign-in", lambda: cred.get_token(MANAGEMENT_SCOPE))


def _http_message(exc: Any) -> str:
    error = getattr(exc, "error", None)
    message = getattr(error, "message", "") or getattr(exc, "message", "") or str(exc)
    return _cli_message(str(message))


def azure_error(exc: Exception, what: str) -> ProviderError:
    """An error of the Azure libraries as a :class:`ProviderError` for the
    user; :class:`NotSignedInError` if the sign-in is missing or expired."""
    from azure.core import exceptions as azure
    from azure.identity import CredentialUnavailableError

    if isinstance(exc, CredentialUnavailableError):
        if "not found" in str(exc).lower():
            return ProviderError(
                f"Couldn't find the Azure CLI to {what}. Install it: {AZURE_CLI_INSTALL_URL}"
            )
        return NotSignedInError(f"Not signed in to Azure. Click “{SIGN_IN_BUTTON}”.")
    status = getattr(exc, "status_code", None)
    if isinstance(exc, azure.ClientAuthenticationError) or status == 401:
        return NotSignedInError(
            f"The Azure sign-in has expired or doesn't work ({_http_message(exc)}). "
            f"Click “{SIGN_IN_BUTTON}”."
        )
    if isinstance(exc, azure.ServiceRequestError):
        return ProviderError(f"Couldn't reach Azure to {what}. Check the connection.")
    if isinstance(exc, azure.HttpResponseError):
        if status == 403:
            return ProviderError(f"Couldn't {what}: no permission. {_http_message(exc)}")
        return ProviderError(f"Couldn't {what}: {_http_message(exc)}")
    return ProviderError(f"Couldn't {what}: {exc}")


def azure_call(what: str, call: Callable[[], _T]) -> _T:
    """``call()``, with the Azure libraries' errors as :func:`azure_error`."""
    from azure.core.exceptions import AzureError

    try:
        return call()
    except AzureError as exc:  # CredentialUnavailableError is one too
        raise azure_error(exc, what) from exc


# ----------------------------------------------------------------------
# Clients and regions

#: Short timeouts and a few retries for every Azure client.
_CLIENT_OPTIONS = {"connection_timeout": 10, "read_timeout": 30, "retry_total": 3}


def subscription_client(cred: Any) -> Any:
    """An Azure ``SubscriptionClient`` (subscriptions and their regions)."""
    from azure.mgmt.resource.subscriptions import SubscriptionClient

    return SubscriptionClient(cred, **_CLIENT_OPTIONS)


def resource_client(cred: Any, subscription_id: str) -> Any:
    """An Azure ``ResourceManagementClient`` (resource providers and groups,
    and other Resource Manager calls through its ``send_request``)."""
    from azure.mgmt.resource.resources import ResourceManagementClient

    return ResourceManagementClient(cred, subscription_id, **_CLIENT_OPTIONS)


def compute_client(cred: Any, subscription_id: str) -> Any:
    """An Azure ``ComputeManagementClient`` (VMs, quotas)."""
    from azure.mgmt.compute import ComputeManagementClient

    return ComputeManagementClient(cred, subscription_id, **_CLIENT_OPTIONS)


@dataclass(frozen=True)
class AzureRegion:
    """A region of a subscription."""

    name: str  #: e.g. ``eastus``
    display_name: str = ""  #: e.g. ``East US``

    @property
    def label(self) -> str:
        """The name in the Azure portal, with the CLI's name."""
        return f"{self.display_name} ({self.name})" if self.display_name else self.name


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def list_regions(cred: Any, subscription_id: str) -> list[AzureRegion]:
    """The subscription's physical regions (``Subscriptions.list_locations``),
    sorted by their names in the portal."""

    def regions() -> list[AzureRegion]:
        locations = subscription_client(cred).subscriptions.list_locations(subscription_id)
        return [
            AzureRegion(location.name, location.display_name or "")
            for location in locations
            if location.metadata is not None
            and _enum_text(location.metadata.region_type).lower() == "physical"
        ]

    found = azure_call("list the regions", regions)
    return sorted(found, key=lambda r: (r.display_name or r.name).lower())


def pick_region(regions: Sequence[str], *preferred: str) -> str:
    """The first of ``preferred`` that is one of ``regions``, else
    :data:`~orangecontrib.cloud.client.providers.azure_vm.DEFAULT_REGION`,
    else the first region (``""`` if none)."""
    for name in (*preferred, DEFAULT_REGION):
        if name and name in regions:
            return name
    return regions[0] if regions else ""


# ----------------------------------------------------------------------
# The sizes a region offers

#: The Compute Resource SKUs API version.
SKUS_API = "2021-07-01"
#: Seconds a region's offered sizes are kept (the list is a few MB, and
#: changes rarely).
SIZES_TTL = 3600.0
_sizes: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}
_sizes_lock = threading.Lock()


def offered_sizes(cred: Any, subscription_id: str, region: str, *, fresh: bool = False) -> set[str]:
    """The add-on's VM sizes (:data:`~orangecontrib.cloud.client.providers.
    azure_vm.MACHINE_TYPES`) that ``region`` offers to the subscription
    (Compute Resource SKUs, one listing of the region; kept for
    :data:`SIZES_TTL` unless ``fresh``)."""
    key = (subscription_id, region.lower())
    with _sizes_lock:
        cached = _sizes.get(key)
    if cached is not None and not fresh and time.monotonic() - cached[0] < SIZES_TTL:
        return set(cached[1])
    skus = azure_call(
        f"list the machine sizes of {region}", lambda: _region_skus(cred, subscription_id, region)
    )
    names = sizes_offered(skus, region)
    with _sizes_lock:
        _sizes[key] = (time.monotonic(), frozenset(names))
    return names


def _region_skus(cred: Any, subscription_id: str, region: str) -> list[dict[str, Any]]:
    """The Compute Resource SKUs of ``region``, all pages (raw JSON)."""
    from azure.core.rest import HttpRequest

    client = resource_client(cred, subscription_id)
    path = f"/subscriptions/{subscription_id}/providers/Microsoft.Compute/skus"
    params = {"api-version": SKUS_API, "$filter": f"location eq '{region}'"}
    request: Any = HttpRequest("GET", path, params=params)
    found: list[dict[str, Any]] = []
    while request is not None:
        response = client.send_request(request)
        response.raise_for_status()
        reply = response.json()
        found += reply.get("value", [])
        link = reply.get("nextLink")
        request = HttpRequest("GET", link) if link else None
    return found


def sizes_offered(skus: Sequence[dict[str, Any]], region: str) -> set[str]:
    """The add-on's sizes among ``skus`` (Compute Resource SKUs) that
    ``region`` offers and doesn't restrict for the subscription."""
    names, region = {t.name for t in MACHINE_TYPES}, region.lower()
    offered = set()
    for sku in skus:
        if sku.get("resourceType") != "virtualMachines" or sku.get("name") not in names:
            continue
        if region not in {where.lower() for where in sku.get("locations") or []}:
            continue
        if not any(_restricts(r, region) for r in sku.get("restrictions") or []):
            offered.add(sku["name"])
    return offered


def _restricts(restriction: dict[str, Any], region: str) -> bool:
    """Whether a SKU's restriction keeps it out of ``region`` (e.g.
    ``NotAvailableForSubscription``). Zone restrictions don't: the add-on's
    machines aren't in a zone."""
    if restriction.get("type") != "Location":
        return False
    info = restriction.get("restrictionInfo") or {}
    where = info.get("locations") or restriction.get("values") or []
    return region in {w.lower() for w in where}
