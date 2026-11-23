"""Cloud File and Cloud Data Table, against the machine's server running
in-process over HTTPS, plus the memory estimate.

``TestCloudFileMillion`` loads about 1M rows: ``pytest -m slow tests/test_cloud_file.py``.
"""

import functools
import gzip
import http.server
import shutil
import tempfile
import threading
from pathlib import Path

import numpy as np
import pytest
from AnyQt.QtCore import Qt
from conftest import SPEC, https_server
from Orange.data import ContinuousVariable, DiscreteVariable, StringVariable, Table
from Orange.widgets.tests import base  # not WidgetTest itself: pytest would collect it
from test_widget_base import TOKEN, SharedMachineProvider

from orangecontrib.cloud.client.providers.gcp import OwnGCPProvider
from orangecontrib.cloud.client.providers.gcp_vm import MACHINE_TYPES
from orangecontrib.cloud.client.providers.sizes import GIB
from orangecontrib.cloud.client.session import CloudSession, HandleStore
from orangecontrib.cloud.data.estimate import estimate_local_file, file_format
from orangecontrib.cloud.data.remote_table import is_cloud_table
from orangecontrib.cloud.gui.machine_size import MachineSizePicker, size_hints
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app
from orangecontrib.cloud.settings import ProviderSettings
from orangecontrib.cloud.widgets.file_columns import column_specs
from orangecontrib.cloud.widgets.file_sources import expand_pattern, url_problem
from orangecontrib.cloud.widgets.owclouddatatable import OWCloudDataTable
from orangecontrib.cloud.widgets.owcloudfile import OWCloudFile


def write_csv(path, n_rows):
    rng = np.random.default_rng(0)
    x = rng.normal(10, 5, size=(n_rows, 2)).round(4).astype(str)
    color = rng.choice(np.array(["red", "green", "blue"]), n_rows)
    label = rng.choice(np.array(["no", "yes"]), n_rows)
    ids = np.char.add("row", np.arange(n_rows).astype(str))
    with open(path, "w") as f:
        f.write("x1,x2,color,label,id\n")
        np.savetxt(f, np.column_stack([x, color, label, ids]), fmt="%s", delimiter=",")


#: A file with Orange's three header rows: names, types and flags. ``grade``
#: lists its values ("z" isn't one: missing), ``note`` is text flagged as the
#: class (a meta in Orange), ``size`` and ``flag`` have no type (inferred),
#: ``w`` holds weights (skipped), and " blue " loses its spaces.
ORANGE_HEADER_ROWS = [
    ["x", "grade", "color", "size", "when", "note", "skip", "w", "flag", "cls"],
    ["c", "b a c", "d", "", "t", "s", "c", "c", "", "d"],
    ["unit=cm", "", "meta", "", "", "class", "i", "w", "", "class"],
    ["1.5", "a", "red", "10", "2020-01-02", "first", "1", "1", "0", "yes"],
    ["?", "b", "green", "", "2020-03-04", "", "2", "1", "1", "no"],
    ["3.25", "c", "", "12.5", "", "third", "3", "2", "1", "yes"],
    ["4", "z", "red", "7", "2021-12-31", "fourth", "4", "2", "0", ""],
    ["-2", "a", " blue ", "8", "2020-01-02", "fifth", "5", "1", "1", "no"],
]


def write_orange_header_file(path, delimiter):
    path.write_text("\n".join(delimiter.join(row) for row in ORANGE_HEADER_ROWS) + "\n")


def assert_same_table(cloud, orange):
    """Same domain (types, roles, values, attributes) and rows as Orange's."""
    for part in ("attributes", "class_vars", "metas"):
        mine, theirs = getattr(cloud.domain, part), getattr(orange.domain, part)
        assert [v.name for v in mine] == [v.name for v in theirs], part
        for a, b in zip(mine, theirs, strict=True):
            assert type(a) is type(b), a.name
            assert getattr(a, "values", None) == getattr(b, "values", None), a.name
            assert a.attributes == b.attributes, a.name
    assert cloud.domain == orange.domain
    assert len(cloud) == len(orange)
    np.testing.assert_array_equal(cloud.X, orange.X)
    np.testing.assert_array_equal(cloud._Y, orange._Y)
    assert cloud.metas.astype(str).tolist() == orange.metas.astype(str).tolist()


# --------------------------------------------------------------------------
# Memory estimate


def test_estimate_csv_and_gzip(tmp_path):
    csv = tmp_path / "data.csv"
    write_csv(csv, 50_000)
    est = estimate_local_file(csv)
    assert est.format == "csv" and not est.gzip and est.columns == 5
    assert 45_000 < est.rows < 55_000 and not est.exact_rows
    # 4 columns of 8 bytes; the IDs are text with many values.
    assert 50_000 * 4 * 8 < est.table_bytes < 50_000 * 120
    assert est.needed_low == 4 * est.table_bytes and est.needed_high == 6 * est.table_bytes

    small = tmp_path / "small.csv.gz"
    with gzip.open(small, "wt") as f:
        f.write("a;b\n1;x\n2;y\n")
    est = estimate_local_file(small)
    assert est.gzip and est.exact_rows and est.rows == 2 and est.table_bytes == 2 * 2 * 8
    assert file_format("https://x.org/d.parquet?raw=1") == ("parquet", False)
    with pytest.raises(ValueError):
        file_format("data.xlsx")


def test_estimate_suggests_a_machine(tmp_path):
    big = tmp_path / "big.csv"
    write_csv(big, 2000)
    with open(big, "r+b") as f:  # a sparse file: the size of 20 GB, the rows of 2000
        f.truncate(20 * 1024**3)
    est = estimate_local_file(big)
    assert est.rows > 10**8
    assert not est.fits(16 * GIB)
    suggested = est.suggested_machine_type(MACHINE_TYPES)
    assert suggested is not None and suggested.memory_gb * GIB >= est.needed_low
    assert est.disk_gb == 100  # 20 GB file + 30 GB for the system, at least the default


def test_column_specs():
    inferred = [
        {"name": "a", "type": "numeric", "role": "feature"},
        {"name": "b", "type": "categorical", "role": "feature"},
        {"name": "c", "type": "text", "role": "meta"},
    ]
    rows = [
        {"name": "a", "type": "numeric", "role": "skip"},
        {"name": "b", "type": "categorical", "role": "target"},
        {"name": "c", "type": "text", "role": "meta"},
    ]
    specs = [s.model_dump(exclude_none=True) for s in column_specs(rows, inferred)]
    assert specs == [{"name": "a", "role": "skip"}, {"name": "b", "role": "target"}]


# --------------------------------------------------------------------------
# The widgets on the in-process machine


def test_patterns_name_several_files():
    assert expand_pattern("https://x.org/a.csv") == ["https://x.org/a.csv"]
    assert expand_pattern("https://x.org/t_2025-{01..03}.parquet") == [
        f"https://x.org/t_2025-0{i}.parquet" for i in (1, 2, 3)
    ]
    assert expand_pattern("gs://b/{n,s}/{9..10}.csv") == [
        "gs://b/n/9.csv", "gs://b/n/10.csv", "gs://b/s/9.csv", "gs://b/s/10.csv",
    ]  # fmt: skip
    schemes = ("https://",)
    assert url_problem("https://x.org/{01..12.csv", schemes).startswith("Use {01..12}")
    assert "at most 1,000" in url_problem("https://x.org/{1..40}{1..40}.csv", schemes)
    assert "all be CSV" in url_problem("https://x.org/a.{csv,parquet}", schemes)
    assert url_problem("https://x.org/{a,b}.csv", schemes) is None


class TestCloudFile(base.WidgetTest):
    N_ROWS = 2_000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = folder = Path(tempfile.mkdtemp())
        cls.tear_down_stack.callback(shutil.rmtree, folder, ignore_errors=True)
        config = ServerConfig(
            token=TOKEN, machine="test-machine", data_dir=folder / "machine", worker_threads=2
        )
        url, cert, _ = cls.tear_down_stack.enter_context(
            https_server(create_machine_app(config), folder)
        )
        cls.provider = SharedMachineProvider(url, cert)
        cls.csv = folder / "data.csv"
        write_csv(cls.csv, cls.N_ROWS)

    def setUp(self):
        super().setUp()
        self.state = Path(tempfile.mkdtemp())
        self.session = CloudSession(self.provider, store=HandleStore(self.state / "m.json"))
        self._previous, CloudSession._instance = CloudSession._instance, self.session
        self.session.start_machine(spec=SPEC, wait=True)
        self.process_events()

    def tearDown(self):
        super().tearDown()
        self.session.disconnect(forget=True, wait=True)
        self.session.shutdown()
        CloudSession._instance = self._previous
        shutil.rmtree(self.state, ignore_errors=True)

    def load(self, w, timeout=60000):
        assert w.load() or w.task is not None
        self.wait_until_finished(w, timeout=timeout)
        assert not w.Error.active, [str(m) for m in w.Error.active]
        return self.get_output(w.Outputs.data, widget=w)

    def test_load_view_and_change_target(self):
        client = self.session.client
        w = self.create_widget(OWCloudFile)
        w.set_local_path(str(self.csv))
        assert w.estimate is not None and w.load_button.text() == "Load"
        data = self.load(w)
        assert is_cloud_table(data) and data.ref.n_rows == self.N_ROWS
        assert data.name == f"data ({self.N_ROWS:,} rows, in the cloud)"
        assert data.domain.class_var is None
        assert f"{self.N_ROWS:,} rows, 5 columns" in w.info_label.text()
        assert w.cloud_status.startswith("Loaded with Orange")
        first_id = data.ref.table_id

        # Cloud Data Table: real rows and the exact row count; sorted on the machine.
        table = self.create_widget(OWCloudDataTable)
        table.rows_per_page = 500
        self.send_signal(table.Inputs.data, data, widget=table)
        self.wait_until_finished(table, timeout=20000)
        assert not table.Error.active
        assert table.n_rows == self.N_ROWS and len(table.model.page) == 500
        assert f"{self.N_ROWS:,} rows, 5 columns" in table.info_label.text()
        assert table.page_label.text() == f"Rows 1–500 of {self.N_ROWS:,}"
        ids = [table.model.data(table.model.index(r, 0)) for r in range(3)]
        assert ids == ["row0", "row1", "row2"]  # meta first (no target yet)
        table.last_page()
        self.wait_until_finished(table, timeout=20000)
        last = (self.N_ROWS - 1) // 500 * 500
        assert table.page_label.text() == f"Rows {last + 1:,}–{self.N_ROWS:,} of {self.N_ROWS:,}"
        assert table.model.headerData(0, Qt.Orientation.Vertical) == f"{last + 1:,}"
        table.sort_by("x1", descending=True)
        self.wait_until_finished(table, timeout=20000)
        x1 = table.model.page.get_column("x1")
        assert table.page_start == 0 and np.all(np.diff(x1) <= 0)
        assert x1[0] == np.loadtxt(self.csv, delimiter=",", skiprows=1, usecols=0).max()

        # Make "label" the target: the file is loaded again, without uploading it.
        rows = w.columns_model.variables
        label = next(r for r in rows if r[0] == "label")
        label[2] = 1  # Place.class_var
        w._columns_edited()
        assert w.apply_button.isEnabled()
        uploads = len(list((self.folder / "machine" / "uploads").iterdir()))
        w.apply_columns()
        self.wait_until_finished(w, timeout=60000)
        data = self.get_output(w.Outputs.data, widget=w)
        assert data.domain.class_var.name == "label"
        assert isinstance(data.domain.class_var, DiscreteVariable)
        assert data.ref.n_rows == self.N_ROWS and data.ref.table_id != first_id
        assert len(list((self.folder / "machine" / "uploads").iterdir())) == uploads
        assert first_id not in [t.id for t in client.list_tables()]  # freed
        assert w.column_rows[3]["role"] == "target" and not w.apply_button.isEnabled()

        # The workflow saves the source and the roles; a new widget reuses the table.
        settings = w.settingsHandler.pack_data(w)
        assert settings["local_path"] == str(self.csv)
        assert not any("token" in str(v).lower() for v in settings.values())
        w2 = self.create_widget(OWCloudFile, stored_settings=settings)
        self.wait_until_finished(w2, timeout=20000)
        again = self.get_output(w2.Outputs.data, widget=w2)
        assert again.ref.table_id == data.ref.table_id
        assert again.domain.class_var.name == "label"

        # Type changes go to the machine too: "color" as text (a meta column).
        color = next(r for r in w.columns_model.variables if r[0] == "color")
        color[1], color[2] = StringVariable, 2  # Place.meta
        w.apply_columns()
        self.wait_until_finished(w, timeout=60000)
        data = self.get_output(w.Outputs.data, widget=w)
        assert isinstance(data.domain["color"], StringVariable)
        assert data.domain["color"] in data.domain.metas
        assert isinstance(data.domain["x1"], ContinuousVariable)

    def test_warns_before_loading_a_file_too_big(self):
        big = self.folder / "big.csv"
        write_csv(big, 2000)
        with open(big, "r+b") as f:  # sparse: 50 GB on paper, 2000 real rows
            f.truncate(50 * 1024**3)
        tables = len(self.session.client.list_tables())
        w = self.create_widget(OWCloudFile)
        w.set_local_path(str(big))
        memory = w.machine_memory()
        assert memory is not None and not w.estimate.fits(memory)
        assert w.Warning.too_big.is_shown()
        text = str(w.Warning.too_big)
        # The test machine's provider has no sizes, and neither has the chosen one.
        assert "probably needs" in text and "Use a cloud machine with at least" in text
        assert "boot disk of at least" in text and "rough" in text
        assert w.load_button.text() == "Load anyway"
        # Opening a workflow doesn't load it.
        assert not w.load(auto=True) and w.task is None
        assert len(self.session.client.list_tables()) == tables

        # The machine size picker in the Cloud settings suggests a size that fits.
        need = size_hints().largest()
        assert need.label == "big.csv (Cloud File)" and need.needed_low == w.estimate.needed_low
        picker = MachineSizePicker(OwnGCPProvider(), ProviderSettings(), lambda: None)
        picker.set_region("us-central1", None)
        suggested, _ = picker.suggestion()
        assert suggested is not None
        assert suggested == w.estimate.suggested_machine_type(MACHINE_TYPES)
        assert suggested.memory_gb * GIB >= w.estimate.needed_low
        assert picker.combo.itemText(picker.combo.findData(suggested.name)).endswith(
            "fits your data"
        )
        assert "big.csv (Cloud File) probably needs" in picker.suggestion_label.text()
        assert "The chosen size is probably too small." in picker.suggestion_label.text()
        w.onDeleteWidget()
        assert size_hints().largest() is None and not picker.use_button.isVisibleTo(None)

    def test_orange_header_files(self):
        """A .tab file and an Orange-style CSV load as Orange's File widget
        reads them; the roles and types set in the widget still win."""
        tab, csv = self.folder / "orange.tab", self.folder / "orange.csv"
        write_orange_header_file(tab, "\t")
        write_orange_header_file(csv, ",")
        w = self.create_widget(OWCloudFile)
        for path in (tab, csv):
            w.set_local_path(str(path))
            assert w.estimate.rows == 5 and w.estimate.exact_rows
            data = self.load(w)
            orange = Table.from_file(str(path))
            assert_same_table(self.session.client.table_rows(data.ref.table_id), orange)
            assert data.domain == orange.domain and data.domain.class_var.name == "cls"
            assert data.domain["grade"].values == ("b", "a", "c")
            assert data.domain["note"] in data.domain.metas
            assert data.domain["x"].attributes == {"unit": "cm"}
            assert "skip" not in data.domain and "w" not in data.domain
            assert "Column 'w' holds weights" in str(w.Warning.load_warnings)

        # The widget's roles and types win over the header rows.
        cls = next(r for r in w.columns_model.variables if r[0] == "cls")
        cls[2] = 0  # Place.feature
        note = next(r for r in w.columns_model.variables if r[0] == "note")
        note[1] = DiscreteVariable
        w.apply_columns()
        self.wait_until_finished(w, timeout=60000)
        data = self.get_output(w.Outputs.data, widget=w)
        assert data.domain.class_var is None and data.domain["cls"] in data.domain.attributes
        assert isinstance(data.domain["note"], DiscreteVariable)
        assert data.domain["grade"].values == ("b", "a", "c")

    def test_several_files_from_a_pattern(self):
        """A URL with a numbered pattern loads its files as one table."""
        web = self.folder / "web"
        web.mkdir(exist_ok=True)
        for month in (1, 2, 3):
            write_csv(web / f"part-{month:02}.csv", 100 * month)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=web)
        handler.log_message = lambda *args: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        w = self.create_widget(OWCloudFile)
        w.set_url(f"http://127.0.0.1:{server.server_address[1]}/part-{{01..03}}.csv")
        data = self.load(w)
        assert len(data) == 1 and data.ref.n_rows == 600
        assert w.table_info.name == "part-{01..03}"
        assert w.estimate.n_files == 3 and w.estimate.describe().startswith("3 files:")
        rows = self.session.client.table_rows(data.ref.table_id, 0, 600)
        assert len(rows) == 600

    def test_errors(self):
        w = self.create_widget(OWCloudFile)
        w.set_local_path(str(self.folder / "missing.csv"))
        assert w.Error.source_error.is_shown() and not w.load_button.isEnabled()
        w.set_url("https://user:secret@example.com/data.csv")
        assert not w.load() and "password" in str(w.Error.source_error)
        table = self.create_widget(OWCloudDataTable)
        assert table.info_label.text() == "No data."
        self.send_signal(table.Inputs.data, Table("iris"), widget=table)
        assert "local data, not a cloud table" in str(table.Error.cloud_error)


@pytest.mark.slow
class TestCloudFileMillion(TestCloudFile):
    """The same on about 1M rows."""

    N_ROWS = 1_000_000

    def test_warns_before_loading_a_file_too_big(self):
        pass

    def test_errors(self):
        pass

    def test_orange_header_files(self):
        pass
