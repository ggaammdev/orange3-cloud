"""Reading one Parquet file of a load (:mod:`.loader`), a row group at a time.

Parquet needs random access, so a ``.parquet.gz`` file is decompressed to the
disk first; the loader copies an ``http(s)://`` file to the disk before it
gets here.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable

import pyarrow as pa

from orangecontrib.cloud.server.columns import Column
from orangecontrib.cloud.server.sources import is_remote, local_path, open_stream, remote_options

__all__ = ["ParquetFile"]

#: Bytes decompressed at a time.
CHUNK_BYTES = 16 * 1024**2


class ParquetFile:
    """One Parquet file, open for reading (``with ParquetFile(...) as f:``)."""

    def __init__(
        self,
        read_from: str,
        gzip: bool,
        tmp_dir: str | None,
        check_cancelled: Callable[[], None],
    ):
        self.read_from = read_from
        self.gzip = gzip
        self.tmp_dir = tmp_dir
        self.check_cancelled = check_cancelled
        self._tmp: str | None = None
        self._file = None

    def __enter__(self) -> ParquetFile:
        import pyarrow.parquet as pq

        try:
            if self.gzip:  # Parquet needs random access: decompress to disk first
                source = self._tmp = self._decompress()
            elif is_remote(self.read_from):
                import fsspec

                options = remote_options(self.read_from)
                source = fsspec.open(self.read_from, "rb", **options).open()
            else:
                source = local_path(self.read_from)
            self._file = pq.ParquetFile(source)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._tmp is not None:
            os.unlink(self._tmp)
            self._tmp = None

    def _decompress(self) -> str:
        fd, tmp = tempfile.mkstemp(suffix=".parquet", dir=self.tmp_dir)
        os.close(fd)
        try:
            stream, raw, _ = open_stream(self.read_from, True)
            try:
                with open(tmp, "wb") as f:
                    while chunk := stream.read(CHUNK_BYTES):
                        self.check_cancelled()
                        f.write(chunk)
            finally:
                stream.close()
                raw.close()
        except BaseException:
            os.unlink(tmp)
            raise
        return tmp

    @property
    def schema(self) -> pa.Schema:
        """The file's columns and their Arrow types."""
        return self._file.schema_arrow

    def read(
        self,
        columns: list[tuple[Column, int | None]],
        add_batch: Callable[[int, Callable[[Column], pa.Array | None]], None],
        report: Callable[[float], None],
    ) -> None:
        """Read the file a row group at a time into ``add_batch``, and in
        each group one column at a time, so only one column of one row
        group is in Arrow's memory at once. ``columns``: the wanted columns
        and their positions in this file (``None``: the file doesn't have
        it; its values are missing). ``report(fraction)`` tells how much of
        the file has been read."""
        pf = self._file
        names = pf.schema_arrow.names
        positions = {col.index: pos for col, pos in columns}
        total = max(pf.metadata.num_rows, 1)

        def column(group: int, col: Column) -> pa.Array | None:
            pos = positions[col.index]
            if pos is None:
                return None
            data = pf.read_row_group(group, columns=[names[pos]])
            return data.column(0).combine_chunks()

        done = 0
        for group in range(pf.metadata.num_row_groups):
            self.check_cancelled()
            n = pf.metadata.row_group(group).num_rows
            add_batch(n, lambda c, g=group: column(g, c))
            done += n
            report(done / total)
