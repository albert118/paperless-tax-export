"""The ``estimate.csv`` figure: an estimate of total deductions, in AUD.

Pure logic over classified rows. The only I/O is :func:`load_rates`, which reads the
owner's rates file. All arithmetic is ``Decimal`` built from strings, never ``float``.

Each row's AUD amount is ``amount x rate`` rounded to cents with ``ROUND_HALF_UP``
(AUD rows are rounded the same way, with rate 1), and the AUD totals are sums of those
per-row amounts. The tax saving is ``total AUD x marginal rate`` rounded to cents with
``ROUND_HALF_UP``. A row whose currency has no rate is excluded from every AUD figure and
counted in a note, never dropped silently.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from paperless_tax_export.classify import ClassifiedRow
from paperless_tax_export.csv_output import formula_guard, render_table_bytes
from paperless_tax_export.errors import EstimateError, SettingsError

AUD = "AUD"
NO_TYPE_LABEL = "(none)"
RATES_HEADER = ("currency", "rate_to_aud")
ESTIMATE_COLUMNS = (
    "section",
    "label",
    "count",
    "currency",
    "amount",
    "rate_to_aud",
    "amount_aud",
    "notes",
)

DISCLAIMER_LABEL = "ESTIMATE only; not tax advice"
DISCLAIMER_NOTE = (
    "from document amounts in this run, converted with the rates file and the marginal rate"
    " you supplied; it ignores the rest of your return"
)
NO_RATE_NOTE = "no rate; excluded from the AUD total"
TAX_SAVING_NOTE = (
    "rough: marginal rate only, ignores Medicare levy, offsets and the rest of your return"
)

_CENT = Decimal("0.01")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
# At most 6 integer digits (no leading zeros) and 12 fraction digits, so a rate can never
# exhaust the Decimal context.
_POSITIVE_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,12})?")
PENDING_NOTE = "upper bound: includes suspected duplicates and items that may never be deductions"


@dataclass(frozen=True, slots=True)
class Rates:
    """Exchange rates to AUD and the SHA-256 of the file they came from (``None``: no file)."""

    by_currency: Mapping[str, Decimal] = field(default_factory=dict)
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class EstimateRow:
    """One line of ``estimate.csv``; ``None`` cells render blank."""

    section: str
    label: str
    count: int | None = None
    currency: str = ""
    amount: Decimal | None = None
    rate: Decimal | None = None
    amount_aud: Decimal | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class Estimate:
    """The estimate rows plus the figures the console summary prints."""

    rows: tuple[EstimateRow, ...]
    deductions_aud: Decimal
    pending_aud: Decimal
    tax_saving: Decimal | None
    unconverted_currencies: tuple[str, ...]
    bills_aud: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class BillsSection:
    """The bill and WFH rows built by :mod:`paperless_tax_export.wfh_estimate`.

    ``total_aud`` is the AUD total of the bills counted; ``unconverted_currencies`` are the
    currencies of bills left out of it for lack of a rate.
    """

    rows: tuple[EstimateRow, ...]
    total_aud: Decimal
    unconverted_currencies: tuple[str, ...] = ()


def parse_positive_decimal(text: str) -> Decimal | None:
    """A plain positive-or-zero decimal string as ``Decimal``, else ``None``.

    Accepts ASCII digits with an optional fraction only (at most 6 integer digits without
    leading zeros, at most 12 fraction digits): no sign, exponent, thousands
    separator, ``NaN`` or ``Infinity``. The value never passes through ``float``.
    """
    return Decimal(text) if _POSITIVE_DECIMAL_RE.fullmatch(text) else None


def load_rates(path: Path) -> Rates:
    """Read and parse the rates file; errors are ``SettingsError`` naming the file."""
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SettingsError(
            f"cannot read rates file '{path}': {exc.strerror or 'I/O error'}"
        ) from exc
    return Rates(by_currency=parse_rates(content, str(path)), sha256=_sha256(content))


def parse_rates(content: bytes, source: str) -> dict[str, Decimal]:
    """Parse a rates file (``currency,rate_to_aud`` header, UTF-8 with or without BOM).

    Errors name *source* and the line, and at most the offending field name.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SettingsError(f"rates file '{source}' is not valid UTF-8") from exc
    reader = csv.reader(io.StringIO(text, newline=""))
    rates: dict[str, Decimal] = {}
    try:
        header = next(reader, None)
        if header is None or tuple(header) != RATES_HEADER:
            raise SettingsError(
                f"rates file '{source}' line 1: header must be exactly {','.join(RATES_HEADER)}"
            )
        for cells in reader:
            if cells:
                code, rate = _parse_rate_row(cells, f"rates file '{source}' line {reader.line_num}")
                if code in rates:
                    raise SettingsError(
                        f"rates file '{source}' line {reader.line_num}: duplicate currency"
                    )
                rates[code] = rate
    except csv.Error as exc:
        raise SettingsError(f"rates file '{source}' is not readable as CSV") from exc
    return rates


def _parse_rate_row(cells: Sequence[str], where: str) -> tuple[str, Decimal]:
    if len(cells) != len(RATES_HEADER):
        raise SettingsError(f"{where}: expected {len(RATES_HEADER)} columns")
    code, raw_rate = cells
    if code == AUD:
        raise SettingsError(f"{where}: currency must not be {AUD}; AUD is always 1")
    if _CURRENCY_RE.fullmatch(code) is None:
        raise SettingsError(f"{where}: currency must be three uppercase letters")
    rate = parse_positive_decimal(raw_rate)
    if rate is None:
        raise SettingsError(
            f"{where}: rate_to_aud must be a positive decimal number "
            "(at most 6 integer and 12 fraction digits, no leading zeros)"
        )
    if rate <= 0:
        raise SettingsError(f"{where}: rate_to_aud must be greater than zero")
    return code, rate


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _to_aud(row: ClassifiedRow, rates: Mapping[str, Decimal]) -> Decimal | None:
    """The row's AUD amount, rounded to cents, or ``None`` when its currency has no rate."""
    if row.amount_inc_gst is None:
        return None
    return convert_to_aud(row.amount_inc_gst, row.currency, rates)


def convert_to_aud(amount: Decimal, currency: str, rates: Mapping[str, Decimal]) -> Decimal | None:
    """*amount* in AUD rounded to cents, or ``None`` when *currency* has no rate."""
    if currency == AUD:
        return amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    rate = rates.get(currency)
    if rate is None:
        return None
    return (amount * rate).quantize(_CENT, rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class _Tally:
    """Count and converted AUD total of a group of rows, plus rows left out for lack of a rate."""

    count: int = 0
    total_aud: Decimal = Decimal(0)
    unconverted: Counter[str] = field(default_factory=Counter)

    def add(self, row: ClassifiedRow, aud: Decimal | None) -> None:
        self.count += 1
        if aud is None:
            if row.amount_inc_gst is not None:
                self.unconverted[row.currency] += 1
        else:
            self.total_aud += aud

    @property
    def note(self) -> str:
        return excluded_note(self.unconverted)


def excluded_note(unconverted: Mapping[str, int]) -> str:
    if not unconverted:
        return ""
    listed = ", ".join(f"{code} {unconverted[code]}" for code in sorted(unconverted))
    return f"excluded, no rate: {listed}"


def _tally_rows(
    rows: Sequence[ClassifiedRow], rates: Mapping[str, Decimal]
) -> list[tuple[ClassifiedRow, Decimal | None]]:
    return [(row, _to_aud(row, rates)) for row in rows]


def build_estimate(
    accepted: Sequence[ClassifiedRow],
    review: Sequence[ClassifiedRow],
    rates: Mapping[str, Decimal],
    marginal_rate: Decimal | None,
    bills_section: Callable[[Decimal], BillsSection] | None = None,
    payslip_rows: Sequence[EstimateRow] = (),
) -> Estimate:
    """Compute the estimate rows in their fixed order from the run's classified rows.

    *bills_section*, when given, is called with the deductions total and returns the bill and
    WFH rows; they follow ``deductions_by_month`` and precede ``pending``. *payslip_rows* follow
    them. Neither changes the deductions, pending or tax-saving figures.

    Raises ``EstimateError`` (exit 4, no value in the message) when an amount is so large
    that the ``Decimal`` arithmetic overflows its context.
    """
    try:
        return _build_estimate(accepted, review, rates, marginal_rate, bills_section, payslip_rows)
    except InvalidOperation as exc:
        raise EstimateError(
            "an amount is too large for the estimate arithmetic; check the document amounts"
        ) from exc


def _join_notes(*notes: str) -> str:
    return "; ".join(note for note in notes if note)


def _build_estimate(
    accepted: Sequence[ClassifiedRow],
    review: Sequence[ClassifiedRow],
    rates: Mapping[str, Decimal],
    marginal_rate: Decimal | None,
    bills_section: Callable[[Decimal], BillsSection] | None,
    payslip_rows: Sequence[EstimateRow],
) -> Estimate:
    claimable = _tally_rows(accepted, rates)
    pending = _tally_rows([row for row in review if row.amount_inc_gst is not None], rates)
    without_amount = sum(1 for row in review if row.amount_inc_gst is None)

    total = _Tally()
    by_currency: dict[str, _Tally] = {}
    by_type: dict[str, _Tally] = {}
    by_month: dict[str, _Tally] = {}
    for row, aud in claimable:
        total.add(row, aud)
        by_currency.setdefault(row.currency, _Tally()).add(row, aud)
        by_type.setdefault(row.document_type or NO_TYPE_LABEL, _Tally()).add(row, aud)
        by_month.setdefault(row.date[:7], _Tally()).add(row, aud)
    native: dict[str, Decimal] = {}
    for row, _aud in claimable:
        if row.amount_inc_gst is not None:
            native[row.currency] = native.get(row.currency, Decimal(0)) + row.amount_inc_gst

    pending_tally = _Tally()
    for row, aud in pending:
        pending_tally.add(row, aud)

    rows: list[EstimateRow] = [
        EstimateRow("estimate", DISCLAIMER_LABEL, notes=DISCLAIMER_NOTE),
        EstimateRow(
            "deductions",
            "total",
            count=total.count,
            currency=AUD,
            amount_aud=total.total_aud,
            notes=total.note,
        ),
    ]
    for code in sorted(by_currency):
        tally = by_currency[code]
        rate = None if code == AUD else rates.get(code)
        converted = code == AUD or rate is not None
        rows.append(
            EstimateRow(
                "deductions_currency",
                code,
                count=tally.count,
                currency=code,
                amount=native.get(code),
                rate=rate,
                amount_aud=tally.total_aud if converted else None,
                notes="" if converted else NO_RATE_NOTE,
            )
        )
    rows.extend(_group_rows("deductions_by_type", by_type))
    rows.extend(_group_rows("deductions_by_month", by_month))
    bills = None if bills_section is None else bills_section(total.total_aud)
    if bills is not None:
        rows.extend(bills.rows)
    rows.extend(payslip_rows)
    rows.append(
        EstimateRow(
            "pending",
            "review rows with an amount",
            count=pending_tally.count,
            currency=AUD,
            amount_aud=pending_tally.total_aud,
            notes=_join_notes(PENDING_NOTE, pending_tally.note),
        )
    )
    rows.append(
        EstimateRow("pending_no_amount", "review rows without an amount", count=without_amount)
    )

    tax_saving: Decimal | None = None
    if marginal_rate is not None:
        tax_saving = (total.total_aud * marginal_rate).quantize(_CENT, rounding=ROUND_HALF_UP)
        rows.append(
            EstimateRow(
                "tax_saving",
                f"estimated saving at marginal rate {marginal_rate:f}",
                currency=AUD,
                amount_aud=tax_saving,
                notes=TAX_SAVING_NOTE,
            )
        )

    unconverted = sorted(
        {row.currency for row, aud in (*claimable, *pending) if aud is None}.union(
            () if bills is None else bills.unconverted_currencies
        ).difference({""})
    )
    return Estimate(
        rows=tuple(rows),
        deductions_aud=total.total_aud,
        pending_aud=pending_tally.total_aud,
        tax_saving=tax_saving,
        unconverted_currencies=tuple(unconverted),
        bills_aud=Decimal(0) if bills is None else bills.total_aud,
    )


def _group_rows(section: str, groups: Mapping[str, _Tally]) -> list[EstimateRow]:
    return [
        EstimateRow(
            section,
            label,
            count=groups[label].count,
            currency=AUD,
            amount_aud=groups[label].total_aud,
            notes=groups[label].note,
        )
        for label in sorted(groups)
    ]


def render_estimate_bytes(estimate: Estimate) -> bytes:
    """The exact bytes of ``estimate.csv`` (BOM, ``\\r\\n``, guarded text, two-decimal amounts)."""
    return render_table_bytes(ESTIMATE_COLUMNS, (_cells(row) for row in estimate.rows))


def _cells(row: EstimateRow) -> list[str]:
    return [
        formula_guard(row.section),
        formula_guard(row.label),
        "" if row.count is None else str(row.count),
        formula_guard(row.currency),
        _money(row.amount),
        "" if row.rate is None else f"{row.rate:f}",
        _money(row.amount_aud),
        formula_guard(row.notes),
    ]


def _money(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.2f}"
