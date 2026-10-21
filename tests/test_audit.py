"""Tests for the Paperless instance audit."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime

import httpx
import pytest
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
from paperless_tax_export.errors import HttpStatusError


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
    tax_info_tag: str = "tax-information",
    ibkr_correspondent: str = "Interactive Brokers",
    field_amount: str = "Gross Amount",
    field_gst: str = "GST Amount",
    field_work_use: str = "Claimable %",
    base_url: str = "",
) -> AuditReport:
    client = make_client(handler)
    return run_audit(
        client,
        tag=tag,
        fy_start=fy_start,
        fy_end=fy_end,
        tax_info_tag=tax_info_tag,
        ibkr_correspondent=ibkr_correspondent,
        field_amount=field_amount,
        field_gst=field_gst,
        field_work_use=field_work_use,
        base_url=base_url,
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


def _segment_report(documents: list[dict[str, object]]) -> AuditReport:
    return _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=len(documents))],
            documents=documents,
        )
    )


def test_archived_segments_report_counted_and_none_summing_to_the_total() -> None:
    report = _segment_report(
        [
            _doc(1, archived_file_name="2025/10/a.pdf"),
            _doc(2, archived_file_name=None),
            _doc(3, archived_file_name=""),
            _doc(4, archived_file_name="2025/11/b.pdf"),
        ]
    )

    assert report.archived_version_count == 2
    assert report.no_archived_version_count == 2
    assert (
        report.archived_version_count + report.no_archived_version_count
        == report.deduction_document_count
    )
    rendered = render(report)
    assert "2 with an archived version, 2 with no archived version (of 4)" in rendered
    data = json.loads(dump_json(report))
    assert data["archived_version_count"] == 2
    assert data["no_archived_version_count"] == 2


def test_archived_segments_say_so_when_no_document_has_an_archived_version() -> None:
    report = _segment_report([_doc(1, archived_file_name=None), _doc(2, archived_file_name="")])

    rendered = render(report)

    assert "0 with an archived version, 2 with no archived version (of 2)" in rendered
    assert "no archived version: no segments to report" in rendered
    assert "first segment:" not in rendered


def test_archived_segments_report_zero_none_when_every_document_has_a_version() -> None:
    report = _segment_report(
        [
            _doc(1, archived_file_name="2025/10/a.pdf"),
            _doc(2, archived_file_name="2025/10/b.pdf"),
        ]
    )

    assert report.archived_version_count == 2
    assert report.no_archived_version_count == 0
    rendered = render(report)
    assert "2 with an archived version, 0 with no archived version (of 2)" in rendered
    assert "no segments to report" not in rendered


def test_single_segment_paths_are_excluded_from_the_second_segment_counts() -> None:
    report = _segment_report(
        [
            _doc(1, archived_file_name="flat.pdf"),
            _doc(2, archived_file_name="2025/10/a.pdf"),
        ]
    )

    assert report.archived_version_count == 2
    assert [(s.segment, s.count) for s in report.archived_first_segments] == [
        ("2025", 1),
        ("flat.pdf", 1),
    ]
    assert [(s.segment, s.count) for s in report.archived_second_segments] == [("10", 1)]


def test_ibkr_findings_are_correspondent_facts_only() -> None:
    tags = [
        tag_payload(1, "tax-deductible", document_count=2),
        tag_payload(2, "tax-information", document_count=1),
    ]
    correspondents = [
        correspondent_payload(5, "Interactive Brokers", document_count=3),
        correspondent_payload(6, "Commission Agent"),
    ]
    documents = [
        _doc(1, tags=[1], correspondent=5),
        _doc(2, tags=[1, 2], correspondent=6),
    ]
    report = _run(_make_handler(tags=tags, documents=documents, correspondents=correspondents))
    ibkr = report.ibkr
    assert ibkr.correspondent_exists
    assert ibkr.correspondent_document_count == 3
    assert not hasattr(ibkr, "tag_name")
    assert not hasattr(ibkr, "correspondent_without_tag")


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
    assert "tag" not in data["ibkr"]
    assert data["tax_metadata_tag"]["exists"] is False
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
    report = _run(handler)
    first = dump_json(report)
    second = dump_json(report)
    assert first == second


def test_dump_of_the_same_report_is_byte_identical() -> None:
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=1)],
            documents=[_doc(1)],
        ),
        base_url="https://paperless.example.com",
    )
    dumped = dump_json(report)
    assert dump_json(report) == dumped


def test_dump_reports_base_url_recorded_at_and_visibility() -> None:
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=1)],
            documents=[_doc(1)],
        ),
        base_url="https://paperless.example.com",
    )
    data = json.loads(dump_json(report))
    assert data["base_url"] == "https://paperless.example.com"
    recorded = data["recorded_at"]
    assert isinstance(recorded, str)
    assert recorded.endswith("+00:00")
    assert datetime.fromisoformat(recorded).tzinfo is not None
    assert data["tags_visible"] == 1
    assert data["correspondents_visible"] == 0
    assert data["document_types_visible"] == 0
    assert data["storage_paths_visible"] == 0
    assert data["unavailable"] == []


def test_deduction_tag_zero_visible_tags_reports_permissions() -> None:
    report = _run(_make_handler(tags=[]))
    rendered = render(report)
    assert "no tags visible to this token — check permissions" in rendered


def test_deduction_tag_no_match_visible_count_is_reported() -> None:
    report = _run(_make_handler(tags=[tag_payload(1, "income")]))
    rendered = render(report)
    assert "no tag named 'tax-deductible' among 1 visible" in rendered


def test_deduction_tag_match_reports_the_stored_spelling() -> None:
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "tax-deductible", document_count=3)],
            documents=[_doc(1)],
        )
    )
    rendered = render(report)
    assert "'tax-deductible': 3 document(s)" in rendered


def test_ibkr_zero_visible_identifiers_report_permissions() -> None:
    report = _run(_make_handler(tags=[], correspondents=[]))
    rendered = render(report)
    assert "tag 'tax-information': no tags visible to this token — check permissions" in rendered
    assert (
        "correspondent 'Interactive Brokers': no correspondents visible to this token"
        " — check permissions" in rendered
    )


def test_ibkr_identifiers_no_match_visible_count_is_reported() -> None:
    report = _run(
        _make_handler(
            tags=[tag_payload(1, "income")],
            correspondents=[correspondent_payload(2, "Commission Agent")],
        )
    )
    rendered = render(report)
    assert "tag 'tax-information': no tag named 'tax-information' among 1 visible" in rendered
    assert (
        "correspondent 'Interactive Brokers': no correspondent named"
        " 'Interactive Brokers' among 1 visible" in rendered
    )


def test_deduction_tag_near_miss_candidates_are_reported() -> None:
    report = _run(_make_handler(tags=[tag_payload(1, "Tax-Deductible", document_count=2)]))
    rendered = render(report)
    assert "no tag named 'tax-deductible' among 1 visible" in rendered
    assert "candidates: 'Tax-Deductible'" in rendered


def test_ibkr_and_tax_metadata_near_miss_candidates_are_reported() -> None:
    tags = [
        tag_payload(2, "Tax-Information", document_count=1),
        tag_payload(3, "income"),
    ]
    correspondents = [correspondent_payload(4, "Interactive  Brokers")]
    report = _run(_make_handler(tags=tags, correspondents=correspondents))
    # A near-miss is reported, never matched on.
    assert report.ibkr.correspondent_exists is False
    assert report.ibkr.correspondent_candidates == ("Interactive  Brokers",)
    assert report.tax_metadata_tag.exists is False
    assert report.tax_metadata_tag.candidates == ("Tax-Information",)
    rendered = render(report)
    assert "candidates: 'Tax-Information'" in rendered
    assert "candidates: 'Interactive  Brokers'" in rendered


def test_failed_optional_endpoint_is_recorded_and_audit_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/correspondents/":
            return json_response({}, status_code=500)
        if path == "/api/tags/":
            return json_response(page_payload([tag_payload(1, "tax-deductible", document_count=1)]))
        if path == "/api/documents/":
            return json_response(page_payload([_doc(1)]))
        if path == "/api/custom_fields/":
            return json_response(page_payload([custom_field_payload(10, "Gross Amount")]))
        if path == "/api/storage_paths/":
            return json_response(
                page_payload([storage_path_payload(4, "FY26 invoices", "2025/07")])
            )
        if path == "/api/document_types/":
            return json_response(page_payload([]))
        return json_response({}, status_code=404)

    report = _run(handler)
    assert report.correspondents_visible == 0
    assert report.ibkr.correspondent_exists is False
    assert any("correspondents" in entry for entry in report.unavailable)
    assert any("HTTP 500" in entry for entry in report.unavailable)
    rendered = render(report)
    assert "Warnings" in rendered
    assert "correspondents:" in rendered
    assert "HTTP 500" in rendered


def test_documents_fetch_remains_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/documents/":
            return json_response({}, status_code=500)
        if path == "/api/tags/":
            return json_response(page_payload([tag_payload(1, "tax-deductible", document_count=3)]))
        if path == "/api/custom_fields/":
            return json_response(page_payload([custom_field_payload(10, "Gross Amount")]))
        if path == "/api/storage_paths/":
            return json_response(page_payload([]))
        if path == "/api/correspondents/":
            return json_response(page_payload([]))
        if path == "/api/document_types/":
            return json_response(page_payload([]))
        return json_response({}, status_code=404)

    with pytest.raises(HttpStatusError):
        _run(handler)


def test_tax_metadata_tag_reports_the_general_tax_metadata_count() -> None:
    tags = [tag_payload(2, "tax-information", document_count=190)]
    report = _run(_make_handler(tags=tags))
    tax = report.tax_metadata_tag
    assert tax.name == "tax-information"
    assert tax.exists is True
    assert tax.document_count == 190
    assert tax.candidates == ()
    rendered = render(report)
    assert "general tax metadata, not IBKR-specific" in rendered
    data = json.loads(dump_json(report))
    assert data["tax_metadata_tag"] == {
        "name": "tax-information",
        "exists": True,
        "document_count": 190,
        "candidates": [],
    }


def test_ibkr_block_mentions_no_tag_or_overlap_counters() -> None:
    tags = [tag_payload(1, "tax-deductible", document_count=2)]
    correspondents = [correspondent_payload(5, "Interactive Brokers", document_count=1)]
    documents = [_doc(1, tags=[1], correspondent=5)]
    report = _run(_make_handler(tags=tags, documents=documents, correspondents=correspondents))
    rendered = render(report)
    ibkr_section = rendered.partition("IBKR identifiers:")[2].partition("Tax-information tag")[0]
    assert "tag" not in ibkr_section.casefold()
    assert "correspondent_without_tag" not in ibkr_section
    assert "tag_with_other_correspondent" not in ibkr_section


def test_near_miss_normalisation_treats_separators_as_spaces() -> None:
    tags = [
        tag_payload(1, "tax-deductible", document_count=2),
        tag_payload(3, "tax_deductible", document_count=1),
        tag_payload(4, "tax deductible", document_count=5),
    ]
    report = _run(_make_handler(tags=tags), tag="tax deductible")
    assert tuple(match.name for match in report.tag_matches) == ("tax deductible",)
    assert report.tag_candidates == ("tax-deductible", "tax_deductible")


def test_client_origin_rewrites_are_reported_as_notes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            if request.url.params.get("page") == "2":
                return json_response(page_payload([tag_payload(2, "income")], count=2))
            return json_response(
                {
                    "count": 2,
                    "next": "https://other.example/api/tags/?page=2",
                    "previous": None,
                    "results": [tag_payload(1, "tax-deductible", document_count=2)],
                }
            )
        if path == "/api/documents/":
            return json_response(page_payload([]))
        if path == "/api/custom_fields/":
            return json_response(page_payload([custom_field_payload(10, "Gross Amount")]))
        if path == "/api/storage_paths/":
            return json_response(page_payload([]))
        if path == "/api/correspondents/":
            return json_response(page_payload([]))
        if path == "/api/document_types/":
            return json_response(page_payload([]))
        return json_response({}, status_code=404)

    report = _run(handler)
    assert any("server advertised" in note for note in report.notes)
    assert any("re-anchored to" in note for note in report.notes)
    assert any("other.example" in note for note in report.notes)
    rendered = render(report)
    assert "Notes:" in rendered
    assert "server advertised" in rendered


def test_notes_are_serialised_in_dump_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            if request.url.params.get("page") == "2":
                return json_response(page_payload([tag_payload(2, "income")], count=2))
            return json_response(
                {
                    "count": 2,
                    "next": "https://other.example/api/tags/?page=2",
                    "previous": None,
                    "results": [tag_payload(1, "tax-deductible", document_count=2)],
                }
            )
        if path == "/api/documents/":
            return json_response(page_payload([]))
        if path == "/api/custom_fields/":
            return json_response(page_payload([custom_field_payload(10, "Gross Amount")]))
        if path == "/api/storage_paths/":
            return json_response(page_payload([]))
        if path == "/api/correspondents/":
            return json_response(page_payload([]))
        if path == "/api/document_types/":
            return json_response(page_payload([]))
        return json_response({}, status_code=404)

    report = _run(handler)
    data = json.loads(dump_json(report))
    assert data["notes"]
    assert any("re-anchored to" in note for note in data["notes"])
