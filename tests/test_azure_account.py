"""Azure sign-in, subscriptions and regions: the Azure CLI's list and
configuration, the sign-in command, the Azure libraries' errors, and the
dialog's Azure page (with the account functions and the setup checker's
Azure replaced, and a fake ``az``).

``test_real_subscription`` uses this computer's own Azure CLI sign-in,
read-only; it runs only when ``ORANGE3_CLOUD_AZURE_SUBSCRIPTION`` is set (the
development env file), so CI needs no Azure sign-in."""

import json
import os
import stat
import sys
import threading
from types import SimpleNamespace

import pytest
from AnyQt.QtCore import QSettings, Qt
from AnyQt.QtTest import QTest
from AnyQt.QtWidgets import QApplication, QMessageBox
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ServiceRequestError
from azure.identity import CredentialUnavailableError
from test_azure_setup import FakeSubscription
from test_gcp_account import spin

from orangecontrib.cloud.client.providers import azure_account, azure_prices, azure_setup
from orangecontrib.cloud.client.providers.azure_account import (
    AzureRegion,
    AzureSubscription,
    CachedCredential,
    NotSignedInError,
)
from orangecontrib.cloud.client.providers.azure_vm import MACHINE_TYPES
from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.settings import CloudSettings

SUBSCRIPTIONS = [
    {"id": "sub-2", "name": "Teaching", "tenantId": "t2", "state": "Enabled", "isDefault": True,
     "user": {"name": "ana@example.com", "type": "user"}},
    {"id": "sub-1", "name": "Research", "tenantId": "t1", "state": "Enabled", "isDefault": False,
     "user": {"name": "ana@example.com", "type": "user"}},
]  # fmt: skip


def fake_tool(path, body):
    """A shell script standing in for az."""
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake az is a shell script")
def test_subscriptions_regions_and_sign_in(tmp_path, monkeypatch):
    """The subscriptions come from ``az account list`` (run without Python's
    warnings); the default is the saved one, else the CLI's; the default
    region is the CLI's default location, else eastus. ``az login`` runs
    without a console and without the question after the sign-in."""
    listing = tmp_path / "subscriptions.json"
    listing.write_text(json.dumps(SUBSCRIPTIONS))
    seen = tmp_path / "seen"
    az = fake_tool(
        tmp_path / "az",
        f'echo "$@ $PYTHONWARNINGS $AZURE_CORE_LOGIN_EXPERIENCE_V2" > "{seen}"\n'
        f'[ "$1" = account ] && cat "{listing}"\n'
        '[ "$1" = login ] && read answer && echo "asked"\n'
        "exit 0\n",
    )
    subscriptions = azure_account.list_subscriptions(az)
    assert [s.name for s in subscriptions] == ["Research", "Teaching"]
    assert subscriptions[1] == AzureSubscription(
        "sub-2", "Teaching", "t2", "Enabled", True, "ana@example.com"
    )
    assert seen.read_text().split() == [
        "account", "list", "--output", "json", "--only-show-errors", "ignore", "off"
    ]  # fmt: skip
    pick = azure_account.pick_subscription
    assert pick(subscriptions).id == "sub-2"  # the CLI's default
    assert pick(subscriptions, "gone", "sub-1").id == "sub-1"
    assert pick([]) is None
    listing.write_text("[]")
    assert azure_account.list_subscriptions(az) == []  # nobody signed in

    assert azure_account.sign_in(az).strip() == ""  # no question was answered
    assert seen.read_text().split() == ["login", "ignore", "off"]
    fake_tool(tmp_path / "az", "echo 'AADSTS50020: User account does not exist.'; exit 1\n")
    with pytest.raises(ProviderError, match=r"\(az exit code 1\)\.\nAADSTS50020"):
        azure_account.sign_in(az)
    fake_tool(
        tmp_path / "az", "printf 'x.py:1: SyntaxWarning: bad\\n  re(1)\\nERROR: no' >&2\nexit 2\n"
    )
    with pytest.raises(ProviderError, match=r"^Couldn't list the Azure CLI's subscriptions: ERROR"):
        azure_account.list_subscriptions(az)

    config = tmp_path / "azure"
    config.mkdir()
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(config))
    assert azure_account.default_location() == ""
    (config / "config").write_text("[defaults]\nlocation = westeurope\n")
    assert azure_account.default_location() == "westeurope"
    assert azure_account.pick_region(["eastus", "westeurope"]) == "eastus"
    assert azure_account.pick_region(["eastus", "westeurope"], "", "westeurope") == "westeurope"
    assert azure_account.pick_region(["westus"], "mars") == "westus"
    assert AzureRegion("eastus", "East US").label == "East US (eastus)"


def test_errors_and_tokens():
    """The Azure libraries' errors as messages that say what to do; tokens
    are asked for once until they are about to expire."""
    error = azure_account.azure_error(
        CredentialUnavailableError("Please run 'az login' to set up an account"), "list"
    )
    assert isinstance(error, NotSignedInError) and "Sign in to Azure" in str(error)
    error = azure_account.azure_error(
        CredentialUnavailableError("Azure CLI not found on path"), "check your Azure sign-in"
    )
    assert "Couldn't find the Azure CLI" in str(error)
    error = azure_account.azure_error(
        ClientAuthenticationError("AADSTS70043: The refresh token has expired."), "list"
    )
    assert isinstance(error, NotSignedInError) and "has expired or doesn't work" in str(error)
    denied = HttpResponseError("The client does not have authorization.")
    denied.status_code = 403
    error = azure_account.azure_error(denied, "read the vCPU quotas of eastus")
    assert str(error).startswith("Couldn't read the vCPU quotas of eastus: no permission.")
    error = azure_account.azure_error(ServiceRequestError("no route"), "list the regions")
    assert str(error) == "Couldn't reach Azure to list the regions. Check the connection."
    with pytest.raises(NotSignedInError):
        azure_account.azure_call("list", lambda: (_ for _ in ()).throw(ClientAuthenticationError()))

    calls = []

    class Credential:
        def get_token(self, *scopes, **kwargs):
            calls.append(scopes)
            return SimpleNamespace(token="t", expires_on=expires)

    import time

    expires = time.time() + 3600
    cached = CachedCredential(Credential())
    assert cached.get_token("scope").token == "t"
    cached.get_token("scope")
    assert calls == [("scope",)]
    expires = time.time() + 60  # about to expire: ask again, each time
    cached._tokens.clear()
    cached.get_token("scope")
    cached.get_token("scope")
    assert len(calls) == 3


@pytest.fixture
def fake_az(tmp_path, monkeypatch):
    """A fake az script and fake account functions: two subscriptions, not
    signed in until the script has run, then the regions; the setup checker
    sees a subscription whose Microsoft.Storage isn't registered."""
    marker = tmp_path / "signed-in"
    script = fake_tool(tmp_path / "az", f'echo "$@" > "{marker}"\n')
    subscriptions = [
        AzureSubscription("sub-1", "Research", "t1", user="ana@example.com"),
        AzureSubscription("sub-2", "Teaching", "t2", is_default=True, user="ana@example.com"),
    ]

    def check_sign_in(credential):
        if not marker.exists():
            raise NotSignedInError("The Azure sign-in has expired or doesn't work.")

    monkeypatch.setattr(azure_account, "find_az", lambda: script)
    monkeypatch.setattr(azure_account, "default_location", lambda: "")
    monkeypatch.setattr(azure_account, "list_subscriptions", lambda az: subscriptions)
    monkeypatch.setattr(
        azure_account, "credential", lambda az, s: SimpleNamespace(tenant=s.tenant_id)
    )
    monkeypatch.setattr(azure_account, "check_sign_in", check_sign_in)
    regions = [AzureRegion("eastus", "East US"), AzureRegion("westeurope", "West Europe")]
    monkeypatch.setattr(azure_account, "list_regions", lambda credential, sub: regions)
    # westeurope doesn't offer the largest size to the subscriptions.
    listed = []

    def offered_sizes(credential, sub, region, *, fresh=False):
        listed.append((sub, region, fresh))
        hidden = {"Standard_E96s_v6"} if region == "westeurope" else set()
        return {t.name for t in MACHINE_TYPES} - hidden

    monkeypatch.setattr(azure_account, "offered_sizes", offered_sizes)
    api = FakeSubscription(
        providers={"Microsoft.Compute": "Registered", "Microsoft.Network": "Registered"}
    )
    # The checker's calls go to a fake subscription (tested in test_azure_setup).
    monkeypatch.setattr(azure_setup, "AzureSetupApi", lambda credential, sub: api)
    return SimpleNamespace(marker=marker, script=script, api=api, listed=listed)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake az is a shell script")
def test_dialog_lists_subscriptions_and_regions(fake_az, fake, tmp_path, monkeypatch):
    from orangecontrib.cloud.client.session import CloudSession, HandleStore
    from orangecontrib.cloud.gui.settings_dialog import CloudSettingsDialog

    app = QApplication.instance() or QApplication([])
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))
    dialog = CloudSettingsDialog(session=session)
    dialog.show()
    page = dialog.pages["azure"]
    assert dialog.provider_buttons["azure"].text() == "Azure"
    assert not page._started  # nothing runs while the Local page shows

    dialog.provider_buttons["azure"].setChecked(True)
    spin(app, lambda: page._started and not page.busy and page.setup_checks is not None)
    assert page.az_label.text() == fake_az.script
    assert [page.subscription_combo.itemText(i) for i in range(2)] == ["Research", "Teaching"]
    assert page.subscription_id == "sub-2"  # the CLI's default
    assert page.account_label.text() == "Not signed in"
    assert "has expired" in page.message_label.text()
    assert page.region_combo.count() == 0 and not page.region_combo.isEnabled()
    assert page.setup_checks[0].key == "credentials" and not page.setup_ok
    assert not dialog.start_button.isEnabled()

    # Signing in runs az login, then lists the regions and checks the setup.
    QTest.mouseClick(page.sign_in_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: page.region_combo.count() > 0 and not page.busy and page.setup_checks)
    assert fake_az.marker.read_text().split() == ["login"]
    assert page.account_label.text() == "ana@example.com"
    assert page.sign_in_button.text() == "Sign in again"
    assert (page.region, page.region_combo.currentText()) == ("eastus", "East US (eastus)")
    assert page.size_picker.machine_type == "Standard_D16s_v6"  # Medium
    # The sizes the region offers to the subscription, with their prices there.
    picker = page.size_picker
    items = [picker.combo.itemText(i) for i in range(picker.combo.count())]
    assert len(items) == 9 and all("/h" in t and "$" in t for t in items), items
    assert items[0].startswith("Small (testing) (Standard_D4s_v6): 4 CPUs, 16 GB, $0.2")
    assert f"in eastus on {azure_prices.load_prices().date}" in picker.note_label.text()
    assert fake_az.listed == [("sub-2", "eastus", False)]
    assert page._region_sizes[("sub-2", "eastus")] == {t.name for t in MACHINE_TYPES}
    providers = next(c for c in page.setup_checks if c.key == "providers")
    assert providers.missing == ("Microsoft.Storage",)
    assert page.register_button.isVisible()

    # Registering asks first; "No" changes nothing.
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answers.pop(0))
    QTest.mouseClick(page.register_button, Qt.MouseButton.LeftButton)
    assert fake_az.api.registered == []
    QTest.mouseClick(page.register_button, Qt.MouseButton.LeftButton)
    spin(app, lambda: not page.busy and page.setup_ok)
    assert fake_az.api.registered == ["Microsoft.Storage"]
    assert "The Azure subscription is ready" in page.setup_label.text()
    assert not page.register_button.isVisible()
    # The check passed, so Start is enabled.
    assert dialog.start_button.isEnabled()
    # The quota checks are for the chosen size: 64 vCPUs don't fit the
    # family's quota of 32.
    picker.combo.setCurrentIndex(picker.combo.findData("Standard_E64s_v6"))
    picker.combo.activated.emit(picker.combo.currentIndex())
    spin(app, lambda: page.setup_checks is not None and not page.busy)
    quota = next(c for c in page.setup_checks if c.key == "family_quota")
    assert quota.detail == "32 of 32 vCPUs free; Standard_E64s_v6 needs 64."
    assert not page.setup_ok and not dialog.start_button.isEnabled()
    picker.combo.setCurrentIndex(picker.combo.findData("Standard_D16s_v6"))
    picker.combo.activated.emit(picker.combo.currentIndex())
    spin(app, lambda: page.setup_checks is not None and not page.busy)
    assert page.setup_ok and dialog.start_button.isEnabled()

    # Choices are saved at once in the settings store, never in a workflow.
    page.region_combo.setCurrentIndex(page.region_combo.findData("westeurope"))
    page.region_combo.activated.emit(page.region_combo.currentIndex())
    spin(app, lambda: not page.busy and ("sub-2", "westeurope") in page._region_sizes)
    # A size the region doesn't offer to the subscription is hidden.
    assert picker.combo.count() == 8 and picker.combo.findData("Standard_E96s_v6") == -1
    assert "Sizes not offered here are hidden (1)." in picker.note_label.text()
    # Reload lists them again, past the cache.
    page.reload_button.click()
    spin(app, lambda: not page.busy and ("sub-2", "westeurope") in page._region_sizes)
    assert fake_az.listed[-1] == ("sub-2", "westeurope", True)
    spin(app, lambda: page.setup_ok and not page.busy)
    page.subscription_combo.setCurrentIndex(0)
    page.subscription_combo.activated.emit(0)
    # The check was the other subscription's: it goes at once, and Start waits.
    assert page.setup_checks is None and page.setup_label.text() == ""
    assert not dialog.start_button.isEnabled()
    spin(app, lambda: not page.busy and page.subscription_id == "sub-1" and page.setup_checks)
    store = QSettings(os.environ["ORANGE3_CLOUD_SETTINGS_FILE"], QSettings.Format.IniFormat)
    assert store.value("orange3-cloud/provider") == "azure"
    assert store.value("orange3-cloud/providers/azure/account") == "sub-1"
    assert store.value("orange3-cloud/providers/azure/region") == "westeurope"
    assert CloudSettings.load().of("azure").region == "westeurope"
    assert page.region == "westeurope"
    dialog.close()
    page.shutdown()
    session.shutdown()


def test_offered_sizes(monkeypatch):
    """The sizes a region offers come from Compute's resource SKUs: a size
    that isn't there, or whose region is restricted for the subscription, is
    left out (a restricted zone isn't: machines aren't in a zone). The list
    is kept per subscription and region, unless asked for afresh."""
    restricted = {
        "type": "Location",
        "reasonCode": "NotAvailableForSubscription",
        "values": ["eastus"],
        "restrictionInfo": {"locations": ["eastus"]},
    }
    zone = {
        "type": "Zone",
        "reasonCode": "NotAvailableForSubscription",
        "values": ["eastus"],
        "restrictionInfo": {"locations": ["eastus"], "zones": ["1"]},
    }
    skus = [
        {"resourceType": "virtualMachines", "name": "Standard_D4s_v6", "locations": ["EastUS"]},
        {"resourceType": "virtualMachines", "name": "Standard_D16s_v6", "locations": ["eastus"],
         "restrictions": [zone]},
        {"resourceType": "virtualMachines", "name": "Standard_E16s_v6", "locations": ["eastus"],
         "restrictions": [restricted]},
        {"resourceType": "virtualMachines", "name": "Standard_D32s_v6", "locations": ["westus"]},
        {"resourceType": "disks", "name": "Standard_E32s_v6", "locations": ["eastus"]},
        {"resourceType": "virtualMachines", "name": "Standard_B1s", "locations": ["eastus"]},
    ]  # fmt: skip
    assert azure_account.sizes_offered(skus, "eastus") == {"Standard_D4s_v6", "Standard_D16s_v6"}

    calls = []
    monkeypatch.setattr(azure_account, "_sizes", {})
    monkeypatch.setattr(
        azure_account, "_region_skus", lambda cred, sub, region: calls.append(region) or skus
    )
    assert azure_account.offered_sizes(None, "sub", "eastus") == {
        "Standard_D4s_v6", "Standard_D16s_v6",
    }  # fmt: skip
    azure_account.offered_sizes(None, "sub", "eastus")
    assert calls == ["eastus"]
    azure_account.offered_sizes(None, "sub", "eastus", fresh=True)
    assert calls == ["eastus", "eastus"]


def test_page_deleted_during_a_background_call(fake, tmp_path):
    """A background call that ends after its page was deleted (a slow cloud
    while Orange quits) leaves the deleted page alone."""
    from AnyQt.sip import delete

    from orangecontrib.cloud.client.providers.azure import AzureProvider
    from orangecontrib.cloud.client.session import CloudSession, HandleStore
    from orangecontrib.cloud.gui.azure_page import AzurePage

    app = QApplication.instance() or QApplication([])
    session = CloudSession(fake, store=HandleStore(tmp_path / "machine.json"))
    settings = CloudSettings.load()
    page = AzurePage(AzureProvider(), settings, lambda: None, session)
    release, results, futures = threading.Event(), [], []
    executor = page._executor
    submit = executor.submit
    executor.submit = lambda fn: futures.append(submit(fn))
    page._submit(lambda: release.wait(5), lambda result, error: results.append(result))
    delete(page)
    release.set()
    executor.shutdown(wait=True)
    for _ in range(5):
        app.processEvents()  # the result reaches the GUI thread, and no page
    # The thread handed its result over without touching the deleted page.
    assert futures[0].exception() is None
    assert results == []
    session.shutdown()


@pytest.mark.skipif(
    not os.environ.get("ORANGE3_CLOUD_AZURE_SUBSCRIPTION"),
    reason="needs this computer's Azure CLI sign-in (ORANGE3_CLOUD_AZURE_SUBSCRIPTION)",
)
def test_real_subscription():
    """Read-only: the Azure CLI's sign-in has the test subscription, whose
    regions include the test region."""
    subscription_id = os.environ["ORANGE3_CLOUD_AZURE_SUBSCRIPTION"]
    from orangecontrib.cloud.gui.azure_page import check_account

    info = check_account(subscription_id)
    assert info.signed_in, info.error
    assert info.subscription is not None and info.subscription.id == subscription_id
    region = os.environ["ORANGE3_CLOUD_AZURE_REGION"]
    assert region in {r.name for r in info.regions}
    # The test subscription may use every size in the test region.
    offered = azure_account.offered_sizes(info.credential, subscription_id, region)
    assert offered == {t.name for t in MACHINE_TYPES}
