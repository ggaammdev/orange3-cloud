import functools
import gzip
import http.server
import pickle
import threading
import time

import numpy as np
import pyarrow as pa
import pyarrow.csv as pv
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from Orange.data import (
    ContinuousVariable,
    DiscreteVariable,
    StringVariable,
    Table,
    TimeVariable,
)

from orangecontrib.cloud.protocol.jobs import JobInfo, JobResult, JobState
from orangecontrib.cloud.protocol.machine import DeleteResponse, InfoResponse
from orangecontrib.cloud.protocol.tables import MAX_PAGE_ROWS, TableInfo, TableList, UploadInfo
from orangecontrib.cloud.protocol.wire import PAYLOAD_HEADER, PAYLOAD_PICKLE
from orangecontrib.cloud.server.app import ServerConfig, create_machine_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("machine")


@pytest.fixture(scope="module")
def client(data_dir):
    config = ServerConfig(token=TOKEN, data_dir=data_dir, worker_threads=2)
    with TestClient(create_machine_app(config)) as client:
        yield client


def make_data(n: int, seed: int = 0) -> pa.Table:
    """Numbers, a 0/1 label, a category, unique IDs, timestamps, dates, and
    text with few distinct values at the start but many later."""
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n).round(5)
    x[::97] = np.nan
    color = rng.choice(np.array(["red", "green", "blue"]), n).astype(object)
    color[::101] = None
    return pa.table(
        {
            "x": x,
            "count": rng.integers(0, 1000, n),
            "label": rng.integers(0, 2, n),
            "color": pa.array(color, pa.string()),
            "id": np.char.add("row", np.arange(n).astype(str)),
            "when": np.datetime64("2020-01-01T00:00:00")
            + rng.integers(0, 10**8, n).astype("timedelta64[s]"),
            "day": (np.datetime64("2020-01-01") + np.arange(n) % 1000).astype("datetime64[D]"),
            "code": np.char.add("c", (np.arange(n) // 20).astype(str)),
        }
    )


def upload(client, path, filename=None) -> UploadInfo:
    r = client.post(
        "/v1/uploads",
        params={"filename": filename or path.name},
        headers=AUTH,
        content=path.read_bytes(),
    )
    assert r.status_code == 200, r.text
    return UploadInfo.model_validate(r.json())


def load(client, timeout=60, **request) -> tuple[TableInfo, JobResult]:
    r = client.post("/v1/tables", headers=AUTH, json=request)
    assert r.status_code == 202, r.text
    job = JobInfo.model_validate(r.json())
    assert job.kind == "load_table"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = JobInfo.model_validate(client.get(f"/v1/jobs/{job.id}", headers=AUTH).json())
        if info.state.finished:
            break
        time.sleep(0.05)
    assert info.state == JobState.DONE, info.error
    result = JobResult.model_validate(client.get(f"/v1/jobs/{job.id}/result", headers=AUTH).json())
    return result.tables["table"], result


def get_pickle(client, url, **params):
    r = client.get(url, headers=AUTH, params=params)
    assert r.status_code == 200, r.text
    assert r.headers[PAYLOAD_HEADER] == PAYLOAD_PICKLE
    return pickle.loads(r.content)


@pytest.fixture(scope="module")
def http_dir(tmp_path_factory):
    """A plain local web server, for loading from an http(s) URL."""
    folder = tmp_path_factory.mktemp("web")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder)
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield folder, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_load_csv_gz_parquet(client, data_dir, tmp_path, http_dir):
    n = 3000
    data = make_data(n)
    csv = tmp_path / "data.csv"
    pv.write_csv(data, csv)
    web_folder, web_url = http_dir
    (web_folder / "data.csv.gz").write_bytes(gzip.compress(csv.read_bytes()))
    pq.write_table(data, tmp_path / "data.parquet", row_group_size=1000)
    (web_folder / "data.parquet").write_bytes((tmp_path / "data.parquet").read_bytes())
    (web_folder / "data.csv").write_bytes(csv.read_bytes())

    target = [{"name": "label", "role": "target"}]
    up = upload(client, csv)
    assert up.size_bytes == csv.stat().st_size
    # Azure's signatures are base64: a "/" in the query isn't part of the name.
    signed = f"{web_url}/data.csv.gz?X-Amz-Expires=900&X-Amz-Signature=abc&sig=a/b%2Bc%3D"
    sources = {
        "upload": {"upload_id": up.id},
        "http": {"uri": f"{web_url}/data.csv.gz"},
        "http csv": {"uri": f"{web_url}/data.csv"},  # read as it arrives
        "parquet": {"uri": str(tmp_path / "data.parquet")},
        # Copied to the disk first: Parquet over http(s), and a signed link
        # that may expire before a long load ends.
        "http parquet": {"uri": f"{web_url}/data.parquet"},
        "signed link": {"uri": signed, "copy_first": True},
    }
    pages = {}
    for kind, source in sources.items():
        info, result = load(client, columns=target, **source)
        assert info.n_rows == n, kind
        assert "Signature" not in info.source, kind  # the machine keeps no signatures
        assert (info.n_attributes, info.n_class_vars, info.n_metas) == (5, 1, 2), kind
        assert info.memory_bytes > n * 7 * 8
        assert info.peak_load_memory_bytes is not None
        assert info.name == "data"
        assert result.values["warnings"] == []

        domain = get_pickle(client, f"/v1/tables/{info.id}/domain")
        types = {v.name: type(v) for v in domain.variables + domain.metas}
        assert types == {
            "x": ContinuousVariable,
            "count": ContinuousVariable,
            "color": DiscreteVariable,
            "when": TimeVariable,
            "day": TimeVariable,
            "label": DiscreteVariable,
            "id": StringVariable,
            "code": StringVariable,
        }, kind
        assert domain.class_var.name == "label"
        assert domain["label"].values == ("0", "1")
        assert domain["color"].values == ("blue", "green", "red")
        assert domain["when"].have_time and not domain["day"].have_time

        page = get_pickle(client, f"/v1/tables/{info.id}/rows", start=10, stop=110)
        assert isinstance(page, Table) and len(page) == 100
        assert page.domain == domain
        assert page.metas[0, 0] == "row10"
        pages[kind] = page
        whole = get_pickle(client, f"/v1/tables/{info.id}/rows", stop=n + 5)
        assert len(whole) == n
        # Missing values and the row order survive.
        assert np.isnan(whole.X[::97, 0]).all()
        assert np.isnan(whole.X[::101, domain.index("color")]).all()
        assert whole.metas[5, 0] == "row5"
        assert whole.metas[1500, 1] == "c75"

    # The same rows, whatever the source; the copies are gone.
    for page in pages.values():
        assert list(page.metas[:, 0]) == list(pages["upload"].metas[:, 0])
    assert list((data_dir / "tmp").iterdir()) == []


def test_file_without_header_row(client, http_dir):
    """A gzipped CSV whose first line is data (like HIGGS): the columns are
    named as Orange names them, and no row is lost."""
    rng = np.random.default_rng(1)
    values = np.column_stack([rng.integers(0, 2, 500), rng.normal(size=(500, 4))])
    web_folder, web_url = http_dir
    text = "\n".join(",".join(f"{v:.18e}" for v in row) for row in values) + "\n"
    (web_folder / "plain.csv.gz").write_bytes(gzip.compress(text.encode()))
    (web_folder / "plain.csv").write_text(text)

    target = [{"name": "Feature 1", "role": "target"}]
    info, _ = load(client, uri=f"{web_url}/plain.csv.gz", columns=target)
    rows = get_pickle(client, f"/v1/tables/{info.id}/rows", stop=1000)
    orange = Table.from_file(str(web_folder / "plain.csv"))
    assert len(rows) == len(orange) == 500
    assert [v.name for v in rows.domain.attributes] == [f"Feature {i}" for i in range(2, 6)]
    assert [v.name for v in orange.domain.attributes][1:] == [f"Feature {i}" for i in range(2, 6)]
    assert rows.domain.class_var.name == "Feature 1" and rows.domain.class_var.values == ("0", "1")
    assert np.array_equal(rows.X, orange.X[:, 1:]) and np.array_equal(rows.Y, orange.X[:, 0])


def test_overrides_list_delete_and_info(client, tmp_path):
    csv = tmp_path / "small.csv"
    pv.write_csv(make_data(500), csv)
    info, _ = load(
        client,
        uri=f"file://{csv}",
        name="small",
        columns=[
            {"name": "count", "type": "categorical", "role": "target"},
            {"name": "color", "type": "text"},
            {"name": "x", "role": "meta"},
            {"name": "id", "role": "skip"},
            {"name": "label", "type": "numeric"},
        ],
    )
    domain = get_pickle(client, f"/v1/tables/{info.id}/domain")
    # 25 distinct codes in 500 rows: categorical
    assert [v.name for v in domain.attributes] == ["label", "when", "day", "code"]
    assert isinstance(domain["label"], ContinuousVariable)
    assert isinstance(domain.class_var, DiscreteVariable) and domain.class_var.name == "count"
    assert [type(m) for m in domain.metas] == [ContinuousVariable, StringVariable]

    listed = TableList.model_validate(client.get("/v1/tables", headers=AUTH).json())
    assert info.id in [t.id for t in listed.tables]
    got = client.get(f"/v1/tables/{info.id}", headers=AUTH)
    assert TableInfo.model_validate(got.json()) == info
    machine = InfoResponse.model_validate(client.get("/v1/info", headers=AUTH).json())
    assert machine.memory_tables_bytes >= info.memory_bytes

    r = client.delete(f"/v1/tables/{info.id}", headers=AUTH)
    assert DeleteResponse.model_validate(r.json()).deleted
    for url in (f"/v1/tables/{info.id}", f"/v1/tables/{info.id}/rows"):
        r = client.get(url, headers=AUTH)
        assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_rows_sorted_on_the_machine(client, tmp_path):
    csv = tmp_path / "sort.csv"
    pv.write_csv(make_data(300), csv)
    info, _ = load(client, uri=str(csv))
    url = f"/v1/tables/{info.id}/rows"
    x = get_pickle(client, url, stop=300).get_column("x")

    up = get_pickle(client, url, stop=300, sort="x").get_column("x")
    defined = np.sort(x[~np.isnan(x)])
    assert np.array_equal(up[: len(defined)], defined) and np.isnan(up[len(defined) :]).all()
    down = get_pickle(client, url, start=0, stop=5, sort="x", descending=True).get_column("x")
    assert np.array_equal(down, defined[::-1][:5])
    # Text metas, descending; the next page continues the same order.
    ids = get_pickle(client, url, start=0, stop=2, sort="id", descending=True).metas[:, 0]
    more = get_pickle(client, url, start=2, stop=3, sort="id", descending=True).metas[:, 0]
    assert list(ids) + list(more) == ["row99", "row98", "row97"]
    assert len(get_pickle(client, url, start=290)) == 10
    assert len(get_pickle(client, url, start=500)) == 0

    for params in ({"sort": "nope"}, {"start": 5, "stop": 4}, {"stop": MAX_PAGE_ROWS + 1}):
        r = client.get(url, headers=AUTH, params=params)
        assert r.status_code == 400 and r.json()["code"] == "bad_request", params


def test_bad_requests(client, tmp_path):
    def post(**request):
        return client.post("/v1/tables", headers=AUTH, json=request)

    assert post(upload_id="u-000000000000").status_code == 404
    assert post(uri=str(tmp_path / "missing.csv")).status_code == 404
    assert post(uri="data.csv").status_code == 400
    (tmp_path / "data.xyz").write_text("a\n1\n")
    assert post(uri=str(tmp_path / "data.xyz")).json()["code"] == "bad_request"


@pytest.mark.slow
def test_million_rows(client, tmp_path):
    """About 1M rows: correct metadata, a page of rows, and peak loading memory
    below twice the table's size. Run with ``pytest -m slow -s``."""
    n = 1_000_000
    csv = tmp_path / "million.csv"
    pv.write_csv(make_data(n), csv)
    up = upload(client, csv)
    started = time.monotonic()
    info, result = load(client, timeout=300, upload_id=up.id, columns=[
        {"name": "label", "role": "target"}
    ])  # fmt: skip
    wall = time.monotonic() - started
    assert info.n_rows == n
    assert (info.n_attributes, info.n_class_vars, info.n_metas) == (5, 1, 2)
    page = get_pickle(client, f"/v1/tables/{info.id}/rows", start=n - 500, stop=n + 500)
    assert len(page) == 500
    assert page.domain == get_pickle(client, f"/v1/tables/{info.id}/domain")
    print(
        f"\n1M rows, {csv.stat().st_size / 1e6:.0f} MB CSV: load job {wall:.1f} s "
        f"(loading {result.values['load_seconds']:.1f} s), table "
        f"{info.memory_bytes / 1e6:.0f} MB, peak extra memory while loading "
        f"{info.peak_load_memory_bytes / 1e6:.0f} MB "
        f"({info.peak_load_memory_bytes / info.memory_bytes:.2f}x)"
    )
    assert info.peak_load_memory_bytes < 2 * info.memory_bytes


def test_http_loads_have_no_total_time_limit():
    """aiohttp stops a request after 5 minutes by default, which broke slow
    downloads (HIGGS over a home connection); only a stalled one fails now."""
    from orangecontrib.cloud.server.sources import remote_options

    timeout = remote_options("https://example.org/big.csv.gz")["client_kwargs"]["timeout"]
    assert timeout.total is None and timeout.sock_read == 300
    assert remote_options("gs://bucket/big.csv") == {}


def test_several_files_as_one_table(client, tmp_path, http_dir):
    """Months of the same data as one table: column types of the same kind
    but another width, a name in other letter case, a column that only later
    files have, and one that a later file lacks. Parquet over http(s) and
    CSV from the disk; clear errors for files that don't fit."""
    when = np.datetime64("2025-01-01T00:00:00") + np.arange(4).astype("timedelta64[h]")
    first = pa.table({
        "zone": pa.array([1, 2, 3, 4], pa.int32()), "Airport_fee": [0.0, 1.75, None, 0.0],
        "flag": pa.array(["N", "Y", "N", "N"], pa.large_string()), "pickup": when,
    })  # fmt: skip
    second = pa.table({
        "zone": pa.array([5, 6], pa.int64()), "airport_fee": [1.75, 0.0],
        "flag": ["Y", "N"], "pickup": when[:2].astype("datetime64[ns]"), "cbd_fee": [0.75, 0.0],
    })  # fmt: skip
    third = pa.table({"zone": [7], "Airport_fee": [0.0], "pickup": when[:1]})
    web_folder, web_url = http_dir
    for name, data in (("m-01", first), ("m-02", second), ("m-03", third)):
        pq.write_table(data, web_folder / f"{name}.parquet")
    uris = [f"{web_url}/m-0{i}.parquet" for i in (1, 2, 3)]
    info, result = load(client, uris=uris, columns=[{"name": "zone", "type": "categorical"}])
    assert info.n_rows == 7 and info.source == f"{uris[0]} (+2 more files)"
    assert info.name == "m-01 and 2 more"
    table = get_pickle(client, f"/v1/tables/{info.id}/rows", stop=10)
    assert [v.name for v in table.domain.attributes] == ["zone", "Airport_fee", "flag", "pickup"]
    assert table.domain["zone"].values == tuple("1234567")
    np.testing.assert_equal(table.get_column("Airport_fee"), [0, 1.75, np.nan, 0, 1.75, 0, 0])
    assert [table.domain["flag"].str_val(v) for v in table.get_column("flag")] == [
        "N", "Y", "N", "N", "Y", "N", "?",
    ]  # fmt: skip
    assert table.get_column("pickup")[4] == table.get_column("pickup")[0]  # ns read as us
    warnings = " ".join(result.values["warnings"])
    assert "Column 'flag' isn't in m-03.parquet" in warnings
    assert "Column 'cbd_fee' of m-02.parquet isn't in the first file; skipped." in warnings

    # CSV: text further down in a later file makes the column text.
    for i, rows in enumerate(("x,y\n1,a\n2,b\n", "y,x\nc,3\nd,oops\n")):
        (tmp_path / f"part-{i}.csv").write_text(rows)
    info, result = load(client, uris=[str(tmp_path / f"part-{i}.csv") for i in (0, 1)])
    table = get_pickle(client, f"/v1/tables/{info.id}/rows")
    assert info.n_rows == 4 and table.domain["x"].is_string  # 4 values in 4 rows: text
    assert list(table.get_column("x")) == ["1", "2", "3", "oops"]
    assert "Column 'x' has values that aren't numeric; read as text." in result.values["warnings"]

    # Errors: other columns, numbers in one file and text in another, mixed formats.
    pq.write_table(pa.table({"other": [1]}), web_folder / "other.parquet")
    pq.write_table(pa.table({"zone": ["x"]}), web_folder / "text.parquet")

    def error(uris):
        r = client.post("/v1/tables", headers=AUTH, json={"uris": uris})
        if r.status_code == 400:
            return r.json()["message"]
        job = JobInfo.model_validate(r.json())
        url, finished = f"/v1/jobs/{job.id}", ("done", "failed", "cancelled")
        while (info := client.get(url, headers=AUTH).json())["state"] not in finished:
            time.sleep(0.05)
        return info["error"]["message"]

    assert "other.parquet has none of the columns of the first file" in error(
        [uris[0], f"{web_url}/other.parquet"]
    )
    assert error([uris[0], f"{web_url}/text.parquet"]) == (
        "Column 'zone' holds numbers in the first file but text in text.parquet. "
        "Set its type in Cloud File to read all the files the same way."
    )
    assert "all be CSV files or all Parquet" in error([uris[0], str(tmp_path / "part-0.csv")])
    assert "File not found" in error([uris[0], f"{web_url}/missing.parquet"])


def test_signed_links_are_copied_once_before_reading(tmp_path, http_dir, monkeypatch):
    """Signed links (``copy_first``) may expire before a long load ends: all
    the files are copied at the start, a CSV read again as text reuses the
    copies, and the copies are gone when the load ends or fails."""
    from orangecontrib.cloud.server import loader, sources
    from orangecontrib.cloud.server.worker import OperationError

    web_folder, web_url = http_dir
    for i, rows in enumerate(("x,y\n1,a\n2,b\n", "y,x\nc,3\nd,oops\n")):
        (web_folder / f"signed-{i}.csv").write_text(rows)
    uris = [f"{web_url}/signed-{i}.csv?X-Amz-Signature=abc" for i in (0, 1)]
    copied = []

    def copy_to_disk(location, *args):
        copied.append(location)
        return sources.copy_to_disk(location, *args)

    monkeypatch.setattr(loader, "copy_to_disk", copy_to_disk)
    texts = []
    tmp = tmp_path / "copies"
    tmp.mkdir()
    options = loader.LoadOptions(copy_first=True, tmp_dir=str(tmp))
    loaded = loader.load_table(uris, options=options, progress=lambda _, t: texts.append(t))
    assert list(loaded.table.get_column("x")) == ["1", "2", "3", "oops"]  # read again as text
    assert copied == uris  # once each, before reading
    first = {text.split(":")[0]: k for k, text in reversed(list(enumerate(texts)))}
    assert first["Copying file 2 of 2"] < first["File 1 of 2"]
    assert list(tmp.iterdir()) == []

    with pytest.raises(OperationError, match="File not found: .*missing.csv"):
        loader.load_table([*uris, f"{web_url}/missing.csv?sig=x"], options=options)
    assert list(tmp.iterdir()) == []
    monkeypatch.setattr(sources.shutil, "disk_usage", lambda _: shutil_usage(free=2 * 1024**3))
    with pytest.raises(OperationError, match="3 files .* don't fit"):
        sources.check_disk_space([600 * 1024**2, None, 600 * 1024**2], None)
    sources.check_disk_space([500 * 1024**2, 100], None)


def shutil_usage(free: int):
    """What ``shutil.disk_usage`` returns, with ``free`` bytes free."""
    import shutil

    return shutil._ntuple_diskusage(10 * free, 9 * free, free)
