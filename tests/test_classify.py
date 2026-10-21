"""Tests for document classification into rows and reason codes."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import pytest
from conftest import document_payload

from paperless_tax_export.classify import (
    Classification,
    ClassifiedRow,
    ClassifyContext,
    classify_documents,
)
from paperless_tax_export.models import CustomFieldDef, Document
from paperless_tax_export.parsing import ReviewCode
from paperless_tax_export.settings import Settings

F_AMOUNT = CustomFieldDef(id=10, name="Transaction Amount", data_type="monetary")
F_GST = CustomFieldDef(id=11, name="GST Amount", data_type="monetary")
F_WORK_USE = CustomFieldDef(id=12, name="Claimable %", data_type="float")
F_DATE = CustomFieldDef(id=13, name="Transaction Date", data_type="date")
F_SUPPLIER = CustomFieldDef(id=14, name="Supplier", data_type="string")

_DEFAULT_FIELDS: dict[str, CustomFieldDef] = {
    F_AMOUNT.name: F_AMOUNT,
    F_GST.name: F_GST,
    F_WORK_USE.name: F_WORK_USE,
    F_DATE.name: F_DATE,
    F_SUPPLIER.name: F_SUPPLIER,
}

CORRESPONDENT_ID = 5
SUPPLIER_NAME = "Alpha Supplies"

FY_START = "2025-07-01"
FY_END = "2026-06-30"


def settings_for(
    *,
    amount: str | None = "Transaction Amount",
    gst: str | None = "GST Amount",
    work_use: str | None = "Claimable %",
    date: str | None = "Transaction Date",
    supplier: str | None = "Supplier",
) -> Settings:
    """A Settings with every field configured (or unset per the override)."""
    return Settings(
        field_amount=amount,
        field_gst=gst,
        field_work_use=work_use,
        field_date=date,
        field_supplier=supplier,
        fy_start=FY_START,
        fy_end=FY_END,
    )


def make_context(
    settings_obj: Settings | None = None,
    *,
    field_defs: Mapping[str, CustomFieldDef] | None = None,
    correspondents: Mapping[int, str] | None = None,
    document_types: Mapping[int, str] | None = None,
    tags: Mapping[int, str] | None = None,
    income_tag_ids: frozenset[int] = frozenset(),
) -> ClassifyContext:
    """Build a context, resolving the settings' field names against *field_defs*.

    The default settings configure every field; pass ``Settings()`` explicitly
    to exercise the real defaults (which leave GST and supplier unset).
    """
    active = settings_for() if settings_obj is None else settings_obj
    defs = dict(_DEFAULT_FIELDS if field_defs is None else field_defs)

    def resolve(name: str | None) -> CustomFieldDef | None:
        if name is None:
            return None
        return defs.get(name)

    return ClassifyContext(
        settings=active,
        amount_field=resolve(active.field_amount),
        date_field=resolve(active.field_date),
        supplier_field=resolve(active.field_supplier),
        correspondents=dict(correspondents or {}),
        document_types=dict(document_types or {}),
        tags=dict(tags or {}),
        income_tag_ids=income_tag_ids,
    )


def doc(
    doc_id: int,
    *,
    title: str = "Statement",
    created: str = "2025-10-01T00:00:00Z",
    tags: list[int] | None = None,
    correspondent: int | None = None,
    document_type: int | None = None,
    custom_fields: list[dict[str, object]] | None = None,
) -> Document:
    """Build a Document model from a payload dict, defaulting tags to empty."""
    return Document.model_validate(
        document_payload(
            doc_id,
            title=title,
            created=created,
            tags=[] if tags is None else tags,
            correspondent=correspondent,
            document_type=document_type,
            custom_fields=custom_fields,
        )
    )


def healthy_doc(
    doc_id: int,
    *,
    title: str = "Statement",
    created: str = "2025-10-01T00:00:00Z",
    tags: list[int] | None = None,
    correspondent: int | None = CORRESPONDENT_ID,
    document_type: int | None = None,
    custom_fields: list[dict[str, object]] | None = None,
) -> Document:
    """A document that passes every check when all fields are configured."""
    default_fields: list[dict[str, object]] = [
        {"field": F_AMOUNT.id, "value": "100.00"},
        {"field": F_GST.id, "value": "10.00"},
        {"field": F_WORK_USE.id, "value": 100.0},
        {"field": F_DATE.id, "value": "2025-11-05"},
        {"field": F_SUPPLIER.id, "value": "Acme Pty Ltd"},
    ]
    fields = default_fields if custom_fields is None else custom_fields
    return doc(
        doc_id,
        title=title,
        created=created,
        tags=tags,
        correspondent=correspondent,
        document_type=document_type,
        custom_fields=fields,
    )


def classify_one(
    document: Document, context: ClassifyContext
) -> tuple[Classification, ClassifiedRow]:
    """Classify a single document and return the classification and its row."""
    classification = classify_documents([document], context)
    rows = classification.accepted + classification.review
    assert len(rows) == 1
    return classification, rows[0]


# --------------------------------------------------------------------------- #
# Date three-state matrix
# --------------------------------------------------------------------------- #


def test_date_field_none_uses_created_without_code() -> None:
    context = make_context(
        settings_for(date=None), correspondents={CORRESPONDENT_ID: SUPPLIER_NAME}
    )
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]), context
    )
    assert row.date == "2025-10-01"
    assert ReviewCode.MISSING_DATE not in row.reason_codes
    assert row in classification.accepted


def test_date_configured_no_entry_falls_back_to_created_without_code() -> None:
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        field_defs={F_AMOUNT.name: F_AMOUNT, F_DATE.name: F_DATE},
    )
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]), context
    )
    assert row.date == "2025-10-01"
    assert row.reason_codes == ()
    assert row in classification.accepted


@pytest.mark.parametrize(
    "entry",
    [None, "", "   ", "not-a-date", "17/05/2025", 42, "2025-13-01"],
)
def test_date_configured_entry_garbage_uses_created_with_missing_date(entry: object) -> None:
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        field_defs={F_AMOUNT.name: F_AMOUNT, F_DATE.name: F_DATE},
    )
    classification, row = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_DATE.id, "value": entry},
            ],
        ),
        context,
    )
    assert row.date == "2025-10-01"
    assert review_codes(classification) == (ReviewCode.MISSING_DATE,)


def test_date_configured_valid_value_is_used() -> None:
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        field_defs={F_AMOUNT.name: F_AMOUNT, F_DATE.name: F_DATE},
    )
    classification, row = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_DATE.id, "value": "2025-11-05"},
            ],
        ),
        context,
    )
    assert row.date == "2025-11-05"
    assert row in classification.accepted


@pytest.mark.parametrize(
    ("value", "expected_codes"),
    [
        ("2025-07-01", ()),  # inclusive lower bound
        ("2026-06-30", ()),  # inclusive upper bound
        ("2026-01-15", ()),
        ("2025-06-30", (ReviewCode.DATE_OUT_OF_FY,)),
        ("2026-07-01", (ReviewCode.DATE_OUT_OF_FY,)),
    ],
)
def test_date_out_of_fy_uses_string_bounds(
    value: str, expected_codes: tuple[ReviewCode, ...]
) -> None:
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        field_defs={F_AMOUNT.name: F_AMOUNT, F_DATE.name: F_DATE},
    )
    _, row = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_DATE.id, "value": value},
            ],
        ),
        context,
    )
    assert row.date == value
    assert row.reason_codes == expected_codes


# --------------------------------------------------------------------------- #
# Amount three-state
# --------------------------------------------------------------------------- #


def test_amount_field_none_marks_every_row_missing_amount() -> None:
    context = make_context(
        settings_for(amount=None),
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        field_defs={F_DATE.name: F_DATE, F_SUPPLIER.name: F_SUPPLIER},
    )
    classification = classify_documents(
        [
            healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "9.99"}]),
            healthy_doc(2, created="2025-10-02T00:00:00Z"),
        ],
        context,
    )
    assert classification.accepted == ()
    assert [row.reason_codes for row in classification.review] == [
        (ReviewCode.MISSING_AMOUNT,),
        (ReviewCode.MISSING_AMOUNT,),
    ]


def test_amount_configured_no_entry_is_missing_amount() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(healthy_doc(1, custom_fields=[]), context)
    assert row.amount_inc_gst is None
    assert row.reason_codes == (ReviewCode.MISSING_AMOUNT,)
    assert row in classification.review


@pytest.mark.parametrize("entry", [None, "", "   ", "abc", "12 34", "$12.34", "AUDx1.00"])
def test_amount_configured_entry_garbage_is_unparseable(entry: object) -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": entry}]), context
    )
    assert row.amount_inc_gst is None
    assert row.reason_codes == (ReviewCode.AMOUNT_UNPARSEABLE,)
    assert row in classification.review


def test_amount_configured_valid_value_is_parsed_and_accepted() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "123.45"}]), context
    )
    assert row.amount_inc_gst == Decimal("123.45")
    assert row.reason_codes == ()
    assert row in classification.accepted


def test_amount_negative_gets_negative_amount() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "-50.00"}]), context
    )
    assert row.amount_inc_gst == Decimal("-50.00")
    assert row.reason_codes == (ReviewCode.NEGATIVE_AMOUNT,)
    assert row in classification.review


def test_amount_large_negative_gets_only_negative_amount() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "-5000.00"}]), context
    )
    assert row.reason_codes == (ReviewCode.NEGATIVE_AMOUNT,)
    assert row in classification.review


def test_amount_over_300_positive_only() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "350.00"}]), context
    )
    assert row.reason_codes == (ReviewCode.AMOUNT_OVER_300,)
    assert row.amount_inc_gst == Decimal("350.00")


@pytest.mark.parametrize("entry", ["300.00", "0.01", "150.25"])
def test_amount_at_or_under_300_has_no_amount_code(entry: str) -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": entry}]), context
    )
    assert row.reason_codes == ()


@pytest.mark.parametrize("entry", ["0", "0.00", "AUD0.00", "-0.00", "USD0"])
def test_zero_amount_goes_to_review_with_only_zero_amount(entry: str) -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": entry}]), context
    )
    assert row.amount_inc_gst == 0
    assert row.reason_codes == (ReviewCode.ZERO_AMOUNT,)
    assert row in classification.review
    assert classification.accepted == ()


def test_integer_zero_amount_gets_zero_amount() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": 0}]), context
    )
    assert row.reason_codes == (ReviewCode.ZERO_AMOUNT,)
    assert row in classification.review


def test_zero_amount_is_not_given_for_missing_or_unparseable_amounts() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, unparseable = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "abc"}]), context
    )
    assert unparseable.reason_codes == (ReviewCode.AMOUNT_UNPARSEABLE,)
    _, missing = classify_one(healthy_doc(2, custom_fields=[]), context)
    assert missing.reason_codes == (ReviewCode.MISSING_AMOUNT,)


def test_zero_amount_with_other_problems_carries_codes_in_declaration_order() -> None:
    context = make_context(settings_for(supplier=None))
    classification, _row = classify_one(
        healthy_doc(
            1,
            correspondent=None,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "0.00"},
                {"field": F_DATE.id, "value": "2025-06-30"},
            ],
        ),
        context,
    )
    assert review_codes(classification) == (
        ReviewCode.MISSING_SUPPLIER,
        ReviewCode.ZERO_AMOUNT,
        ReviewCode.DATE_OUT_OF_FY,
    )


def test_foreign_currency_amount_is_accepted_with_currency_and_note() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "USD5.00"}]), context
    )
    assert row.amount_inc_gst == Decimal("5.00")
    assert row.currency == "USD"
    assert row.raw_amount == "USD5.00"
    assert row.reason_codes == ()
    assert row.notes_for_agent.endswith(
        "foreign currency USD; amount not converted to AUD; over-300 check not applied to USD"
    )
    assert row in classification.accepted


def test_amount_over_300_is_not_applied_to_a_foreign_currency() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "USD350.00"}]), context
    )
    assert row.reason_codes == ()
    assert "over-300 check not applied to USD" in row.notes_for_agent
    assert row in classification.accepted


def test_negative_foreign_currency_amount_still_gets_negative_amount() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "USD-5.00"}]), context
    )
    assert row.reason_codes == (ReviewCode.NEGATIVE_AMOUNT,)


def test_aud_amount_has_currency_aud_and_no_foreign_note() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(healthy_doc(1), context)
    assert row.currency == "AUD"
    assert "foreign" not in row.notes_for_agent
    assert "over-300" not in row.notes_for_agent


def test_currency_is_blank_when_the_amount_is_missing_or_unparseable() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, missing = classify_one(healthy_doc(1, custom_fields=[]), context)
    _, garbage = classify_one(
        healthy_doc(2, custom_fields=[{"field": F_AMOUNT.id, "value": "12 34"}]), context
    )
    assert missing.currency == ""
    assert garbage.currency == ""


def test_amount_default_currency_comes_from_the_field_definition() -> None:
    usd_amount = CustomFieldDef(
        id=10,
        name="Transaction Amount",
        data_type="monetary",
        extra_data={"default_currency": "USD"},
    )
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        field_defs={usd_amount.name: usd_amount},
    )
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "10.00"}]), context
    )
    assert row.amount_inc_gst == Decimal("10.00")
    assert row.currency == "USD"
    assert row.reason_codes == ()
    assert row in classification.accepted


def test_amount_int_value_parses() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": 500}]), context
    )
    assert row.amount_inc_gst == Decimal(500)
    assert row.reason_codes == (ReviewCode.AMOUNT_OVER_300,)


# --------------------------------------------------------------------------- #
# Supplier
# --------------------------------------------------------------------------- #


def test_supplier_none_uses_correspondent() -> None:
    context = make_context(
        settings_for(supplier=None), correspondents={CORRESPONDENT_ID: SUPPLIER_NAME}
    )
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]), context
    )
    assert row.supplier_name == SUPPLIER_NAME
    assert row.reason_codes == ()
    assert row in classification.accepted


def test_supplier_none_without_correspondent_is_missing_supplier() -> None:
    context = make_context(settings_for(supplier=None))
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]), context
    )
    assert row.supplier_name == ""
    assert row.reason_codes == (ReviewCode.MISSING_SUPPLIER,)
    assert row in classification.review


def test_supplier_configured_no_entry_uses_correspondent() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]), context
    )
    assert row.supplier_name == SUPPLIER_NAME
    assert ReviewCode.MISSING_SUPPLIER not in row.reason_codes
    assert row in classification.accepted


def test_supplier_configured_blank_entry_is_missing_supplier() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_SUPPLIER.id, "value": "   "},
            ],
        ),
        context,
    )
    assert ReviewCode.MISSING_SUPPLIER in row.reason_codes
    assert row in classification.review


def test_supplier_configured_value_is_used() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_SUPPLIER.id, "value": "Acme Pty Ltd"},
            ],
        ),
        context,
    )
    assert row.supplier_name == "Acme Pty Ltd"
    assert ReviewCode.MISSING_SUPPLIER not in row.reason_codes
    assert row in classification.accepted


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #


def _duplicate_docs(count: int) -> list[Document]:
    return [
        healthy_doc(
            doc_id,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_DATE.id, "value": "2025-11-05"},
                {"field": F_SUPPLIER.id, "value": "Acme Pty Ltd"},
            ],
        )
        for doc_id in range(1, count + 1)
    ]


def test_duplicate_group_flags_every_member() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification = classify_documents(_duplicate_docs(2), context)
    assert [row.reason_codes for row in classification.accepted + classification.review] == [
        (ReviewCode.DUPLICATE_SUSPECT,),
        (ReviewCode.DUPLICATE_SUSPECT,),
    ]


def test_duplicate_group_of_three_flags_all_three() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification = classify_documents(_duplicate_docs(3), context)
    rows = classification.accepted + classification.review
    assert len(rows) == 3
    assert all(row.reason_codes == (ReviewCode.DUPLICATE_SUSPECT,) for row in rows)


def test_documents_with_different_amounts_are_not_duplicates() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    docs = [
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_DATE.id, "value": "2025-11-05"},
                {"field": F_SUPPLIER.id, "value": "Acme Pty Ltd"},
            ],
        ),
        healthy_doc(
            2,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "101.00"},
                {"field": F_DATE.id, "value": "2025-11-05"},
                {"field": F_SUPPLIER.id, "value": "Acme Pty Ltd"},
            ],
        ),
    ]
    classification = classify_documents(docs, context)
    assert classification.review == ()
    assert len(classification.accepted) == 2


def test_two_missing_amount_rows_with_same_supplier_and_date_are_flagged() -> None:
    """Literal grouping rule: the key is (supplier, date, amount, currency)."""
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    docs = [
        healthy_doc(1, custom_fields=[{"field": F_DATE.id, "value": "2025-11-05"}]),
        healthy_doc(2, custom_fields=[{"field": F_DATE.id, "value": "2025-11-05"}]),
    ]
    classification = classify_documents(docs, context)
    assert len(classification.accepted) == 0
    assert all(ReviewCode.DUPLICATE_SUSPECT in row.reason_codes for row in classification.review)
    assert all(ReviewCode.MISSING_AMOUNT in row.reason_codes for row in classification.review)


# --------------------------------------------------------------------------- #
# Income tag
# --------------------------------------------------------------------------- #


def test_income_tagged_flags_documents_with_an_income_tag() -> None:
    tags = {1: "deduct", 2: "income", 3: "dividend"}
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        tags=tags,
        income_tag_ids=frozenset({2, 3}),
    )
    classification = classify_documents(
        [
            healthy_doc(1, tags=[1], custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]),
            healthy_doc(2, tags=[2], custom_fields=[{"field": F_AMOUNT.id, "value": "101.00"}]),
            healthy_doc(3, tags=[1, 3], custom_fields=[{"field": F_AMOUNT.id, "value": "102.00"}]),
        ],
        context,
    )
    assert [row.document_id for row in classification.review] == [2, 3]
    assert all(ReviewCode.INCOME_TAGGED in row.reason_codes for row in classification.review)
    assert [row.document_id for row in classification.accepted] == [1]


# --------------------------------------------------------------------------- #
# Code ordering, accepted/review split, sorting
# --------------------------------------------------------------------------- #


def review_codes(classification: Classification) -> tuple[ReviewCode, ...]:
    return classification.review[0].reason_codes


def test_multi_code_ordering_follows_enum_declaration() -> None:
    context = make_context(settings_for(supplier=None))
    classification, _row = classify_one(
        healthy_doc(
            1,
            correspondent=None,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "-50.00"},
                {"field": F_DATE.id, "value": "2025-06-30"},
            ],
        ),
        context,
    )
    assert review_codes(classification) == (
        ReviewCode.MISSING_SUPPLIER,
        ReviewCode.NEGATIVE_AMOUNT,
        ReviewCode.DATE_OUT_OF_FY,
    )


def test_duplicate_and_income_order_after_other_codes() -> None:
    tags = {1: "deduct", 2: "income"}
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        tags=tags,
        income_tag_ids=frozenset({2}),
    )

    def same(doc_id: int) -> Document:
        return healthy_doc(
            doc_id,
            tags=[2],
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_DATE.id, "value": "2025-11-05"},
                {"field": F_SUPPLIER.id, "value": "Acme Pty Ltd"},
            ],
        )

    classification = classify_documents([same(1), same(2)], context)
    assert [row.reason_codes for row in classification.review] == [
        (ReviewCode.DUPLICATE_SUSPECT, ReviewCode.INCOME_TAGGED),
        (ReviewCode.DUPLICATE_SUSPECT, ReviewCode.INCOME_TAGGED),
    ]


def test_every_document_lands_in_exactly_one_list() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    docs = [
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]),
        healthy_doc(2, custom_fields=[{"field": F_AMOUNT.id, "value": "-5.00"}]),
        healthy_doc(3, custom_fields=[{"field": F_AMOUNT.id, "value": "200.00"}]),
    ]
    classification = classify_documents(docs, context)
    ids = {row.document_id for row in classification.accepted}
    ids.update(row.document_id for row in classification.review)
    assert ids == {1, 2, 3}
    assert len(classification.accepted) + len(classification.review) == 3


def test_rows_are_sorted_by_date_then_document_id() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    docs = [
        healthy_doc(9, custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]),
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "101.00"}]),
        healthy_doc(5, custom_fields=[{"field": F_AMOUNT.id, "value": "102.00"}]),
    ]
    classification = classify_documents(docs, context)
    assert [row.document_id for row in classification.accepted] == [1, 5, 9]


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


def test_notes_cover_only_document_level_informational_gaps() -> None:
    context = make_context(
        correspondents={CORRESPONDENT_ID: SUPPLIER_NAME},
        field_defs={
            F_AMOUNT.name: F_AMOUNT,
            F_GST.name: F_GST,
            F_WORK_USE.name: F_WORK_USE,
            F_DATE.name: F_DATE,
        },
    )
    _, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_DATE.id, "value": None}]), context
    )
    assert row.notes_for_agent == "amount missing; date from created"


def test_notes_are_empty_when_there_are_no_gaps() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(healthy_doc(1), context)
    assert row.notes_for_agent == ""


def test_amount_note_only_when_the_amount_cell_is_blank() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "12 34"}]), context
    )
    assert "amount missing" in row.notes_for_agent


# --------------------------------------------------------------------------- #
# GST and work-use values are not part of the output
# --------------------------------------------------------------------------- #


def test_absent_gst_and_work_use_entries_add_no_note_or_code() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "100.00"},
                {"field": F_DATE.id, "value": "2025-11-05"},
                {"field": F_SUPPLIER.id, "value": "Acme Pty Ltd"},
            ],
        ),
        context,
    )
    assert row.notes_for_agent == ""
    assert row.reason_codes == ()
    assert row in classification.accepted


def test_gst_and_work_use_values_do_not_change_the_row() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, plain = classify_one(
        healthy_doc(1, custom_fields=[{"field": F_AMOUNT.id, "value": "110.00"}]), context
    )
    _, with_values = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "110.00"},
                {"field": F_GST.id, "value": "10.00"},
                {"field": F_WORK_USE.id, "value": 50.0},
            ],
        ),
        context,
    )
    assert with_values == plain


# --------------------------------------------------------------------------- #
# Zero-populated fields and the real defaults
# --------------------------------------------------------------------------- #


def test_zero_populated_fields_still_classify_every_row_into_review() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    documents = [
        healthy_doc(1, correspondent=None, custom_fields=[]),
        healthy_doc(2, correspondent=None, custom_fields=[{"field": F_AMOUNT.id, "value": None}]),
        healthy_doc(
            3, correspondent=None, custom_fields=[{"field": F_DATE.id, "value": "2025-11-05"}]
        ),
    ]
    classification = classify_documents(documents, context)
    assert len(classification.accepted) == 0
    assert len(classification.review) == 3
    by_id = {row.document_id: row for row in classification.review}
    # No entry -> MISSING_AMOUNT; entry present but null -> AMOUNT_UNPARSEABLE.
    assert ReviewCode.MISSING_AMOUNT in by_id[1].reason_codes
    assert ReviewCode.AMOUNT_UNPARSEABLE in by_id[2].reason_codes
    assert ReviewCode.MISSING_AMOUNT in by_id[3].reason_codes
    assert all(ReviewCode.MISSING_SUPPLIER in row.reason_codes for row in classification.review)


def test_default_settings_accept_a_transaction_amount_row() -> None:
    context = make_context(Settings(), correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    classification, row = classify_one(
        healthy_doc(
            1,
            custom_fields=[
                {"field": F_AMOUNT.id, "value": "33.00"},
                {"field": F_DATE.id, "value": "2025-11-05"},
            ],
        ),
        context,
    )
    assert row.amount_inc_gst == Decimal("33.00")
    assert row.date == "2025-11-05"
    assert row.supplier_name == SUPPLIER_NAME
    assert row.reason_codes == ()
    assert row in classification.accepted


def test_unknown_tag_ids_fall_back_to_hashtag_in_row() -> None:
    context = make_context(correspondents={CORRESPONDENT_ID: SUPPLIER_NAME})
    _, row = classify_one(
        healthy_doc(1, tags=[77], custom_fields=[{"field": F_AMOUNT.id, "value": "100.00"}]),
        context,
    )
    assert row.tags == ("#77",)
