"""The Azure setup checker, with Azure replaced by a fake subscription: an
unprepared subscription lists exactly what's missing, a ready one passes,
and permissions with wildcards and exclusions are read as Azure does.

``test_real_subscription_is_ready`` checks the development test
subscription with this computer's own Azure CLI sign-in (read-only calls:
nothing is created); it runs only when ``ORANGE3_CLOUD_AZURE_SUBSCRIPTION``
is set, so CI needs no Azure sign-in."""

import os

import pytest

from orangecontrib.cloud.client.providers import azure_account, azure_setup
from orangecontrib.cloud.client.providers.azure_account import AzureSubscription, NotSignedInError
from orangecontrib.cloud.client.providers.azure_setup import Permission, Usage
from orangecontrib.cloud.client.providers.azure_vm import family_quota
from orangecontrib.cloud.client.providers.setup_checks import Status, all_passed

SUBSCRIPTION = AzureSubscription("sub-1", "Research", "tenant-1", user="ana@example.com")
OWNER = [Permission(("*",))]
#: The built-in Contributor role: everything but assigning roles.
CONTRIBUTOR = [
    Permission(
        ("*",),
        (
            "Microsoft.Authorization/*/Delete",
            "Microsoft.Authorization/*/Write",
            "Microsoft.Authorization/elevateAccess/Action",
        ),
    )
]
REGISTERED = {"Microsoft.Compute": "Registered", "Microsoft.Network": "Registered"}
REGISTERED["Microsoft.Storage"] = "Registered"
USAGES = {
    "standarddsv6family": Usage(0, 32, "Standard Dsv6 Family vCPUs"),
    "standardesv6family": Usage(0, 32, "Standard Esv6 Family vCPUs"),
    "cores": Usage(0, 76, "Total Regional vCPUs"),
}


class FakeSubscription(azure_setup.SetupApi):
    """A subscription that is ready unless told otherwise."""

    def __init__(
        self,
        signed_in=True,
        state="Enabled",
        providers=REGISTERED,
        group_exists=False,
        permissions=OWNER,
        usages=USAGES,
    ):
        self.signed_in = signed_in
        self.state = state
        self.providers = dict(providers)
        self.group_exists = group_exists
        self._permissions = permissions
        self._usages = usages
        self.scopes = []
        self.registered = []

    def check_sign_in(self):
        if not self.signed_in:
            raise NotSignedInError("The Azure sign-in has expired.")

    def subscription(self):
        if self.state is None:
            return None
        return AzureSubscription("sub-1", "Research", "tenant-1", self.state, quota_id="x")

    def provider_states(self, namespaces):
        return {n: self.providers.get(n, "NotRegistered") for n in namespaces}

    def register_providers(self, namespaces):
        self.registered += namespaces
        self.providers.update(dict.fromkeys(namespaces, "Registered"))

    def resource_group_exists(self, name):
        return self.group_exists

    def permissions(self, scope):
        self.scopes.append(scope)
        return self._permissions

    def usages(self, region):
        return self._usages

    def open_quota_requests(self, region):
        return {"standarddsv6family": 64.0}


def statuses(api, region="eastus", machine_type="Standard_D16s_v6", subscription=SUBSCRIPTION):
    checks = azure_setup.run_checks(object(), subscription, region, machine_type, api=api)
    assert [c.key for c in checks] == list(azure_setup.TITLES)
    return {c.key: c for c in checks}


def failed(checks):
    return {key for key, c in checks.items() if c.status is Status.FAILED}


def test_ready_subscription_passes():
    api = FakeSubscription()
    checks = statuses(api)
    assert all_passed(checks.values()), [c for c in checks.values() if not c.passed]
    assert checks["credentials"].detail == "ana@example.com"
    assert checks["subscription"].detail == "Research (sub-1)"
    assert checks["family_quota"].detail == "32 of 32 vCPUs free; Standard_D16s_v6 needs 16."
    # The resource group doesn't exist yet: the permissions are read on the subscription.
    assert api.scopes == ["/subscriptions/sub-1"]
    assert "doesn't exist yet" in checks["permissions"].detail
    api = FakeSubscription(group_exists=True)
    statuses(api)
    assert api.scopes == ["/subscriptions/sub-1/resourceGroups/orange3-cloud"]


def test_unprepared_subscription_lists_whats_missing():
    api = FakeSubscription(
        providers={"Microsoft.Compute": "Registered", "Microsoft.Network": "Registering"},
        group_exists=True,
        permissions=CONTRIBUTOR,
        usages={**USAGES, "standarddsv6family": Usage(24, 32), "cores": Usage(24, 76)},
    )
    checks = statuses(api, machine_type="Standard_D32s_v6")
    assert failed(checks) == {"providers", "role_assignments", "family_quota"}

    providers = checks["providers"]
    assert providers.missing == ("Microsoft.Network", "Microsoft.Storage")
    assert "Azure is registering Microsoft.Network" in providers.detail
    assert "/resourceproviders" in providers.link
    assert azure_setup.missing_providers(checks.values()) == providers.missing

    roles = checks["role_assignments"]
    assert roles.missing == azure_setup.ROLE_ASSIGNMENT_ACTIONS
    assert roles.detail.startswith("You can't assign roles on the resource group orange3-cloud.")
    assert "Owner, User Access Administrator or Role Based Access Control Administrator" in (
        roles.fix
    )
    assert "Contributor isn't enough" in roles.fix
    assert checks["permissions"].passed  # Contributor may create everything else

    quota = checks["family_quota"]
    assert quota.detail == (
        "8 of 32 vCPUs free; Standard_D32s_v6 needs 32. A request for 64 vCPUs is open."
    )
    assert "at least 56 vCPUs for “Standard Dsv6 Family vCPUs” in eastus" in quota.fix
    assert checks["regional_quota"].passed

    # Registering the missing providers (after the user agreed).
    azure_setup.register_providers(api, providers.missing, poll=0)
    assert api.registered == ["Microsoft.Network", "Microsoft.Storage"]
    assert statuses(api)["providers"].passed


def test_missing_permissions_and_compute():
    reader = [Permission(("*/read",))]
    api = FakeSubscription(providers={}, permissions=reader, usages={})
    checks = statuses(api)
    assert failed(checks) == {"providers", "permissions", "role_assignments"}
    permissions = checks["permissions"]
    assert "you can't manage the resource group, machines, networks, storage" in (
        permissions.detail
    )
    assert "Microsoft.Compute/virtualMachines/read" not in permissions.missing
    assert "Microsoft.Compute/virtualMachines/write" in permissions.missing
    # Without Microsoft.Compute, the quotas can't be read.
    assert checks["family_quota"].detail == "Not checked: Microsoft.Compute isn't registered."
    # A region without the size's family.
    checks = statuses(FakeSubscription(usages={"cores": Usage(0, 76)}))
    assert failed(checks) == {"family_quota"}
    assert "doesn't offer Standard_D16s_v6" in checks["family_quota"].detail


def test_not_signed_in_or_no_subscription():
    checks = statuses(FakeSubscription(signed_in=False))
    assert failed(checks) == {"credentials"}
    assert checks["credentials"].fix == "Click “Sign in to Azure”."
    assert {c.status for k, c in checks.items() if k != "credentials"} == {Status.UNKNOWN}
    # No credential at all (never signed in): no calls.
    checks = azure_setup.run_checks(None, None, "eastus", "Standard_D4s_v6")
    assert [c.status for c in checks] == [Status.FAILED] + [Status.UNKNOWN] * 6

    checks = statuses(FakeSubscription(), subscription=None)
    assert failed(checks) == {"subscription"}
    assert checks["subscription"].detail == "No subscription is selected."
    assert checks["providers"].detail == "Not checked: choose an enabled subscription first."
    checks = statuses(FakeSubscription(state="Disabled"))
    assert checks["subscription"].detail == "Subscription Research is Disabled."
    checks = statuses(FakeSubscription(state=None))
    assert "wasn't found, or you have no access to it" in checks["subscription"].detail
    checks = statuses(FakeSubscription(), region="")
    assert checks["family_quota"].detail == "No region is selected."


def test_permissions_and_families():
    """Azure's wildcards and exclusions; each size's family quota."""
    allowed = azure_setup.allowed
    write = "Microsoft.Compute/virtualMachines/write"
    assert allowed(write, OWNER) and allowed(write, CONTRIBUTOR)
    assert allowed(write, [Permission(("microsoft.compute/*",))])  # case doesn't matter
    assert not allowed("Microsoft.Authorization/roleAssignments/write", CONTRIBUTOR)
    # Another role that allows it is enough (e.g. User Access Administrator).
    access_admin = Permission(("*/read", "Microsoft.Authorization/*"))
    assert allowed("Microsoft.Authorization/roleAssignments/write", [*CONTRIBUTOR, access_admin])
    assert not allowed(write, [Permission(("*/read",))])
    assert not allowed(write, [])

    assert family_quota("Standard_D4s_v6") == ("StandardDsv6Family", "Standard Dsv6 Family vCPUs")
    assert family_quota("Standard_E96s_v6")[0] == "StandardEsv6Family"


@pytest.mark.skipif(
    not os.environ.get("ORANGE3_CLOUD_AZURE_SUBSCRIPTION"),
    reason="needs this computer's Azure CLI sign-in (ORANGE3_CLOUD_AZURE_SUBSCRIPTION)",
)
def test_real_subscription_is_ready():
    """The test subscription passes every check for the Small size
    (read-only calls: nothing is created)."""
    az = azure_account.find_az()
    subscription_id = os.environ["ORANGE3_CLOUD_AZURE_SUBSCRIPTION"]
    subscription = azure_account.pick_subscription(
        azure_account.list_subscriptions(az), subscription_id
    )
    assert subscription is not None and subscription.id == subscription_id
    credential = azure_account.credential(az, subscription)
    checks = azure_setup.run_checks(
        credential, subscription, os.environ["ORANGE3_CLOUD_AZURE_REGION"], "Standard_D4s_v6"
    )
    assert all_passed(checks), [c for c in checks if not c.passed]
