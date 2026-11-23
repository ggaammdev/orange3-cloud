"""The files uploaded to the provider's storage (e.g. the Google Cloud
project's bucket), where the user can delete them.

The calls run in a background thread, which hands their results to the
dialog in the GUI thread through :func:`~orangecontrib.cloud.gui.relay.
receiver`, so a dialog closed (and deleted) meanwhile is left alone. The
thread holds no reference to the dialog.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Callable
from typing import Any

from AnyQt.QtCore import Qt
from AnyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from orangecontrib.cloud.client.providers.base import ClusterProvider, MachineHandle, StoredFile
from orangecontrib.cloud.gui.relay import receiver

__all__ = ["UploadedFilesDialog"]

#: ``callback(dialog, result, error)`` of a background call, in the GUI thread.
_Callback = Callable[["UploadedFilesDialog", Any, Any], None]


def format_bytes(n: float) -> str:
    """E.g. "1.5 GB" (binary units)."""
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            break
        n /= 1024
    return f"{int(n)} bytes" if unit == "bytes" else f"{n:.1f} {unit}"


class UploadedFilesDialog(QDialog):
    """Lists the files uploaded to ``provider``'s storage; deletes the
    checked ones after the user confirms."""

    def __init__(
        self,
        provider: ClusterProvider,
        handle: MachineHandle | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.provider = provider
        self.handle = handle
        #: The listed files (``None`` while listing).
        self.files: list[StoredFile] | None = None
        self.busy = False
        self.setWindowTitle("Uploaded files")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.list = QListWidget()
        self.list.itemChanged.connect(self._update_buttons)
        layout.addWidget(self.list)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.delete_button = QPushButton("Delete…")
        self.delete_button.clicked.connect(self.delete_checked)
        buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(self.reload)
        buttons.addButton(self.reload_button, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)
        self.reload()

    # ------------------------------------------------------------------

    def _run(self, fn: Callable[[], Any], callback: _Callback) -> None:
        """Run ``fn`` in a background thread (it mustn't refer to the
        dialog); ``callback(dialog, result, error)`` gets its outcome in the
        GUI thread if the dialog still exists."""
        self.busy = True
        self._update_buttons()
        deliver = receiver(self, UploadedFilesDialog._on_done)

        def run() -> None:
            try:
                result, error = fn(), None
            except Exception as exc:  # noqa: BLE001 - shown in the dialog
                result, error = None, exc
            deliver(callback, result, error)

        threading.Thread(target=run, name="orange3-cloud-uploads", daemon=True).start()

    def _on_done(self, callback: _Callback, result: Any, error: Any) -> None:
        self.busy = False
        callback(self, result, error)
        self._update_buttons()

    def reload(self) -> None:
        """List the files again."""
        self.files = None
        self.list.clear()
        self.label.setText("Listing the uploaded files…")
        provider, handle = self.provider, self.handle
        self._run(lambda: provider.stored_files(handle), UploadedFilesDialog._show_files)

    def _show_files(self, files: list[StoredFile] | None, error: Exception | None) -> None:
        if error is not None:
            self.label.setText(f"Couldn't list the uploaded files: {html.escape(str(error))}")
            return
        self.files = files or []
        self.list.clear()
        for f in self.files:
            date = f.updated[:16].replace("T", " ")
            item = QListWidgetItem(f"{f.name}  ({format_bytes(f.size_bytes)}, {date})")
            item.setToolTip(f.uri)
            item.setData(Qt.ItemDataRole.UserRole, f.uri)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        total = sum(f.size_bytes for f in self.files)
        self.label.setText(
            f"{len(self.files)} file{'s' if len(self.files) != 1 else ''} "
            f"({format_bytes(total)}) uploaded from this computer, kept for later sessions. "
            "Check the ones to delete."
            if self.files
            else "No files are uploaded."
        )

    def checked_uris(self) -> list[str]:
        """The URIs of the checked files."""
        items = (self.list.item(i) for i in range(self.list.count()))
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in items
            if item.checkState() == Qt.CheckState.Checked
        ]

    def set_checked(self, uris: list[str]) -> None:
        """Check the files with these URIs (and uncheck the rest)."""
        for i in range(self.list.count()):
            item = self.list.item(i)
            wanted = item.data(Qt.ItemDataRole.UserRole) in uris
            item.setCheckState(Qt.CheckState.Checked if wanted else Qt.CheckState.Unchecked)

    def confirm_delete(self, names: list[str]) -> bool:
        """Ask the user before deleting (tests replace it)."""
        answer = QMessageBox.question(
            self,
            "Delete uploaded files",
            f"Delete these files from {self.provider.label} storage?\n\n"
            + "\n".join(f"• {name}" for name in names)
            + "\n\nThey can't be restored. Workflows that use them upload them again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def delete_checked(self) -> None:
        """Delete the checked files after the user confirms."""
        uris = self.checked_uris()
        if not uris:
            return
        names = [f.name for f in self.files or [] if f.uri in uris]
        if not self.confirm_delete(names):
            return
        self.label.setText("Deleting…")
        provider = self.provider
        self._run(lambda: provider.delete_stored_files(uris), UploadedFilesDialog._on_deleted)

    def _on_deleted(self, _: Any, error: Exception | None) -> None:
        if error is not None:
            self.label.setText(f"Couldn't delete the files: {html.escape(str(error))}")
            return
        self.reload()

    def _update_buttons(self, *_: Any) -> None:
        self.reload_button.setEnabled(not self.busy)
        self.delete_button.setEnabled(not self.busy and bool(self.checked_uris()))
