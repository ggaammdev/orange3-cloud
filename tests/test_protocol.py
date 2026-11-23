import importlib
import inspect
import io
import pickle
import pkgutil
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from orangecontrib.cloud import protocol
from orangecontrib.cloud.protocol import pickles
from orangecontrib.cloud.protocol.endpoints import ENDPOINTS
from orangecontrib.cloud.protocol.errors import (
    ERROR_HTTP_STATUS,
    OUT_OF_MEMORY_MESSAGE,
    ErrorCode,
    ErrorResponse,
)
from orangecontrib.cloud.protocol.jobs import (
    JobInfo,
    JobKind,
    JobRequest,
    JobResult,
    JobState,
    ObjectInfo,
    ObjectKind,
)
from orangecontrib.cloud.protocol.machine import (
    DeleteResponse,
    InfoResponse,
    InstallFailure,
    ShutdownResponse,
)
from orangecontrib.cloud.protocol.pickles import (
    BY_NAME_CLASSES,
    BY_NAME_MODULES,
    class_by_name,
    dump,
    dumps,
    load,
    loads,
)
from orangecontrib.cloud.protocol.tables import (
    ColumnRole,
    ColumnSpec,
    ColumnType,
    DeriveRequest,
    RowsParams,
    StatsKind,
    StatsRequest,
    TableCreateRequest,
    TableFormat,
    TableInfo,
    TableList,
    UploadInfo,
    UploadParams,
)
from orangecontrib.cloud.protocol.wire import API_PREFIX

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)

TABLE = TableInfo(
    id="t-1",
    name="higgs",
    n_rows=1_000_000,
    n_attributes=28,
    n_class_vars=1,
    n_metas=0,
    memory_bytes=232_000_000,
    peak_load_memory_bytes=400_000_000,
    source="gs://bucket/higgs.csv.gz",
    format=TableFormat.CSV,
    created_at=NOW,
)
OBJECT = ObjectInfo(
    id="o-1",
    kind=ObjectKind.DISTANCES,
    type="Orange.misc.DistMatrix",
    size_bytes=8 * 100,
    shape=[10, 10],
    created_at=NOW,
)
ERROR = ErrorResponse(
    code=ErrorCode.OUT_OF_MEMORY,
    message=OUT_OF_MEMORY_MESSAGE,
    remote_traceback="Traceback (most recent call last): ...",
)

# One example per model in protocol/.
EXAMPLES: list[BaseModel] = [
    ERROR,
    InstallFailure(name="foo", version="1.0", error="no wheel"),
    InfoResponse(
        server_version="1.0.0",
        python_version="3.12.13",
        packages={"orange3": "3.41.0"},
        install_failures=[InstallFailure(name="foo", version="1.0", error="x")],
        machine="n2-standard-16",
        cpu_count=16,
        memory_total_bytes=64 << 30,
        memory_free_bytes=60 << 30,
        memory_tables_bytes=1 << 30,
        disk_total_bytes=100 << 30,
        disk_free_bytes=90 << 30,
        uptime_seconds=12.5,
        idle_seconds=3.0,
        idle_timeout_seconds=1800,
        max_lifetime_seconds=43200,
        idle_time_left_seconds=1797.0,
        lifetime_left_seconds=43187.5,
        time_left_seconds=1797.0,
        jobs_running=1,
    ),
    ShutdownResponse(message="bye"),
    DeleteResponse(id="t-1"),
    UploadParams(filename="data.csv.gz"),
    UploadInfo(id="u-1", filename="data.csv.gz", size_bytes=123, created_at=NOW),
    ColumnSpec(name="y", role=ColumnRole.TARGET, type=ColumnType.CATEGORICAL),
    TableCreateRequest(
        uri="gs://bucket/data.parquet",
        format=TableFormat.PARQUET,
        name="d",
        columns=[ColumnSpec(name="id", role=ColumnRole.SKIP)],
    ),
    TABLE,
    TableList(tables=[TABLE]),
    RowsParams(start=1000, stop=2000, sort="age", descending=True),
    DeriveRequest(name="filtered", steps_key="0123abcd"),
    StatsRequest(kind=StatsKind.DISTRIBUTION, columns=[0, -1], group_by=4, binning=2, fit="norm"),
    OBJECT,
    JobRequest(
        kind=JobKind.EVALUATE,
        inputs={"data": "t-1", "models": ["o-1", "o-2"]},
        params={"folds": 10, "stratified": True, "nested": {"a": [1, 2.5, None]}},
        parts=["learners"],
    ),
    JobInfo(
        id="j-1",
        kind=JobKind.FIT,
        state=JobState.FAILED,
        progress=0.5,
        status="Fold 5 of 10",
        error=ERROR,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    ),
    JobResult(
        job_id="j-1",
        kind="evaluate",
        tables={"table": TABLE},
        objects={"results": OBJECT},
        values={"scores": {"CA": [0.9, 0.8]}},
        runtime_seconds=3.2,
    ),
]


def protocol_modules():
    return [
        importlib.import_module(f"{protocol.__name__}.{module.name}")
        for module in pkgutil.iter_modules(protocol.__path__)
    ]


def protocol_models():
    return [
        cls
        for module in protocol_modules()
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, BaseModel) and cls.__module__ == module.__name__
    ]


def test_every_model_round_trips():
    covered = {type(e) for e in EXAMPLES}
    missing = [m.__name__ for m in protocol_models() if m not in covered]
    assert not missing, f"add examples for {missing}"
    for example in EXAMPLES:
        again = type(example).model_validate_json(example.model_dump_json())
        assert again == example


def test_table_create_needs_one_source():
    assert TableCreateRequest(uris=["gs://b/1.csv", "gs://b/2.csv"]).locations == [
        "gs://b/1.csv", "gs://b/2.csv",
    ]  # fmt: skip
    assert TableCreateRequest(upload_id="u-1").locations is None
    for kwargs in (
        {},
        {"uri": "gs://b/x.csv", "upload_id": "u-1"},
        {"uri": "gs://b/x.csv", "uris": ["gs://b/y.csv"]},
        {"uris": []},
    ):
        try:
            TableCreateRequest(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(kwargs)


def test_docs_cover_every_endpoint():
    doc = (Path(__file__).parents[1] / "docs" / "api-v1.md").read_text()
    missing = [e.key for e in ENDPOINTS if f"`{e.key}`" not in doc]
    assert not missing
    assert len({e.key for e in ENDPOINTS}) == len(ENDPOINTS)
    for e in ENDPOINTS:
        assert e.path.startswith(API_PREFIX + "/")
        if e.pickle_parts:
            assert e.request_body == "multipart"


def test_every_error_code_has_a_status_and_docs():
    doc = (Path(__file__).parents[1] / "docs" / "api-v1.md").read_text()
    for code in ErrorCode:
        assert code in ERROR_HTTP_STATUS
        assert f"| `{code.value}` | {ERROR_HTTP_STATUS[code]} |" in doc
    assert ERROR_HTTP_STATUS[ErrorCode.WORKER_CRASHED] == 500


#: Imports every module of protocol/ in a new Python process.
IMPORT_ALL = """
import importlib, pkgutil
from orangecontrib.cloud import protocol
for module in pkgutil.iter_modules(protocol.__path__):
    importlib.import_module(f"{protocol.__name__}.{module.name}")
"""


def test_imports_without_server_dependencies():
    code = (
        "import sys\n"
        "for m in ('fastapi', 'starlette', 'uvicorn', 'distributed', 'httpx'):\n"
        "    sys.modules[m] = None\n"
    )
    subprocess.run([sys.executable, "-c", code + IMPORT_ALL], check=True)


def test_imports_without_qt():
    """protocol/ imports no Qt and none of Orange's widget code, which need
    Qt: every module imports where they can't be imported, as on the machine."""
    no_qt = textwrap.dedent(
        """
        import importlib.abc, sys

        QT = ("AnyQt", "PyQt5", "PyQt6", "PySide2", "PySide6", "orangewidget", "Orange.widgets")

        class NoQt(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if any(name == qt or name.startswith(qt + ".") for qt in QT):
                    raise ImportError(f"no Qt: {name}")

        sys.meta_path.insert(0, NoQt())
        """
    )
    subprocess.run([sys.executable, "-c", no_qt + IMPORT_ALL], check=True)


def test_class_by_name_imports_only_its_modules(monkeypatch):
    imported = []

    def import_module(name):
        imported.append(name)
        return importlib.import_module(name)

    monkeypatch.setattr(pickles, "importlib", SimpleNamespace(import_module=import_module))
    with pytest.raises(pickle.UnpicklingError):
        class_by_name("Orange.widgets.data.owselectrows.OWSelectRows")
    assert imported == []
    for name in BY_NAME_CLASSES:
        assert class_by_name(name).__qualname__ == name.rpartition(".")[2]
    assert set(imported) == BY_NAME_MODULES


def test_dump_and_load_files():
    """The file form pickles as the bytes form, with the same references."""
    from Orange.data import ContinuousVariable

    x = ContinuousVariable("x")
    refs = {"columns": [x]}
    file = io.BytesIO()
    dump([x, 1], file, refs)
    assert file.getvalue() == dumps([x, 1], refs)
    file.seek(0)
    loaded = load(file, refs)
    assert loaded[0] is x and loaded == loads(dumps([x, 1], refs), refs)


def test_file_names_without_extensions():
    """The machine's default table names and Cloud File's pattern names
    strip the same extensions."""
    from orangecontrib.cloud.protocol.tables import without_extensions
    from orangecontrib.cloud.server.loader import default_name
    from orangecontrib.cloud.widgets.file_sources import pattern_name

    assert without_extensions("Trips.CSV.GZ") == "Trips"
    assert without_extensions("y.parquet") == "y" and without_extensions("notes.md") == "notes.md"
    assert default_name("https://x.org/a/m-01.tsv.gzip?sig=a/b") == "m-01"
    assert pattern_name("s3://b/trips_2025-{01..12}.parquet") == "trips_2025-{01..12}"
