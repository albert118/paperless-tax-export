"""Deterministic CSV rendering of classified rows.

The byte contract is exact: UTF-8 with a BOM (``utf-8-sig``), ``\\r\\n`` line
terminators written with ``newline=""`` (no platform translation), amounts as
two-decimal strings from ``Decimal``, ISO dates passed through untouched, and
text cells prefixed with a single quote when they start with a spreadsheet
formula character (``= + - @ \\t \\r \\n``). Decimal and date cells are never
guarded.

``work_use_pct`` is a float custom-field value. It is rendered with plain
formatting (``f"{value:.2f}"``) straight from the stored number so the output
is deterministic — no float arithmetic ever happens here.
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
    ("gst_amount", "decimal"),
    ("document_type", "text"),
    ("work_use_pct", "decimal"),
    ("document_modified", "date"),
    ("notes_for_agent", "text"),
)

REVIEW_COLUMNS: tuple[tuple[str, str], ...] = DEDUCTIONS_COLUMNS + (
    ("title", "text"),
    ("correspondent", "text"),
    ("created", "date"),
    ("tags", "text"),
    ("reason_codes", "text"),
    ("raw_amount", "text"),
    ("raw_date", "text"),
)

IBKR_COLUMNS: tuple[str, ...] = (
    "paperless_document_id",
    "title",
    "created",
    "document_type",
    "correspondent",
    "tags",
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
    if name == "tags":
        return _formula_guard("; ".join(sorted(row.tags)))
    raw = getattr(row, _ATTR_BY_COLUMN.get(name, name))
    return "" if raw is None else _formula_guard(str(raw))


def _formula_guard(text: str) -> str:
    """Prefix *text* with a single quote when it could be a spreadsheet formula."""
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


@dataclass(frozen=True, slots=True)
class IbkrRow:
    """One line of the IBKR documents index; ``created`` is an ISO date."""

    document_id: int
    title: str
    created: str
    document_type: str | None
    correspondent: str | None
    tags: tuple[str, ...]


def write_ibkr_csv(path: Path, *, rows: Sequence[IbkrRow]) -> None:
    """Write the IBKR index under the same byte contract as :func:`write_csv`.

    Rows are sorted by ``(created, paperless_document_id)``; with no rows the
    file holds the header only.
    """
    path.write_bytes(render_ibkr_csv_bytes(rows))


def render_ibkr_csv_bytes(rows: Sequence[IbkrRow]) -> bytes:
    """The exact bytes :func:`write_ibkr_csv` writes (BOM included), for hashing."""
    return _render_ibkr_csv(rows).encode("utf-8-sig")


def _render_ibkr_csv(rows: Sequence[IbkrRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(IBKR_COLUMNS)
    for row in sorted(rows, key=lambda item: (item.created, item.document_id)):
        writer.writerow(
            [
                str(row.document_id),
                _formula_guard(row.title),
                row.created,
                _formula_guard(row.document_type or ""),
                _formula_guard(row.correspondent or ""),
                _formula_guard("; ".join(sorted(row.tags))),
            ]
        )
    return buffer.getvalue()
