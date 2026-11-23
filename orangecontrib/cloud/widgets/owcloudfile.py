"""Cloud File: load a CSV or Parquet file into the cloud machine's memory.

The file comes from this computer, from an ``https://`` URL or from the
cloud's storage, e.g. ``gs://`` on Google Cloud, ``s3://`` on AWS or
``az://`` on Azure (the machine downloads it; the provider says which
schemes it accepts and how its
machine reads them, :meth:`~orangecontrib.cloud.client.providers.base.
ClusterProvider.machine_location`, e.g. through a signed link). A URL with a
numbered pattern, e.g. ``…/trips_2025-{01..12}.parquet``, loads several files
with the same columns as one table (:mod:`.file_sources`). A local file
is uploaded straight to a local machine; on a cloud it goes to the
provider's storage, e.g. the project's storage bucket (created after the
user agrees), where it stays for later sessions until the user deletes it
("Uploaded files…"). Before loading, the widget estimates the memory the
file needs and warns when the machine is too small. The output is a
:class:`~orangecontrib.cloud.data.remote_table.RemoteTable`: the cloud
table's domain and one placeholder row.

Column roles and types are edited with Orange's own domain editor. Applying
a change loads the file again with the new roles and types (an uploaded file
isn't uploaded again).

The workflow saves the source and the column roles, never credentials. A
relative path to a local file is relative to the workflow's folder, so the
example workflows find their data next to them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from AnyQt.QtCore import Qt, QTimer
from AnyQt.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
)
from Orange.data import Table
from Orange.widgets import gui
from Orange.widgets.settings import Setting, SettingProvider
from Orange.widgets.utils.domaineditor import DomainEditor
from Orange.widgets.utils.widgetpreview import WidgetPreview
from Orange.widgets.widget import Msg, Output

from orangecontrib.cloud.client.api import CloudNotFoundError
from orangecontrib.cloud.client.providers.base import (
    ClusterProvider,
    MachineHandle,
    StorageMissingError,
)
from orangecontrib.cloud.data.estimate import (
    MemoryEstimate,
    estimate_local_file,
    file_format,
    format_bytes,
)
from orangecontrib.cloud.data.remote_table import RemoteTable
from orangecontrib.cloud.gui.machine_size import SizeNeed, size_hints
from orangecontrib.cloud.gui.uploaded_files import UploadedFilesDialog
from orangecontrib.cloud.protocol.machine import InfoResponse
from orangecontrib.cloud.protocol.tables import (
    ColumnRole,
    ColumnSpec,
    ColumnType,
    TableCreateRequest,
    TableInfo,
)
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget, computed_with
from orangecontrib.cloud.widgets.file_columns import (
    ColumnsModel,
    column_specs,
    editor_row,
    rows_from_domain,
    setting_row,
)
from orangecontrib.cloud.widgets.file_sources import (
    WEB_SCHEMES,
    estimate_remote,
    machine_source,
    or_list,
    upload_progress,
    url_problem,
)

__all__ = ["OWCloudFile"]

log = logging.getLogger(__name__)

FILE_FILTER = "Data files (*.csv *.tsv *.tab *.txt *.parquet *.pq *.gz *.gzip);;All files (*)"


@dataclass
class _TooBig:
    """The load stopped before it began: the file is probably too big."""

    estimate: MemoryEstimate
    location: str


@dataclass
class _NeedsBucket:
    """The load stopped before it began: local files go to the provider's
    storage (e.g. a bucket), which must be created first (after the user
    agrees)."""

    bucket: str
    message: str
    force: bool


@dataclass
class _Loaded:
    table: RemoteTable
    info: TableInfo
    warnings: list[str]
    location: str
    estimate: MemoryEstimate | None
    #: (key, upload ID) of a local file uploaded to the machine, to load it
    #: again without uploading.
    upload: tuple[tuple, str] | None
    #: Where a local file was uploaded to the provider's storage (e.g. ``gs://…``).
    staged_uri: str | None = None


@dataclass
class _LoadRequest:
    """What a load needs, collected in the GUI thread for the worker."""

    location: str
    #: The local file's path, or the URL.
    path: str
    source: int
    specs: list[ColumnSpec]
    machine_id: str
    memory: int | None
    estimate: MemoryEstimate | None
    upload: tuple[tuple, str] | None
    #: The machine's provider (local files go to its storage if it has one).
    provider: ClusterProvider | None
    handle: MachineHandle | None
    force: bool = False
    create_bucket: str | None = None


class OWCloudFile(OWCloudBaseWidget):
    """Load a CSV or Parquet file into the memory of the cloud machine."""

    name = "Cloud File"
    description = "Load a CSV or Parquet file into the memory of the cloud machine."
    icon = "icons/CloudFile.svg"
    priority = 10
    keywords = "cloud file, load, read, open, csv, parquet, gs, s3, az, storage, url"

    class Outputs:
        data = Output(
            "Data",
            Table,
            doc="The cloud table: its columns and a placeholder row; the data stays on the "
            "machine.",
        )

    want_main_area = False

    LOCAL, URL = 0, 1

    source: int = Setting(LOCAL, schema_only=True)
    local_path: str = Setting("", schema_only=True)
    url: str = Setting("", schema_only=True)
    #: The applied column roles and types (the domain editor's rows), and the
    #: ones the machine inferred, for ``columns_source``.
    column_rows: list = Setting([], schema_only=True)
    inferred_rows: list = Setting([], schema_only=True)
    columns_source: str = Setting("", schema_only=True)
    #: The last loaded table, reused when the workflow opens again while the
    #: same machine runs.
    table_id: str = Setting("", schema_only=True)
    table_machine_id: str = Setting("", schema_only=True)

    domain_editor = SettingProvider(DomainEditor)

    class Warning(OWCloudBaseWidget.Warning):
        too_big = Msg("{}")
        load_warnings = Msg("{}")
        no_estimate = Msg("The memory this file needs can't be estimated before loading: {}")

    class Error(OWCloudBaseWidget.Error):
        source_error = Msg("{}")

    def __init__(self):
        super().__init__()
        #: The output (``None`` until a table is loaded).
        self.data: RemoteTable | None = None
        self.table_info: TableInfo | None = None
        #: Estimate for ``_estimate_location``.
        self.estimate: MemoryEstimate | None = None
        self._estimate_location = ""
        #: The last upload: ((machine ID, path, size, mtime), upload ID).
        self._upload: tuple[tuple, str] | None = None
        #: Where the loaded local file was uploaded to the provider's storage.
        self.staged_uri: str | None = None
        self._build_gui()
        self._update_provider_texts()
        self._update_estimate()
        self._auto_load()

    # ------------------------------------------------------------------
    # GUI

    def _build_gui(self) -> None:
        box = gui.vBox(self.controlArea, "Source")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        box.layout().addLayout(grid)
        buttons = gui.radioButtons(None, self, "source", callback=self._source_changed)
        local = gui.appendRadioButton(buttons, "File on this computer:", addToLayout=False)
        url = gui.appendRadioButton(buttons, "URL:", addToLayout=False)
        self.path_edit = QLineEdit(self.local_path)
        self.path_edit.setPlaceholderText("/path/to/data.csv")
        self.path_edit.editingFinished.connect(self._path_edited)
        self.browse_button = gui.button(
            None, self, "…", callback=self.browse_file, autoDefault=False, width=30
        )
        self.url_edit = QLineEdit(self.url)
        self.url_edit.editingFinished.connect(self._url_edited)
        grid.addWidget(local, 0, 0)
        grid.addWidget(self.path_edit, 0, 1)
        grid.addWidget(self.browse_button, 0, 2)
        grid.addWidget(url, 1, 0)
        grid.addWidget(self.url_edit, 1, 1, 1, 2)
        row = gui.hBox(box)
        self.load_button = gui.button(row, self, "Load", callback=self.load, autoDefault=False)
        self.files_button = gui.button(
            row, self, "Uploaded files…", callback=self.show_uploaded_files, autoDefault=False
        )

        box = gui.vBox(self.controlArea, "Info")
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.info_label.setMinimumWidth(360)
        box.layout().addWidget(self.info_label)

        box = gui.vBox(self.controlArea, "Columns (double click to edit)")
        self.domain_editor = DomainEditor(self)
        self.columns_model = ColumnsModel([], self.domain_editor)
        self.domain_editor.setModel(self.columns_model)
        self.columns_model.dataChanged.connect(self._columns_edited)
        box.layout().addWidget(self.domain_editor)
        row = gui.hBox(box)
        self.reset_button = gui.button(
            row, self, "Reset", callback=self.reset_columns, autoDefault=False
        )
        gui.rubber(row)
        self.apply_button = gui.button(
            row, self, "Apply", callback=self.apply_columns, autoDefault=False
        )
        self._set_editor_rows()

    def browse_file(self) -> None:
        """Choose a file on this computer."""
        start = os.path.dirname(self.local_file()) if self.local_path else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Open a data file", start, FILE_FILTER)
        if path:
            self.set_local_path(path)

    def set_local_path(self, path: str) -> None:
        """Use the file at ``path`` on this computer (as the dialog does)."""
        self.path_edit.setText(path)
        self.local_path = path
        self.source = self.LOCAL
        self._source_changed()

    def set_url(self, url: str) -> None:
        """Use the file at ``url`` (as typing it does)."""
        self.url_edit.setText(url)
        self.url = url
        self.source = self.URL
        self._source_changed()

    def _path_edited(self) -> None:
        if self.path_edit.text().strip() != self.local_path:
            self.local_path = self.path_edit.text().strip()
            self.source = self.LOCAL
            self._source_changed()

    def _url_edited(self) -> None:
        if self.url_edit.text().strip() != self.url:
            self.url = self.url_edit.text().strip()
            self.source = self.URL
            self._source_changed()

    def _source_changed(self) -> None:
        self.cancel()
        self._update_estimate()

    @property
    def location(self) -> str:
        """The chosen file: a local path or a URL ("" if none)."""
        return (self.local_path if self.source == self.LOCAL else self.url).strip()

    def local_file(self) -> str:
        """The local file's path; a relative path is relative to the workflow's
        folder (the current folder if the workflow isn't saved)."""
        path = os.path.expanduser(self.local_path.strip())
        basedir = self.workflowEnv().get("basedir")
        if path and not os.path.isabs(path) and basedir:
            path = os.path.join(basedir, path)
        return path

    def workflowEnvChanged(self, key: str, value: Any, oldvalue: Any) -> None:
        super().workflowEnvChanged(key, value, oldvalue)
        if key == "basedir" and self.source == self.LOCAL and not os.path.isabs(self.local_path):
            self._estimate_location = None  # estimate the file in the new folder
            self._update_estimate()

    # ------------------------------------------------------------------
    # Columns

    def _set_editor_rows(self) -> None:
        """Show the applied column rows (and the inferred ones as the original)."""
        applied = self.column_rows if self.columns_source == self.location else []
        inferred = self.inferred_rows if applied else []
        self.columns_model.set_orig_variables([editor_row(r) for r in inferred])
        self.columns_model.set_variables([editor_row(r) for r in applied])
        self.domain_editor.resizeColumnsToContents()
        self._columns_edited()

    def editor_rows(self) -> list[dict[str, Any]]:
        """The rows as they are in the editor, applied or not."""
        return [setting_row(r) for r in self.columns_model.variables]

    def _columns_edited(self, *_: Any) -> None:
        edited = [(r["name"], r["type"], r["role"]) for r in self.editor_rows()]
        applied = [(r["name"], r["type"], r["role"]) for r in self.column_rows]
        loaded = self.data is not None and self.columns_source == self.location
        self.apply_button.setEnabled(loaded and edited != applied)
        self.reset_button.setEnabled(loaded and bool(edited))

    def reset_columns(self) -> None:
        """Back to the roles and types the machine inferred (apply to load)."""
        self.columns_model.reset_variables()
        self._columns_edited()

    def apply_columns(self) -> None:
        """Load the file again with the edited roles and types."""
        rows = self.editor_rows()
        for row in rows:
            if row["type"] == ColumnType.TEXT.value and row["role"] in ("feature", "target"):
                row["role"] = ColumnRole.META.value
        self.column_rows = rows
        self.load(force=True)

    # ------------------------------------------------------------------
    # Memory estimate

    def _update_estimate(self) -> None:
        """Estimate the chosen file's memory (local files now, URLs when loading)."""
        self.Error.source_error.clear()
        self.Warning.no_estimate.clear()
        location = self.location
        if location != self._estimate_location:
            self.estimate, self._estimate_location = None, location
            if location and self.source == self.LOCAL:
                try:
                    self.estimate = estimate_local_file(self.local_file())
                except FileNotFoundError:
                    self.Error.source_error(f"File not found: {self.local_file()}")
                except ValueError as exc:
                    self.Error.source_error(str(exc))
                except Exception as exc:  # noqa: BLE001 - shown; e.g. a broken .gz file
                    self.Warning.no_estimate(str(exc) or type(exc).__name__)
        if self.data is None or self.columns_source != location:
            self._set_editor_rows()
        self._show_estimate()

    def machine_memory(self) -> int | None:
        """The connected machine's memory (``None`` when not connected)."""
        info = self.session.last_info if self.session.is_connected else None
        return info.memory_total_bytes if info is not None else None

    def _too_big_text(self, estimate: MemoryEstimate, memory: int) -> str:
        where, sizes = self.size_choices()
        suggested = estimate.suggested_machine_type(sizes)
        if suggested is not None:
            use = (
                f'On {where}, use "{suggested.label}" ({suggested.name}, '
                f"{suggested.memory_gb:g} GB) or larger"
            )
        elif sizes:
            use = f"It's probably too big even for the largest machine on {where}; use less data"
        else:
            use = f"Use a cloud machine with at least {format_bytes(estimate.needed_low)} of memory"
        return (
            f"This file probably needs {format_bytes(estimate.needed_low)}–"
            f"{format_bytes(estimate.needed_high)} of memory, but the machine has "
            f"{format_bytes(memory)}. {use}, with a boot disk of at least "
            f"{estimate.disk_gb} GB (Cloud settings…). The estimate is rough."
        )

    def estimate_too_big(self) -> bool:
        """True if the file's estimated memory doesn't fit the machine."""
        memory = self.machine_memory()
        return self.estimate is not None and memory is not None and not self.estimate.fits(memory)

    def _show_estimate(self) -> None:
        """The info text, the too-big warning and the Load button's text."""
        self.Warning.too_big.clear()
        location, estimate = self.location, self.estimate
        lines = []
        loaded = self.data is not None and location == self.columns_source
        if loaded and self.table_info is not None:
            lines.append(self._table_text(self.table_info))
            if self.staged_uri and self.source == self.LOCAL:
                lines.append(f"Uploaded to {self.staged_uri}.")
        elif not location:
            lines.append("Choose a file on this computer or type a URL.")
        elif estimate is not None:
            lines.append(estimate.describe() + " (Rough estimate.)")
        elif self.source == self.URL:
            lines.append("The memory estimate is made when you click Load.")
        too_big = self.estimate_too_big() and not loaded
        if too_big:
            self.Warning.too_big(self._too_big_text(estimate, self.machine_memory()))
        self.info_label.setText("\n".join(lines))
        self.load_button.setText("Load anyway" if too_big else "Load")
        self.load_button.setEnabled(bool(location) and not self.Error.source_error.is_shown())
        # The Cloud settings' machine size picker suggests a size that fits.
        name = urlsplit(location).path.rsplit("/", 1)[-1] or location
        hint = estimate and SizeNeed(
            estimate.needed_low, estimate.needed_high, estimate.disk_gb, f"{name} (Cloud File)"
        )
        size_hints().set(self, hint if location else None)

    @staticmethod
    def _table_text(info: TableInfo) -> str:
        n_columns = info.n_attributes + info.n_class_vars + info.n_metas
        parts = [f"{info.n_attributes} feature{'s' if info.n_attributes != 1 else ''}"]
        if info.n_class_vars:
            parts.append(f"{info.n_class_vars} target{'s' if info.n_class_vars != 1 else ''}")
        if info.n_metas:
            parts.append(f"{info.n_metas} meta")
        return (
            f"{info.n_rows:,} rows, {n_columns} columns ({', '.join(parts)}).\n"
            f"Memory used on the machine: {format_bytes(info.memory_bytes)}."
        )

    # ------------------------------------------------------------------
    # Loading

    def on_cloud_connected(self) -> None:
        self._update_provider_texts()
        self._show_estimate()
        if self.data is None and self.task is None:
            self._auto_load()

    def _auto_load(self) -> None:
        """Load the saved source when the workflow opens, reusing the saved
        table if the same machine still has it. Doesn't load a file that is
        probably too big."""
        if self.location and self.session.is_connected:
            self.load(auto=True)

    def url_schemes(self) -> tuple[str, ...]:
        """The URL schemes the machine loads: the web's and those of its
        provider's storage (e.g. ``gs://``)."""
        provider = self.cloud_provider()
        return WEB_SCHEMES + (provider.storage_schemes if provider is not None else ())

    def _source_problem(self) -> str | None:
        """Why the chosen source can't be loaded (``None``: it can)."""
        location = self.location
        if self.source == self.URL and (problem := url_problem(location, self.url_schemes())):
            return problem
        try:
            file_format(location)
        except ValueError as exc:
            return str(exc)
        return None

    def load(
        self, auto: bool = False, force: bool = False, create_bucket: str | None = None
    ) -> bool:
        """Load the chosen file on the machine (the Load button).

        A file that is probably too big for the machine isn't loaded until
        the button, then "Load anyway", is clicked again (or ``force``).
        ``create_bucket``: the storage bucket the user agreed to create.
        """
        force = force or self.load_button.text() == "Load anyway"
        self._update_estimate()
        if not self.location or self.Error.source_error.is_shown():
            return False
        if problem := self._source_problem():
            self.Error.source_error(problem)
            return False
        if self.estimate_too_big() and (auto or not force):
            return False
        if not self.session.is_connected:
            self.Error.not_connected()
            return False
        request = self._load_request(force, create_bucket)
        previous = self.table_id if self.table_machine_id == request.machine_id else ""
        reuse = previous if auto and self.columns_source == request.location else ""
        if previous and not reuse:
            # The previous table is deleted before loading, so it's freed first.
            self.data = None
            self.Outputs.data.send(None)
        self.Warning.load_warnings.clear()
        return self.run_cloud(lambda task: self._load_work(task, request, previous, reuse))

    def _load_request(self, force: bool, create_bucket: str | None) -> _LoadRequest:
        location, source, session = self.location, self.source, self.session
        same_columns = self.columns_source == location
        return _LoadRequest(
            location=location,
            path=self.local_file() if source == self.LOCAL else location,
            source=source,
            specs=column_specs(self.column_rows, self.inferred_rows) if same_columns else [],
            machine_id=session.handle.machine_id,
            memory=self.machine_memory(),
            estimate=self.estimate if self._estimate_location == location else None,
            upload=self._upload,
            provider=session.machine_provider,
            handle=session.handle,
            force=force,
            create_bucket=create_bucket,
        )

    def _load_work(
        self, task: CloudTask, request: _LoadRequest, previous: str, reuse: str
    ) -> _TooBig | _NeedsBucket | _Loaded:
        """In the worker thread: reuse the saved table, or estimate, upload
        and load the file."""
        client = task.client
        if reuse:
            try:
                info = client.table_info(reuse)
                domain = client.table_domain(reuse)
            except CloudNotFoundError:
                log.info("The saved table %s is gone; loading the file again", reuse)
            else:
                table = RemoteTable.from_info(info, domain, request.machine_id)
                return _Loaded(table, info, [], request.location, request.estimate, request.upload)
        if request.estimate is None and request.source == self.URL:
            request.estimate = est = estimate_remote(task, request.location, request.provider)
            memory = request.memory
            too_big = est is not None and memory is not None and not est.fits(memory)
            if too_big and not request.force:
                return _TooBig(est, request.location)
        if previous:
            try:
                client.delete_table(previous)
            except CloudNotFoundError:
                log.info("The previous table %s was already gone", previous)
        try:
            return self._load_on_machine(task, request)
        except StorageMissingError as exc:
            return _NeedsBucket(exc.bucket, str(exc), request.force)

    @staticmethod
    def _send_local_file(task: CloudTask, request: _LoadRequest) -> tuple[dict, str | None]:
        """In the worker thread: put a local file where the machine reads it.
        Returns ``create_table``'s arguments and the file's URI (e.g.
        ``gs://…``) if it went to the provider's storage."""
        provider = request.provider
        if provider is not None and provider.stores_uploads:
            task.set_status(f"Uploading the file to {provider.label} storage")
            uri = provider.stage_file(
                request.handle,
                request.path,
                on_progress=upload_progress(task, 0.5),
                create_bucket=request.create_bucket,
                on_status=task.set_status,
            )
            if uri is not None:
                return machine_source(provider, uri, request.handle), uri
        stat = os.stat(request.path)
        key = (request.machine_id, os.path.abspath(request.path), stat.st_size, stat.st_mtime_ns)
        if request.upload is None or request.upload[0] != key:
            task.set_status("Uploading the file to the machine")
            uploaded = task.client.upload(request.path, on_progress=upload_progress(task, 0.5))
            request.upload = (key, uploaded.id)
        return {"upload_id": request.upload[1]}, None

    def _load_on_machine(self, task: CloudTask, request: _LoadRequest) -> _Loaded:
        """In the worker thread: upload a local file (unless it's already
        there), load it, and fetch the new table's domain."""
        client = task.client
        low, staged = 0.0, None
        if request.source == self.LOCAL:
            upload = request.upload
            source, staged = self._send_local_file(task, request)
            if staged is not None or request.upload is not upload:
                low = 0.5  # the upload had the first half of the progress bar
        else:
            source = machine_source(request.provider, request.location, request.handle)
        job = client.create_table(TableCreateRequest(columns=request.specs, **source))
        task.set_status("Loading the file on the machine")

        def on_job_progress(fraction: float, text: str) -> None:
            task.set_progress(low + (1.0 - low) * fraction)
            if text:
                task.set_status(text)

        result = client.wait_for_job(job, on_job_progress, should_cancel=task.is_cancelled)
        task.job_results.append(result)
        info = result.tables["table"]
        domain = client.table_domain(info.id)
        table = RemoteTable.from_info(info, domain, request.machine_id)
        warnings = [str(w) for w in result.values.get("warnings") or []]
        return _Loaded(
            table, info, warnings, request.location, request.estimate, request.upload, staged
        )

    def confirm_create_bucket(self, message: str) -> bool:
        """Ask the user whether to create the storage (tests replace it)."""
        answer = QMessageBox.question(
            self,
            "Storage for uploaded files",
            message + "\n\nThe files you load from this computer are kept there for later "
            "sessions, until you delete them (Uploaded files…). Storage costs a little "
            "per GB and month.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_needs_bucket(self, result: _NeedsBucket) -> None:
        if self.confirm_create_bucket(result.message):
            # Not from here: a task can't start while the last one finishes.
            QTimer.singleShot(0, lambda: self.load(force=result.force, create_bucket=result.bucket))
        else:
            where = provider.label if (provider := self.cloud_provider()) else "the cloud"
            self.Error.source_error(
                f"Files from this computer need storage on {where}. Load again to "
                f"create it, or use a URL that starts with {or_list(list(self.url_schemes()))}."
            )

    def on_cloud_result(self, result: _TooBig | _NeedsBucket | _Loaded) -> None:
        if isinstance(result, _NeedsBucket):
            self._on_needs_bucket(result)
            return
        if isinstance(result, _TooBig):
            if result.location == self.location:
                self.estimate, self._estimate_location = result.estimate, result.location
            self._show_estimate()
            return
        if result.upload is not None:
            self._upload = result.upload
        if result.estimate is not None and result.location == self.location:
            self.estimate, self._estimate_location = result.estimate, result.location
        handle = self.session.handle
        self.table_id = result.info.id
        self.table_machine_id = handle.machine_id if handle is not None else ""
        self.data, self.table_info = result.table, result.info
        self.staged_uri = result.staged_uri
        self._update_column_rows(result)
        if result.warnings:
            self.Warning.load_warnings("\n".join(result.warnings))
        self._show_estimate()
        self._update_provider_texts()
        self.Outputs.data.send(result.table)

    def _update_column_rows(self, result: _Loaded) -> None:
        loaded_rows = rows_from_domain(result.table.domain)
        if self.columns_source != result.location or not self.column_rows:
            self.columns_source = result.location
            self.inferred_rows = loaded_rows
            self.column_rows = [dict(r) for r in loaded_rows]
        else:
            # Show the values of categorical columns as they are now.
            values = {r["name"]: r["values"] for r in loaded_rows}
            for row in self.column_rows:
                row["values"] = values.get(row["name"], row["values"])
        self._set_editor_rows()

    # ------------------------------------------------------------------
    # Uploaded files

    def _storage_provider(self) -> ClusterProvider | None:
        """The provider that keeps uploaded files, if the machine's (or the
        chosen) provider does."""
        provider = self.cloud_provider()
        return provider if provider is not None and provider.stores_uploads else None

    def _update_provider_texts(self) -> None:
        """The texts that name the provider's storage, and the "Uploaded
        files…" button if the provider keeps them."""
        storage = self._storage_provider()
        self.files_button.setVisible(storage is not None)
        if storage is not None:
            self.files_button.setToolTip(
                f"The files uploaded from this computer to {storage.label} storage, "
                "where you can delete them."
            )
        schemes, provider = self.url_schemes(), self.cloud_provider()
        examples = ["https://example.com/data.csv.gz"]
        examples += provider.storage_examples() if provider is not None else []
        self.url_edit.setPlaceholderText(" or ".join(examples))
        self.url_edit.setToolTip(
            f"A URL that starts with {or_list(list(schemes))}. Several files with the same "
            "columns load as one table with a numbered pattern, e.g. …/data-{01..12}.parquet."
        )

    def show_uploaded_files(self) -> UploadedFilesDialog | None:
        """Open the list of uploaded files, where they can be deleted."""
        provider = self._storage_provider()
        if provider is None:
            return None
        dialog = UploadedFilesDialog(provider, self.session.handle, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    def cloud_status_text(
        self, info: InfoResponse | None, handle: MachineHandle | None, runtime: float | None
    ) -> str:
        return computed_with(info, handle, runtime).replace("Computed", "Loaded", 1)


if __name__ == "__main__":  # pragma: no cover
    WidgetPreview(OWCloudFile).run()
