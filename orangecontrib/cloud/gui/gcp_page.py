"""The "Google Cloud" page of the Cloud settings dialog (``PAGE``, see
:mod:`~orangecontrib.cloud.gui.provider_page`): the ``gcloud`` tool, signing
in, the project, region and zone, and the machine size and boot disk
(:class:`~orangecontrib.cloud.gui.machine_size.MachineSizePicker`; only the
sizes the zone offers, with their prices).

Below the choices, the setup checker
(:mod:`~orangecontrib.cloud.client.providers.gcp_setup`) shows what's missing
in the project before a machine can start, with an "Enable APIs" button that
asks before turning on the missing APIs. The dialog's Start button needs the
check to pass (:attr:`GoogleCloudPage.setup_ok`, signal ``setupChanged``).

Nothing here runs until the page is first shown. Slow calls (finding
``gcloud``, reading the credentials, listing projects and zones) run on a
background thread, and signing in (``gcloud auth application-default login``)
goes through the session (see :mod:`~orangecontrib.cloud.gui.account_page`).

The choices are written to the Google Cloud part of the dialog's
:class:`~orangecontrib.cloud.settings.CloudSettings` (the project is its
``account``) and saved at once.
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

from orangecontrib.cloud.client.providers import gcp_account, gcp_setup
from orangecontrib.cloud.client.providers.base import ClusterProvider, ProviderError
from orangecontrib.cloud.client.providers.gcp_account import (
    GCLOUD_INSTALL_URL,
    GcloudDefaults,
    NotSignedInError,
    Project,
    Region,
)
from orangecontrib.cloud.client.providers.setup_checks import Check
from orangecontrib.cloud.client.session import CloudSession
from orangecontrib.cloud.gui.account_page import AccountPage
from orangecontrib.cloud.settings import CloudSettings

__all__ = ["PAGE", "GoogleCloudPage"]


@dataclass
class AccountInfo:
    """What the background check of the account found."""

    gcloud: str | None = None
    defaults: GcloudDefaults = field(default_factory=GcloudDefaults)
    signed_in: bool = False
    email: str = ""
    credentials: Any = None
    #: The credentials' own default project, if any.
    adc_project: str = ""
    projects: list[Project] = field(default_factory=list)
    error: str = ""


def check_account() -> AccountInfo:
    """Find gcloud, read the credentials and list the projects (slow)."""
    info = AccountInfo(gcloud=gcp_account.find_gcloud(), defaults=gcp_account.gcloud_defaults())
    try:
        info.credentials, info.adc_project = gcp_account.load_credentials()
        info.projects = gcp_account.list_projects(info.credentials)
        info.signed_in = True
    except NotSignedInError as exc:
        info.error = str(exc)
        info.credentials = None
    except ProviderError as exc:
        # Signed in, but the projects couldn't be listed.
        info.signed_in = info.credentials is not None
        info.error = str(exc)
    if info.credentials is not None:
        info.email = gcp_account.account_email(info.credentials)
    return info


class GoogleCloudPage(AccountPage):
    """Sign in, then choose the project, region and zone. ``setupChanged``:
    the setup check finished (or was invalidated); see :attr:`setup_ok`."""

    ready_text = "The project is ready: billing, APIs, quotas and permissions are all set."
    sign_in_message = "Finish signing in in your browser. Google's sign-in page has opened there."

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
        self._regions: dict[str, list[Region]] = {}
        #: (project, zone) -> the add-on's machine types the zone offers.
        self._zone_types: dict[tuple[str, str], set[str]] = {}

        form = QFormLayout(self)
        form.setContentsMargins(0, 4, 0, 0)

        self.gcloud_label = QLabel(wordWrap=True, openExternalLinks=True)
        self.gcloud_label.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("gcloud tool:", self.gcloud_label)

        row = QHBoxLayout()
        self.account_label = QLabel("…", wordWrap=True)
        self.account_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sign_in_button.setText("Sign in with Google")
        self.sign_in_button.setToolTip(
            "Runs “gcloud auth application-default login”: Google's sign-in page opens "
            "in your browser. The add-on never sees your password."
        )
        row.addWidget(self.account_label, 1)
        row.addWidget(self.sign_in_button)
        form.addRow("Account:", row)

        row = QHBoxLayout()
        self.project_combo = QComboBox(minimumContentsLength=24)
        self.project_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.reload_button = QPushButton("Reload")
        self.reload_button.setToolTip("List the projects and regions again")
        self.reload_button.clicked.connect(self.refresh)
        row.addWidget(self.project_combo, 1)
        row.addWidget(self.reload_button)
        form.addRow("Project:", row)

        self.region_combo = QComboBox(minimumContentsLength=16)
        self.zone_combo = QComboBox(minimumContentsLength=16)
        form.addRow("Region:", self.region_combo)
        form.addRow("Zone:", self.zone_combo)

        self.size_picker = picker = self.add_size_rows(form)
        picker.machineTypeChanged.connect(self._on_machine_type_changed)

        self.check_button.setToolTip(
            "Check billing, APIs, quotas and permissions in the chosen project"
        )
        self.enable_button = QPushButton("Enable APIs…")
        self.enable_button.setToolTip("Turn on the missing APIs in the project")
        self.enable_button.clicked.connect(self._on_enable_clicked)
        self.enable_button.setVisible(False)
        self.add_setup_rows(form, self.enable_button)

        self.project_combo.activated.connect(self._on_project_chosen)
        self.region_combo.activated.connect(self._on_region_chosen)
        self.zone_combo.activated.connect(self._on_zone_chosen)
        self._show_account(None)

    # ------------------------------------------------------------------
    # Public

    @property
    def project(self) -> str:
        """The chosen project's ID (``""`` if none)."""
        return self.project_combo.currentData() or ""

    @property
    def region(self) -> str:
        """The chosen region."""
        return self.region_combo.currentText()

    @property
    def zone(self) -> str:
        """The chosen zone."""
        return self.zone_combo.currentText()

    @property
    def machine_type(self) -> str:
        """The chosen machine type."""
        return self.provider.size_or_default(self.provider_settings.machine_type)

    @property
    def not_ready_text(self) -> str:
        """Why Start waits."""
        return "Starts when the setup check of the Google Cloud project passes."

    @property
    def credentials(self) -> Any:
        """The signed-in user's credentials (``None`` if not signed in)."""
        return self._account.credentials if self._account else None

    @Slot()
    def refresh(self) -> None:
        """Check gcloud and the sign-in, and list the projects again."""
        self._started = True
        self._regions.clear()
        self._zone_types.clear()
        self._checked_for = None
        self._set_message("Checking your Google Cloud account…")
        self._update_enabled(loading=True)
        self._submit(check_account, self._on_account)

    def setup_key(self) -> tuple[str, ...]:
        """The quotas depend on the region and the machine's family and size."""
        return (self.project, self.region, self.machine_type)

    def setup_job(self) -> Callable[[], list[Check]]:
        """The setup check of the chosen project, region and machine type."""
        credentials, project, region = self.credentials, self.project, self.region
        machine_type = self.machine_type
        return lambda: gcp_setup.run_checks(
            credentials, project, region, machine_type, make_api=gcp_setup.GoogleSetupApi
        )

    def _show_checks(self, checks: list[Check]) -> None:
        super()._show_checks(checks)
        self.enable_button.setVisible(bool(gcp_setup.missing_apis(checks)))

    @Slot()
    def _on_enable_clicked(self) -> None:
        missing = gcp_setup.missing_apis(self.setup_checks or [])
        project, credentials = self.project, self.credentials
        if not missing or not project or credentials is None:
            return
        names = "\n".join(f"• {gcp_setup.REQUIRED_APIS[s]} ({s})" for s in missing)
        answer = QMessageBox.question(
            self,
            "Enable APIs",
            f"Enable these APIs in project {project}?\n\n{names}\n\n"
            "Enabling them costs nothing by itself. It may take a minute.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._set_message("Enabling the APIs…")

        def done(_: Any, error: BaseException | None) -> None:
            if error is not None:
                self._set_message(str(error), error=True)
            else:
                self._set_message("")
            self.check_setup()

        self._submit(
            lambda: gcp_setup.enable_apis(gcp_setup.GoogleSetupApi(credentials), project, missing),
            done,
        )
        self._update_enabled()

    # ------------------------------------------------------------------
    # Account and projects

    def _on_account(self, info: AccountInfo | None, error: BaseException | None) -> None:
        if error is not None or info is None:
            info = AccountInfo(error=f"Couldn't check the Google Cloud account: {error}")
        self._account = info
        self._show_account(info)
        if info.error:
            self._set_message(info.error, error=True)
        else:
            self._set_message("")
        self._fill_projects(info)
        if not info.signed_in or not self.project:
            self._schedule_check()

    def _show_account(self, info: AccountInfo | None) -> None:
        if info is None:
            self.gcloud_label.setText("…")
            self.account_label.setText("…")
        else:
            if info.gcloud:
                self.gcloud_label.setText(html.escape(info.gcloud))
                self.gcloud_label.setToolTip("")
            else:
                self.gcloud_label.setText(
                    "Not found. Install the Google Cloud CLI "
                    f'(<a href="{GCLOUD_INSTALL_URL}">instructions</a>), then click Reload. '
                    "If it is installed, start Orange from a terminal where "
                    "“gcloud” works."
                )
            if info.signed_in:
                who = info.email or "Signed in"
                self.account_label.setText(who)
                self.account_label.setToolTip(
                    "Google Cloud's application-default credentials of this computer"
                )
                self.sign_in_button.setText("Sign in again")
            else:
                self.account_label.setText("Not signed in")
                self.account_label.setToolTip("")
                self.sign_in_button.setText("Sign in with Google")
        self._update_enabled()

    def _fill_projects(self, info: AccountInfo) -> None:
        combo = self.project_combo
        combo.clear()
        projects = list(info.projects)
        known = {p.project_id for p in projects}
        # A saved project that isn't listed (e.g. no permission to list it)
        # stays selectable.
        saved = self.provider_settings.account
        if info.signed_in and saved and saved not in known:
            projects.insert(0, Project(saved))
            known.add(saved)
        for project in projects:
            combo.addItem(project.label, project.project_id)
        chosen = next(
            (p for p in (saved, info.defaults.project, info.adc_project) if p and p in known),
            projects[0].project_id if projects else "",
        )
        if chosen:
            combo.setCurrentIndex(combo.findData(chosen))
        elif info.signed_in and not info.error:
            self._set_message(
                "Your Google account has no projects. Create one in the Google Cloud "
                "console, then click Reload.",
                error=True,
            )
        self._project_changed()

    @Slot(int)
    def _on_project_chosen(self, _index: int) -> None:
        self._project_changed()

    def _project_changed(self) -> None:
        project = self.project
        if project != self.provider_settings.account:
            self.provider_settings.account = project
            self._save()
        self.region_combo.clear()
        self.zone_combo.clear()
        if not project or self.credentials is None:
            self._update_enabled()
            return
        if project in self._regions:
            self._fill_regions(project)
            return
        credentials = self.credentials
        self._set_message(f"Listing the regions of {project}…")

        def done(regions: list[Region] | None, error: BaseException | None) -> None:
            if self.project != project:
                return  # the user chose another project meanwhile
            if error is not None:
                self._set_message(str(error), error=True)
                return
            self._set_message("")
            self._regions[project] = regions or []
            self._fill_regions(project)

        self._submit(lambda: gcp_account.list_regions(credentials, project), done)

    def _fill_regions(self, project: str) -> None:
        regions = self._regions.get(project, [])
        defaults = self._account.defaults if self._account else GcloudDefaults()
        self.region_combo.clear()
        for region in regions:
            self.region_combo.addItem(region.name)
        chosen = gcp_account.pick_region(regions, self.provider_settings.region, defaults.region)
        if chosen:
            self.region_combo.setCurrentText(chosen)
        self._region_changed()

    @Slot(int)
    def _on_region_chosen(self, _index: int) -> None:
        self._region_changed()

    def _region_changed(self) -> None:
        region = self.region
        zones: tuple[str, ...] = ()
        for r in self._regions.get(self.project, []):
            if r.name == region:
                zones = r.zones
        defaults = self._account.defaults if self._account else GcloudDefaults()
        self.zone_combo.clear()
        self.zone_combo.addItems(list(zones))
        chosen = gcp_account.pick_zone(zones, region, self.provider_settings.zone, defaults.zone)
        if chosen:
            self.zone_combo.setCurrentText(chosen)
        self._zone_changed()

    @Slot(int)
    def _on_zone_chosen(self, _index: int) -> None:
        self._zone_changed()

    def _zone_changed(self) -> None:
        if not self.region or not self.zone:
            self._update_enabled()
            return
        chosen = self.provider_settings
        if (chosen.region, chosen.zone) != (self.region, self.zone):
            chosen.region, chosen.zone = self.region, self.zone
            self._save()
        self._list_machine_types()
        # The quotas are per region; a new zone in the same region changes nothing.
        self._schedule_check_if_changed()
        self._update_enabled()

    def _list_machine_types(self) -> None:
        """Show the sizes the chosen zone offers (listed once per zone)."""
        project, region, zone = self.project, self.region, self.zone
        key, credentials = (project, zone), self.credentials
        if key in self._zone_types or credentials is None:
            self.size_picker.set_region(region, self._zone_types.get(key))
            return
        self.size_picker.set_region(region, None)

        def done(names: set[str] | None, error: BaseException | None) -> None:
            if (self.project, self.zone) != key:
                return  # another zone was chosen meanwhile
            if error is not None:
                self._set_message(f"All sizes are shown. {error}", error=True)
                return
            self._zone_types[key] = names or set()
            self.size_picker.set_region(region, self._zone_types[key])

        self._submit(lambda: gcp_account.available_machine_types(credentials, project, zone), done)

    @Slot(str)
    def _on_machine_type_changed(self, _name: str) -> None:
        # The quota check depends on the machine's family and size.
        self._schedule_check_if_changed()

    # ------------------------------------------------------------------

    @property
    def can_sign_in(self) -> bool:
        """gcloud was found."""
        return bool(self._account and self._account.gcloud)

    def _update_enabled(self, loading: bool = False) -> None:
        loading = loading or bool(self._callbacks)
        signing_in = self._signing_in
        account = self._account
        self.sign_in_button.setEnabled(
            signing_in or (not loading and account is not None and bool(account.gcloud))
        )
        self.reload_button.setEnabled(not loading and not signing_in)
        signed_in = account is not None and account.signed_in
        idle = not loading and not signing_in and signed_in
        self.project_combo.setEnabled(idle and self.project_combo.count() > 0)
        self.region_combo.setEnabled(idle and self.region_combo.count() > 0)
        self.zone_combo.setEnabled(idle and self.zone_combo.count() > 0)
        self.size_picker.combo.setEnabled(not signing_in and self.size_picker.combo.count() > 0)
        self.check_button.setEnabled(not loading and not signing_in)
        self.enable_button.setEnabled(idle)


#: The page of the ``gcp`` provider.
PAGE = GoogleCloudPage
