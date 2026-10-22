"""The bill and work-from-home rows of ``estimate.csv``.

Two WFH methods are shown side by side as illustrative scenarios: a fixed rate
(hours x rate) and an actual-cost view (internet and phone by annual cost x a
work-use percentage, plus the owner's own energy figure). The method is undecided and
only one applies to a year, so neither figure is added to the base total: the
scenarios only show ``base deductions + method`` next to it.

The inputs are the owner's own assumptions (``WFH_*`` variables), not tax rules. A bill
counts only when :attr:`BillRow.counts_toward_wfh`. All arithmetic is ``Decimal`` from
strings, rounded to cents with ``ROUND_HALF_UP``.

Row order (fixed): ``bills`` (total, then electricity, internet, phone, gas, ``(no kind)``),
``wfh_evidence`` (internet, phone, energy, stationery), ``wfh_fixed_rate``,
``wfh_actual_cost`` (internet, phone, energy, total), ``wfh_scenarios``, ``wfh_note``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from functools import partial

from paperless_tax_export.bills import BillRow
from paperless_tax_export.estimate import (
    AUD,
    BillsSection,
    EstimateRow,
    convert_to_aud,
    excluded_note,
)
from paperless_tax_export.settings import BILL_KINDS, WfhInputs

NO_KIND_LABEL = "(no kind)"
NO_BILL_NOTE = "no bill found"
STATIONERY_NOTE = "not tracked by this tool; keep your own bill or receipt for it"
SCENARIO_NOTE = "illustrative; one method per year; not added to the base total"
WFH_NOTE = (
    "illustrative only: fixed rate covers internet, phone and energy, so those bills are not"
    " also claimed in deductions.csv; your method is decided with your accountant"
)
ENERGY_NOTE = "your own appliance-based figure; not computed from bills"

_CENT = Decimal("0.01")
_HUNDRED = Decimal(100)


@dataclass(slots=True)
class _Tally:
    count: int = 0
    total_aud: Decimal = Decimal(0)
    unconverted: Counter[str] = field(default_factory=Counter)

    def add(self, bill: BillRow, rates: Mapping[str, Decimal]) -> None:
        self.count += 1
        if bill.amount_inc_gst is None:
            return
        aud = convert_to_aud(bill.amount_inc_gst, bill.currency, rates)
        if aud is None:
            self.unconverted[bill.currency] += 1
        else:
            self.total_aud += aud

    @property
    def note(self) -> str:
        return excluded_note(self.unconverted)


def make_bills_section(
    bills: Sequence[BillRow], rates: Mapping[str, Decimal], wfh: WfhInputs
) -> Callable[[Decimal], BillsSection]:
    """A builder for ``build_estimate``: it takes the deductions total and returns the rows."""
    return partial(build_bills_section, bills, rates, wfh)


def build_bills_section(
    bills: Sequence[BillRow],
    rates: Mapping[str, Decimal],
    wfh: WfhInputs,
    deductions_aud: Decimal,
) -> BillsSection:
    """The bill, evidence and WFH scenario rows, in their fixed order."""
    total = _Tally()
    by_kind: dict[str, _Tally] = {kind: _Tally() for kind in (*BILL_KINDS, "")}
    for bill in bills:
        if bill.counts_toward_wfh:
            total.add(bill, rates)
            by_kind[bill.kind].add(bill, rates)

    rows: list[EstimateRow] = [
        EstimateRow(
            "bills",
            "total",
            count=total.count,
            currency=AUD,
            amount_aud=total.total_aud,
            notes=total.note,
        )
    ]
    for kind, tally in by_kind.items():
        rows.append(
            EstimateRow(
                "bills",
                kind or NO_KIND_LABEL,
                count=tally.count,
                currency=AUD,
                amount_aud=tally.total_aud,
                notes=tally.note,
            )
        )

    rows.extend(_evidence_rows(by_kind))
    fixed = _fixed_rate(wfh)
    rows.append(fixed.row)
    actual_rows, actual_total, actual_missing = _actual_cost_rows(by_kind, wfh)
    rows.extend(actual_rows)
    rows.extend(_scenario_rows(deductions_aud, fixed.amount, actual_total, actual_missing))
    rows.append(EstimateRow("wfh_note", "illustrative only", notes=WFH_NOTE))
    return BillsSection(
        rows=tuple(rows),
        total_aud=total.total_aud,
        unconverted_currencies=tuple(sorted(total.unconverted)),
    )


def _evidence_rows(by_kind: Mapping[str, _Tally]) -> list[EstimateRow]:
    counts = {
        "internet": by_kind["internet"].count,
        "phone": by_kind["phone"].count,
        "energy": by_kind["electricity"].count + by_kind["gas"].count,
    }
    rows = [
        EstimateRow("wfh_evidence", expense, count=count, notes="" if count else NO_BILL_NOTE)
        for expense, count in counts.items()
    ]
    rows.append(EstimateRow("wfh_evidence", "stationery", notes=STATIONERY_NOTE))
    return rows


@dataclass(frozen=True, slots=True)
class _Fixed:
    row: EstimateRow
    amount: Decimal | None


def _fixed_rate(wfh: WfhInputs) -> _Fixed:
    label = "hours x rate"
    if wfh.hours is None or wfh.rate_per_hour is None:
        row = EstimateRow(
            "wfh_fixed_rate",
            label,
            currency=AUD,
            notes="not computed: set WFH_HOURS and WFH_RATE_PER_HOUR",
        )
        return _Fixed(row, None)
    amount = (wfh.hours * wfh.rate_per_hour).quantize(_CENT, rounding=ROUND_HALF_UP)
    row = EstimateRow(
        "wfh_fixed_rate",
        label,
        currency=AUD,
        amount_aud=amount,
        notes=f"{wfh.hours:f} hours x AUD {wfh.rate_per_hour:f} per hour",
    )
    return _Fixed(row, amount)


def _actual_cost_rows(
    by_kind: Mapping[str, _Tally], wfh: WfhInputs
) -> tuple[list[EstimateRow], Decimal | None, list[str]]:
    """The actual-cost rows, the total of the computed ones (``None``: none) and missing names."""
    parts: list[tuple[str, str, Decimal | None, str]] = []
    for label, variable, pct in (
        ("internet", "WFH_INTERNET_PCT", wfh.internet_pct),
        ("phone", "WFH_PHONE_PCT", wfh.phone_pct),
    ):
        tally = by_kind[label]
        amount = None if pct is None else _share(tally.total_aud, pct)
        detail = "" if pct is None else f"{label} bills AUD {tally.total_aud:.2f} x {pct:f}%"
        parts.append(
            (label, variable, amount, "; ".join(item for item in (detail, tally.note) if item))
        )
    parts.append(("energy", "WFH_ENERGY_AMOUNT", wfh.energy_amount, ENERGY_NOTE))

    rows: list[EstimateRow] = []
    computed: list[Decimal] = []
    missing: list[str] = []
    for label, variable, amount, note in parts:
        if amount is None:
            missing.append(variable)
            rows.append(
                EstimateRow(
                    "wfh_actual_cost", label, currency=AUD, notes=f"not computed: set {variable}"
                )
            )
        else:
            computed.append(amount)
            rows.append(
                EstimateRow("wfh_actual_cost", label, currency=AUD, amount_aud=amount, notes=note)
            )
    total = sum(computed, Decimal(0)) if computed else None
    if not missing:
        total_note = ""
    elif computed:
        total_note = f"partial; not computed: set {', '.join(missing)}"
    else:
        total_note = f"not computed: set {', '.join(missing)}"
    rows.append(
        EstimateRow("wfh_actual_cost", "total", currency=AUD, amount_aud=total, notes=total_note)
    )
    return rows, total, missing


def _share(total: Decimal, pct: Decimal) -> Decimal:
    return (total * pct / _HUNDRED).quantize(_CENT, rounding=ROUND_HALF_UP)


def _scenario_rows(
    deductions_aud: Decimal,
    fixed: Decimal | None,
    actual: Decimal | None,
    actual_missing: Sequence[str],
) -> list[EstimateRow]:
    rows: list[EstimateRow] = []
    if fixed is not None:
        rows.append(
            EstimateRow(
                "wfh_scenarios",
                "base deductions + fixed rate",
                currency=AUD,
                amount_aud=deductions_aud + fixed,
                notes=SCENARIO_NOTE,
            )
        )
    if actual is not None:
        note = SCENARIO_NOTE
        if actual_missing:
            note += f"; partial, not computed: {', '.join(actual_missing)}"
        rows.append(
            EstimateRow(
                "wfh_scenarios",
                "base deductions + actual cost",
                currency=AUD,
                amount_aud=deductions_aud + actual,
                notes=note,
            )
        )
    return rows
