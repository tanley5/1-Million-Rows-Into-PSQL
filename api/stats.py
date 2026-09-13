"""Global cleaning statistics accumulator."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CleaningStats:
    normalized_per_column: dict[str, int] = field(default_factory=dict)
    blank_per_column: dict[str, int] = field(default_factory=dict)
    rows_in: int = 0
    rows_out: int = 0
    rows_error: int = 0

    def record_batch(self, rows_in: int, rows_out: int, rows_error: int) -> None:
        self.rows_in += rows_in
        self.rows_out += rows_out
        self.rows_error += rows_error

    def bump_normalized(self, column: str, n: int = 1) -> None:
        self.normalized_per_column[column] = (
            self.normalized_per_column.get(column, 0) + n
        )

    def bump_blank(self, column: str, n: int = 1) -> None:
        self.blank_per_column[column] = self.blank_per_column.get(column, 0) + n

    def to_dict(self) -> dict:
        return {
            "normalized_per_column": dict(self.normalized_per_column),
            "blank_per_column": dict(self.blank_per_column),
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_error": self.rows_error,
        }
