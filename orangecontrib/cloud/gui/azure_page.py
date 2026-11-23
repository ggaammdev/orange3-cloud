"""The "Azure" page of the Cloud settings dialog (``PAGE``, see
:mod:`~orangecontrib.cloud.gui.provider_page`): the Azure CLI, signing in,
the subscription, the region, and the machine size and boot disk
(:class:`~orangecontrib.cloud.gui.machine_size.MachineSizePicker`): the sizes
the region offers to the subscription, with their prices there.

Below the choices, the setup checker
(:mod:`~orangecontrib.cloud.client.providers.azure_setup`) shows what's
missing in the subscription before a machine can start: the resource
providers (with a "Register…" button that registers them after the user
agrees), the permissions, and the vCPU quotas for the chosen size.

Nothing here runs until the page is first shown. Slow calls (finding ``az``,
listing the subscriptions, reading the sign-in and listing the regions) run
on a background thread, and signing in (``az login``) goes through the
session (see :mod:`~orangecontrib.cloud.gui.account_page`). The sizes a
region offers are listed once per subscription and region (and kept for a
while, see :func:`~orangecontrib.cloud.client.providers.azure_account.
offered_sizes`); Reload lists them again.

The choices are written to the Azure part of the dialog's
:class:`~orangecontrib.cloud.settings.CloudSettings` (the subscription's ID
is its ``account``) and saved at once.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtCore import pyqtSlot as Slot
from AnyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from orangecontrib.cloud.client.providers import azure_account, azure_setup
from orangecontrib.cloud.client.providers.azure_account import (
    AZURE_CLI_INSTALL_URL,
    SIGN_IN_BUTTON,
    AzureRegion,
    AzureSubscription,
)
from orangecontrib.cloud.client.providers.azure_vm import RESOURCE_PROVIDERS
from orangecontrib.cloud.client.providers.base import ClusterProvider, ProviderError
from orangecontrib.cloud.client.providers.setup_checks import Check
from orangecontrib.cloud.client.session import CloudSession
from orangecontrib.cloud.gui.account_page import AccountPage
from orangecontrib.cloud.settings import CloudSettings

__all__ = ["PAGE", "AccountInfo", "AzurePage", "check_account"]


@dataclass
class AccountInfo:
    """What the background check of the account found."""

    az: str | None = None
    #: The subscriptions of the Azure CLI's sign-in.
    subscriptions: list[AzureSubscription] = field(default_factory=list)
    #: The chosen subscription.
    subscription: AzureSubscription | None = None
    signed_in: bool = False
    #: The Azure CLI's credentials for the chosen subscription (``None`` if
    #: not signed in).
    credential: Any = None
    #: The subscription's regions.
    regions: list[AzureRegion] = field(default_factory=list)
    #: The Azure CLI's default location ("": none).
    default_region: str = ""
    error: str = ""


def check_account(saved_subscription: str) -> AccountInfo:
    """Find az, list its subscriptions, read the chosen one's sign-in and
    list its regions (slow)."""
    info = AccountInfo(az=azure_account.find_az(), default_region=azure_account.default_location())
    if not info.az:
        return info
    try:
        info.subscriptions = azure_account.list_subscriptions(info.az)
    except ProviderError as exc:
        info.error = str(exc)
        return info
    if not info.subscriptions:
        info.error = f"Not signed in to Azure. Click “{SIGN_IN_BUTTON}”."
        return info
    info.subscription = azure_account.pick_subscription(info.subscriptions, saved_subscription)
    try:
        credential = azure_account.credential(info.az, info.subscription)
        azure_account.check_sign_in(credential)
        info.credential, info.signed_in = credential, True
        if info.subscription is not None:
            info.regions = azure_account.list_regions(credential, info.subscription.id)
    except ProviderError as exc:  # not signed in, or the regions couldn't be listed
        info.error = str(exc)
    return info


class AzurePage(AccountPage):
    """Sign in with the Azure CLI, then choose the subscription and region.
    ``setupChanged``: the setup check finished (or was invalidated); see
    :attr:`setup_ok`."""

    ready_text = (
        "The Azure subscription is ready: resource providers, permissions and vCPU quotas "
        "are all set."
    )
    sign_in_message = (
        "Finish signing in in your browser. Microsoft's sign-in page has opened there."
    )

    def __init__(
        self,
        provider: ClusterProvider,
        settings: CloudSettings,
        save: Callable[[], None],
        session: CloudSession,
        parent: QWidget | None = None,
    ):
        super().__init__(provider, settings, save, session, parent)
        self._account: AccountInfo | None = None
        self._account_request = 0  # the latest refresh; earlier results are stale
        #: (subscription, region) -> the sizes the region offers to it.
        self._region_sizes: dict[tuple[str, str], set[str]] = {}
        self._fresh_sizes = False  # Reload: list the sizes again

        form = QFormLayout(self)
        form.setContentsMargins(0, 4, 0, 0)

        self.az_label = QLabel(wordWrap=True, openExternalLinks=True)
        self.az_label.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("Azure CLI:", self.az_label)

        row = QHBoxLayout()
        self.account_label = QLabel("…", wordWrap=True)
        self.account_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sign_in_button.setText(SIGN_IN_BUTTON)
        self.sign_in_button.setToolTip(
            "Runs “az login”: Microsoft's sign-in page opens in your browser. "
            "The add-on never sees your password."
        )
        row.addWidget(self.account_label, 1)
        row.addWidget(self.sign_in_button)
        form.addRow("Account:", row)

        row = QHBoxLayout()
        self.subscription_combo = QComboBox(minimumContentsLength=24)
        self.subscription_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip(
            "Read the sign-in, and list the subscriptions and the sizes of the region again"
        )
        self.reload_button.clicked.connect(self._on_reload_clicked)
        row.addWidget(self.subscription_combo, 1)
        row.addWidget(self.reload_button)
        form.addRow("Subscription:", row)

        self.region_combo = QComboBox(minimumContentsLength=16)
        form.addRow("Region:", self.region_combo)

        self.size_picker = picker = self.add_size_rows(form)
        picker.machineTypeChanged.connect(self._on_machine_type_changed)

        self.check_button.setToolTip(
            "Check the resource providers, your permissions and the vCPU quotas"
        )
        self.register_button = QPushButton("Register…")
        self.register_button.setToolTip("Register the missing resource providers")
        self.register_button.clicked.connect(self._on_register_clicked)
        self.register_button.setVisible(False)
        self.add_setup_rows(form, self.register_button)

        self.subscription_combo.activated.connect(self._on_subscription_chosen)
        self.region_combo.activated.connect(self._on_region_chosen)
        self._show_account(None)

    # ------------------------------------------------------------------
    # Public

    @property
    def subscription_id(self) -> str:
        """The chosen subscription's ID (``""`` if none)."""
        return self.subscription_combo.currentData() or ""

    @property
    def subscription(self) -> AzureSubscription | None:
        """The chosen subscription."""
        account = self._account
        if account is None:
            return None
        return next((s for s in account.subscriptions if s.id == self.subscription_id), None)

    @property
    def region(self) -> str:
        """The chosen region's name, e.g. ``eastus``."""
        return self.region_combo.currentData() or ""

    @property
    def machine_type(self) -> str:
        """The chosen machine type."""
        return self.provider.size_or_default(self.provider_settings.machine_type)

    @property
    def ready(self) -> bool:
        """A machine can start when the setup check passed."""
        return self.setup_ok

    @property
    def not_ready_text(self) -> str:
        """Why Start waits."""
        return "Starts when the setup check of the Azure subscription passes."

    @property
    def credential(self) -> Any:
        """The Azure CLI's credentials (``None`` if not signed in)."""
        return self._account.credential if self._account else None

    @Slot()
    def refresh(self) -> None:
        """Check az and the sign-in, and list the subscriptions and regions again."""
        self._started = True
        self._clear_setup()  # the shown check may be another subscription's
        self._account_request += 1
        request, saved = self._account_request, self.provider_settings.account
        self._set_message("Checking your Azure sign-in…")
        self._update_enabled(loading=True)

        def done(info: AccountInfo | None, error: BaseException | None) -> None:
            if request == self._account_request:  # else another refresh follows
                self._on_account(info, error)

        self._submit(lambda: check_account(saved), done)

    @Slot()
    def _on_reload_clicked(self) -> None:
        self._region_sizes.clear()
        self._fresh_sizes = True
        self.refresh()

    def setup_key(self) -> tuple[str, ...]:
        """The checks depend on the subscription, the region and the size."""
        return (self.subscription_id, self.region, self.machine_type)

    def setup_job(self) -> Callable[[], list[Check]]:
        """The setup check of the chosen subscription, region and machine type."""
        credential, subscription = self.credential, self.subscription
        region, machine_type = self.region, self.machine_type
        return lambda: azure_setup.run_checks(
            credential, subscription, region, machine_type, make_api=azure_setup.AzureSetupApi
        )

    def _show_checks(self, checks: list[Check]) -> None:
        super()._show_checks(checks)
        self.register_button.setVisible(bool(azure_setup.missing_providers(checks)))

    @Slot()
    def _on_register_clicked(self) -> None:
        missing = azure_setup.missing_providers(self.setup_checks or [])
        subscription, credential = self.subscription, self.credential
        if not missing or subscription is None or credential is None:
            return
        names = "\n".join(f"• {n} ({RESOURCE_PROVIDERS.get(n, '')})" for n in missing)
        answer = QMessageBox.question(
            self,
            "Register resource providers",
            f"Register these resource providers in subscription {subscription.label}?\n\n"
            f"{names}\n\nRegistering costs nothing by itself. It may take a few minutes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_message("Registering the resource providers…")

        def done(_: Any, error: BaseException | None) -> None:
            self._set_message(str(error) if error is not None else "", error=error is not None)
            self.check_setup()

        api = azure_setup.AzureSetupApi(credential, subscription.id)
        self._submit(lambda: azure_setup.register_providers(api, missing), done)
        self._update_enabled()

    # ------------------------------------------------------------------
    # Account, subscriptions and regions

    def _on_account(self, info: AccountInfo | None, error: BaseException | None) -> None:
        if error is not None or info is None:
            info = AccountInfo(error=f"Couldn't check the Azure account: {error}")
        self._account = info
        chosen = info.subscription.id if info.subscription else ""
        if chosen and chosen != self.provider_settings.account:
            self.provider_settings.account = chosen
            self._save()
        self._fill_subscriptions(info)
        self._show_account(info)
        self._set_message(info.error, error=bool(info.error))
        self._fill_regions(info)

    def _show_account(self, info: AccountInfo | None) -> None:
        if info is None:
            self.az_label.setText("…")
            self.account_label.setText("…")
        else:
            if info.az:
                self.az_label.setText(html.escape(info.az))
            else:
                self.az_label.setText(
                    "Not found. Install the Azure CLI "
                    f'(<a href="{AZURE_CLI_INSTALL_URL}">instructions</a>), then click Reload. '
                    "If it is installed, start Orange from a terminal where “az” works."
                )
            if info.signed_in:
                user = info.subscription.user if info.subscription else ""
                self.account_label.setText(user or "Signed in")
                self.account_label.setToolTip("The Azure CLI's sign-in")
                self.sign_in_button.setText("Sign in again")
            else:
                self.account_label.setText("Not signed in")
                self.account_label.setToolTip("")
                self.sign_in_button.setText(SIGN_IN_BUTTON)
        self._update_enabled()

    def _fill_subscriptions(self, info: AccountInfo) -> None:
        combo = self.subscription_combo
        combo.clear()
        for subscription in info.subscriptions:
            label = subscription.label
            if subscription.state and subscription.state != "Enabled":
                label += f" ({subscription.state.lower()})"
            combo.addItem(label, subscription.id)
            combo.setItemData(combo.count() - 1, subscription.id, Qt.ItemDataRole.ToolTipRole)
        if info.subscription is not None:
            combo.setCurrentIndex(combo.findData(info.subscription.id))

    @Slot(int)
    def _on_subscription_chosen(self, _index: int) -> None:
        chosen = self.subscription_id
        if chosen and chosen != self.provider_settings.account:
            self.provider_settings.account = chosen
            self._save()
            self.refresh()  # another tenant, and its regions

    def _fill_regions(self, info: AccountInfo) -> None:
        self.region_combo.clear()
        for region in info.regions:
            self.region_combo.addItem(region.label, region.name)
        chosen = azure_account.pick_region(
            [r.name for r in info.regions], self.provider_settings.region, info.default_region
        )
        if chosen:
            self.region_combo.setCurrentIndex(self.region_combo.findData(chosen))
        self._region_changed()

    @Slot(int)
    def _on_region_chosen(self, _index: int) -> None:
        self._region_changed()

    def _region_changed(self) -> None:
        region = self.region
        if region and region != self.provider_settings.region:
            self.provider_settings.region = region
            self._save()
        self._list_machine_types()
        self._schedule_check_if_changed()
        self._update_enabled()

    def _list_machine_types(self) -> None:
        """Show the sizes the chosen region offers to the subscription
        (listed once per subscription and region; all while unknown)."""
        picker, region, credential = self.size_picker, self.region, self.credential
        key = (self.subscription_id, region)
        if picker is None:
            return
        if key in self._region_sizes or credential is None or not region:
            picker.set_region(region, self._region_sizes.get(key))
            return
        picker.set_region(region, None)
        fresh, self._fresh_sizes = self._fresh_sizes, False

        def done(names: set[str] | None, error: BaseException | None) -> None:
            if (self.subscription_id, self.region) != key:
                return  # another subscription or region was chosen meanwhile
            if error is not None:
                self._set_message(f"All sizes are shown. {error}", error=True)
                return
            self._region_sizes[key] = names or set()
            picker.set_region(region, self._region_sizes[key])

        subscription_id = key[0]
        self._submit(
            lambda: azure_account.offered_sizes(credential, subscription_id, region, fresh=fresh),
            done,
        )

    @Slot(str)
    def _on_machine_type_changed(self, _name: str) -> None:
        # The quota checks depend on the size's family and vCPUs.
        self._schedule_check_if_changed()

    # ------------------------------------------------------------------

    @property
    def can_sign_in(self) -> bool:
        """az was found."""
        return bool(self._account and self._account.az)

    def _update_enabled(self, loading: bool = False) -> None:
        loading = loading or bool(self._callbacks)
        signing_in = self._signing_in
        idle = not loading and not signing_in
        signed_in = self._account is not None and self._account.signed_in
        self.sign_in_button.setEnabled(signing_in or (not loading and self.can_sign_in))
        self.reload_button.setEnabled(idle)
        self.subscription_combo.setEnabled(idle and self.subscription_combo.count() > 0)
        self.region_combo.setEnabled(idle and signed_in and self.region_combo.count() > 0)
        if self.size_picker is not None:
            combo = self.size_picker.combo
            combo.setEnabled(not signing_in and combo.count() > 0)
        self.check_button.setEnabled(idle)
        self.register_button.setEnabled(idle)


#: The page of the ``azure`` provider.
PAGE = AzurePage
