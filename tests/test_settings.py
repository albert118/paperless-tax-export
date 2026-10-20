"""Tests for Settings validation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from paperless_tax_export.errors import EXIT_USAGE, SettingsError
from paperless_tax_export.settings import Settings


def test_defaults_match_the_brief() -> None:
    settings = Settings()
    assert settings.base_url == "http://localhost:8000"
    assert settings.token_file == Path("secrets/paperless-token.txt")
    assert settings.deduction_tag == "tax-deductible"
    assert settings.ibkr_tag == "tax-information"
    assert settings.ibkr_correspondent == "Interactive Brokers"
    assert settings.field_amount == "Gross Amount"
    assert settings.field_gst == "GST Amount"
    assert settings.field_work_use == "Claimable %"
    assert settings.field_date is None
    assert settings.field_supplier is None
    assert settings.fy_start == "2025-07-01"
    assert settings.fy_end == "2026-06-30"
    assert settings.page_size == 100
    assert settings.default_currency == "AUD"
    assert settings.income_tags == ("income", "dividend")
    assert settings.expected_count is None
    assert settings.timeout_seconds == 30.0


def test_settings_are_frozen() -> None:
    settings = Settings()
    with pytest.raises(FrozenInstanceError):
        settings.base_url = "http://localhost:9999"  # type: ignore[misc]


def test_fy_bounds_are_stored_as_iso_strings() -> None:
    settings = Settings()
    assert isinstance(settings.fy_start, str)
    assert isinstance(settings.fy_end, str)
    assert settings.fy_start == "2025-07-01"
    assert settings.fy_end == "2026-06-30"


@pytest.mark.parametrize("value", ["not-a-date", "01/07/2025", "2025-7-1", ""])
def test_invalid_fy_dates_raise_settings_error(value: str) -> None:
    with pytest.raises(SettingsError):
        Settings(fy_start=value)


@pytest.mark.parametrize("value", ["2025-02-30", "2025-13-01"])
def test_impossible_fy_dates_raise_settings_error(value: str) -> None:
    with pytest.raises(SettingsError):
        Settings(fy_start=value)


def test_fy_start_after_fy_end_raises() -> None:
    with pytest.raises(SettingsError):
        Settings(fy_start="2026-07-01", fy_end="2025-06-30")


def test_equal_fy_bounds_are_valid() -> None:
    settings = Settings(fy_start="2026-06-30", fy_end="2026-06-30")
    assert settings.fy_start == settings.fy_end == "2026-06-30"


@pytest.mark.parametrize("page_size", [0, -1, 100_001])
def test_page_size_out_of_range_raises(page_size: int) -> None:
    with pytest.raises(SettingsError):
        Settings(page_size=page_size)


@pytest.mark.parametrize("page_size", [1, 100_000])
def test_page_size_boundaries_are_valid(page_size: int) -> None:
    assert Settings(page_size=page_size).page_size == page_size


@pytest.mark.parametrize("base_url", ["localhost:8000", "ftp://example.com", ""])
def test_base_url_requires_http_scheme(base_url: str) -> None:
    with pytest.raises(SettingsError):
        Settings(base_url=base_url)


@pytest.mark.parametrize("base_url", ["http://localhost:8000", "https://docs.internal"])
def test_base_url_accepts_http_and_https(base_url: str) -> None:
    assert Settings(base_url=base_url).base_url == base_url


@pytest.mark.parametrize("currency", ["aud", "AU", "AUDS", "A1D", "AUD "])
def test_default_currency_must_be_three_uppercase_letters(currency: str) -> None:
    with pytest.raises(SettingsError):
        Settings(default_currency=currency)


@pytest.mark.parametrize("timeout_seconds", [0.0, -5.0])
def test_timeout_must_be_positive(timeout_seconds: float) -> None:
    with pytest.raises(SettingsError):
        Settings(timeout_seconds=timeout_seconds)


def test_settings_error_carries_the_usage_exit_code() -> None:
    with pytest.raises(SettingsError) as exc_info:
        Settings(base_url="nope")
    assert exc_info.value.exit_code == EXIT_USAGE
