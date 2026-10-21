"""Tests for the run layer: resolution, selection, guard and file writing."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from conftest import (
    correspondent_payload,
    custom_field_payload,
    document_payload,
    document_type_payload,
    filtered_documents_response,
    json_response,
    make_client,
    page_payload,
    tag_payload,
)

from paperless_tax_export.classify import Classification
from paperless_tax_export.errors import CountGuardError, StartupError
from paperless_tax_export.export import run_export, unconfigured_field_notes
from paperless_tax_export.settings import Settings

AMOUNT_FIELD = custom_field_payload(10, "Transaction Amount", data_type="monetary")
WORK_USE_FIELD = custom_field_payload(12, "Claimable %", data_type="float")
DATE_FIELD = custom_field_payload(13, "Transaction Date", data_type="date")


def make_handler(
    *,
    tags: list[dict[str, object]] | None = None,
    fields: list[dict[str, object]] | None = None,
    correspondents: list[dict[str, object]] | None = None,
    document_types: list[dict[str, object]] | None = None,
    documents: list[dict[str, object]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler serving every endpoint the run layer touches."""
    tags = tags if tags is not None else [tag_payload(1, "tax deductible", document_count=2)]
    fields = fields if fields is not None else [AMOUNT_FIELD, WORK_USE_FIELD, DATE_FIELD]
    correspondents = (
        correspondents
        if correspondents is not None
        else [correspondent_payload(5, "Alpha Supplies")]
    )
    document_types = (
        document_types if document_types is not None else [document_type_payload(6, "Receipt")]
    )
    documents = documents if documents is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(page_payload(list(tags)))
        if path == "/api/custom_fields/":
            return json_response(page_payload(list(fields)))
        if path == "/api/correspondents/":
            return json_response(page_payload(list(correspondents)))
        if path == "/api/document_types/":
            return json_response(page_payload(list(document_types)))
        if path == "/api/documents/":
            return filtered_documents_response(request, documents)
        return json_response({}, status_code=404)

    return handler


def _happy_documents() -> list[dict[str, object]]:
    return [
        document_payload(
            1,
            title="Receipt A",
            correspondent=5,
            custom_fields=[{"field": 10, "value": "100.00"}],
        ),
        document_payload(
            2,
            title="Refund B",
            correspondent=5,
            custom_fields=[{"field": 10, "value": "-5.00"}],
        ),
    ]


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def test_run_export_writes_both_files_with_expected_counts(tmp_path: Path) -> None:
    client = make_client(make_handler(documents=_happy_documents()))
    result = run_export(Settings(), client, output=tmp_path / "out")
    assert result.selected_count == 2
    assert result.accepted_count == 1
    assert result.review_count == 1
    deductions = read_bytes(tmp_path / "out" / "deductions.csv").decode("utf-8-sig")
    review = read_bytes(tmp_path / "out" / "review.csv").decode("utf-8-sig")
    assert deductions.startswith("paperless_document_id,date,supplier_name")
    assert "reason_codes" not in deductions
    assert deductions.count("\r\n") == 2  # header + one accepted row
    assert "1,2025-10-01,Alpha Supplies" in deductions
    assert review.count("\r\n") == 2
    assert (
        "2,NEGATIVE_AMOUNT,2025-10-01,Alpha Supplies,Refund B,-5.00,AUD,,2025-10-01,"
        "date from created"
    ) in review
    assert "NEGATIVE_AMOUNT" in review


def test_run_twice_is_byte_identical(tmp_path: Path) -> None:
    documents = _happy_documents()
    first = tmp_path / "first"
    second = tmp_path / "second"
    run_export(Settings(), make_client(make_handler(documents=documents)), output=first)
    run_export(Settings(), make_client(make_handler(documents=documents)), output=second)
    assert read_bytes(first / "deductions.csv") == read_bytes(second / "deductions.csv")
    assert read_bytes(first / "review.csv") == read_bytes(second / "review.csv")


def test_missing_required_field_raises_startup_error(tmp_path: Path) -> None:
    handler = make_handler(fields=[WORK_USE_FIELD, DATE_FIELD], documents=_happy_documents())
    with pytest.raises(StartupError) as exc_info:
        run_export(Settings(), make_client(handler), output=tmp_path / "out")
    assert "Transaction Amount" in str(exc_info.value)
    assert not (tmp_path / "out").exists()


def test_missing_deduction_tag_raises_startup_error(tmp_path: Path) -> None:
    handler = make_handler(tags=[tag_payload(9, "income")], documents=_happy_documents())
    with pytest.raises(StartupError) as exc_info:
        run_export(Settings(), make_client(handler), output=tmp_path / "out")
    assert "tax deductible" in str(exc_info.value)
    assert not (tmp_path / "out").exists()


def test_guard_failure_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handler = make_handler(documents=_happy_documents())
    client = make_client(handler)

    def broken_classify(documents: object, context: object) -> Classification:
        return Classification(accepted=(), review=())

    monkeypatch.setattr("paperless_tax_export.export.classify_documents", broken_classify)
    with pytest.raises(CountGuardError):
        run_export(Settings(), client, output=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_notes_for_unconfigured_fields_are_deterministic() -> None:
    notes = unconfigured_field_notes(Settings())
    assert not any("GST" in note or "work-use" in note for note in notes)
    assert any(note.startswith("supplier_name is the document correspondent") for note in notes)
    assert not any("not configured" in note and "supplier" in note for note in notes)
    assert not any("amount field not configured" in note for note in notes)
    assert unconfigured_field_notes(Settings()) == notes


def test_all_configured_fields_produce_no_notes() -> None:
    settings = Settings(
        field_amount="Transaction Amount",
        field_gst="GST Amount",
        field_work_use="Claimable %",
        field_date="Transaction Date",
        field_supplier="Supplier",
    )
    assert unconfigured_field_notes(settings) == ()


def test_review_row_carries_reason_codes(tmp_path: Path) -> None:
    documents = [
        document_payload(
            2,
            title="Refund B",
            correspondent=5,
            custom_fields=[{"field": 10, "value": "-5.00"}],
        )
    ]
    client = make_client(make_handler(documents=documents))
    run_export(Settings(), client, output=tmp_path / "out")
    review = read_bytes(tmp_path / "out" / "review.csv").decode("utf-8-sig")
    assert "-5.00" in review  # amount_inc_gst, not guarded (decimal column)
    assert "'-5.00" not in review  # raw_amount is no longer a column
    assert "NEGATIVE_AMOUNT" in review
