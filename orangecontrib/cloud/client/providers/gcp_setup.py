"""The setup checker: what's missing in the user's Google Cloud project
before a machine can start there.

:func:`run_checks` checks, in order: credentials, project, billing, the
Compute Engine and Cloud Storage APIs, the CPU quotas for the chosen machine
type in the region (the family's quota and the global ``CPUS_ALL_REGIONS``)
and the user's permissions for machines, firewall rules and buckets. Each
:class:`~orangecontrib.cloud.client.providers.setup_checks.Check` says
whether it passed, what was found, how to fix it and where (a link to the
Google Cloud console). :func:`enable_apis` turns on missing APIs (Service
Usage API); the settings dialog calls it only after the user confirms.

Google's APIs are called with plain REST requests through a
:class:`SetupApi`, so tests can replace it with a fake. Every call may be
slow, so the dialog runs the checks off the GUI thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple
from urllib.parse import urlencode

from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.gcp_account import ApiDisabledError, NotSignedInError
from orangecontrib.cloud.client.providers.gcp_vm import MACHINE_TYPES
from orangecontrib.cloud.client.providers.setup_checks import Check, Status, not_checked

__all__ = [
    "PERMISSIONS",
    "REQUIRED_APIS",
    "GoogleSetupApi",
    "SetupApi",
    "cpu_quota_metric",
    "enable_apis",
    "missing_apis",
    "run_checks",
]

#: The APIs a machine needs: service name -> name for the user.
REQUIRED_APIS: dict[str, str] = {
    "compute.googleapis.com": "Compute Engine API",
    "storage.googleapis.com": "Cloud Storage API",
}

#: Project-level permissions the add-on needs, by what they are for. Bucket
#: permissions on objects and the bucket's own policy aren't listed: the
#: project's owners and editors get those on each bucket, not on the project.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "machines": (
        "compute.instances.create",
        "compute.instances.delete",
        "compute.instances.get",
        "compute.instances.list",
        "compute.instances.setLabels",
        "compute.instances.setMetadata",
        "compute.instances.setServiceAccount",
        "compute.instances.setTags",
        "compute.disks.create",
        "compute.subnetworks.use",
        "compute.subnetworks.useExternalIp",
        "iam.serviceAccounts.actAs",
    ),
    "firewall rules": (
        "compute.firewalls.create",
        "compute.firewalls.delete",
        "compute.firewalls.get",
        "compute.firewalls.update",
        "compute.networks.updatePolicy",
    ),
    "buckets": (
        "storage.buckets.create",
        "storage.buckets.delete",
        "storage.buckets.list",
    ),
}

#: The global CPU quota every machine counts against.
GLOBAL_CPU_METRIC = "CPUS_ALL_REGIONS"

_CONSOLE = "https://console.cloud.google.com"
_ADC_DOCS = "https://cloud.google.com/docs/authentication/provide-credentials-adc"
_QUOTA_DOCS = "https://cloud.google.com/compute/resource-usage"


def missing_apis(checks: Iterable[Check]) -> tuple[str, ...]:
    """The service names of the APIs that the checks found disabled."""
    for check in checks:
        if check.key == "apis" and check.status is Status.FAILED:
            return check.missing
    return ()


def cpu_quota_metric(machine_type: str) -> str:
    """The regional CPU quota of a machine type's family: E2 machines use
    ``CPUS``, the others ``<FAMILY>_CPUS`` (``N2_CPUS``, ``N2D_CPUS``,
    ``C3_CPUS``)."""
    family = machine_type.split("-", 1)[0].upper()
    return "CPUS" if family in ("E2", "N1", "F1", "G1") else f"{family}_CPUS"


def _console(path: str, project: str, **params: str) -> str:
    query = urlencode({**params, "project": project}) if project else urlencode(params)
    return f"{_CONSOLE}/{path}" + (f"?{query}" if query else "")


# ----------------------------------------------------------------------
# Google's APIs


class SetupApi:
    """The Google calls the checker makes. Each raises a
    :class:`ProviderError` (:class:`ApiDisabledError` if the API is off,
    :class:`NotSignedInError` if the sign-in no longer works)."""

    def billing_enabled(self, project: str) -> bool:
        """Whether the project has billing enabled."""
        raise NotImplementedError

    def enabled_services(self, project: str, services: Iterable[str]) -> set[str]:
        """Which of ``services`` are enabled."""
        raise NotImplementedError

    def enable_services(self, project: str, services: Iterable[str]) -> None:
        """Enable ``services`` and wait until they are."""
        raise NotImplementedError

    def region_quotas(self, project: str, region: str) -> dict[str, tuple[float, float]]:
        """Regional quotas: metric -> (limit, usage)."""
        raise NotImplementedError

    def project_quotas(self, project: str) -> dict[str, tuple[float, float]]:
        """Global quotas: metric -> (limit, usage)."""
        raise NotImplementedError

    def granted_permissions(self, project: str, permissions: Iterable[str]) -> set[str]:
        """Which of ``permissions`` the user has in the project."""
        raise NotImplementedError


class GoogleSetupApi(SetupApi):
    """:class:`SetupApi` with REST calls, using the credentials from
    :func:`~orangecontrib.cloud.client.providers.gcp_account.load_credentials`."""

    BILLING = "https://cloudbilling.googleapis.com/v1"
    SERVICE_USAGE = "https://serviceusage.googleapis.com/v1"
    COMPUTE = "https://compute.googleapis.com/compute/v1"
    RESOURCE_MANAGER = "https://cloudresourcemanager.googleapis.com/v3"

    def __init__(self, credentials: Any, timeout: float = 30):
        from google.auth.transport.requests import AuthorizedSession

        self.session = AuthorizedSession(credentials)
        self.timeout = timeout

    def _call(self, method: str, url: str, what: str, project: str, **kwargs: Any) -> Any:
        from google.auth import exceptions as auth
        from requests import RequestException

        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
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
        raise _http_error(response, what, project)

    def billing_enabled(self, project: str) -> bool:
        info = self._call(
            "GET", f"{self.BILLING}/projects/{project}/billingInfo", "read the billing", project
        )
        return bool(info.get("billingEnabled"))

    def enabled_services(self, project: str, services: Iterable[str]) -> set[str]:
        names = [f"projects/{project}/services/{s}" for s in services]
        reply = self._call(
            "GET",
            f"{self.SERVICE_USAGE}/projects/{project}/services:batchGet",
            "read the enabled APIs",
            project,
            params={"names": names},
        )
        return {
            s.get("config", {}).get("name") or s.get("name", "").rsplit("/", 1)[-1]
            for s in reply.get("services", [])
            if s.get("state") == "ENABLED"
        }

    def enable_services(self, project: str, services: Iterable[str], wait: float = 300) -> None:
        operation = self._call(
            "POST",
            f"{self.SERVICE_USAGE}/projects/{project}/services:batchEnable",
            "enable the APIs",
            project,
            json={"serviceIds": list(services)},
        )
        deadline = time.monotonic() + wait
        while not operation.get("done"):
            if time.monotonic() > deadline:
                raise ProviderError("Enabling the APIs is taking long. Check again in a minute.")
            time.sleep(2)
            operation = self._call(
                "GET",
                f"{self.SERVICE_USAGE}/{operation['name']}",
                "enable the APIs",
                project,
            )
        if "error" in operation:
            message = operation["error"].get("message", "")
            raise ProviderError(f"Couldn't enable the APIs: {message}")

    @staticmethod
    def _quotas(reply: dict) -> dict[str, tuple[float, float]]:
        return {
            q["metric"]: (float(q.get("limit", 0)), float(q.get("usage", 0)))
            for q in reply.get("quotas", [])
            if "metric" in q
        }

    def region_quotas(self, project: str, region: str) -> dict[str, tuple[float, float]]:
        reply = self._call(
            "GET",
            f"{self.COMPUTE}/projects/{project}/regions/{region}",
            "read the quotas",
            project,
        )
        return self._quotas(reply)

    def project_quotas(self, project: str) -> dict[str, tuple[float, float]]:
        reply = self._call("GET", f"{self.COMPUTE}/projects/{project}", "read the quotas", project)
        return self._quotas(reply)

    def granted_permissions(self, project: str, permissions: Iterable[str]) -> set[str]:
        reply = self._call(
            "POST",
            f"{self.RESOURCE_MANAGER}/projects/{project}:testIamPermissions",
            "check your permissions",
            project,
            json={"permissions": list(permissions)},
        )
        return set(reply.get("permissions", []))


def _http_error(response: Any, what: str, project: str) -> ProviderError:
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    message = error.get("message") or response.text[:300] or response.reason
    reasons = {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}
    for item in error.get("errors", []) or []:
        if isinstance(item, dict):
            reasons.add(item.get("reason"))
    if response.status_code == 401:
        return NotSignedInError(
            'Your Google sign-in has expired or was revoked. Click "Sign in with Google".'
        )
    if (
        "SERVICE_DISABLED" in reasons
        or "accessNotConfigured" in reasons
        or "has not been used in project" in message
    ):
        return ApiDisabledError(f"Couldn't {what}: the API isn't enabled. {message}")
    if response.status_code == 403:
        return ProviderError(f"Couldn't {what}: no permission in project {project}. {message}")
    if response.status_code == 404:
        return ProviderError(f"Couldn't {what}: project {project} wasn't found.")
    return ProviderError(f"Couldn't {what}: {message}")


# ----------------------------------------------------------------------
# The checks


def enable_apis(api: SetupApi, project: str, services: Iterable[str]) -> None:
    """Enable the APIs (only after the user confirmed)."""
    services = list(services)
    if services:
        api.enable_services(project, services)


def run_checks(
    credentials: Any,
    project: str,
    region: str,
    machine_type: str,
    *,
    api: SetupApi | None = None,
    make_api: Callable[[Any], SetupApi] = GoogleSetupApi,
) -> list[Check]:
    """Check everything a machine of ``machine_type`` in ``region`` needs in
    ``project``. ``credentials`` is ``None`` when the user isn't signed in.
    ``api`` replaces Google's APIs (tests)."""
    checks: list[Check] = []
    title = "Signed in to Google Cloud"
    if credentials is None:
        checks.append(
            Check(
                "credentials",
                title,
                Status.FAILED,
                detail="No Google Cloud sign-in on this computer.",
                fix="Click “Sign in with Google”.",
                link=_ADC_DOCS,
            )
        )
    else:
        checks.append(Check("credentials", title, Status.PASSED))

    title = "Project selected"
    if not project:
        checks.append(
            Check(
                "project",
                title,
                Status.FAILED,
                detail="No project is selected.",
                fix="Choose a project, or create one in the Google Cloud console and click Reload.",
                link=f"{_CONSOLE}/projectcreate",
            )
        )
    else:
        checks.append(Check("project", title, Status.PASSED, detail=project))

    rest = [
        ("billing", "Billing enabled"),
        ("apis", "Compute Engine and Cloud Storage APIs enabled"),
        ("quota_family", "CPU quota in the region"),
        ("quota_global", "CPU quota in all regions"),
        ("permissions", "Permissions for machines, firewall rules and buckets"),
    ]
    if credentials is None or not project:
        why = "sign in first" if credentials is None else "choose a project first"
        checks.extend(
            Check(key, t, Status.UNKNOWN, detail=f"Not checked: {why}.") for key, t in rest
        )
        return checks

    api = api if api is not None else make_api(credentials)
    titles = dict(rest)

    # Billing
    link = _console("billing/linkedaccount", project)
    try:
        enabled = api.billing_enabled(project)
    except ProviderError as exc:
        checks.append(not_checked("billing", titles["billing"], exc, link))
    else:
        checks.append(
            Check("billing", titles["billing"], Status.PASSED)
            if enabled
            else Check(
                "billing",
                titles["billing"],
                Status.FAILED,
                detail=f"Project {project} has no billing account.",
                fix="Link the project to a billing account in the Google Cloud console.",
                link=link,
            )
        )

    # APIs
    compute_on = True
    try:
        enabled_apis = api.enabled_services(project, REQUIRED_APIS)
    except ProviderError as exc:
        checks.append(not_checked("apis", titles["apis"], exc, _console("apis/dashboard", project)))
    else:
        missing = tuple(s for s in REQUIRED_APIS if s not in enabled_apis)
        compute_on = "compute.googleapis.com" not in missing
        if missing:
            names = ", ".join(REQUIRED_APIS[s] for s in missing)
            checks.append(
                Check(
                    "apis",
                    titles["apis"],
                    Status.FAILED,
                    detail=f"Not enabled: {names}.",
                    fix="Click “Enable APIs”, or enable them in the Google Cloud console."
                    + (" Billing must be enabled first." if not _passed(checks, "billing") else ""),
                    link=_console("flows/enableapi", project, apiid=",".join(missing)),
                    missing=missing,
                )
            )
        else:
            checks.append(Check("apis", titles["apis"], Status.PASSED))

    # Quotas
    checks.extend(_quota_checks(api, project, region, machine_type, compute_on, titles))

    # Permissions
    wanted = [p for group in PERMISSIONS.values() for p in group]
    link = _console("iam-admin/iam", project)
    try:
        granted = api.granted_permissions(project, wanted)
    except ProviderError as exc:
        checks.append(not_checked("permissions", titles["permissions"], exc, link))
    else:
        missing = tuple(p for p in wanted if p not in granted)
        if missing:
            groups = [g for g, perms in PERMISSIONS.items() if set(perms) & set(missing)]
            checks.append(
                Check(
                    "permissions",
                    titles["permissions"],
                    Status.FAILED,
                    detail=f"You can't manage {', '.join(groups)}. Missing: {', '.join(missing)}.",
                    fix="Ask the project's owner for the Editor role "
                    "(or Compute Admin, Storage Admin and Service Account User).",
                    link=link,
                    missing=missing,
                )
            )
        else:
            checks.append(Check("permissions", titles["permissions"], Status.PASSED))
    return checks


def _passed(checks: list[Check], key: str) -> bool:
    return any(c.key == key and c.passed for c in checks)


def _quota_checks(
    api: SetupApi,
    project: str,
    region: str,
    machine_type: str,
    compute_on: bool,
    titles: dict[str, str],
) -> list[Check]:
    size = next((t for t in MACHINE_TYPES if t.name == machine_type), None)
    cpus = (size.cpus if size else None) or 0
    metric = cpu_quota_metric(machine_type)
    label = machine_type
    family_title = f"{metric} quota in {region or 'the region'}"
    global_title = f"{GLOBAL_CPU_METRIC} quota"
    if not compute_on:
        why = "Not checked: the Compute Engine API isn't enabled."
        return [
            Check("quota_family", family_title, Status.UNKNOWN, detail=why),
            Check("quota_global", global_title, Status.UNKNOWN, detail=why),
        ]
    if not region:
        return [
            Check(
                "quota_family", family_title, Status.UNKNOWN, detail="Not checked: choose a region."
            ),
            _global_quota(api, project, cpus, label, global_title),
        ]
    link = _console("iam-admin/quotas", project)
    try:
        quotas = api.region_quotas(project, region)
    except ProviderError as exc:
        family = not_checked("quota_family", family_title, exc, link)
    else:
        family = _quota_check(
            "quota_family",
            family_title,
            quotas.get(metric),
            cpus,
            label,
            _Quota(metric, region, link),
        )
    return [family, _global_quota(api, project, cpus, label, global_title)]


def _global_quota(api: SetupApi, project: str, cpus: int, label: str, title: str) -> Check:
    link = _console("iam-admin/quotas", project)
    try:
        quotas = api.project_quotas(project)
    except ProviderError as exc:
        return not_checked("quota_global", title, exc, link)
    return _quota_check(
        "quota_global",
        title,
        quotas.get(GLOBAL_CPU_METRIC),
        cpus,
        label,
        _Quota(GLOBAL_CPU_METRIC, "", link),
    )


class _Quota(NamedTuple):
    """A CPU quota: its metric, its region (``""``: global) and where to change it."""

    metric: str
    region: str
    link: str


def _quota_check(
    key: str,
    title: str,
    quota: tuple[float, float] | None,
    cpus: int,
    label: str,
    which: _Quota,
) -> Check:
    metric, region, link = which
    where = f" in {region}" if region else ""
    if quota is None:
        return Check(
            key,
            title,
            Status.FAILED,
            detail=f"No {metric} quota{where}, so {label} machines can't run there.",
            fix="Choose another region or machine size, or request this quota.",
            link=_QUOTA_DOCS,
        )
    limit, usage = quota
    free = limit - usage
    detail = f"{free:g} of {limit:g} vCPUs free; {label} needs {cpus}."
    if free >= cpus:
        return Check(key, title, Status.PASSED, detail=detail)
    return Check(
        key,
        title,
        Status.FAILED,
        detail=detail,
        fix=f"Request a {metric} quota{where} of at least {usage + cpus:g}, "
        "stop other machines, or choose a smaller size.",
        link=link,
    )
