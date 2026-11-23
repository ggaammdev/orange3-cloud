"""A machine provider's page in the Cloud settings dialog.

The dialog offers every registered provider (:func:`~orangecontrib.cloud.
client.providers.registry.registered_providers`) and shows the chosen one's
page below the choice. A provider's page is the :class:`ProviderPage`
subclass named ``PAGE`` in the module ``orangecontrib.cloud.gui.<id>_page``
(e.g. :mod:`~orangecontrib.cloud.gui.gcp_page`), or one given to
:func:`register_page`; providers without one get :class:`SizesPage`, which
shows their machine sizes (if they have any).

A page writes the provider's choices to its
:class:`~orangecontrib.cloud.settings.ProviderSettings` and saves them at
once. Its :attr:`~ProviderPage.ready` says whether a machine can start (e.g.
the setup check passed); the page emits ``setupChanged`` when that changes.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

from AnyQt.QtCore import pyqtSignal as Signal
from AnyQt.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QWidget

from orangecontrib.cloud.client.providers.base import ClusterProvider
from orangecontrib.cloud.client.session import CloudSession
from orangecontrib.cloud.gui.machine_size import MachineSizePicker
from orangecontrib.cloud.settings import CloudSettings

__all__ = ["ProviderPage", "SizesPage", "make_page", "page_class", "register_page"]


class ProviderPage(QWidget):
    """The base of providers' pages: the provider, its settings and the
    interface the dialog uses.

    :param settings: the dialog's settings; the page writes the provider's
        part (:attr:`provider_settings`) and calls ``save``.
    """

    #: :attr:`ready` may have changed.
    setupChanged = Signal()

    def __init__(
        self,
        provider: ClusterProvider,
        settings: CloudSettings,
        save: Callable[[], None],
        session: CloudSession,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.provider = provider
        self.settings = settings
        self.provider_settings = settings.of(provider.id)
        self._save = save
        self._session = session
        #: The size picker (``None``: the provider has no sizes to choose).
        self.size_picker: MachineSizePicker | None = None

    @property
    def ready(self) -> bool:
        """Whether a machine can start with the page's choices."""
        return True

    @property
    def not_ready_text(self) -> str:
        """Why a machine can't start yet, for the Start button's tooltip."""
        return ""

    def add_size_rows(self, form: QFormLayout) -> MachineSizePicker | None:
        """Add the machine size and boot disk rows to ``form`` if the provider
        has sizes to choose, else a row with its one machine."""
        if not self.provider.machine_sizes:
            types = self.provider.machine_types()
            form.addRow("Machine:", QLabel(types[0].label if types else self.provider.label))
            return None
        self.size_picker = picker = MachineSizePicker(
            self.provider, self.provider_settings, self._save, self
        )
        form.addRow("Machine size:", picker.combo)
        form.addRow("Boot disk:", picker.disk_spin)
        form.addRow(picker.note_label)
        row = QHBoxLayout()
        row.addWidget(picker.suggestion_label, 1)
        row.addWidget(picker.use_button)
        form.addRow(row)
        return picker

    def shutdown(self) -> None:
        """Stop the page's background work (the dialog is going away)."""
        return None


class SizesPage(ProviderPage):
    """The page of a provider without its own: its machine sizes, if any."""

    def __init__(
        self,
        provider: ClusterProvider,
        settings: CloudSettings,
        save: Callable[[], None],
        session: CloudSession,
        parent: QWidget | None = None,
    ):
        super().__init__(provider, settings, save, session, parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 4, 0, 0)
        self.add_size_rows(form)


_PAGES: dict[str, type[ProviderPage]] = {}


def register_page(provider_id: str, page: type[ProviderPage]) -> None:
    """Show ``page`` for provider ``provider_id``."""
    _PAGES[provider_id] = page


def page_class(provider_id: str) -> type[ProviderPage]:
    """The page of provider ``provider_id``: a registered one, else ``PAGE``
    in ``orangecontrib.cloud.gui.<provider_id>_page``, else :class:`SizesPage`."""
    if provider_id in _PAGES:
        return _PAGES[provider_id]
    name = f"{__package__}.{provider_id}_page"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:  # the module exists, but something it imports doesn't
            raise
        return SizesPage
    return module.PAGE


def make_page(
    provider: ClusterProvider,
    settings: CloudSettings,
    save: Callable[[], None],
    session: CloudSession,
) -> ProviderPage:
    """The page of ``provider`` (see :func:`page_class`)."""
    return page_class(provider.id)(provider, settings, save, session)
