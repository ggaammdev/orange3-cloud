"""Google's Compute Engine API, as the Google Cloud provider uses it.

:class:`ComputeApi` lists the calls; :class:`GoogleComputeApi` makes them as
plain REST requests with the signed-in credentials (like the setup checker),
so tests can replace it with a fake project.
"""

from __future__ import annotations

import time
from typing import Any

from orangecontrib.cloud.client.providers.base import ProviderError

__all__ = ["ComputeApi", "GoogleComputeApi", "NotFoundError"]


class NotFoundError(ProviderError):
    """The VM, rule or operation doesn't exist."""


class ComputeApi:
    """The Compute Engine calls the provider makes, as REST resources (dicts
    in the API's own JSON form). Each raises :class:`ProviderError`
    (:class:`NotFoundError` for a missing resource). Calls that change things
    return when Google has finished them."""

    def default_service_account(self, project: str) -> str:
        """The email of the project's default Compute Engine service account."""
        raise NotImplementedError

    def get_instance(self, project: str, zone: str, name: str) -> dict:
        """The VM resource."""
        raise NotImplementedError

    def insert_instance(self, project: str, zone: str, body: dict) -> None:
        """Create a VM from an ``instances.insert`` request body."""
        raise NotImplementedError

    def stop_instance(self, project: str, zone: str, name: str) -> None:
        """Ask the VM to stop, without waiting."""
        raise NotImplementedError

    def delete_instance(self, project: str, zone: str, name: str) -> None:
        """Delete the VM (and its auto-delete disks)."""
        raise NotImplementedError

    def list_instances(self, project: str, label: str) -> list[dict]:
        """VMs in all zones that have ``label=true``."""
        raise NotImplementedError

    def serial_output(self, project: str, zone: str, name: str, start: int = 0) -> tuple[str, int]:
        """Serial port 1 output from byte ``start``: (text, next start)."""
        raise NotImplementedError

    def get_firewall(self, project: str, name: str) -> dict:
        """The firewall rule resource."""
        raise NotImplementedError

    def insert_firewall(self, project: str, body: dict) -> None:
        """Create a firewall rule."""
        raise NotImplementedError

    def patch_firewall(self, project: str, name: str, body: dict) -> None:
        """Change the fields of a firewall rule that ``body`` has."""
        raise NotImplementedError

    def delete_firewall(self, project: str, name: str) -> None:
        """Delete a firewall rule."""
        raise NotImplementedError


class GoogleComputeApi(ComputeApi):
    """:class:`ComputeApi` with REST calls, using the signed-in credentials
    (:func:`~orangecontrib.cloud.client.providers.gcp_account.load_credentials`)."""

    COMPUTE = "https://compute.googleapis.com/compute/v1"

    def __init__(self, credentials: Any, timeout: float = 60, operation_timeout: float = 300):
        from google.auth.transport.requests import AuthorizedSession

        self.session = AuthorizedSession(credentials)
        self.timeout = timeout
        self.operation_timeout = operation_timeout

    def _call(self, method: str, path: str, what: str, project: str, **kwargs: Any) -> Any:
        from google.auth import exceptions as auth
        from requests import RequestException

        from orangecontrib.cloud.client.providers.gcp_account import NotSignedInError
        from orangecontrib.cloud.client.providers.gcp_setup import _http_error

        try:
            response = self.session.request(
                method, f"{self.COMPUTE}/{path}", timeout=self.timeout, **kwargs
            )
        except (auth.RefreshError, auth.DefaultCredentialsError) as exc:
            raise NotSignedInError(
                'Your Google sign-in has expired or was revoked. Click "Sign in with Google".'
            ) from exc
        except (auth.TransportError, RequestException) as exc:
            raise ProviderError(
                f"Couldn't reach Google Cloud to {what}. Check the connection."
            ) from exc
        if response.ok:
            return response.json() if response.content else {}
        if response.status_code == 404:
            raise NotFoundError(f"Couldn't {what}: not found.")
        raise _http_error(response, what, project)

    def _wait(self, operation: dict, what: str, project: str) -> None:
        """Wait until a zone or global operation is done; raise its error."""
        deadline = time.monotonic() + self.operation_timeout
        while operation.get("status") != "DONE":
            if time.monotonic() > deadline:
                raise ProviderError(
                    f"Google Cloud didn't {what} within {self.operation_timeout:.0f} s."
                )
            zone = operation.get("zone", "").rsplit("/", 1)[-1]
            where = f"zones/{zone}" if zone else "global"
            operation = self._call(
                "POST",
                f"projects/{project}/{where}/operations/{operation['name']}/wait",
                what,
                project,
            )
        errors = (operation.get("error") or {}).get("errors") or []
        if errors:
            message = "; ".join(e.get("message", e.get("code", "")) for e in errors)
            raise ProviderError(f"Couldn't {what}: {message}")

    def default_service_account(self, project: str) -> str:
        reply = self._call("GET", f"projects/{project}", "read the project", project)
        return reply.get("defaultServiceAccount", "")

    def get_instance(self, project: str, zone: str, name: str) -> dict:
        return self._call(
            "GET", f"projects/{project}/zones/{zone}/instances/{name}", "read the machine", project
        )

    def insert_instance(self, project: str, zone: str, body: dict) -> None:
        what = "create the machine"
        operation = self._call(
            "POST", f"projects/{project}/zones/{zone}/instances", what, project, json=body
        )
        self._wait(operation, what, project)

    def stop_instance(self, project: str, zone: str, name: str) -> None:
        self._call(
            "POST",
            f"projects/{project}/zones/{zone}/instances/{name}/stop",
            "stop the machine",
            project,
        )

    def delete_instance(self, project: str, zone: str, name: str) -> None:
        what = "delete the machine"
        operation = self._call(
            "DELETE", f"projects/{project}/zones/{zone}/instances/{name}", what, project
        )
        self._wait(operation, what, project)

    def list_instances(self, project: str, label: str) -> list[dict]:
        instances: list[dict] = []
        params = {"filter": f"labels.{label}=true", "returnPartialSuccess": "true"}
        while True:
            reply = self._call(
                "GET",
                f"projects/{project}/aggregated/instances",
                "list the machines",
                project,
                params=params,
            )
            for scoped in (reply.get("items") or {}).values():
                instances.extend(scoped.get("instances", []))
            if not reply.get("nextPageToken"):
                return instances
            params["pageToken"] = reply["nextPageToken"]

    def serial_output(self, project: str, zone: str, name: str, start: int = 0) -> tuple[str, int]:
        reply = self._call(
            "GET",
            f"projects/{project}/zones/{zone}/instances/{name}/serialPort",
            "read the machine's output",
            project,
            params={"port": 1, "start": start},
        )
        return reply.get("contents", ""), int(reply.get("next", start))

    def get_firewall(self, project: str, name: str) -> dict:
        return self._call(
            "GET", f"projects/{project}/global/firewalls/{name}", "read the firewall rule", project
        )

    def insert_firewall(self, project: str, body: dict) -> None:
        what = "create the firewall rule"
        operation = self._call(
            "POST", f"projects/{project}/global/firewalls", what, project, json=body
        )
        self._wait(operation, what, project)

    def patch_firewall(self, project: str, name: str, body: dict) -> None:
        what = "update the firewall rule"
        operation = self._call(
            "PATCH", f"projects/{project}/global/firewalls/{name}", what, project, json=body
        )
        self._wait(operation, what, project)

    def delete_firewall(self, project: str, name: str) -> None:
        what = "delete the firewall rule"
        operation = self._call(
            "DELETE", f"projects/{project}/global/firewalls/{name}", what, project
        )
        self._wait(operation, what, project)
