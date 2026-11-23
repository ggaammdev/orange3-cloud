"""The setup checker for Azure: what's missing in the user's subscription
before a machine can start there.

:func:`run_checks` checks, in order: the sign-in, the subscription, the
resource providers the add-on needs (registered once per subscription), the
user's permissions to create what a machine needs in the resource group
``orange3-cloud``, their right to give each machine the role that lets it
deallocate its own VM, and the vCPU quotas for the chosen size in the region
(its family's and the region's total). Each
:class:`~orangecontrib.cloud.client.providers.setup_checks.Check` says
whether it passed, what was found, how to fix it and where (a link to the
Azure portal or its docs). :func:`register_providers` registers missing
resource providers, after the user agreed.

The permissions are read, not tried: Resource Manager lists what the user
may do at a scope (the resource group, or the subscription while the group
doesn't exist yet), and the needed actions are matched against it.

Azure is called through a :class:`SetupApi` (the Azure libraries in
:class:`AzureSetupApi`), so tests can replace it with a fake subscription.
Every call may be slow, so the dialog runs the checks off the GUI thread.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any

from orangecontrib.cloud.client.providers.azure_account import (
    SIGN_IN_BUTTON,
    AzureSubscription,
    NotSignedInError,
    azure_call,
    check_sign_in,
    compute_client,
    resource_client,
    subscription_client,
)
from orangecontrib.cloud.client.providers.azure_vm import (
    MACHINE_TYPES,
    POWER_ROLE,
    REGIONAL_QUOTA,
    REGIONAL_QUOTA_NAME,
    RESOURCE_GROUP,
    RESOURCE_PROVIDERS,
    family_quota,
)
from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.setup_checks import Check, Status, not_checked

__all__ = [
    "PERMISSIONS",
    "ROLE_ASSIGNMENT_ACTIONS",
    "TITLES",
    "AzureSetupApi",
    "Permission",
    "SetupApi",
    "Usage",
    "allowed",
    "missing_providers",
    "register_providers",
    "run_checks",
]

log = logging.getLogger(__name__)

#: The actions the add-on needs, by what they are for.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "the resource group": (
        "Microsoft.Resources/subscriptions/resourceGroups/read",
        "Microsoft.Resources/subscriptions/resourceGroups/write",
    ),
    "machines": (
        "Microsoft.Compute/virtualMachines/read",
        "Microsoft.Compute/virtualMachines/write",
        "Microsoft.Compute/virtualMachines/deallocate/action",
        "Microsoft.Compute/virtualMachines/delete",
    ),
    "networks": (
        "Microsoft.Network/virtualNetworks/write",
        "Microsoft.Network/virtualNetworks/subnets/join/action",
        "Microsoft.Network/networkSecurityGroups/write",
        "Microsoft.Network/networkSecurityGroups/join/action",
        "Microsoft.Network/networkSecurityGroups/delete",
        "Microsoft.Network/publicIPAddresses/write",
        "Microsoft.Network/publicIPAddresses/join/action",
        "Microsoft.Network/networkInterfaces/write",
        "Microsoft.Network/networkInterfaces/join/action",
    ),
    "storage": (
        "Microsoft.Storage/storageAccounts/read",
        "Microsoft.Storage/storageAccounts/write",
        "Microsoft.Storage/storageAccounts/blobServices/containers/write",
        "Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action",
    ),
}
#: Giving each machine its role (and taking it away before the VM goes), and
#: the user the data role on the add-on's storage account.
ROLE_ASSIGNMENT_ACTIONS = (
    "Microsoft.Authorization/roleAssignments/write",
    "Microsoft.Authorization/roleAssignments/delete",
)

#: Each check's key and title, in order.
TITLES: dict[str, str] = {
    "credentials": "Signed in to Azure",
    "subscription": "Subscription enabled",
    "providers": "Resource providers registered",
    "permissions": "Permissions for machines, networks and storage",
    "role_assignments": "Permission to give machines their role",
    "family_quota": "vCPU quota of the size's family in the region",
    "regional_quota": "Total vCPU quota in the region",
}

_PORTAL = "https://portal.azure.com/#"
_SIGN_IN_DOCS = "https://learn.microsoft.com/cli/azure/authenticate-azure-cli-interactively"
_SUBSCRIPTIONS = _PORTAL + "view/Microsoft_Azure_Billing/SubscriptionsBladeV2"
_QUOTAS = _PORTAL + "view/Microsoft_Azure_Capacity/QuotaMenuBlade/~/myQuotas"
#: Microsoft.Quota's states of a request that isn't decided yet.
_OPEN_REQUEST_STATES = ("Accepted", "InProgress")


def _subscription_page(subscription_id: str, page: str) -> str:
    return f"{_PORTAL}@/resource/subscriptions/{subscription_id}/{page}"


@dataclass(frozen=True)
class Permission:
    """One of the user's permissions at a scope: what it allows, except
    what it excludes (wildcards ``*`` allowed)."""

    actions: tuple[str, ...] = ()
    not_actions: tuple[str, ...] = ()


def _matches(pattern: str, action: str) -> bool:
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, action, flags=re.IGNORECASE) is not None


def allowed(action: str, permissions: Iterable[Permission]) -> bool:
    """Whether ``permissions`` allow ``action`` (as Azure combines them: one
    permission that has it and doesn't exclude it is enough)."""
    return any(
        any(_matches(p, action) for p in permission.actions)
        and not any(_matches(p, action) for p in permission.not_actions)
        for permission in permissions
    )


@dataclass(frozen=True)
class Usage:
    """A quota in a region and how much of it is used."""

    used: float
    limit: float
    name: str = ""  #: as the Azure portal names it


class SetupApi:
    """The Azure calls the checker makes, for one subscription. Each raises
    a :class:`ProviderError` (:class:`~orangecontrib.cloud.client.providers.
    azure_account.NotSignedInError` if the sign-in no longer works)."""

    def check_sign_in(self) -> None:
        """Raise :class:`NotSignedInError` unless the sign-in works."""
        raise NotImplementedError

    def subscription(self) -> AzureSubscription | None:
        """The subscription (``None``: not found, or no access)."""
        raise NotImplementedError

    def provider_states(self, namespaces: Sequence[str]) -> dict[str, str]:
        """Each resource provider's registration state (``Registered``, …)."""
        raise NotImplementedError

    def register_providers(self, namespaces: Sequence[str]) -> None:
        """Register the resource providers (registering goes on in Azure)."""
        raise NotImplementedError

    def resource_group_exists(self, name: str) -> bool:
        """Whether the subscription has the resource group ``name``."""
        raise NotImplementedError

    def permissions(self, scope: str) -> list[Permission]:
        """The signed-in user's permissions at ``scope`` (a resource ID)."""
        raise NotImplementedError

    def usages(self, region: str) -> dict[str, Usage]:
        """The region's vCPU quotas and their use, by their names in
        lower case (e.g. ``standarddsv6family``, ``cores``)."""
        raise NotImplementedError

    def open_quota_requests(self, region: str) -> dict[str, float]:
        """The requested value of each quota (lower-case name) with a
        request that isn't decided yet; empty if they can't be read."""
        return {}


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


class AzureSetupApi(SetupApi):
    """:class:`SetupApi` with the Azure libraries and a credential from
    :func:`~orangecontrib.cloud.client.providers.azure_account.credential`."""

    def __init__(self, credential: Any, subscription_id: str):
        self.credential = credential
        self.subscription_id = subscription_id
        self._clients: dict[str, Any] = {}
        #: The checks call from several threads.
        self._lock = threading.Lock()

    def _client(self, kind: str) -> Any:
        """The ``subscriptions``, ``resources`` or ``compute`` client (made once)."""
        with self._lock:
            if kind not in self._clients:
                if kind == "subscriptions":
                    self._clients[kind] = subscription_client(self.credential)
                elif kind == "resources":
                    self._clients[kind] = resource_client(self.credential, self.subscription_id)
                else:
                    self._clients[kind] = compute_client(self.credential, self.subscription_id)
            return self._clients[kind]

    def _get(self, path: str, api_version: str, what: str) -> dict:
        """A Resource Manager ``GET`` of ``path`` (JSON)."""
        from azure.core.rest import HttpRequest

        def get() -> dict:
            request = HttpRequest("GET", path, params={"api-version": api_version})
            response = self._client("resources").send_request(request)
            response.raise_for_status()
            return response.json()

        return azure_call(what, get)

    def check_sign_in(self) -> None:
        check_sign_in(self.credential)

    def subscription(self) -> AzureSubscription | None:
        from azure.core.exceptions import ResourceNotFoundError

        def get() -> Any:
            try:
                return self._client("subscriptions").subscriptions.get(self.subscription_id)
            except ResourceNotFoundError:
                return None

        found = azure_call("read the subscription", get)
        if found is None:
            return None
        policies = found.subscription_policies
        return AzureSubscription(
            id=found.subscription_id or self.subscription_id,
            name=found.display_name or "",
            tenant_id=found.tenant_id or "",
            state=_enum_text(found.state),
            quota_id=(policies.quota_id if policies is not None else "") or "",
        )

    def provider_states(self, namespaces: Sequence[str]) -> dict[str, str]:
        providers = self._client("resources").providers
        return azure_call(
            "read the resource providers",
            lambda: {n: providers.get(n).registration_state or "" for n in namespaces},
        )

    def register_providers(self, namespaces: Sequence[str]) -> None:
        providers = self._client("resources").providers
        for namespace in namespaces:
            azure_call(f"register {namespace}", lambda n=namespace: providers.register(n))

    def resource_group_exists(self, name: str) -> bool:
        groups = self._client("resources").resource_groups
        return azure_call(
            f"look for the resource group {name}", lambda: bool(groups.check_existence(name))
        )

    def permissions(self, scope: str) -> list[Permission]:
        reply = self._get(
            f"{scope}/providers/Microsoft.Authorization/permissions",
            "2022-04-01",
            "read your permissions",
        )
        return [
            Permission(tuple(p.get("actions") or ()), tuple(p.get("notActions") or ()))
            for p in reply.get("value", [])
        ]

    def usages(self, region: str) -> dict[str, Usage]:
        compute = self._client("compute")
        return azure_call(
            f"read the vCPU quotas of {region}",
            lambda: {
                u.name.value.lower(): Usage(
                    float(u.current_value), float(u.limit), u.name.localized_value or ""
                )
                for u in compute.usage.list(region)
            },
        )

    def open_quota_requests(self, region: str) -> dict[str, float]:
        path = (
            f"/subscriptions/{self.subscription_id}/providers/Microsoft.Compute/locations/"
            f"{region}/providers/Microsoft.Quota/quotaRequests"
        )
        try:
            reply = self._get(path, "2023-02-01", "list the quota requests")
        except ProviderError as exc:  # e.g. Microsoft.Quota isn't registered
            log.info("Can't list the quota requests: %s", exc)
            return {}
        requested: dict[str, float] = {}
        for request in reply.get("value", []):
            properties = request.get("properties") or {}
            if properties.get("provisioningState") not in _OPEN_REQUEST_STATES:
                continue
            for item in properties.get("value") or []:
                name = str((item.get("name") or {}).get("value", "")).lower()
                value = (item.get("limit") or {}).get("value")
                if name and isinstance(value, (int, float)):
                    requested[name] = max(requested.get(name, 0.0), float(value))
        return requested


# ----------------------------------------------------------------------
# Registering resource providers


def missing_providers(checks: Iterable[Check]) -> tuple[str, ...]:
    """The resource providers that the checks found unregistered."""
    for check in checks:
        if check.key == "providers" and check.status is Status.FAILED:
            return check.missing
    return ()


def register_providers(
    api: SetupApi, namespaces: Iterable[str], *, wait: float = 120, poll: float = 5
) -> None:
    """Register the resource providers (only after the user agreed), and
    wait up to ``wait`` seconds until Azure has registered them."""
    namespaces = list(namespaces)
    if not namespaces:
        return
    api.register_providers(namespaces)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        states = api.provider_states(namespaces)
        if all(states.get(n, "").lower() == "registered" for n in namespaces):
            return
        time.sleep(poll)


# ----------------------------------------------------------------------
# The checks


def _skipped(keys: Iterable[str], why: str) -> list[Check]:
    return [Check(key, TITLES[key], Status.UNKNOWN, detail=f"Not checked: {why}.") for key in keys]


def run_checks(
    credential: Any,
    subscription: AzureSubscription | None,
    region: str,
    machine_type: str,
    *,
    api: SetupApi | None = None,
    make_api: Callable[[Any, str], SetupApi] = AzureSetupApi,
) -> list[Check]:
    """Check everything a machine of ``machine_type`` in ``region`` needs in
    ``subscription``, with the Azure CLI's ``credential`` (``None`` when the
    user isn't signed in). ``api`` replaces Azure (tests)."""
    keys = list(TITLES)
    if credential is None and api is None:
        signed_out = NotSignedInError("No Azure sign-in on this computer.")
        return [_credentials_failed(signed_out), *_skipped(keys[1:], "sign in first")]
    if api is None:
        api = make_api(credential, subscription.id if subscription else "")
    checks = [_credentials_check(api, subscription)]
    if not checks[-1].passed:
        return checks + _skipped(keys[1:], "sign in first")
    subscription_check, found = _subscription_check(api, subscription)
    checks.append(subscription_check)
    if found is None:
        return checks + _skipped(keys[2:], "choose an enabled subscription first")
    # The rest are independent calls: make them at once.
    with ThreadPoolExecutor(3, thread_name_prefix="orange3-cloud-azure-check") as pool:
        providers = pool.submit(_providers_check, api, found.id)
        permissions = pool.submit(_permission_checks, api, found.id)
        quotas = pool.submit(_quota_checks, api, found, region, machine_type)
        providers_check = providers.result()
        checks += [providers_check, *permissions.result(), *quotas.result()]
    if "Microsoft.Compute" in providers_check.missing:
        checks[-2:] = _skipped(keys[-2:], "Microsoft.Compute isn't registered")
    return checks


def _credentials_failed(error: Exception) -> Check:
    return Check(
        "credentials",
        TITLES["credentials"],
        Status.FAILED,
        detail=str(error),
        fix=f"Click “{SIGN_IN_BUTTON}”.",
        link=_SIGN_IN_DOCS,
    )


def _credentials_check(api: SetupApi, subscription: AzureSubscription | None) -> Check:
    try:
        api.check_sign_in()
    except NotSignedInError as exc:
        return _credentials_failed(exc)
    except ProviderError as exc:
        return not_checked("credentials", TITLES["credentials"], exc, _SIGN_IN_DOCS)
    who = subscription.user if subscription is not None else ""
    return Check("credentials", TITLES["credentials"], Status.PASSED, detail=who)


def _subscription_check(
    api: SetupApi, chosen: AzureSubscription | None
) -> tuple[Check, AzureSubscription | None]:
    """The check, and the subscription if it is enabled."""
    title = TITLES["subscription"]
    if chosen is None or not chosen.id:
        return Check(
            "subscription",
            title,
            Status.FAILED,
            detail="No subscription is selected.",
            fix="Choose a subscription, or create one in the Azure portal.",
            link=_SUBSCRIPTIONS,
        ), None
    try:
        found = api.subscription()
    except ProviderError as exc:
        return not_checked("subscription", title, exc, _SUBSCRIPTIONS), None
    if found is None:
        return Check(
            "subscription",
            title,
            Status.FAILED,
            detail=f"Subscription {chosen.label} wasn't found, or you have no access to it.",
            fix=f"Choose another subscription, or click “{SIGN_IN_BUTTON}” as a user who "
            "has access to it.",
            link=_SUBSCRIPTIONS,
        ), None
    found = replace(found, user=chosen.user, name=found.name or chosen.name)
    if found.state != "Enabled":
        return Check(
            "subscription",
            title,
            Status.FAILED,
            detail=f"Subscription {found.label} is {found.state or 'not enabled'}.",
            fix="Check its billing in the Azure portal, or choose another subscription.",
            link=_SUBSCRIPTIONS,
        ), None
    return Check("subscription", title, Status.PASSED, detail=f"{found.label} ({found.id})"), found


def _providers_check(api: SetupApi, subscription_id: str) -> Check:
    title, names = TITLES["providers"], list(RESOURCE_PROVIDERS)
    link = _subscription_page(subscription_id, "resourceproviders")
    try:
        states = api.provider_states(names)
    except ProviderError as exc:
        return not_checked("providers", title, exc, link)
    missing = tuple(n for n in names if states.get(n, "").lower() != "registered")
    if not missing:
        return Check("providers", title, Status.PASSED, detail=", ".join(names))
    registering = [n for n in missing if states.get(n, "").lower() == "registering"]
    detail = f"Not registered: {', '.join(missing)}."
    if registering:
        detail += f" Azure is registering {', '.join(registering)}: check again in a few minutes."
    return Check(
        "providers",
        title,
        Status.FAILED,
        detail=detail,
        fix="Click “Register…”, or register them in the Azure portal "
        "(the subscription's Resource providers).",
        link=link,
        missing=missing,
    )


def _permission_checks(api: SetupApi, subscription_id: str) -> list[Check]:
    """The permissions on the resource group ``orange3-cloud``, or on the
    subscription while the group doesn't exist (the add-on creates it)."""
    keys = ("permissions", "role_assignments")
    link = _subscription_page(subscription_id, "users")
    try:
        exists = api.resource_group_exists(RESOURCE_GROUP)
        scope = f"/subscriptions/{subscription_id}"
        if exists:
            scope += f"/resourceGroups/{RESOURCE_GROUP}"
            link = _subscription_page(subscription_id, f"resourceGroups/{RESOURCE_GROUP}/users")
        permissions = api.permissions(scope)
    except ProviderError as exc:
        return [not_checked(key, TITLES[key], exc, link) for key in keys]
    where = (
        f"the resource group {RESOURCE_GROUP}"
        if exists
        else f"the subscription (the resource group {RESOURCE_GROUP} doesn't exist yet)"
    )
    return [_resources_check(permissions, where, link), _roles_check(permissions, where, link)]


def _resources_check(permissions: list[Permission], where: str, link: str) -> Check:
    title = TITLES["permissions"]
    denied = [a for group in PERMISSIONS.values() for a in group if not allowed(a, permissions)]
    if not denied:
        return Check("permissions", title, Status.PASSED, detail=f"Checked on {where}.")
    groups = [g for g, actions in PERMISSIONS.items() if set(actions) & set(denied)]
    return Check(
        "permissions",
        title,
        Status.FAILED,
        detail=f"On {where}, you can't manage {', '.join(groups)}. Missing: {', '.join(denied)}.",
        fix=f"Ask the subscription's owner for the Contributor role on the resource group "
        f"{RESOURCE_GROUP} or the subscription.",
        link=link,
        missing=tuple(denied),
    )


def _roles_check(permissions: list[Permission], where: str, link: str) -> Check:
    title = TITLES["role_assignments"]
    denied = tuple(a for a in ROLE_ASSIGNMENT_ACTIONS if not allowed(a, permissions))
    if not denied:
        return Check("role_assignments", title, Status.PASSED, detail=f"Checked on {where}.")
    return Check(
        "role_assignments",
        title,
        Status.FAILED,
        detail=f"You can't assign roles on {where}. Each machine gets the role “{POWER_ROLE}” "
        "on its own VM, so that it can deallocate itself when it stops, and you get the role "
        "“Storage Blob Data Contributor” on the add-on's storage account for uploads. "
        f"Missing: {', '.join(denied)}.",
        fix="Ask the subscription's owner for the Owner, User Access Administrator or Role "
        f"Based Access Control Administrator role on the resource group {RESOURCE_GROUP} or "
        "the subscription. Contributor isn't enough.",
        link=link,
        missing=denied,
    )


@dataclass(frozen=True)
class _Need:
    """What the chosen size needs from a region's quotas."""

    machine_type: str
    cpus: int
    region: str
    #: A free trial subscription (it can't get more quota).
    free_trial: bool
    #: Values of open requests to raise quotas, by lower-case name.
    requested: dict[str, float]


def _quota_checks(
    api: SetupApi, subscription: AzureSubscription, region: str, machine_type: str
) -> list[Check]:
    keys = ["family_quota", "regional_quota"]
    if not region:
        no_region = Check(
            keys[0],
            TITLES[keys[0]],
            Status.FAILED,
            detail="No region is selected.",
            fix="Choose a region.",
        )
        return [no_region, *_skipped(keys[1:], "choose a region first")]
    try:
        usages = api.usages(region)
    except ProviderError as exc:
        return [not_checked(key, TITLES[key], exc, _QUOTAS) for key in keys]
    size = next((t for t in MACHINE_TYPES if t.name == machine_type), None)
    need = _Need(
        machine_type,
        (size.cpus if size else None) or 0,
        region,
        subscription.quota_id.lower().startswith("freetrial"),
        api.open_quota_requests(region),
    )
    family, family_name = family_quota(machine_type)
    return [
        _quota_check(keys[0], family.lower(), family_name, usages, need),
        _quota_check(keys[1], REGIONAL_QUOTA, REGIONAL_QUOTA_NAME, usages, need),
    ]


def _quota_check(key: str, name: str, label: str, usages: dict[str, Usage], need: _Need) -> Check:
    title = TITLES[key]
    usage = usages.get(name)
    if usage is None:
        return Check(
            key,
            title,
            Status.FAILED,
            detail=f"Region {need.region} has no quota “{label}”, so it doesn't offer "
            f"{need.machine_type}.",
            fix="Choose another region.",
            link=_QUOTAS,
        )
    free = usage.limit - usage.used
    detail = f"{free:g} of {usage.limit:g} vCPUs free; {need.machine_type} needs {need.cpus}."
    if free >= need.cpus:
        return Check(key, title, Status.PASSED, detail=detail)
    if need.requested.get(name):
        detail += f" A request for {need.requested[name]:g} vCPUs is open."
    fix = (
        f"Request a quota of at least {usage.used + need.cpus:g} vCPUs for “{label}” in "
        f"{need.region} (Azure portal, Quotas), stop other VMs there, or choose a smaller size."
    )
    if need.free_trial:
        fix += " Free trial subscriptions can't get more: upgrade to pay-as-you-go first."
    return Check(key, title, Status.FAILED, detail=detail, fix=fix, link=_QUOTAS)
