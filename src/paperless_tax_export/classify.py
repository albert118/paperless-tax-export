"""Classify selected documents into accountant rows and reason codes.

Every selected document becomes exactly one :class:`ClassifiedRow` carrying
all CSV output columns plus ``reason_codes`` (a ``ReviewCode`` per issue, in
declaration order) and the Paperless document id. Rows with any code go to the
review list; rows with none go to the accepted list. Nothing here reads or
writes a file, opens a socket, or depends on the current date.

Three-state rule for custom fields (applied everywhere in this module)
-----------------------------------------------------------------------
- Settings-level ``None``: the field is not configured. The CSV column stays
  blank, the fact is reported once by the run layer (``export.run_export``),
  and a per-row code is never emitted for it (the one exception is the amount
  field, whose rules mandate ``MISSING_AMOUNT`` on every row).
- Document-level *no entry*: silent fallback — the row date falls back to
  ``created``, the supplier to the correspondent, the amount gets
  ``MISSING_AMOUNT``.
- Document-level *entry present but null/empty/garbage*: the ``MISSING_*`` /
  ``AMOUNT_UNPARSEABLE`` code.

``notes_for_agent`` carries only per-document informational gaps (never
settings-level facts, never blocking codes restated): "amount missing", "date
from created" and, for a non-AUD amount, "foreign currency <CODE>; amount not
converted to AUD" followed by "over-300 check not applied to <CODE>", in that
fixed order, joined with ``"; "``. A foreign-currency amount is accepted as it is
and never gets a review code of its own; ``AMOUNT_OVER_300`` applies to AUD only.
A zero amount (any currency, including ``-0.00``) gets ``ZERO_AMOUNT`` and goes to
review.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from paperless_tax_export.models import (
    CustomFieldDef,
    CustomFieldValueType,
    Document,
)
from paperless_tax_export.parsing import ReviewCode, parse_amount, parse_date
from paperless_tax_export.settings import Settings

AUD = "AUD"

# ReviewCode declaration order, so codes can be re-sorted deterministically.
_CODE_ORDER: tuple[ReviewCode, ...] = tuple(ReviewCode)


@dataclass(frozen=True, slots=True)
class ClassifyContext:
    """Everything classification needs beyond the documents themselves."""

    settings: Settings
    amount_field: CustomFieldDef | None
    date_field: CustomFieldDef | None
    supplier_field: CustomFieldDef | None
    correspondents: Mapping[int, str]
    document_types: Mapping[int, str]
    tags: Mapping[int, str]
    income_tag_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class ClassifiedRow:
    """One classified document, mirroring the CSV output columns.

    ``date`` is the resolved row date (a valid field value or the document's
    ``created`` date part), never empty.
    """

    document_id: int
    date: str
    supplier_name: str
    description: str
    amount_inc_gst: Decimal | None
    currency: str
    document_type: str
    document_modified: str
    notes_for_agent: str
    title: str
    correspondent: str
    created: str
    tags: tuple[str, ...]
    reason_codes: tuple[ReviewCode, ...]
    raw_amount: str
    raw_date: str


@dataclass(frozen=True, slots=True)
class Classification:
    """Accepted and review rows, each sorted by ``(date, document_id)``."""

    accepted: tuple[ClassifiedRow, ...]
    review: tuple[ClassifiedRow, ...]


@dataclass(frozen=True, slots=True)
class _Classified:
    """Internal classification result before duplicate detection."""

    row: ClassifiedRow
    codes: frozenset[ReviewCode]
    currency: str | None


def classify_documents(documents: Sequence[Document], context: ClassifyContext) -> Classification:
    """Classify *documents*, split them accepted/review, and sort each list.

    Rows are sorted by ``(date, document_id)`` — the row date is always the
    resolved field value or the ``created`` fallback, so the sort key is never
    empty. Every document lands in exactly one list.
    """
    classified = [_classify_document(document, context) for document in documents]

    groups: dict[tuple[str, str, Decimal | None, str | None], set[int]] = {}
    for item in classified:
        key = (item.row.supplier_name, item.row.date, item.row.amount_inc_gst, item.currency)
        groups.setdefault(key, set()).add(item.row.document_id)
    duplicate_document_ids = frozenset(
        document_id for members in groups.values() if len(members) >= 2 for document_id in members
    )

    rows: list[ClassifiedRow] = []
    for item in classified:
        codes = set(item.codes)
        if item.row.document_id in duplicate_document_ids:
            codes.add(ReviewCode.DUPLICATE_SUSPECT)
        rows.append(replace(item.row, reason_codes=_order_codes(codes)))
    rows.sort(key=lambda row: (row.date, row.document_id))

    accepted = tuple(row for row in rows if not row.reason_codes)
    review = tuple(row for row in rows if row.reason_codes)
    return Classification(accepted=accepted, review=review)


def _classify_document(document: Document, context: ClassifyContext) -> _Classified:
    """Classify one document and return its row plus the base reason codes."""
    settings = context.settings
    codes: set[ReviewCode] = set()

    created_date = document.created[:10]  # Paperless timestamps are ISO 8601
    date_field = context.date_field
    if date_field is None:
        row_date = created_date
        date_from_created = False
    elif not document.has_field(date_field.id):
        row_date = created_date
        date_from_created = True
    else:
        raw = document.field_value(date_field.id)
        parsed = parse_date(raw if isinstance(raw, str) else None)
        if parsed is None:
            row_date = created_date
            date_from_created = True
            codes.add(ReviewCode.MISSING_DATE)
        else:
            row_date = parsed
            date_from_created = False
            if not settings.fy_start <= parsed <= settings.fy_end:
                codes.add(ReviewCode.DATE_OUT_OF_FY)

    correspondent_name = (
        context.correspondents.get(document.correspondent, "")
        if document.correspondent is not None
        else ""
    )
    supplier_field = context.supplier_field
    if supplier_field is None:
        supplier_name = correspondent_name
        if not correspondent_name:
            codes.add(ReviewCode.MISSING_SUPPLIER)
    elif not document.has_field(supplier_field.id):
        supplier_name = correspondent_name
        if not correspondent_name:
            codes.add(ReviewCode.MISSING_SUPPLIER)
    else:
        raw = document.field_value(supplier_field.id)
        if isinstance(raw, str) and raw.strip():
            supplier_name = raw.strip()
        else:
            # The configured source is present but blank: flag it even though
            # the correspondent still makes a useful fallback for the column.
            supplier_name = correspondent_name
            codes.add(ReviewCode.MISSING_SUPPLIER)

    amount: Decimal | None = None
    currency: str | None = None
    amount_field = context.amount_field
    if amount_field is None:
        codes.add(ReviewCode.MISSING_AMOUNT)
    elif not document.has_field(amount_field.id):
        codes.add(ReviewCode.MISSING_AMOUNT)
    else:
        default_currency = amount_field.default_currency() or settings.default_currency
        parsed_amount = parse_amount(
            _amount_raw(document.field_value(amount_field.id)),
            default_currency=default_currency,
        )
        amount = parsed_amount.amount
        currency = parsed_amount.currency
        if parsed_amount.code is not None:
            codes.add(parsed_amount.code)
        if parsed_amount.amount is not None:
            if parsed_amount.amount < 0:
                codes.add(ReviewCode.NEGATIVE_AMOUNT)
            # A zero amount is a missing value or a placeholder, in any currency.
            if parsed_amount.amount == 0:
                codes.add(ReviewCode.ZERO_AMOUNT)
            # Greater than $300, positive only, AUD only: a negative or zero
            # amount never gets AMOUNT_OVER_300.
            if currency == AUD and parsed_amount.amount > 300:
                codes.add(ReviewCode.AMOUNT_OVER_300)

    document_type = (
        context.document_types.get(document.document_type, "")
        if document.document_type is not None
        else ""
    )
    tag_names = tuple(sorted(context.tags.get(tag_id, f"#{tag_id}") for tag_id in document.tags))
    if context.income_tag_ids.intersection(document.tags):
        codes.add(ReviewCode.INCOME_TAGGED)

    notes: list[str] = []
    if amount_field is not None and amount is None:
        notes.append("amount missing")
    if date_field is not None and date_from_created:
        notes.append("date from created")
    if amount is not None and currency is not None and currency != AUD:
        notes.append(f"foreign currency {currency}; amount not converted to {AUD}")
        notes.append(f"over-300 check not applied to {currency}")

    raw_amount = _raw_cell_value(document, amount_field)
    raw_date = _raw_cell_value(document, date_field)
    row = ClassifiedRow(
        document_id=document.id,
        date=row_date,
        supplier_name=supplier_name,
        description=document.title,
        amount_inc_gst=amount,
        currency="" if amount is None or currency is None else currency,
        document_type=document_type,
        document_modified=document.modified[:10],  # ISO 8601 like `created`
        notes_for_agent="; ".join(notes),
        title=document.title,
        correspondent=correspondent_name,
        created=created_date,
        tags=tag_names,
        reason_codes=_order_codes(codes),
        raw_amount=raw_amount,
        raw_date=raw_date,
    )
    return _Classified(row=row, codes=frozenset(codes), currency=currency)


def _order_codes(codes: set[ReviewCode]) -> tuple[ReviewCode, ...]:
    """Return the codes sorted by their declaration order in ``ReviewCode``."""
    return tuple(sorted(codes, key=_CODE_ORDER.index))


def _amount_raw(value: CustomFieldValueType) -> str | int | None:
    """Normalise a custom-field value for amount parsing (``float`` → str)."""
    if value is None:
        return None
    if isinstance(value, str) or isinstance(value, int):
        return value
    if isinstance(value, float):
        # Parsing happens on strings; a float from Paperless keeps its repr so
        # the parse is still strict (e.g. 12.34 -> "12.34", 1e+20 unparseable).
        return str(value)
    return None


def _raw_cell_value(document: Document, field: CustomFieldDef | None) -> str:
    """The raw stored text for a review column, or ``""`` when there is none."""
    if field is None or not document.has_field(field.id):
        return ""
    return _raw_text(document.field_value(field.id))


def _raw_text(value: CustomFieldValueType) -> str:
    """The raw cell text for a review-only column, or ``""`` for null."""
    return "" if value is None else str(value)
