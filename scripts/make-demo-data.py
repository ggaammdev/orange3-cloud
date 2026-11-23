#!/usr/bin/env python3
"""Make the synthetic data set for the demo workflows in ``examples/``.

Writes a gzipped CSV with one row per (made-up) customer: eight numeric
columns ``x1`` to ``x8``, a categorical column ``group`` (A to D) and the
target ``outcome`` (no/yes). The outcome depends on the columns in a
non-linear way, with noise, so a random forest scores better than logistic
regression. The same seed always gives the same file.

Examples::

    python scripts/make-demo-data.py                     # examples/demo-data.csv.gz, 1M rows
    python scripts/make-demo-data.py --rows 2000 --out /tmp/small.csv.gz
"""

from __future__ import annotations

import argparse
import gzip
import sys
import time
from pathlib import Path

import numpy as np

#: Where the demo workflows (``examples/*.ows``) look for the data.
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "examples" / "demo-data.csv.gz"
DEFAULT_ROWS = 1_000_000
COLUMNS = [f"x{i}" for i in range(1, 9)] + ["group", "outcome"]
GROUPS = np.array(["A", "B", "C", "D"])
CHUNK_ROWS = 100_000


def make_rows(rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``n`` rows: the numeric columns, the group and the outcome."""
    x = rng.normal(0.0, 1.0, size=(n, 8))
    group = rng.integers(0, len(GROUPS), size=n)
    logit = (
        1.2 * x[:, 0]
        - 0.8 * x[:, 1]
        + 1.5 * np.sin(2.0 * x[:, 2])
        + 0.9 * x[:, 3] * x[:, 4]
        + 1.4 * (np.abs(x[:, 5]) > 1.0)
        + np.array([-0.6, 0.0, 0.4, 0.9])[group]
        - 0.7
    )
    outcome = rng.random(n) < 1.0 / (1.0 + np.exp(-logit))
    return x, GROUPS[group], np.where(outcome, "yes", "no")


def write_demo_data(path: Path, rows: int = DEFAULT_ROWS, seed: int = 42) -> Path:
    """Write ``rows`` rows to the gzipped CSV ``path``, in chunks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    tmp = path.with_name(path.name + ".part")
    with gzip.open(tmp, "wt", compresslevel=6, newline="") as f:
        f.write(",".join(COLUMNS) + "\n")
        for start in range(0, rows, CHUNK_ROWS):
            x, group, outcome = make_rows(rng, min(CHUNK_ROWS, rows - start))
            text = np.char.mod("%.4f", x.round(4))
            table = np.column_stack([text, group, outcome])
            f.write("\n".join(",".join(row) for row in table.tolist()))
            f.write("\n")
    tmp.replace(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="number of rows")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output .csv.gz file")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    args = parser.parse_args(argv)
    t0 = time.monotonic()
    path = write_demo_data(args.out, args.rows, args.seed)
    size = path.stat().st_size / 1e6
    print(f"Wrote {args.rows:,} rows to {path} ({size:.1f} MB) in {time.monotonic() - t0:.1f} s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
