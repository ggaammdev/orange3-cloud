"""Choosing the machine size and boot disk, with prices.

:class:`MachineSizePicker` makes the controls of a provider page's "Machine
size" and "Boot disk" rows: the provider's sizes (:attr:`~orangecontrib.cloud.
client.providers.base.ClusterProvider.machine_sizes`) that the chosen region
or zone offers, each with its hourly price there (from the provider's bundled
price list, :meth:`~orangecontrib.cloud.client.providers.base.ClusterProvider.
size_price`), and the disk size. The choices go to the provider's
:class:`~orangecontrib.cloud.settings.ProviderSettings` and are saved at once.

**Size suggestion.** Widgets that know how much memory their data needs (Cloud
File's memory estimate) tell :func:`size_hints`. The picker then marks the
smallest size and disk that fit, offers to use them, and warns when the chosen
size or disk is too small.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from AnyQt.QtCore import QObject
from AnyQt.QtCore import pyqtSignal as Signal
from AnyQt.QtWidgets import QComboBox, QLabel, QPushButton, QSpinBox

from orangecontrib.cloud.client.costs import format_price
from orangecontrib.cloud.client.providers.base import ClusterProvider, MachineType
from orangecontrib.cloud.client.providers.sizes import (
    DISK_MAX_GB,
    DISK_MIN_GB,
    GIB,
    suggest_machine_type,
)
from orangecontrib.cloud.settings import ProviderSettings

__all__ = ["MachineSizePicker", "SizeHints", "SizeNeed", "size_hints"]

_WARNING_STYLE = "color: #c62828;"


@dataclass(frozen=True)
class SizeNeed:
    """What some data needs on the machine."""

    needed_low: int  #: bytes of memory: the table and the least working memory
    needed_high: int  #: bytes of memory: the table and the most working memory
    disk_gb: int  #: boot disk
    label: str = ""  #: what needs it, e.g. "data.csv in Cloud File"


class SizeHints(QObject):
    """What the open widgets' data needs; the picker suggests a size for the
    largest need. Use the shared :func:`size_hints`."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._needs: dict[int, SizeNeed] = {}

    def set(self, owner: object, need: SizeNeed | None) -> None:
        """Set (or, with ``None``, clear) what ``owner``'s data needs."""
        key = id(owner)
        if need is None:
            changed = self._needs.pop(key, None) is not None
        else:
            changed = self._needs.get(key) != need
            self._needs[key] = need
        if changed:
            self.changed.emit()

    def largest(self) -> SizeNeed | None:
        """The need with the most memory (``None``: nothing known)."""
        needs = list(self._needs.values())
        if not needs:
            return None
        biggest = max(needs, key=lambda n: (n.needed_high, n.disk_gb))
        disk = max(n.disk_gb for n in needs)
        return SizeNeed(biggest.needed_low, biggest.needed_high, disk, biggest.label)


_hints: SizeHints | None = None


def size_hints() -> SizeHints:
    """The shared :class:`SizeHints`."""
    global _hints
    from AnyQt.sip import isdeleted

    if _hints is None or isdeleted(_hints):  # e.g. a new QApplication in tests
        _hints = SizeHints()
    return _hints


def _gb(n_bytes: float) -> str:
    return f"{math.ceil(n_bytes / GIB):,} GB"


class MachineSizePicker(QObject):
    """The machine size combo, the disk size spin box and their notes.

    :param provider: whose sizes and prices are shown.
    :param settings: the provider's; written and saved (``save``) when the
        user chooses.
    """

    #: The chosen machine type changed (and was saved).
    machineTypeChanged = Signal(str)

    def __init__(
        self,
        provider: ClusterProvider,
        settings: ProviderSettings,
        save: Callable[[], None],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.provider = provider
        self.settings = settings
        self._save = save
        self.region = settings.region
        #: Names of the sizes the region or zone offers (``None``: not known;
        #: all shown).
        self.available: set[str] | None = None

        self.combo = QComboBox(minimumContentsLength=40)
        self.combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.disk_spin = QSpinBox(minimum=DISK_MIN_GB, maximum=DISK_MAX_GB, suffix=" GB")
        self.disk_spin.setSingleStep(10)
        self.disk_spin.setValue(settings.disk_gb)
        self.disk_spin.setToolTip(
            "The machine's boot disk. It holds the system, Python, your packages "
            "and temporary files."
        )
        self.note_label = QLabel(wordWrap=True)
        self.note_label.setEnabled(False)
        self.suggestion_label = QLabel(wordWrap=True)
        self.use_button = QPushButton("Use suggested size")
        self.use_button.clicked.connect(self.use_suggestion)

        self.combo.activated.connect(self._on_chosen)
        self.disk_spin.valueChanged.connect(self._on_disk_changed)
        size_hints().changed.connect(self.update_suggestion)
        self._fill()

    # ------------------------------------------------------------------

    @property
    def machine_type(self) -> str:
        """The chosen machine type's name."""
        return self.combo.currentData() or self.provider.size_or_default(self.settings.machine_type)

    def all_types(self) -> list[MachineType]:
        """All the provider's sizes, offered here or not."""
        return list(self.provider.machine_sizes)

    def types(self) -> list[MachineType]:
        """The sizes offered in the chosen region or zone."""
        types = self.all_types()
        if self.available is None:
            return types
        return [t for t in types if t.name in self.available]

    def price(self, machine_type: MachineType | str) -> float | None:
        """USD per hour in the chosen region with the chosen disk."""
        name = machine_type if isinstance(machine_type, str) else machine_type.name
        return self.provider.size_price(name, self.region, self.disk_spin.value())

    def set_region(self, region: str, available: set[str] | None) -> None:
        """The chosen region and the sizes offered there, e.g. in the chosen
        zone (``None``: unknown)."""
        self.region, self.available = region, available
        self._fill()

    def suggestion(self) -> tuple[MachineType | None, SizeNeed | None]:
        """The smallest offered size that fits the largest need, and the need."""
        need = size_hints().largest()
        if need is None:
            return None, None
        return suggest_machine_type(need.needed_low, need.needed_high, self.types()), need

    def use_suggestion(self) -> None:
        """Choose the suggested size and a disk at least as big as suggested."""
        suggested, need = self.suggestion()
        if need is None:
            return
        if need.disk_gb > self.disk_spin.value():
            self.disk_spin.setValue(min(need.disk_gb, DISK_MAX_GB))
        if suggested is not None:
            self.combo.setCurrentIndex(self.combo.findData(suggested.name))
            self._on_chosen()

    # ------------------------------------------------------------------

    def _fill(self) -> None:
        """List the offered sizes with their prices; keep the choice if offered."""
        types = self.types()
        suggested, _ = self.suggestion()
        self.combo.clear()
        for t in types:
            mark = "  ← fits your data" if suggested is not None and t == suggested else ""
            text = (
                f"{t.label} ({t.name}): {t.cpus} CPUs, {t.memory_gb:g} GB, "
                f"{format_price(self.price(t))}{mark}"
            )
            self.combo.addItem(text, t.name)
        names = [t.name for t in types]
        saved = self.settings.machine_type
        chosen = next(
            (n for n in (saved, self.provider.default_size) if n in names),
            names[0] if names else "",
        )
        if chosen:
            self.combo.setCurrentIndex(self.combo.findData(chosen))
            if saved and chosen != saved:  # the saved size isn't offered here
                self._on_chosen()
        self._update_notes()

    def _on_chosen(self, _index: int = -1) -> None:
        name = self.combo.currentData()
        if name and name != self.settings.machine_type:
            self.settings.machine_type = name
            self._save()
            self.machineTypeChanged.emit(name)
        self._update_notes()

    def _on_disk_changed(self, value: int) -> None:
        if value != self.settings.disk_gb:
            self.settings.disk_gb = value
            self._save()
        self._fill()

    def update_suggestion(self) -> None:
        """Mark the size that fits the data (after the needs changed)."""
        self._fill()

    def _update_notes(self) -> None:
        date = self.provider.prices_date()
        unavailable = len(self.all_types()) - len(self.types())
        note = (
            f"On-demand list prices in {self.region or 'the region'} on {date}, "
            "for the machine, its disk and its IP address, without discounts or "
            "network traffic. Charged while the machine exists."
            if date
            else "Prices aren't known."
        )
        if unavailable:
            note += f" Sizes not offered here are hidden ({unavailable})."
        self.note_label.setText(note)
        self._update_warning()

    def _update_warning(self) -> None:
        suggested, need = self.suggestion()
        self.use_button.setVisible(need is not None)
        if need is None:
            self.suggestion_label.setVisible(False)
            return
        chosen = next((t for t in self.all_types() if t.name == self.machine_type), None)
        memory = (chosen.memory_gb or 0) * GIB if chosen else 0
        what = need.label or "Your data"
        text = (
            f"{what} probably needs {_gb(need.needed_low)}–{_gb(need.needed_high)} of "
            f"memory and a disk of {need.disk_gb} GB. "
        )
        problems = []
        if suggested is None:
            problems.append("Even the largest size here is probably too small.")
        else:
            text += f"Suggested: {suggested.label} ({suggested.name}). "
        if memory < need.needed_low:
            problems.append("The chosen size is probably too small.")
        if self.disk_spin.value() < need.disk_gb:
            problems.append("The chosen disk is too small.")
        self.suggestion_label.setText(text + " ".join(problems))
        self.suggestion_label.setStyleSheet(_WARNING_STYLE if problems else "")
        self.suggestion_label.setVisible(True)
        fits = suggested is None or (
            self.machine_type == suggested.name and self.disk_spin.value() >= need.disk_gb
        )
        self.use_button.setEnabled(not fits)
