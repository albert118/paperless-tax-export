"""Tests for the Paperless instance audit."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import httpx
from conftest import (
    correspondent_payload,
    custom_field_payload,
    document_payload,
    document_type_payload,
    json_response,
    make_client,
    page_payload,
    storage_path_payload,
    tag_payload,
)

from paperless_tax_export.audit import (
    AuditReport,
    FieldCoverage,
    SelectOption,
    StoragePathInfo,
    dump_json,
    render,
    run_audit,
)


def _doc(
    doc_id: int,
    *,
    title: str = "Statement",
    tags: Sequence[int] = (1,),
    correspondent: int | None = None,
    archived_file_name: str | None = None,
    custom_fields: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload = document_payload(doc_id, title=title, custom_fields=custom_fields)
    payload["tags"] = list(tags)
    payload["correspondent"] = correspondent
    payload["archived_file_name"] = archived_file_name
    return payload


def _make_handler(
    *,
    tags: Sequence[dict[str, object]] = (),
    documents: Sequence[dict[str, object]] = (),
    fields: Sequence[dict[str, object]] = (),
    storage_paths: Sequence[dict[str, object]] = (),
    correspondents: Sequence[dict[str, object]] = (),
    document_types: Sequence[dict[str, object]] = (),
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(page_payload(list(tags)))
        if path == "/api/documents/":
            return json_response(page_payload(list(documents)))
        if path == "/api/custom_fields/":
            return json_response(page_payload(list(fields)))
        if path == "/api/storage_paths/":
            return json_response(page_payload(list(storage_paths)))
        if path == "/api/correspondents/":
            return json_response(page_payload(list(correspondents)))
        if path == "/api/document_types/":
            return json_response(page_payload(list(document_types)))
        return json_response({}, status_code=404)

    return handler


def _run(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    tag: str = "tax-deductible",
    fy_start: str = "2025-07-01",
    fy_end: str = "2026-06-30",
    ibkr_tag: str = "tax-information",
    ibkr_correspondent: str = "Interactive Brokers",
    field_amount: str = "Gross Amount",
    field_gst: str = "GST Amount",
    field_work_use: str = "Claimable %",
) -> AuditReport:
    client = make_client(handler)
    return run_audit(
        client,
        tag=tag,
        fy_start=fy_start,
        fy_end=fy_end,
        ibkr_tag=ibkr_tag,
        ibkr_correspondent=ibkr_correspondent,
        field_amount=field_amount,
        field_gst=field_gst,
        field_work_use=field_work_use,
    )


def test_coverage_counts_present_null_and_absent() -> None:
    fields = [custom_field_payload(10, "Gross Amount")]
    documents = [
        _doc(1, custom_fields=[]),
        _doc(2, custom_fields=[{"field": 10, "value": None}]),
        _doc(3, custom_fields=[{"field": 10, "value": "123.45"}]),
    ]
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=3)],
            documents=documents,
            fields=fields,
        )
    )
    assert report.deduction_document_count == 3
    assert report.coverage == (
        FieldCoverage(
            field_id=10,
            field_name="Gross Amount",
            present_count=1,
            null_count=1,
            absent_count=1,
        ),
    )


def test_select_options_are_read_from_extra_data() -> None:
    fields = [
        custom_field_payload(
            12,
            "Claimable %",
            data_type="select",
            extra_data={
                "select_options": [{"id": 1, "label": "Home"}, {"id": 2, "label": "Office"}]
            },
        )
    ]
    report = _run(_make_handler(fields=fields))
    assert report.custom_fields[0].select_options == (
        SelectOption(id="1", label="Home"),
        SelectOption(id="2", label="Office"),
    )


def test_unknown_select_id_in_a_document_is_tolerated() -> None:
    fields = [
        custom_field_payload(
            12,
            "Claimable %",
            data_type="select",
            extra_data={"select_options": [{"id": 1, "label": "Home"}]},
        )
    ]
    documents = [_doc(1, custom_fields=[{"field": 12, "value": 99}])]
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=1)],
            documents=documents,
            fields=fields,
        )
    )
    assert report.coverage[0].present_count == 1
    assert report.coverage[0].null_count == 0
    assert report.coverage[0].absent_count == 0


def test_default_currency_is_captured_when_set() -> None:
    fields = [
        custom_field_payload(
            10, "Gross Amount", data_type="monetary", extra_data={"default_currency": "USD"}
        )
    ]
    report = _run(_make_handler(fields=fields))
    assert report.custom_fields[0].default_currency == "USD"
    assert report.custom_fields[0].data_type == "monetary"


def test_storage_paths_include_id_name_path_and_count() -> None:
    storage_paths = [
        storage_path_payload(4, "FY26 invoices", "2025/07/{created_month}", document_count=3)
    ]
    report = _run(_make_handler(storage_paths=storage_paths))
    assert report.storage_paths == (
        StoragePathInfo(
            id=4,
            name="FY26 invoices",
            path="2025/07/{created_month}",
            document_count=3,
        ),
    )


def test_archived_file_name_segments_are_counted_in_render() -> None:
    documents = [
        _doc(1, archived_file_name="2025/10/2025-10-01 receipt.pdf"),
        _doc(2, archived_file_name="2025/10/2025-10-15 receipt.pdf"),
        _doc(3, archived_file_name="2025/11/2025-11-01 receipt.pdf"),
    ]
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=3)],
            documents=documents,
        )
    )
    rendered = render(report)
    assert "'2025' x3" in rendered
    assert "'10' x2" in rendered
    assert "'11' x1" in rendered


def test_ibkr_near_misses_are_counted() -> None:
    tags = [
        tag_payload(1, "tax-deductible", document_count=2),
        tag_payload(2, "tax-information", document_count=1),
    ]
    correspondents = [
        correspondent_payload(5, "Interactive Brokers"),
        correspondent_payload(6, "Commission Agent"),
    ]
    documents = [
        _doc(1, tags=[1], correspondent=5),
        _doc(2, tags=[1, 2], correspondent=6),
    ]
    report = _run(_make_handler(tags=tags, documents=documents, correspondents=correspondents))
    ibkr = report.ibkr
    assert ibkr.tag_exists
    assert ibkr.correspondent_exists
    assert ibkr.correspondent_without_tag == 1
    assert ibkr.tag_with_other_correspondent == 1


def test_ibkr_document_type_matches_are_case_insensitive() -> None:
    document_types = [
        document_type_payload(6, "Statement of Trade Confirmation"),
        document_type_payload(7, "REALIZED GAINS SUMMARY 2025"),
    ]
    report = _run(_make_handler(document_types=document_types))
    assert report.ibkr.trade_confirmation_types == ("Statement of Trade Confirmation",)
    assert report.ibkr.realized_gains_types == ("REALIZED GAINS SUMMARY 2025",)


def test_dump_json_never_contains_document_derived_strings() -> None:
    fields = [custom_field_payload(10, "Gross Amount")]
    documents = [
        _doc(
            7,
            title="Supplier A invoice 77",
            archived_file_name="2025/10/2025-10-01 Supplier A invoice 77.pdf",
            custom_fields=[{"field": 10, "value": "Supplier A invoice 77 $200.00"}],
        )
    ]
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=1)],
            documents=documents,
            fields=fields,
        )
    )
    dumped = dump_json(report)
    assert "Supplier A" not in dumped
    assert "invoice 77" not in dumped
    assert "2025-10-01" not in dumped
    data = json.loads(dumped)
    assert data["deduction_document_count"] == 1
    coverage = data["coverage"][0]
    assert coverage["present_count"] == 1
    assert coverage["null_count"] == 0
    assert coverage["absent_count"] == 0
    assert data["ibkr"]["correspondent_without_tag"] == 0
    assert data["ibkr"]["tag_with_other_correspondent"] == 0
    rendered = render(report)
    assert "'2025' x1" in rendered
    assert "'10' x1" in rendered


def test_missing_deduction_tag_reports_zero_without_raising() -> None:
    fields = [custom_field_payload(10, "Gross Amount")]
    report = _run(_make_handler(tags=[tag_payload(9, "income")], fields=fields))
    assert report.tag_matches == ()
    assert report.deduction_document_count == 0
    assert report.coverage == (
        FieldCoverage(
            field_id=10,
            field_name="Gross Amount",
            present_count=0,
            null_count=0,
            absent_count=0,
        ),
    )


def test_missing_required_field_is_reported_not_raised() -> None:
    report = _run(_make_handler(fields=[custom_field_payload(11, "Something Else")]))
    by_name = {info.name: info for info in report.expected_fields}
    assert by_name["Gross Amount"].exists is False
    assert by_name["Gross Amount"].required is True
    assert by_name["GST Amount"].exists is False
    assert by_name["Claimable %"].exists is False


def test_duplicate_tag_names_are_findings_not_errors() -> None:
    tags = [
        tag_payload(1, "tax-deductible", document_count=2),
        tag_payload(8, "tax-deductible", document_count=5),
    ]
    report = _run(_make_handler(tags=tags))
    assert len(report.tag_matches) == 2
    assert sum(match.document_count for match in report.tag_matches) == 7
    assert report.deduction_document_count == 0


def test_custom_fields_are_sorted_by_name() -> None:
    fields = [
        custom_field_payload(2, "Zebra"),
        custom_field_payload(1, "Alpha"),
        custom_field_payload(3, "Mango"),
    ]
    report = _run(_make_handler(fields=fields))
    assert [field.name for field in report.custom_fields] == ["Alpha", "Mango", "Zebra"]


def test_render_reports_versions_and_settings_defaults() -> None:
    report = _run(_make_handler())
    rendered = render(report)
    assert "Paperless API version: 10" in rendered
    assert "Paperless server version: 2024.12.1" in rendered
    assert "Settings.field_date" in rendered
    assert "Settings.field_supplier" in rendered


def test_dump_json_is_deterministic() -> None:
    handler = _make_handler(
        documents=[
            _doc(1, custom_fields=[{"field": 10, "value": "1.00"}]),
            _doc(2, archived_file_name="2025/10/x.pdf"),
        ],
        fields=[custom_field_payload(10, "Gross Amount")],
    )
    first = dump_json(_run(handler))
    second = dump_json(_run(handler))
    assert first == second
