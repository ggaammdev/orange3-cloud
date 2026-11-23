"""The base class of the Cloud viewers.

A viewer gets a cloud table (a ``RemoteTable``) and shows statistics that
the machine computes on all its rows (``POST /v1/tables/{id}/stats``), never
anything computed on this computer from the placeholder row. In the worker
thread it resolves the table (the machine replays recorded steps, if any)
and asks for the statistics; the GUI thread draws them.

Writing a viewer::

    class OWCloudSomething(OWCloudViewer):
        @Inputs.data
        def set_data(self, data):
            if self.set_cloud_data(data):  # False for no data or local data
                ...  # set up the variable choices
            self.refresh()

        def refresh(self):
            self.fetch_stats({"basic": StatsRequest(kind=StatsKind.BASIC)}, self.show)

        def show(self, result: ViewerResult):
            ...  # result.stats["basic"], result.info.n_rows
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from Orange.data import Domain, Table

from orangecontrib.cloud.data.remote_table import LOCAL_DATA, is_cloud_table, resolve
from orangecontrib.cloud.protocol.tables import MAX_PAGE_ROWS, StatsRequest, TableInfo
from orangecontrib.cloud.widgets.base import CloudTask, OWCloudBaseWidget

__all__ = ["OWCloudViewer", "ViewerResult", "column_index"]


@dataclass
class ViewerResult:
    """What a viewer gets back from the machine."""

    info: TableInfo  #: the resolved table (``n_rows`` is the number of all rows)
    stats: dict[str, Any] = field(default_factory=dict)  #: answers, by request name
    #: All rows, when asked for and the table has at most that many.
    rows: Table | None = None


def column_index(domain: Domain, var: Any) -> int | None:
    """The variable's index in the domain, as the stats requests take it."""
    return None if var is None else domain.index(var)


def _all_rows(task: CloudTask, table_id: str, n_rows: int) -> Table:
    pages = []
    for start in range(0, n_rows, MAX_PAGE_ROWS):
        pages.append(task.client.table_rows(table_id, start, min(start + MAX_PAGE_ROWS, n_rows)))
    return pages[0] if len(pages) == 1 else Table.concatenate(pages)


class OWCloudViewer(OWCloudBaseWidget, openclass=True):
    """Base class of the Cloud viewers (see the module's documentation)."""

    def __init__(self):
        super().__init__()
        #: The cloud table; ``None`` for no data or local data.
        self.data: Table | None = None
        #: The last result, ``None`` until it arrives.
        self.result: ViewerResult | None = None
        self._pending = False

    def set_cloud_data(self, data: Table | None) -> bool:
        """Take a new input. Returns whether it's a cloud table; for local
        data it shows an error."""
        self.cancel()
        self._clear_cloud_messages()
        self._set_cloud_status("")
        self.result = None
        self.data = data if is_cloud_table(data) else None
        if data is not None and self.data is None:
            self._show_error(self.Error.cloud_error, LOCAL_DATA)
        return self.data is not None

    def refresh(self) -> None:
        """Ask the machine for what the viewer shows. Override."""

    def fetch_stats(
        self,
        requests: Mapping[str, StatsRequest],
        on_done: Callable[[ViewerResult], None],
        *,
        rows_up_to: int = 0,
    ) -> bool:
        """Compute ``requests`` on all rows of the cloud table, off the GUI
        thread, and pass the :class:`ViewerResult` to ``on_done``. With
        ``rows_up_to``, a table with at most that many rows also comes
        whole. Returns False without a cloud table or a machine."""
        data = self.data
        if data is None:
            return False
        handle = self.session.handle
        machine_id = handle.machine_id if handle is not None else None
        requests = dict(requests)

        def work(task: CloudTask) -> ViewerResult:
            task.set_status("Computing on all rows on the machine")
            table_id = resolve(data, task.client, machine_id, wait=task.wait_for_job)
            info = task.client.table_info(table_id)
            stats = {name: task.client.stats(table_id, r) for name, r in requests.items()}
            rows = None
            if 0 < info.n_rows <= rows_up_to:
                task.set_status("Fetching the rows")
                rows = _all_rows(task, table_id, info.n_rows)
            return ViewerResult(info, stats, rows)

        def done(result: ViewerResult) -> None:
            self.result = result
            rows = result.info.n_rows
            self._set_cloud_status(f"From all {rows:,} rows. {self.cloud_status}")
            on_done(result)

        self._pending = not self.run_cloud(work, on_done=done)
        return not self._pending

    def on_cloud_connected(self) -> None:
        """Refresh a view that waited for the machine."""
        if self._pending and self.data is not None:
            self.refresh()
