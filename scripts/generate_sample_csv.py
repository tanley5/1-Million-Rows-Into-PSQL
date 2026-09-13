"""Generate sample interaction CSVs with CSPRNG + SHA256 unique IDs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXPECTED_HEADERS = [
    "Start Datetime",
    "End Datetime",
    "Language Random (EN,SP,FR,ZM,ID)",
    "Interaction ID Random (12 char)",
    "Contact: (Yes/No)",
]

LANGUAGES = ("EN", "SP", "FR", "ZM", "ID")
CONTACTS = ("Yes", "No")


def generate_interaction_id() -> str:
    """Return a 12-char lowercase hex ID from SHA256 of CSPRNG bytes."""
    digest = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    return digest[:12]


def _random_row(base: datetime) -> dict[str, str]:
    offset_minutes = secrets.randbelow(60 * 24 * 365)
    duration_minutes = 1 + secrets.randbelow(120)
    start = base + timedelta(minutes=offset_minutes)
    end = start + timedelta(minutes=duration_minutes)
    return {
        EXPECTED_HEADERS[0]: start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        EXPECTED_HEADERS[1]: end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        EXPECTED_HEADERS[2]: LANGUAGES[secrets.randbelow(len(LANGUAGES))],
        EXPECTED_HEADERS[3]: generate_interaction_id(),
        EXPECTED_HEADERS[4]: CONTACTS[secrets.randbelow(len(CONTACTS))],
    }


def write_sample_csv(path: Path, rows: int) -> None:
    """Write ``rows`` sample interactions to ``path`` with contract headers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    seen: set[str] = set()

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPECTED_HEADERS)
        writer.writeheader()
        for _ in range(rows):
            row = _random_row(base)
            interaction_id = row[EXPECTED_HEADERS[3]]
            while interaction_id in seen:
                interaction_id = generate_interaction_id()
                row[EXPECTED_HEADERS[3]] = interaction_id
            seen.add(interaction_id)
            writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, required=True, help="Number of data rows")
    parser.add_argument("--out", type=Path, required=True, help="Output CSV path")
    args = parser.parse_args(argv)
    write_sample_csv(args.out, rows=args.rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
