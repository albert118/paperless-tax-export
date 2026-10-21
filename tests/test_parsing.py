"""Tests for amount and date parsing and select-label resolution."""

from __future__ import annotations

from decimal import Decimal

import pytest

from paperless_tax_export.parsing import (
    ParsedAmount,
    ReviewCode,
    parse_amount,
    parse_date,
    resolve_select_label,
)


def test_parse_amount_accepts_aud_prefixed_value() -> None:
    parsed = parse_amount("AUD12.34", default_currency=None)
    assert parsed == ParsedAmount(Decimal("12.34"), "AUD", None)


def test_parse_amount_accepts_bare_value_defaulting_to_aud() -> None:
    parsed = parse_amount("12.34", default_currency=None)
    assert parsed == ParsedAmount(Decimal("12.34"), "AUD", None)


def test_parse_amount_accepts_negative_value_after_prefix() -> None:
    parsed = parse_amount("AUD-12.34", default_currency=None)
    assert parsed == ParsedAmount(Decimal("-12.34"), "AUD", None)


def test_parse_amount_handles_none() -> None:
    parsed = parse_amount(None, default_currency=None)
    assert parsed == ParsedAmount(None, None, ReviewCode.AMOUNT_UNPARSEABLE)


def test_parse_amount_handles_blank_strings() -> None:
    for raw in ("", "   "):
        parsed = parse_amount(raw, default_currency=None)
        assert parsed == ParsedAmount(None, None, ReviewCode.AMOUNT_UNPARSEABLE)


def test_parse_amount_rejects_currency_symbol() -> None:
    parsed = parse_amount("$12.34", default_currency=None)
    assert parsed.amount is None
    assert parsed.currency is None
    assert parsed.code == ReviewCode.AMOUNT_UNPARSEABLE


def test_parse_amount_keeps_a_foreign_amount_and_currency_without_a_code() -> None:
    parsed = parse_amount("USD5.00", default_currency=None)
    assert parsed.amount == Decimal("5.00")
    assert parsed.currency == "USD"
    assert parsed.code is None


def test_parse_amount_uses_default_currency_when_no_prefix() -> None:
    parsed = parse_amount("5.00", default_currency="USD")
    assert parsed.amount == Decimal("5.00")
    assert parsed.currency == "USD"
    assert parsed.code is None


def test_parse_amount_accepts_thousands_separators() -> None:
    parsed = parse_amount("1,234.50", default_currency=None)
    assert parsed == ParsedAmount(Decimal("1234.50"), "AUD", None)


def test_parse_amount_rejects_garbage_and_bad_grouping() -> None:
    for raw in ("abc", "12 34", "1,23", "12,3456", "--5", "AUDx1.00"):
        parsed = parse_amount(raw, default_currency=None)
        assert parsed.amount is None
        assert parsed.code == ReviewCode.AMOUNT_UNPARSEABLE


def test_parse_amount_accepts_zero() -> None:
    parsed = parse_amount("0.00", default_currency=None)
    assert parsed == ParsedAmount(Decimal("0.00"), "AUD", None)
    assert parse_amount("0", default_currency=None).amount == Decimal(0)


def test_parse_amount_accepts_integers_exactly() -> None:
    parsed = parse_amount(3, default_currency=None)
    assert parsed == ParsedAmount(Decimal(3), "AUD", None)


def test_parse_amount_int_uses_default_currency() -> None:
    parsed = parse_amount(3, default_currency="USD")
    assert parsed.amount == Decimal(3)
    assert parsed.currency == "USD"
    assert parsed.code is None


def test_parse_amount_never_converts_a_failure_to_zero() -> None:
    parsed = parse_amount("oops", default_currency=None)
    assert parsed.amount is None
    assert parsed == ParsedAmount(None, None, ReviewCode.AMOUNT_UNPARSEABLE)


def test_parse_amount_code_is_none_for_zero_amounts() -> None:
    assert parse_amount("0.00", default_currency=None).code is None


@pytest.mark.parametrize("raw", ["2025-06-30", "2025-07-01", "2026-06-30", "2026-07-01"])
def test_parse_date_returns_iso_strings_for_valid_dates(raw: str) -> None:
    assert parse_date(raw) == raw


@pytest.mark.parametrize(
    "raw", ["17/05/2025", "garbage", "2025-13-01", "2025-02-30", "2025-6-30", "", None]
)
def test_parse_date_rejects_invalid_input(raw: str | None) -> None:
    assert parse_date(raw) is None


def test_resolve_select_label_matches_ids_as_strings() -> None:
    options: list[dict[str, object]] = [
        {"id": 1, "label": "Home"},
        {"id": "2", "label": "Office"},
    ]
    assert resolve_select_label(1, options) == "Home"
    assert resolve_select_label("1", options) == "Home"
    assert resolve_select_label("2", options) == "Office"
    assert resolve_select_label(2, options) == "Office"


def test_resolve_select_label_returns_none_for_unknown_ids() -> None:
    options: list[dict[str, object]] = [{"id": 1, "label": "Home"}]
    assert resolve_select_label(99, options) is None
    assert resolve_select_label(None, options) is None


def test_resolve_select_label_returns_none_when_label_is_missing() -> None:
    options: list[dict[str, object]] = [{"id": 1}]
    assert resolve_select_label(1, options) is None
