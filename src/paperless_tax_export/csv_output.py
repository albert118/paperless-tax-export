"""Deterministic CSV rendering of classified rows.

The byte contract is exact: UTF-8 with a BOM (``utf-8-sig``), ``\\r\\n`` line
terminators written with ``newline=""`` (no platform translation), amounts as
two-decimal strings from ``Decimal``, ISO dates passed through untouched, and
text cells prefixed with a single quote when they start with a spreadsheet
formula character (``= + - @ \\t \\r \\n``). Decimal and date cells are never
guarded.

Decimal cells are rendered from the stored ``Decimal`` with ``f"{value:.2f}"``,
so the output is deterministic.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from paperless_tax_export.classify import ClassifiedRow

# Column spec: ordered (name, kind) with kind one of "text" | "decimal" | "date".
DEDUCTIONS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("paperless_document_id", "text"),
    ("date", "date"),
    ("supplier_name", "text"),
    ("description", "text"),
    ("amount_inc_gst", "decimal"),
    ("currency", "text"),
    ("document_type", "text"),
    ("notes_for_agent", "text"),
)

# Defined independently of DEDUCTIONS_COLUMNS: the two files no longer share a prefix.
REVIEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("paperless_document_id", "text"),
    ("reason_codes", "text"),
    ("date", "date"),
    ("correspondent", "text"),
    ("title", "text"),
    ("amount_inc_gst", "decimal"),
    ("currency", "text"),
    ("document_type", "text"),
    ("created", "date"),
    ("notes_for_agent", "text"),
)

INVESTMENTS_COLUMNS: tuple[str, ...] = (
    "paperless_document_id",
    "title",
    "created",
    "document_type",
    "correspondent",
)

# Column *names* differ from the row attribute for exactly one traceability key.
_ATTR_BY_COLUMN: dict[str, str] = {"paperless_document_id": "document_id"}

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def write_csv(
    path: Path, *, rows: Sequence[ClassifiedRow], columns: Sequence[tuple[str, str]]
) -> None:
    """Write *rows* to *path* under the exact byte contract (BOM + ``\\r\\n``).

    Rows are sorted by ``(date, paperless_document_id)`` before writing so the
    output is deterministic regardless of input order. The full file is built
    in memory first and written in one ``write_bytes`` call.
    """
    path.write_bytes(render_csv_bytes(rows, columns))


def render_csv_bytes(rows: Sequence[ClassifiedRow], columns: Sequence[tuple[str, str]]) -> bytes:
    """The exact bytes :func:`write_csv` writes (BOM included), for hashing."""
    return _render_csv(rows, columns).encode("utf-8-sig")


def _render_csv(rows: Sequence[ClassifiedRow], columns: Sequence[tuple[str, str]]) -> str:
    """Render the CSV text (without the BOM, which ``utf-8-sig`` adds on write)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([name for name, _kind in columns])
    for row in sorted(rows, key=lambda item: (item.date, item.document_id)):
        writer.writerow([_cell(row, name, kind) for name, kind in columns])
    return buffer.getvalue()


def _cell(row: ClassifiedRow, name: str, kind: str) -> str:
    """Render one cell: decimal two-decimal text, everything else guarded text."""
    if kind == "decimal":
        value = getattr(row, name)
        return "" if value is None else f"{value:.2f}"
    if name == "reason_codes":
        return ";".join(code.value for code in row.reason_codes)
    raw = getattr(row, _ATTR_BY_COLUMN.get(name, name))
    return "" if raw is None else _formula_guard(str(raw))


def _formula_guard(text: str) -> str:
    """Prefix *text* with a single quote when it could be a spreadsheet formula."""
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


@dataclass(frozen=True, slots=True)
class InvestmentRow:
    """One line of the investments index; ``created`` is an ISO date."""

    document_id: int
    title: str
    created: str
    document_type: str | None
    correspondent: str | None


def write_investments_csv(path: Path, *, rows: Sequence[InvestmentRow]) -> None:
    """Write the investments index under the same byte contract as :func:`write_csv`.

    Rows are sorted by ``(created, paperless_document_id)``; with no rows the
    file holds the header only.
    """
    path.write_bytes(render_investments_csv_bytes(rows))


def render_investments_csv_bytes(rows: Sequence[InvestmentRow]) -> bytes:
    """The exact bytes :func:`write_investments_csv` writes (BOM included), for hashing."""
    return _render_investments_csv(rows).encode("utf-8-sig")


def _render_investments_csv(rows: Sequence[InvestmentRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(INVESTMENTS_COLUMNS)
    for row in sorted(rows, key=lambda item: (item.created, item.document_id)):
        writer.writerow(
            [
                str(row.document_id),
                _formula_guard(row.title),
                row.created,
                _formula_guard(row.document_type or ""),
                _formula_guard(row.correspondent or ""),
            ]
        )
    return buffer.getvalue()
