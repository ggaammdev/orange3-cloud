"""The Azure Resource Manager calls of the Azure provider, in one subscription.

:class:`Arm` sends Resource Manager's REST requests through the pipeline of
an Azure SDK client (``send_request``: the Azure CLI's credentials, timeouts
and retries; see :func:`make_arm`), and turns failures into
:class:`~orangecontrib.cloud.client.providers.base.ProviderError` with a
message for the user (:class:`ArmError` with Azure's error code,
:class:`NotFoundError` for a resource that doesn't exist). The request
bodies are built in :mod:`~orangecontrib.cloud.client.providers.azure_vm`.
Tests give it a fake ``send``. Every call goes to Azure, so the provider
makes them off the GUI thread.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

from orangecontrib.cloud.client.providers import azure_account
from orangecontrib.cloud.client.providers.azure_account import (
    SIGN_IN_BUTTON,
    NotSignedInError,
    azure_call,
)
from orangecontrib.cloud.client.providers.azure_vm import (
    COMPUTE_API,
    RESOURCES_API,
    TAG,
    Paths,
)
from orangecontrib.cloud.client.providers.base import ProviderError

__all__ = ["Arm", "ArmError", "NotFoundError", "make_arm"]

#: ``send(request)``: an ``azure.core.rest.HttpResponse`` for an ``HttpRequest``.
Send = Callable[[Any], Any]
#: ``fetch(url)``: the text at a plain HTTPS URL (e.g. a signed blob link).
Fetch = Callable[[str], str]


class ArmError(ProviderError):
    """A Resource Manager request failed (``status``: HTTP status, ``code``:
    Azure's error code, e.g. ``Conflict``)."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class NotFoundError(ArmError):
    """The resource doesn't exist."""


def _fetch(url: str) -> str:
    """The text of a signed blob link (``""`` while the blob doesn't exist).
    Errors don't name the link, which is a secret."""
    import requests

    response = requests.get(url, timeout=30)
    if response.status_code == 404:  # not written yet
        return ""
    if not response.ok:
        raise ProviderError(f"Couldn't read the machine's output (HTTP {response.status_code}).")
    return response.content.decode("utf-8", errors="replace")


class Arm:
    """Resource Manager requests in one subscription (``paths``: what the
    add-on creates there). ``credential``: the sign-in they use, for calls to
    other Azure services (e.g. Blob Storage)."""

    def __init__(
        self, send: Send, subscription_id: str, fetch: Fetch | None = None, credential: Any = None
    ):
        self._send = send
        self.paths = Paths(subscription_id)
        self._fetch = fetch or _fetch
        self.credential = credential
        #: Seconds between reads while waiting for Azure.
        self.poll_interval = 3.0

    # ------------------------------------------------------------------
    # Requests

    def request(
        self,
        method: str,
        path: str,
        what: str,
        *,
        api: str = COMPUTE_API,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send a request (``path`` may be a full ``nextLink`` URL, which has
        its API version) and return its JSON (``{}`` if empty); raises
        :class:`ArmError` saying it couldn't ``what``."""
        from azure.core.rest import HttpRequest

        query = {} if "api-version=" in path else {"api-version": api}
        request = HttpRequest(method, path, params={**query, **(params or {})}, json=body)
        response = azure_call(what, lambda: self._send(request))
        content = response.content or b""
        try:
            reply = json.loads(content) if content.strip() else {}
        except ValueError:
            reply = {"error": {"message": content[:300].decode("utf-8", "replace")}}
        if response.status_code >= 400:
            raise _error(response.status_code, reply, what)
        return reply

    def get(
        self, path: str, what: str, *, api: str = COMPUTE_API, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        """The resource at ``path`` (``None`` if it doesn't exist)."""
        try:
            return self.request("GET", path, what, api=api, params=params)
        except NotFoundError:
            return None

    def put(self, path: str, body: dict[str, Any], what: str, *, api: str) -> dict[str, Any]:
        """Create or replace the resource at ``path``."""
        return self.request("PUT", path, what, api=api, body=body)

    def delete(self, path: str, what: str, *, api: str) -> None:
        """Ask Azure to delete the resource at ``path`` (gone already: fine)."""
        with contextlib.suppress(NotFoundError):  # deleted already
            self.request("DELETE", path, what, api=api)

    def list(self, path: str, what: str, *, api: str = COMPUTE_API) -> list[dict[str, Any]]:
        """The resources a list request returns, all pages (none if the
        resource group doesn't exist)."""
        found: list[dict[str, Any]] = []
        next_path: str | None = path
        while next_path:
            try:
                reply = self.request("GET", next_path, what, api=api)
            except NotFoundError:
                return found
            found += reply.get("value", [])
            next_path = reply.get("nextLink")
        return found

    def wait_provisioned(self, path: str, what: str, *, api: str, timeout: float = 300) -> None:
        """Wait until the resource at ``path`` is provisioned."""
        deadline = time.monotonic() + timeout
        while True:
            state = ((self.get(path, what, api=api) or {}).get("properties") or {}).get(
                "provisioningState", "Succeeded"
            )
            if state == "Succeeded":
                return
            if state in ("Failed", "Canceled"):
                raise ArmError(f"Couldn't {what}: Azure says {state}.")
            if time.monotonic() > deadline:
                raise ArmError(f"Couldn't {what} within {timeout:.0f} s ({state}).")
            time.sleep(self.poll_interval)

    def wait_gone(self, path: str, what: str, *, api: str, timeout: float = 300) -> None:
        """Wait until the resource at ``path`` no longer exists."""
        deadline = time.monotonic() + timeout
        while self.get(path, what, api=api) is not None:
            if time.monotonic() > deadline:
                raise ArmError(f"Couldn't {what}: it still exists after {timeout:.0f} s.")
            time.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # The add-on's resources

    def ensure_group(self, region: str) -> bool:
        """Create the resource group in ``region`` unless it exists; return
        whether it was created."""
        what = "create the resource group"
        if self.get(self.paths.group, what, api=RESOURCES_API) is not None:
            return False
        body = {"location": region, "tags": {TAG: "true"}}
        self.put(self.paths.group, body, what, api=RESOURCES_API)
        return True

    def vm(self, name: str, *, instance_view: bool = False) -> dict[str, Any] | None:
        """The VM ``name``, with its instance view (power state) if asked
        (``None`` if it doesn't exist)."""
        params = {"$expand": "instanceView"} if instance_view else None
        return self.get(self.paths.vm(name), "read the machine", params=params)

    def vms(self) -> list[dict[str, Any]]:
        """The add-on's VMs (tagged ``orange3-cloud=true``) in the resource group."""
        found = self.list(self.paths.vms, "list the machines")
        return [vm for vm in found if (vm.get("tags") or {}).get(TAG) == "true"]

    def boot_log(self, name: str) -> str:
        """The serial log of the VM ``name`` (boot diagnostics; it lags)."""
        reply = self.request(
            "POST",
            f"{self.paths.vm(name)}/retrieveBootDiagnosticsData",
            "read the machine's output",
        )
        link = reply.get("serialConsoleLogBlobUri")
        if not link:
            return ""
        try:
            return self._fetch(link)
        except OSError as exc:  # requests' errors; their text has the link
            raise ProviderError(
                f"Couldn't read the machine's output ({type(exc).__name__})."
            ) from exc


def _error(status: int, reply: dict[str, Any], what: str) -> ProviderError:
    """A failed request's error for the user."""
    error = reply.get("error") or {}
    code, message = error.get("code", ""), error.get("message") or f"HTTP {status}"
    if status == 401:
        return NotSignedInError(
            f"The Azure sign-in has expired or doesn't work ({message}). Click “{SIGN_IN_BUTTON}”."
        )
    if status == 404:
        return NotFoundError(f"Couldn't {what}: it doesn't exist.", status, code)
    if status == 403:
        return ArmError(f"Couldn't {what}: no permission. {message}", status, code)
    return ArmError(f"Couldn't {what}: {message}", status, code)


def make_arm(subscription_id: str) -> Arm:
    """:class:`Arm` with the Azure CLI's credentials for the subscription's
    tenant (an Azure SDK client's pipeline)."""
    az = azure_account.find_az()
    subscriptions = azure_account.list_subscriptions(az) if az else []
    chosen = next((s for s in subscriptions if s.id == subscription_id), None)
    credential = azure_account.credential(az, chosen)
    client = azure_account.resource_client(credential, subscription_id)
    return Arm(client.send_request, subscription_id, credential=credential)
