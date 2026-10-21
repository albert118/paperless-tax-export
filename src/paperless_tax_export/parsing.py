"""Parsing of amounts and dates from Paperless custom-field values.

Amounts are read as strings only; ``Decimal`` is built from a strict regex so
garbage input is flagged rather than silently coerced to zero. No ``float``
ever appears in an amount path. Dates are strict ISO ``YYYY-MM-DD`` strings so
they can be compared lexicographically afterwards.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class ReviewCode(StrEnum):
    """Reason codes attached to documents that need a human decision.

    Every code from the requirements is declared here up front so the later
    review stages reuse a single enumeration instead of redefining strings.
    """

    MISSING_DATE = "MISSING_DATE"
    MISSING_AMOUNT = "MISSING_AMOUNT"
    MISSING_SUPPLIER = "MISSING_SUPPLIER"
    AMOUNT_UNPARSEABLE = "AMOUNT_UNPARSEABLE"
    NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
    DATE_OUT_OF_FY = "DATE_OUT_OF_FY"
    AMOUNT_OVER_300 = "AMOUNT_OVER_300"
    DUPLICATE_SUSPECT = "DUPLICATE_SUSPECT"
    INCOME_TAGGED = "INCOME_TAGGED"


@dataclass(frozen=True)
class ParsedAmount:
    """The result of parsing one amount value, including a review code."""

    amount: Decimal | None
    currency: str | None
    code: ReviewCode | None


# Optional 3-letter uppercase currency prefix, then an optionally signed number
# with grouped thousands separators and optional decimals.
_AMOUNT_RE = re.compile(
    r"^(?P<cur>[A-Z]{3})?(?P<num>[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)$"
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_amount(raw: str | int | None, *, default_currency: str | None) -> ParsedAmount:
    """Parse an amount value without ever raising.

    A failed parse yields ``ParsedAmount(None, None, AMOUNT_UNPARSEABLE)`` —
    never a fabricated zero. The amount is returned with its currency code;
    deciding what a non-AUD currency means is the caller's job.
    """
    if raw is None:
        return ParsedAmount(None, None, ReviewCode.AMOUNT_UNPARSEABLE)

    if isinstance(raw, int):
        currency = default_currency or "AUD"
        return ParsedAmount(amount=Decimal(raw), currency=currency, code=None)

    stripped = raw.strip()
    if not stripped:
        return ParsedAmount(None, None, ReviewCode.AMOUNT_UNPARSEABLE)

    match = _AMOUNT_RE.match(stripped)
    if match is None:
        return ParsedAmount(None, None, ReviewCode.AMOUNT_UNPARSEABLE)

    currency = match.group("cur") or default_currency or "AUD"
    try:
        amount = Decimal(match.group("num").replace(",", ""))
    except InvalidOperation, ValueError:
        return ParsedAmount(None, None, ReviewCode.AMOUNT_UNPARSEABLE)
    return ParsedAmount(amount=amount, currency=currency, code=None)


def parse_date(raw: str | None) -> str | None:
    """Return *raw* as a strict ``YYYY-MM-DD`` ISO string, or ``None``."""
    if raw is None or _ISO_DATE_RE.match(raw) is None:
        return None
    try:
        return datetime.date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def resolve_select_label(
    value: str | int | None,
    options: Sequence[Mapping[str, Any]],
) -> str | None:
    """Resolve a select-field option id to its label, or ``None``.

    Option ids are compared as strings so an int id and a "1" string agree.
    An unknown id returns ``None`` so the caller can keep the raw value.
    """
    if value is None:
        return None
    needle = str(value)
    for option in options:
        if str(option.get("id")) == needle:
            label = option.get("label")
            return label if isinstance(label, str) else None
    return None
