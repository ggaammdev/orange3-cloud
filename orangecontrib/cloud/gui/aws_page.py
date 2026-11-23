"""The "AWS" page of the Cloud settings dialog (``PAGE``, see
:mod:`~orangecontrib.cloud.gui.provider_page`): the AWS CLI, its profile,
signing in, the region, and the machine size and boot disk
(:class:`~orangecontrib.cloud.gui.machine_size.MachineSizePicker`): the sizes
the region offers, with their prices there.

Below the choices, the setup checker
(:mod:`~orangecontrib.cloud.client.providers.aws_setup`) shows what's missing
in the account before a machine can start: the region's default VPC, the
permissions and the vCPU quota for the chosen size.

Nothing here runs until the page is first shown. Slow calls (finding ``aws``,
reading the profile's credentials, listing the regions and, once per profile
and region, the sizes the region offers) run on a background thread, and
signing in (``aws login`` or ``aws sso login`` for the profile)
goes through the session (see :mod:`~orangecontrib.cloud.gui.account_page`).

The choices are written to the AWS part of the dialog's
:class:`~orangecontrib.cloud.settings.CloudSettings` (the profile is its
``account``) and saved at once.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtCore import pyqtSlot as Slot
from AnyQt.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QWidget

from orangecontrib.cloud.client.providers import aws_account, aws_setup
from orangecontrib.cloud.client.providers.aws_account import (
    AWS_CLI_INSTALL_URL,
    SIGN_IN_BUTTON,
    AwsProfile,
    Identity,
)
from orangecontrib.cloud.client.providers.base import ClusterProvider, ProviderError
from orangecontrib.cloud.client.providers.setup_checks import Check
from orangecontrib.cloud.client.session import CloudSession
from orangecontrib.cloud.gui.account_page import AccountPage
from orangecontrib.cloud.settings import CloudSettings

__all__ = ["PAGE", "AccountInfo", "AwsPage", "check_account"]


@dataclass
class AccountInfo:
    """What the background check of the account found."""

    aws: str | None = None
    profiles: list[AwsProfile] = field(default_factory=list)
    #: The chosen profile.
    profile: str = ""
    #: The profile's region in the AWS CLI's configuration ("": none).
    profile_region: str = ""
    signed_in: bool = False
    identity: Identity | None = None
    #: The profile's ``boto3`` session (``None`` if not signed in).
    session: Any = None
    #: The regions enabled for the account.
    regions: list[str] = field(default_factory=list)
    error: str = ""


def check_account(saved_profile: str) -> AccountInfo:
    """Find aws, list the profiles, read the chosen one's sign-in and list
    the account's regions (slow)."""
    info = AccountInfo(aws=aws_account.find_aws())
    try:
        info.profiles = aws_account.list_profiles()
    except ProviderError as exc:
        info.error = str(exc)
        return info
    info.profile = aws_account.pick_profile(info.profiles, saved_profile)
    try:
        session = aws_account.load_session(info.profile)
        info.profile_region = session.region_name or ""
        info.identity = aws_account.identity(session)
        info.session, info.signed_in = session, True
        info.regions = aws_account.list_regions(session)
    except ProviderError as exc:  # not signed in, or the regions couldn't be listed
        info.error = str(exc)
    return info


class AwsPage(AccountPage):
    """Choose the AWS CLI's profile, sign in, then choose the region.
    ``setupChanged``: the setup check finished (or was invalidated); see
    :attr:`setup_ok`."""

    ready_text = "The AWS account is ready: default VPC, permissions and vCPU quota are all set."
    sign_in_message = "Finish signing in in your browser. AWS's sign-in page has opened there."

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
        #: (profile, region) -> the sizes the region offers.
        self._region_types: dict[tuple[str, str], set[str]] = {}

        form = QFormLayout(self)
        form.setContentsMargins(0, 4, 0, 0)

        self.aws_label = QLabel(wordWrap=True, openExternalLinks=True)
        self.aws_label.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("AWS CLI:", self.aws_label)

        row = QHBoxLayout()
        self.profile_combo = QComboBox(minimumContentsLength=24)
        self.profile_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.profile_combo.setToolTip("The AWS CLI's profile (in ~/.aws/config) to sign in with")
        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip("Read the profiles and list the regions again")
        self.reload_button.clicked.connect(self.refresh)
        row.addWidget(self.profile_combo, 1)
        row.addWidget(self.reload_button)
        form.addRow("Profile:", row)

        row = QHBoxLayout()
        self.account_label = QLabel("…", wordWrap=True)
        self.account_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sign_in_button.setText(SIGN_IN_BUTTON)
        self.sign_in_button.setToolTip(
            "Runs “aws login” for the profile (“aws sso login” for IAM Identity Center): "
            "AWS's sign-in page opens in your browser. The add-on never sees your password."
        )
        row.addWidget(self.account_label, 1)
        row.addWidget(self.sign_in_button)
        form.addRow("Account:", row)

        self.region_combo = QComboBox(minimumContentsLength=16)
        form.addRow("Region:", self.region_combo)

        self.size_picker = picker = self.add_size_rows(form)
        picker.machineTypeChanged.connect(self._on_machine_type_changed)

        self.check_button.setToolTip(
            "Check the region's default VPC, your permissions and the vCPU quota"
        )
        self.add_setup_rows(form)

        self.profile_combo.activated.connect(self._on_profile_chosen)
        self.region_combo.activated.connect(self._on_region_chosen)
        self._show_account(None)

    # ------------------------------------------------------------------
    # Public

    @property
    def profile(self) -> str:
        """The chosen profile (``""`` if none)."""
        return self.profile_combo.currentText()

    @property
    def region(self) -> str:
        """The chosen region."""
        return self.region_combo.currentText()

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
        return "Starts when the setup check of the AWS account passes."

    @property
    def session(self) -> Any:
        """The profile's ``boto3`` session (``None`` if not signed in)."""
        return self._account.session if self._account else None

    @Slot()
    def refresh(self) -> None:
        """Check aws, read the profiles and the sign-in, and list the regions again."""
        self._started = True
        self._clear_setup()  # the shown check may be another profile's
        self._account_request += 1
        request, saved = self._account_request, self.provider_settings.account
        self._set_message("Checking your AWS sign-in…")
        self._update_enabled(loading=True)

        def done(info: AccountInfo | None, error: BaseException | None) -> None:
            if request == self._account_request:  # else another refresh follows
                self._on_account(info, error)

        self._submit(lambda: check_account(saved), done)

    def setup_key(self) -> tuple[str, ...]:
        """The checks depend on the profile, the region and the size (its vCPUs)."""
        return (self.profile, self.region, self.machine_type)

    def setup_job(self) -> Callable[[], list[Check]]:
        """The setup check of the chosen profile, region and machine type."""
        session, region, machine_type = self.session, self.region, self.machine_type
        return lambda: aws_setup.run_checks(
            session, region, machine_type, make_api=aws_setup.BotoSetupApi
        )

    # ------------------------------------------------------------------
    # Account, profiles and regions

    def _on_account(self, info: AccountInfo | None, error: BaseException | None) -> None:
        if error is not None or info is None:
            info = AccountInfo(error=f"Couldn't check the AWS account: {error}")
        self._account = info
        if info.profile and info.profile != self.provider_settings.account:
            self.provider_settings.account = info.profile
            self._save()
        self._fill_profiles(info)
        self._show_account(info)
        self._set_message(info.error, error=bool(info.error))
        self._fill_regions(info)

    def _show_account(self, info: AccountInfo | None) -> None:
        if info is None:
            self.aws_label.setText("…")
            self.account_label.setText("…")
        else:
            if info.aws:
                self.aws_label.setText(html.escape(info.aws))
            else:
                self.aws_label.setText(
                    "Not found. Install the AWS CLI "
                    f'(<a href="{AWS_CLI_INSTALL_URL}">instructions</a>), then click Reload. '
                    "If it is installed, start Orange from a terminal where “aws” works."
                )
            if info.signed_in and info.identity is not None:
                self.account_label.setText(info.identity.label)
                self.account_label.setToolTip(f"The AWS CLI's sign-in of profile {info.profile}")
                self.sign_in_button.setText("Sign in again")
            else:
                self.account_label.setText("Not signed in")
                self.account_label.setToolTip("")
                self.sign_in_button.setText(SIGN_IN_BUTTON)
        self._update_enabled()

    def _fill_profiles(self, info: AccountInfo) -> None:
        names = [p.name for p in info.profiles]
        if info.profile and info.profile not in names:
            names.insert(0, info.profile)  # signing in creates it
        self.profile_combo.clear()
        self.profile_combo.addItems(names)
        self.profile_combo.setCurrentText(info.profile)

    @Slot(int)
    def _on_profile_chosen(self, _index: int) -> None:
        profile = self.profile
        if profile and profile != self.provider_settings.account:
            self.provider_settings.account = profile
            self._save()
            self.refresh()

    def _fill_regions(self, info: AccountInfo) -> None:
        self.region_combo.clear()
        self.region_combo.addItems(info.regions)
        chosen = aws_account.pick_region(
            info.regions, self.provider_settings.region, info.profile_region
        )
        if chosen:
            self.region_combo.setCurrentText(chosen)
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
        """Show the sizes the chosen region offers (listed once per profile
        and region)."""
        picker, profile, region, session = self.size_picker, self.profile, self.region, self.session
        key = (profile, region)
        if picker is None:
            return
        if key in self._region_types or session is None or not region:
            picker.set_region(region, self._region_types.get(key))
            return
        picker.set_region(region, None)

        def done(names: set[str] | None, error: BaseException | None) -> None:
            if (self.profile, self.region) != key:
                return  # another profile or region was chosen meanwhile
            if error is not None:
                self._set_message(f"All sizes are shown. {error}", error=True)
                return
            self._region_types[key] = names or set()
            picker.set_region(region, self._region_types[key])

        self._submit(lambda: aws_account.offered_instance_types(session, region), done)

    @Slot(str)
    def _on_machine_type_changed(self, _name: str) -> None:
        # The quota check depends on the size's vCPUs.
        self._schedule_check_if_changed()

    # ------------------------------------------------------------------

    @property
    def can_sign_in(self) -> bool:
        """aws was found and a profile is chosen."""
        return bool(self._account and self._account.aws and self.profile)

    def _update_enabled(self, loading: bool = False) -> None:
        loading = loading or bool(self._callbacks)
        signing_in = self._signing_in
        idle = not loading and not signing_in
        signed_in = self._account is not None and self._account.signed_in
        self.sign_in_button.setEnabled(signing_in or (not loading and self.can_sign_in))
        self.reload_button.setEnabled(idle)
        self.profile_combo.setEnabled(idle and self.profile_combo.count() > 0)
        self.region_combo.setEnabled(idle and signed_in and self.region_combo.count() > 0)
        if self.size_picker is not None:
            combo = self.size_picker.combo
            combo.setEnabled(not signing_in and combo.count() > 0)
        self.check_button.setEnabled(idle)


#: The page of the ``aws`` provider.
PAGE = AwsPage
