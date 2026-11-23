"""Tables: uploading files, loading them into tables, the tables' metadata,
pages of their rows, replaying recorded steps (``derive``) and statistics on
all rows (``stats``)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "DEFAULT_PAGE_ROWS",
    "FORMAT_EXTENSIONS",
    "GZIP_EXTENSIONS",
    "MAX_FILES",
    "MAX_PAGE_ROWS",
    "STATS_FITS",
    "ColumnRole",
    "ColumnSpec",
    "ColumnType",
    "DeriveRequest",
    "RowsParams",
    "StatsKind",
    "StatsRequest",
    "TableCreateRequest",
    "TableFormat",
    "TableInfo",
    "TableList",
    "URI_SCHEMES",
    "UploadInfo",
    "UploadParams",
    "without_extensions",
]

#: The schemes of a :class:`TableCreateRequest`'s ``uri`` that the machine
#: reads itself (``gs://`` and ``gcs://`` with its Google credentials). A
#: provider whose storage the machine can't read turns its locations into one
#: of these first, e.g. a signed ``https://`` link.
URI_SCHEMES = ("https://", "http://", "gs://", "gcs://")


class UploadParams(BaseModel):
    """Query parameters of ``POST /v1/uploads``."""

    #: Original file name; its extension tells the format (``.csv``,
    #: ``.csv.gz``, ``.parquet``).
    filename: str


class UploadInfo(BaseModel):
    """``POST /v1/uploads``: a file stored on the machine."""

    id: str
    filename: str
    size_bytes: int
    created_at: datetime


class TableFormat(str, Enum):
    """The format of a table file."""

    CSV = "csv"
    PARQUET = "parquet"


#: File name extensions of each format, in lower case. One of
#: :data:`GZIP_EXTENSIONS` may follow (a gzip-compressed file).
FORMAT_EXTENSIONS = {
    TableFormat.CSV: (".csv", ".tsv", ".tab", ".txt"),
    TableFormat.PARQUET: (".parquet", ".pq"),
}
#: File name extensions of gzip-compressed files, in lower case.
GZIP_EXTENSIONS = (".gz", ".gzip")


def without_extensions(name: str) -> str:
    """A file name without its compression's and its format's extensions,
    e.g. "trips" for "trips.csv.gz"."""
    for suffixes in (GZIP_EXTENSIONS, *FORMAT_EXTENSIONS.values()):
        for suffix in suffixes:
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
                break
    return name


class ColumnRole(str, Enum):
    """Role of a column, as in Orange's File widget."""

    FEATURE = "feature"
    TARGET = "target"
    META = "meta"
    SKIP = "skip"


class ColumnType(str, Enum):
    """Orange variable type for a column."""

    NUMERIC = "numeric"  #: ContinuousVariable
    CATEGORICAL = "categorical"  #: DiscreteVariable
    TEXT = "text"  #: StringVariable
    DATETIME = "datetime"  #: TimeVariable


class ColumnSpec(BaseModel):
    """Override the inferred role or type of one column when loading."""

    name: str
    role: ColumnRole | None = None
    type: ColumnType | None = None


#: Most files that one ``POST /v1/tables`` loads into one table (``uris``).
MAX_FILES = 1000


class TableCreateRequest(BaseModel):
    """``POST /v1/tables``: load a file, or several files with the same
    columns, into a table on the machine.

    Exactly one of ``uri``, ``uris`` and ``upload_id`` is set.
    """

    #: A URL with one of :data:`URI_SCHEMES` (e.g. ``gs://bucket/path``,
    #: ``https://...``), or an absolute path on the machine's disk.
    uri: str | None = None
    #: Several files, each as ``uri``, all CSV or all Parquet, loaded one
    #: after the other into one table, e.g. the months of a year. The first
    #: file's columns are the table's; the other files' columns are matched
    #: by name (``docs/api-v1.md`` says how they may differ).
    uris: list[str] | None = Field(default=None, min_length=1, max_length=MAX_FILES)
    #: ID from ``POST /v1/uploads``.
    upload_id: str | None = None
    #: ``None``: from the file extension. ``.gz`` files are decompressed.
    format: TableFormat | None = None
    name: str | None = None
    #: Overrides for the inferred domain; columns not listed keep theirs.
    columns: list[ColumnSpec] = Field(default_factory=list)
    #: Copy an ``http(s)://`` file (each of ``uris``) to the machine's disk
    #: with one request before reading it, e.g. a signed link that may expire
    #: before a long load ends. (``http(s)://`` Parquet files are always
    #: copied first.)
    copy_first: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> TableCreateRequest:
        sources = (self.uri, self.uris, self.upload_id)
        if sum(source is not None for source in sources) != 1:
            raise ValueError("set exactly one of 'uri', 'uris' and 'upload_id'")
        return self

    @property
    def locations(self) -> list[str] | None:
        """The files to load, as a list (``None`` for an upload)."""
        if self.uris is not None:
            return list(self.uris)
        return None if self.uri is None else [self.uri]


class TableInfo(BaseModel):
    """Metadata of a table stored on the machine.

    The table's Orange ``Domain`` comes as a pickle from
    ``GET /v1/tables/{id}/domain``.
    """

    id: str
    name: str
    n_rows: int
    #: Attributes, class variables and metas, as in Orange's Domain.
    n_attributes: int
    n_class_vars: int
    n_metas: int
    #: Memory the table's arrays use on the machine.
    memory_bytes: int
    #: Highest extra memory measured while loading or deriving the table.
    peak_load_memory_bytes: int | None = None
    #: Where the data came from: a URI, ``upload:<id>``, or ``table:<id>``
    #: for tables derived from another one (``derive`` or a job). For
    #: several files, the first one's URI and how many more there were, e.g.
    #: ``https://example.com/2025-01.parquet (+11 more files)``.
    source: str
    format: TableFormat | None = None
    #: The table this one was derived from, if any.
    parent_id: str | None = None
    #: For tables made by ``derive``: the replayed steps, one short line each
    #: (e.g. ``"transform: 4 columns"``), and the request's ``steps_key``.
    steps: list[str] = Field(default_factory=list)
    steps_key: str | None = None
    created_at: datetime


class TableList(BaseModel):
    """``GET /v1/tables``."""

    tables: list[TableInfo] = Field(default_factory=list)


#: Most rows that one ``GET /v1/tables/{id}/rows`` returns.
MAX_PAGE_ROWS = 100_000

#: Rows that ``GET /v1/tables/{id}/rows`` returns when ``stop`` isn't given.
DEFAULT_PAGE_ROWS = 1000


class RowsParams(BaseModel):
    """Query parameters of ``GET /v1/tables/{id}/rows``: a page of rows,
    ``start`` (inclusive) to ``stop`` (exclusive), counted from 0 in the
    order of the table or, with ``sort``, in sorted order."""

    start: int = Field(default=0, ge=0)
    #: ``None``: ``start`` + :data:`DEFAULT_PAGE_ROWS`. At most
    #: :data:`MAX_PAGE_ROWS` rows per request; past the end, fewer rows.
    stop: int | None = Field(default=None, ge=0)
    #: Name of the column to sort by (a feature, target or meta column);
    #: ``None``: the table's own order. Missing values come last.
    sort: str | None = None
    descending: bool = False


class DeriveRequest(BaseModel):
    """``spec`` part of ``POST /v1/tables/{id}/derive``.

    The ``steps`` part is a pickled tuple of the steps that Orange's widgets
    recorded, in order: ``("transform", Domain)`` or ``("filter", Filter)``.
    The machine replays them on all rows of the table and keeps the result.
    """

    name: str | None = None
    #: A hash of the pickled steps. The machine keeps the result for each
    #: table and key, so the same steps aren't replayed twice.
    steps_key: str = Field(min_length=1, max_length=128)
    #: The same hashes of the first 1, 2, ... n-1 steps (pickled as a tuple
    #: of their own): the machine starts from a table it already derived
    #: with some of the first steps, e.g. for another branch of a workflow.
    prefix_keys: list[str] = Field(default_factory=list, max_length=1000)


class StatsKind(str, Enum):
    """What ``POST /v1/tables/{id}/stats`` computes, on all rows."""

    #: Per column, the numbers of Orange's Feature Statistics widget.
    BASIC = "basic"
    #: Per column, counts per value or per bin, as Orange's Distributions.
    DISTRIBUTION = "distribution"
    #: Per column and group, the numbers of Orange's Box Plot.
    QUANTILES = "quantiles"
    #: Counts on a grid of two numeric columns (a scatter plot's density).
    HISTOGRAM2D = "histogram2d"
    #: Per column, its values and how many rows have each (Cloud Categories,
    #: Cloud Create Class).
    VALUES = "values"


#: Curves that ``distribution`` can fit: those of Orange's Distributions
#: widget (``kde`` is its "Kernel density").
STATS_FITS = ("norm", "beta", "gamma", "rayleigh", "pareto", "expon", "kde")


class StatsRequest(BaseModel):
    """``POST /v1/tables/{id}/stats``. The answer is a pickled ``dict``;
    ``docs/api-v1.md`` lists its keys for each kind."""

    kind: StatsKind
    #: Columns as indices into Orange's Domain: attributes and class variables
    #: from 0, metas from -1 down. ``None``: all columns (``basic`` and
    #: ``distribution``). ``histogram2d`` takes exactly two (x and y).
    columns: list[int] | None = None
    #: A categorical column to split by (``distribution``, ``quantiles``,
    #: ``histogram2d``).
    group_by: int | None = None
    #: ``distribution`` of numeric columns: which of the binnings that
    #: Orange's Distributions widget offers (0 is the finest). ``None``: 10
    #: bins of equal width.
    binning: int | None = Field(default=None, ge=0)
    #: ``distribution``: a curve to fit to the values of each group, one of
    #: :data:`STATS_FITS`.
    fit: str | None = None
    #: Smoothing of the ``kde`` curve, as in Orange's Distributions (1–20).
    smoothing: int = Field(default=10, ge=1, le=20)
    #: ``histogram2d``: bins on each axis.
    grid: int = Field(default=100, ge=2, le=1000)

    @model_validator(mode="after")
    def _known_fit(self) -> StatsRequest:
        if self.fit is not None and self.fit not in STATS_FITS:
            raise ValueError(f"'fit' must be one of {', '.join(STATS_FITS)}")
        return self
