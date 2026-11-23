import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "baseline.py"


def test_baseline_smoke(tmp_path):
    out = tmp_path / "results.json"
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--sizes",
            "2k",
            "--timeout",
            "120",
            "--workdir",
            work,
            "--out",
            out,
        ],
        check=True,
        timeout=180,
    )
    results = json.loads(out.read_text())
    assert [(r["operation"], r["status"]) for r in results] == [("load", "ok"), ("rf_cv", "ok")]
    assert all(r["peak_rss_bytes"] > 0 and r["seconds"] > 0 for r in results)
    assert not list(work.iterdir())  # generated files are deleted


def load_baseline():
    import importlib.util

    spec = importlib.util.spec_from_file_location("baseline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cloud_files_and_table(tmp_path):
    baseline = load_baseline()
    orange, plain = tmp_path / "orange.csv", tmp_path / "plain.csv"
    baseline.generate_csv(str(orange), 10, 0)
    baseline.generate_csv(str(plain), 10, 0, orange_header=False)
    orange_lines, plain_lines = orange.read_text().splitlines(), plain.read_text().splitlines()
    assert orange_lines[0] == plain_lines[0] and orange_lines[3:] == plain_lines[1:]  # same rows
    table = baseline.markdown_table([
        {"rows": 10, "operation": "load", "seconds": 1.0, "peak_rss_bytes": 2**30,
         "status": "ok", "machine": "n2-standard-16", "cost_usd": 0.0002},
    ])  # fmt: skip
    row = "| n2-standard-16 | 10 | load | 1.0 s | 1.00 GiB | ok | $0.000 |"
    assert table.splitlines()[-1] == row
