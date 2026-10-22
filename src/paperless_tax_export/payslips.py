"""The payslip hours ceiling of ``estimate.csv``.

Payslips are the documents of one Paperless document type (``salary``). A separate
extractor writes six custom fields on each: the pay period start and end and four hour
figures (ordinary, annual leave, personal leave, public holiday). This module reads only
those fields. It never reads a document's ``content`` or ``title``, and it has no field for
a pay amount, tax or identifier.

The figure is a ceiling, not evidence: ``ordinary hours - leave hours``, on the owner's
assumption that Ordinary Hours includes the leave hours paid. Payslips show hours paid and
leave, not where the work happened, so the ceiling bounds the WFH hours and proves none.

A payslip is usable when its two dates and its ordinary hours are valid. A leave field with
no entry on the document counts as zero (the extractor may omit zero leave); a leave entry
that is null or invalid makes the payslip unusable. Day arithmetic uses ``datetime.date``
only and all hours are ``Decimal`` built from strings.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from paperless_tax_export.classify import amount_raw
from paperless_tax_export.estimate import EstimateRow, parse_positive_decimal
from paperless_tax_export.models import CustomFieldDef, Document
from paperless_tax_export.parsing import parse_date
from paperless_tax_export.settings import Settings

SECTION = "wfh_hours_ceiling"
MAX_HOURS = Decimal(744)  # 31 days x 24 hours, per payslip and field
MAX_LISTED_GAPS = 5
MAX_LISTED_IDS = 20
DATE_KEYS = ("period_start", "period_end")
LEAVE_KEYS = ("annual_leave_hours", "personal_leave_hours", "public_holiday_hours")
REQUIRED_KEYS = (*DATE_KEYS, "ordinary_hours")
CEILING_NOTE = "ordinary hours minus leave hours; bounds WFH hours, proves none"

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Payslip:
    """The usable figures of one payslip document (dates and hours only)."""

    document_id: int
    period_start: datetime.date
    period_end: datetime.date
    ordinary_hours: Decimal
    annual_leave_hours: Decimal
    personal_leave_hours: Decimal
    public_holiday_hours: Decimal

    @property
    def leave_hours(self) -> Decimal:
        return self.annual_leave_hours + self.personal_leave_hours + self.public_holiday_hours


@dataclass(frozen=True, slots=True)
class PayslipReading:
    """What was read from Paperless: usable payslips, unusable ids and missing field keys.

    ``missing_keys`` are the keys of ``Settings.payslip_fields`` whose custom field does not
    exist in Paperless, in the fixed key order.
    """

    type_found: bool
    document_count: int = 0
    missing_keys: tuple[str, ...] = ()
    usable: tuple[Payslip, ...] = ()
    unusable_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PayslipSection:
    """The estimate rows, the console figures and the warnings for the payslip hours."""

    rows: tuple[EstimateRow, ...]
    warnings: tuple[str, ...]
    document_count: int = 0
    usable_count: int = 0
    unusable_count: int = 0
    ceiling: Decimal | None = None


def read_payslips(
    type_found: bool,
    documents: Sequence[Document],
    settings: Settings,
    resolved: Mapping[str, CustomFieldDef | None],
) -> PayslipReading:
    """Read the payslip custom fields of *documents*; *resolved* maps field name to its definition.

    A field missing from Paperless never raises: every payslip is unusable when a required
    field is missing, and the leave figures are not computed when a leave field is missing.
    """
    if not type_found:
        return PayslipReading(type_found=False)
    definitions = {key: resolved.get(name) for key, name in settings.payslip_fields.items()}
    missing = tuple(key for key in settings.payslip_fields if definitions[key] is None)
    usable: list[Payslip] = []
    unusable: list[int] = []
    for document in documents:
        payslip = _read_payslip(document, definitions)
        if payslip is None:
            unusable.append(document.id)
        else:
            usable.append(payslip)
    return PayslipReading(
        type_found=True,
        document_count=len(documents),
        missing_keys=missing,
        usable=tuple(usable),
        unusable_ids=tuple(unusable),
    )


def _read_payslip(
    document: Document, definitions: Mapping[str, CustomFieldDef | None]
) -> Payslip | None:
    values: dict[str, Decimal] = {}
    for key in ("ordinary_hours", *LEAVE_KEYS):
        hours = _read_hours(
            document, definitions[key], default=_ZERO if key in LEAVE_KEYS else None
        )
        if hours is None:
            return None
        values[key] = hours
    start = _read_date(document, definitions["period_start"])
    end = _read_date(document, definitions["period_end"])
    if start is None or end is None or end < start:
        return None
    return Payslip(
        document_id=document.id,
        period_start=start,
        period_end=end,
        ordinary_hours=values["ordinary_hours"],
        annual_leave_hours=values["annual_leave_hours"],
        personal_leave_hours=values["personal_leave_hours"],
        public_holiday_hours=values["public_holiday_hours"],
    )


def _read_date(document: Document, definition: CustomFieldDef | None) -> datetime.date | None:
    if definition is None or not document.has_field(definition.id):
        return None
    raw = document.field_value(definition.id)
    parsed = parse_date(raw if isinstance(raw, str) else None)
    return None if parsed is None else datetime.date.fromisoformat(parsed)


def _read_hours(
    document: Document, definition: CustomFieldDef | None, *, default: Decimal | None
) -> Decimal | None:
    """The hours of one field: *default* when it has no entry, ``None`` when invalid."""
    if definition is None or not document.has_field(definition.id):
        return default
    raw = document.field_value(definition.id)
    # A Paperless boolean arrives as an int (the model coerces it), so it reads as 0 or 1.
    normalised = amount_raw(raw)
    if normalised is None:
        return None
    hours = parse_positive_decimal(str(normalised).strip())
    if hours is None or hours > MAX_HOURS:
        return None
    return hours


def build_payslip_section(
    reading: PayslipReading, settings: Settings, wfh_hours: Decimal | None
) -> PayslipSection:
    """The ``wfh_hours_ceiling`` rows (fixed order) and the warnings, in their fixed order."""
    warnings = _warnings(reading, settings)
    usable = reading.usable
    unusable_count = len(reading.unusable_ids)
    if not usable:
        reason = _not_computed_reason(reading)
        return PayslipSection(
            rows=_not_computed_rows(reason, wfh_hours, reading.document_count, unusable_count),
            warnings=warnings,
            document_count=reading.document_count,
            unusable_count=unusable_count,
        )

    fy_start = datetime.date.fromisoformat(settings.fy_start)
    fy_end = datetime.date.fromisoformat(settings.fy_end)
    coverage = _coverage(usable, fy_start, fy_end)
    ordinary = sum((slip.ordinary_hours for slip in usable), _ZERO)
    annual = sum((slip.annual_leave_hours for slip in usable), _ZERO)
    personal = sum((slip.personal_leave_hours for slip in usable), _ZERO)
    holiday = sum((slip.public_holiday_hours for slip in usable), _ZERO)
    leave = annual + personal + holiday
    leave_missing = [key for key in reading.missing_keys if key in LEAVE_KEYS]

    rows = [
        EstimateRow(
            SECTION,
            "payslips",
            count=len(usable),
            notes=_join(
                f"unusable: {unusable_count}" if unusable_count else "",
                _outside_note(usable, fy_start, fy_end),
            ),
        ),
        EstimateRow(
            SECTION,
            "period coverage",
            notes=(
                f"first period start {min(slip.period_start for slip in usable).isoformat()}; "
                f"last period end {max(slip.period_end for slip in usable).isoformat()}; "
                f"FY {fy_start.isoformat()} to {fy_end.isoformat()}"
            ),
        ),
        EstimateRow(
            SECTION,
            "coverage gaps",
            count=coverage.gap_days,
            notes=_gap_note(coverage.gap_ranges),
        ),
        EstimateRow(SECTION, "overlapping days", count=coverage.overlap_days),
        EstimateRow(SECTION, "ordinary hours", amount=ordinary, notes="hours"),
    ]
    ceiling: Decimal | None = None
    if leave_missing:
        names = ", ".join(repr(settings.payslip_fields[key]) for key in leave_missing)
        reason = f"not computed: custom field {names} missing in Paperless"
        rows.append(EstimateRow(SECTION, "leave hours", notes=reason))
        rows.append(EstimateRow(SECTION, "ceiling", notes=reason))
        check_reason = "not computed: no ceiling"
    else:
        ceiling = ordinary - leave
        rows.append(
            EstimateRow(
                SECTION,
                "leave hours",
                amount=leave,
                notes=(
                    f"hours; annual {annual:.2f}; personal {personal:.2f}; "
                    f"public holiday {holiday:.2f}"
                ),
            )
        )
        rows.append(
            EstimateRow(
                SECTION,
                "ceiling",
                amount=ceiling,
                notes=_join(
                    "hours",
                    CEILING_NOTE,
                    "negative: Ordinary Hours may not include leave" if ceiling < 0 else "",
                ),
            )
        )
        check_reason = ""
    if wfh_hours is not None:
        rows.append(_check_row(wfh_hours, ceiling, check_reason))
    return PayslipSection(
        rows=tuple(rows),
        warnings=warnings,
        document_count=reading.document_count,
        usable_count=len(usable),
        unusable_count=unusable_count,
        ceiling=ceiling,
    )


def _missing_required(reading: PayslipReading) -> bool:
    return any(key in REQUIRED_KEYS for key in reading.missing_keys)


def _not_computed_reason(reading: PayslipReading) -> str:
    if not reading.type_found:
        return "no payslip document type"
    if _missing_required(reading):
        return "a required payslip custom field is missing in Paperless"
    return "no usable payslips"


def _not_computed_rows(
    reason: str, wfh_hours: Decimal | None, documents: int, unusable: int
) -> tuple[EstimateRow, ...]:
    rows = [
        EstimateRow(
            SECTION,
            "payslips",
            count=0,
            notes=f"payslip documents: {documents}; unusable: {unusable}",
        ),
        EstimateRow(SECTION, "ceiling", notes=f"not computed: {reason}"),
    ]
    if wfh_hours is not None:
        rows.append(EstimateRow(SECTION, "wfh_hours_check", notes=f"not computed: {reason}"))
    return tuple(rows)


def _check_row(wfh_hours: Decimal, ceiling: Decimal | None, no_ceiling: str) -> EstimateRow:
    label = "wfh_hours_check"
    if ceiling is None:
        return EstimateRow(SECTION, label, notes=no_ceiling)
    if wfh_hours <= ceiling:
        return EstimateRow(
            SECTION,
            label,
            notes=f"claimed WFH hours <= ceiling; {ceiling - wfh_hours:.2f} hours below it",
        )
    return EstimateRow(
        SECTION, label, notes=f"exceeds the ceiling by {wfh_hours - ceiling:.2f} hours"
    )


def _join(*notes: str) -> str:
    return "; ".join(note for note in notes if note)


def _outside_note(usable: Sequence[Payslip], fy_start: datetime.date, fy_end: datetime.date) -> str:
    outside = sum(1 for slip in usable if slip.period_start < fy_start or slip.period_end > fy_end)
    return f"{outside} period(s) extend outside the FY; hours counted in full" if outside else ""


@dataclass(frozen=True, slots=True)
class _Coverage:
    gap_days: int
    gap_ranges: tuple[tuple[datetime.date, datetime.date], ...]
    overlap_days: int


def _coverage(
    payslips: Sequence[Payslip], fy_start: datetime.date, fy_end: datetime.date
) -> _Coverage:
    """Days of the FY covered by no period (and as ranges) and by more than one period."""
    window_start = fy_start.toordinal()
    window_end = fy_end.toordinal() + 1  # exclusive
    clipped = [
        (
            max(slip.period_start.toordinal(), window_start),
            min(slip.period_end.toordinal() + 1, window_end),
        )
        for slip in payslips
    ]
    clipped = [(start, end) for start, end in clipped if start < end]
    cuts = sorted({window_start, window_end, *(c[0] for c in clipped), *(c[1] for c in clipped)})
    gap_days = 0
    overlap_days = 0
    ranges: list[tuple[int, int]] = []
    for low, high in zip(cuts, cuts[1:], strict=False):
        depth = sum(1 for start, end in clipped if start <= low < end)
        if depth == 0:
            gap_days += high - low
            if ranges and ranges[-1][1] == low:
                ranges[-1] = (ranges[-1][0], high)
            else:
                ranges.append((low, high))
        elif depth > 1:
            overlap_days += high - low
    return _Coverage(
        gap_days=gap_days,
        gap_ranges=tuple(
            (datetime.date.fromordinal(low), datetime.date.fromordinal(high - 1))
            for low, high in ranges
        ),
        overlap_days=overlap_days,
    )


def _gap_note(ranges: Sequence[tuple[datetime.date, datetime.date]]) -> str:
    listed = [f"{low.isoformat()}..{high.isoformat()}" for low, high in ranges[:MAX_LISTED_GAPS]]
    if len(ranges) > MAX_LISTED_GAPS:
        listed.append(f"and {len(ranges) - MAX_LISTED_GAPS} more")
    return ", ".join(listed)


def _warnings(reading: PayslipReading, settings: Settings) -> tuple[str, ...]:
    """Missing type, then missing field names, then unusable payslip ids (ids only)."""
    if not reading.type_found:
        return (
            f"document type {settings.payslip_document_type!r} not found in Paperless; "
            "no payslip hours",
        )
    warnings: list[str] = []
    if reading.missing_keys:
        names = ", ".join(repr(settings.payslip_fields[key]) for key in reading.missing_keys)
        warnings.append(
            f"payslip custom field(s) not found in Paperless: {names}; "
            "the affected payslip hours are not computed"
        )
    if reading.unusable_ids:
        listed = ", ".join(str(item) for item in reading.unusable_ids[:MAX_LISTED_IDS])
        extra = len(reading.unusable_ids) - MAX_LISTED_IDS
        suffix = f" and {extra} more" if extra > 0 else ""
        warnings.append(
            f"{len(reading.unusable_ids)} payslip(s) unusable (missing or invalid dates or "
            f"hours), not counted in the hours: document ids {listed}{suffix}"
        )
    return tuple(warnings)
