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
    ("category_label", "text"),
    ("claim_type", "text"),
    ("work_use_pct", "decimal"),
    ("apportionment_basis", "text"),
    ("reimbursed", "text"),
    ("wfh_method", "text"),
    ("asset_cost_inc_gst", "decimal"),
    ("asset_set_id", "text"),
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

# Column *names* differ from the row attribute for exactly one traceability key.
_ATTR_BY_COLUMN: dict[str, str] = {"paperless_document_id": "document_id"}

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def write_csv(
    path: Path, *, rows: Sequence[ClassifiedRow], columns: Sequence[tuple[str, str]]
) -> None:
    """Write *rows* to *path* under the exact byte contract (BOM + ``\\r\\n``).

    Rows are sorted by ``(date, paperless_document_id)`` before writing so the
    output is deterministic regardless of input order. The full file is built
    in memory first and written in one ``open(..., newline="")`` call.
    """
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write(_render_csv(rows, columns))


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
