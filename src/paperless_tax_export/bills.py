"""The ``bills.csv`` listing: utilities and bills of one financial year.

Bills are the documents of one Paperless document type. Their kind (electricity,
internet, phone, gas) comes from a tag, one tag per kind. This is a data-gathering
file: every selected bill is listed, with notes for gaps, and nothing is split into an
accepted and a review list. The date and amount rules are those of ``deductions.csv``
(the configured row-date field, else ``created``; ``parse_amount`` and the same
default-currency rules).

A bill that also carries the deduction tag stays in ``deductions.csv``. Here it is
listed with a note and kept out of the estimate's bill figures so nothing counts twice.
Nothing here reads a file or opens a socket.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from paperless_tax_export.classify import AUD, ClassifyContext, amount_raw
from paperless_tax_export.csv_output import formula_guard, render_table_bytes
from paperless_tax_export.models import Document
from paperless_tax_export.parsing import parse_amount, parse_date

BILLS_COLUMNS = (
    "paperless_document_id",
    "date",
    "correspondent",
    "title",
    "kind",
    "amount_inc_gst",
    "currency",
    "notes_for_agent",
)

NO_KIND_NOTE = "no kind tag"
ALSO_IN_DEDUCTIONS_NOTE = "also in deductions.csv; excluded from WFH totals"


@dataclass(frozen=True, slots=True)
class BillRow:
    """One bill. ``kind`` is blank when no single kind tag applies.

    ``also_deduction`` marks a bill that carries the deduction tag too.
    """

    document_id: int
    date: str
    correspondent: str
    title: str
    kind: str
    amount_inc_gst: Decimal | None
    currency: str
    notes_for_agent: str
    also_deduction: bool = False

    @property
    def counts_toward_wfh(self) -> bool:
        """Counted in the estimate: not also a deduction, and not a negative or zero amount."""
        if self.also_deduction:
            return False
        return self.amount_inc_gst is None or self.amount_inc_gst > 0


def build_bill_rows(
    documents: Sequence[Document], context: ClassifyContext, deduction_tag_id: int
) -> tuple[BillRow, ...]:
    """One row per bill document, sorted by ``(date, document id)``."""
    kind_by_tag = {tag: kind for kind, tag in context.settings.bill_kind_tags.items()}
    rows = [_bill_row(document, context, kind_by_tag, deduction_tag_id) for document in documents]
    rows.sort(key=lambda row: (row.date, row.document_id))
    return tuple(rows)


def bills_also_deductions(rows: Sequence[BillRow]) -> tuple[int, ...]:
    """Ids of the bills that carry the deduction tag too, in ascending order."""
    return tuple(sorted(row.document_id for row in rows if row.also_deduction))


def _bill_row(
    document: Document,
    context: ClassifyContext,
    kind_by_tag: dict[str, str],
    deduction_tag_id: int,
) -> BillRow:
    notes: list[str] = []
    amount, currency = _amount_and_currency(document, context)
    if amount is None:
        notes.append("amount missing")
    elif amount < 0:
        notes.append("negative amount")
    elif amount == 0:
        notes.append("zero amount")

    row_date, date_from_created = _row_date(document, context)
    if date_from_created:
        notes.append("date from created")

    kind, kind_note = _kind(document, context, kind_by_tag)
    if kind_note:
        notes.append(kind_note)

    also_deduction = deduction_tag_id in document.tags
    if also_deduction:
        notes.append(ALSO_IN_DEDUCTIONS_NOTE)

    correspondent = (
        context.correspondents.get(document.correspondent, "")
        if document.correspondent is not None
        else ""
    )
    return BillRow(
        document_id=document.id,
        date=row_date,
        correspondent=correspondent,
        title=document.title,
        kind=kind,
        amount_inc_gst=amount,
        currency=currency,
        notes_for_agent="; ".join(notes),
        also_deduction=also_deduction,
    )


def _amount_and_currency(
    document: Document, context: ClassifyContext
) -> tuple[Decimal | None, str]:
    """The parsed amount and its currency, or ``(None, "")`` when missing or unparseable."""
    amount_field = context.amount_field
    if amount_field is None or not document.has_field(amount_field.id):
        return None, ""
    parsed = parse_amount(
        amount_raw(document.field_value(amount_field.id)),
        default_currency=amount_field.default_currency() or context.settings.default_currency,
    )
    if parsed.amount is None:
        return None, ""
    return parsed.amount, parsed.currency or AUD


def _row_date(document: Document, context: ClassifyContext) -> tuple[str, bool]:
    """The row date and whether it fell back to ``created`` although a date field is set."""
    created = document.created[:10]  # Paperless timestamps are ISO 8601
    date_field = context.date_field
    if date_field is None:
        return created, False
    if document.has_field(date_field.id):
        raw = document.field_value(date_field.id)
        parsed = parse_date(raw if isinstance(raw, str) else None)
        if parsed is not None:
            return parsed, False
    return created, True


def _kind(
    document: Document, context: ClassifyContext, kind_by_tag: dict[str, str]
) -> tuple[str, str]:
    """The kind from the document's tags and the note explaining a blank kind."""
    document_tag_names = {context.tags.get(tag_id, "") for tag_id in document.tags}
    tag_names = sorted(document_tag_names.intersection(kind_by_tag))
    if len(tag_names) == 1:
        return kind_by_tag[tag_names[0]], ""
    if not tag_names:
        return "", NO_KIND_NOTE
    return "", f"several kind tags: {'; '.join(tag_names)}"


def render_bills_bytes(rows: Sequence[BillRow]) -> bytes:
    """The exact bytes of ``bills.csv`` (BOM, ``\\r\\n``, guarded text, two-decimal amounts)."""
    return render_table_bytes(BILLS_COLUMNS, (_cells(row) for row in rows))


def _cells(row: BillRow) -> list[str]:
    return [
        str(row.document_id),
        row.date,
        formula_guard(row.correspondent),
        formula_guard(row.title),
        formula_guard(row.kind),
        "" if row.amount_inc_gst is None else f"{row.amount_inc_gst:.2f}",
        formula_guard(row.currency),
        formula_guard(row.notes_for_agent),
    ]
