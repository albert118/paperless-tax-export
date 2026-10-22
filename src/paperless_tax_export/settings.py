"""Runtime configuration for a paperless-tax-export run.

The settings live in a frozen, slotted dataclass and are validated eagerly in
``__post_init__`` so a bad configuration fails before any network traffic.
Command-line flags populate a subset of these values (the name and date
identifiers stay here, since they are corrected in one place). The environment
and dotenv lookup for the token and base URL lives in ``env_config``.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from paperless_tax_export.errors import SettingsError

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

BILL_KINDS = ("electricity", "internet", "phone", "gas")
DEFAULT_BILL_KIND_TAGS: Mapping[str, str] = {kind: kind for kind in BILL_KINDS}


@dataclass(frozen=True, slots=True)
class WfhInputs:
    """The owner's work-from-home figures; ``None`` means the variable is not set.

    ``hours``, ``rate_per_hour`` and ``energy_amount`` are non-negative amounts; the two
    percentages are percentages from 0 to 100 (50 means half), not fractions.
    """

    hours: Decimal | None = None
    rate_per_hour: Decimal | None = None
    internet_pct: Decimal | None = None
    phone_pct: Decimal | None = None
    energy_amount: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("hours", "rate_per_hour", "energy_amount"):
            value: Decimal | None = getattr(self, name)
            if value is not None and not (value.is_finite() and value >= 0):
                raise SettingsError(f"wfh {name} must be a non-negative decimal")
        for name in ("internet_pct", "phone_pct"):
            value = getattr(self, name)
            if value is not None and not (value.is_finite() and 0 <= value <= 100):
                raise SettingsError(f"wfh {name} must be a decimal from 0 to 100")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings with sensible local-development defaults."""

    base_url: str = "http://localhost:8000"
    token_file: Path = Path("secrets/paperless-token.txt")
    deduction_tag: str = "tax deductible"
    tax_info_tag: str = "tax-information"
    ibkr_correspondent: str = "Interactive Brokers"
    field_amount: str | None = "Transaction Amount"
    field_gst: str | None = None
    field_work_use: str | None = "Claimable %"
    field_date: str | None = "Transaction Date"
    field_supplier: str | None = None
    fy_start: str = "2025-07-01"
    fy_end: str = "2026-06-30"
    page_size: int = 100
    default_currency: str = "AUD"
    income_tags: tuple[str, ...] = ("income", "dividend")
    asset_reviewed_tag: str | None = "asset-reviewed"
    expected_count: int | None = None
    marginal_rate: Decimal | None = None
    rates_file: Path | None = None
    bill_document_type: str = "bill"
    bill_kind_tags: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_BILL_KIND_TAGS))
    wfh: WfhInputs = field(default_factory=WfhInputs)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        _validate_iso_date(self.fy_start, "fy_start")
        _validate_iso_date(self.fy_end, "fy_end")
        if self.fy_start > self.fy_end:
            raise SettingsError(
                f"fy_start must be on or before fy_end ({self.fy_start!r} > {self.fy_end!r})"
            )
        if not 1 <= self.page_size <= 100_000:
            raise SettingsError(f"page_size must be between 1 and 100000, got {self.page_size}")
        if not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
            raise SettingsError(
                f"base_url must start with http:// or https://, got {self.base_url!r}"
            )
        if not (
            len(self.default_currency) == 3
            and self.default_currency.isalpha()
            and self.default_currency.isupper()
        ):
            raise SettingsError(
                f"default_currency must be three uppercase letters, got {self.default_currency!r}"
            )
        if self.asset_reviewed_tag is not None and not self.asset_reviewed_tag.strip():
            raise SettingsError("asset_reviewed_tag must not be empty; use None to disable it")
        if self.asset_reviewed_tag == self.deduction_tag:
            raise SettingsError(
                f"asset_reviewed_tag must differ from deduction_tag (both {self.deduction_tag!r}): "
                "every selected document would carry it and every over-300 row would be cleared"
            )
        if self.expected_count is not None and self.expected_count < 0:
            raise SettingsError(f"expected_count must not be negative, got {self.expected_count}")
        if self.marginal_rate is not None and not (
            self.marginal_rate.is_finite() and 0 <= self.marginal_rate <= 1
        ):
            raise SettingsError("marginal_rate must be a decimal between 0 and 1")
        if self.rates_file is not None and self.rates_file == Path():
            raise SettingsError("rates_file must name a file")
        if not self.bill_document_type.strip():
            raise SettingsError("bill_document_type must not be empty")
        _validate_bill_kind_tags(self.bill_kind_tags)
        if self.timeout_seconds <= 0:
            raise SettingsError(f"timeout_seconds must be positive, got {self.timeout_seconds}")


def _validate_bill_kind_tags(kind_tags: Mapping[str, str]) -> None:
    """Raise ``SettingsError`` unless every kind has exactly one non-empty, distinct tag name."""
    if set(kind_tags) != set(BILL_KINDS):
        raise SettingsError(f"bill_kind_tags must map exactly these kinds: {', '.join(BILL_KINDS)}")
    names = list(kind_tags.values())
    if any(not name.strip() for name in names):
        raise SettingsError("bill_kind_tags tag names must not be empty")
    if len(set(names)) != len(names):
        raise SettingsError("bill_kind_tags must use a different tag for each kind")


def _validate_iso_date(value: str, name: str) -> None:
    """Raise ``SettingsError`` unless *value* is a strict ISO calendar date."""
    if _ISO_DATE_RE.match(value) is None:
        raise SettingsError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc
