"""Failing tests for sample CSV generation (CSPRNG + SHA256 unique IDs)."""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

import pytest

HEX12 = re.compile(r"^[0-9a-f]{12}$")
REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "generate_sample_csv.py"


def test_generator_module_importable():
    from scripts.generate_sample_csv import (  # noqa: F401
        EXPECTED_HEADERS,
        generate_interaction_id,
        write_sample_csv,
    )


def test_expected_headers_match_contract(expected_csv_headers):
    from scripts.generate_sample_csv import EXPECTED_HEADERS

    assert EXPECTED_HEADERS == expected_csv_headers


def test_write_sample_csv_emits_exact_headers(tmp_csv_path, expected_csv_headers):
    from scripts.generate_sample_csv import write_sample_csv

    write_sample_csv(tmp_csv_path, rows=5)

    with tmp_csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)

    assert header == expected_csv_headers


def test_write_sample_csv_row_count_matches_flag(tmp_csv_path):
    from scripts.generate_sample_csv import write_sample_csv

    rows = 250
    write_sample_csv(tmp_csv_path, rows=rows)

    with tmp_csv_path.open(newline="", encoding="utf-8") as fh:
        line_count = sum(1 for _ in fh)

    # header + data rows
    assert line_count == rows + 1


def test_interaction_ids_are_12_hex_and_unique(tmp_csv_path, expected_csv_headers):
    from scripts.generate_sample_csv import write_sample_csv

    rows = 5_000
    write_sample_csv(tmp_csv_path, rows=rows)

    id_col = expected_csv_headers[3]
    with tmp_csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        ids = [row[id_col] for row in reader]

    assert len(ids) == rows
    assert all(HEX12.match(i) for i in ids)
    assert len(set(ids)) == rows


def test_generate_interaction_id_is_hex12_not_sequential():
    from scripts.generate_sample_csv import generate_interaction_id

    samples = [generate_interaction_id() for _ in range(200)]
    assert all(HEX12.match(s) for s in samples)
    assert len(set(samples)) == len(samples)
    # Sequential counters would be lexicographically ordered integers in hex;
    # CSPRNG output must not be a contiguous arithmetic sequence.
    as_ints = [int(s, 16) for s in samples]
    deltas = {as_ints[i + 1] - as_ints[i] for i in range(len(as_ints) - 1)}
    assert 1 not in deltas or len(deltas) > 1


def test_cli_respects_rows_and_out(tmp_path, expected_csv_headers):
    out = tmp_path / "cli_out.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--rows",
            "17",
            "--out",
            str(out),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()

    with out.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        data = list(reader)

    assert header == expected_csv_headers
    assert len(data) == 17
