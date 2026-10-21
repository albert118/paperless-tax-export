"""Tests for tag and custom-field resolution and FY selection."""

from __future__ import annotations

import httpx
import pytest
from conftest import (
    custom_field_payload,
    document_payload,
    filtered_documents_response,
    json_response,
    make_client,
    page_payload,
    paperless_filter_matches,
    tag_payload,
)

from paperless_tax_export.errors import CountGuardError, StartupError
from paperless_tax_export.models import CustomFieldDef, Tag
from paperless_tax_export.selection import (
    SelectionResult,
    resolve_fields,
    resolve_tag,
    select_documents,
)


def test_resolve_tag_finds_exactly_one_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([tag_payload(4, "tax-deductible")], count=1))

    client = make_client(handler)
    tag = resolve_tag(client, "tax-deductible")
    assert tag == Tag(id=4, name="tax-deductible")


def test_resolve_tag_raises_when_there_are_no_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([tag_payload(1, "other")], count=1))

    client = make_client(handler)
    with pytest.raises(StartupError) as exc_info:
        resolve_tag(client, "tax-deductible")
    message = str(exc_info.value)
    assert "tax-deductible" in message
    assert "0" in message


def test_resolve_tag_raises_when_there_are_two_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            page_payload(
                [tag_payload(1, "tax-deductible"), tag_payload(2, "tax-deductible")],
                count=2,
            )
        )

    client = make_client(handler)
    with pytest.raises(StartupError) as exc_info:
        resolve_tag(client, "tax-deductible")
    assert "tax-deductible" in str(exc_info.value)
    assert "2" in str(exc_info.value)


def test_resolve_fields_reports_every_missing_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([]))

    client = make_client(handler)
    with pytest.raises(StartupError) as exc_info:
        resolve_fields(
            client,
            required=("Gross Amount", "GST Amount", "Claimable %"),
            optional=(),
        )
    message = str(exc_info.value)
    assert "Gross Amount" in message
    assert "GST Amount" in message
    assert "Claimable %" in message


def test_resolve_fields_maps_missing_optional_names_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([custom_field_payload(1, "Gross Amount")], count=1))

    client = make_client(handler)
    result = resolve_fields(
        client,
        required=("Gross Amount",),
        optional=("GST Amount", "Claimable %"),
    )
    assert isinstance(result["Gross Amount"], CustomFieldDef)
    assert result["GST Amount"] is None
    assert result["Claimable %"] is None


FY_START = "2025-07-01"
FY_END = "2026-06-30"


def _tagged(doc_id: int, created: str, tag_id: int = 7) -> dict[str, object]:
    return document_payload(doc_id, created=f"{created}T00:00:00Z", tags=[tag_id])


def test_select_documents_counts_out_of_fy_documents_without_returning_them() -> None:
    documents = [
        _tagged(1, "2025-07-01"),
        _tagged(2, "2026-01-15"),
        _tagged(3, "2026-06-30"),
        _tagged(4, "2025-06-30"),
        _tagged(5, "2026-07-01"),
        _tagged(6, "2024-01-01"),
        _tagged(7, "2027-01-01"),
        _tagged(8, "2020-05-05"),
        _tagged(9, "2026-01-15", tag_id=99),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return filtered_documents_response(request, documents)

    client = make_client(handler)
    result = select_documents(
        client,
        tag_id=7,
        fy_start="2025-07-01",
        fy_end="2026-06-30",
    )
    assert isinstance(result, SelectionResult)
    assert result.total_tagged == 8
    assert [doc.id for doc in result.documents] == [1, 2, 3]
    assert result.out_of_fy_count == 5


def test_select_documents_requests_inclusive_fy_bounds() -> None:
    document_params: dict[str, str] = {}
    documents = [document_payload(1, tags=[3])]

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if not (params.get("page_size") == "1" and params.get("fields") == "id"):
            document_params.update({key: value for key, value in params.items()})
        return filtered_documents_response(request, documents)

    client = make_client(handler)
    result = select_documents(
        client,
        tag_id=3,
        fy_start="2025-07-01",
        fy_end="2026-06-30",
    )
    assert result.total_tagged == 1
    assert document_params["tags__id__all"] == "3"
    assert document_params["created__gte"] == "2025-07-01"
    assert document_params["created__lte"] == "2026-06-30"
    assert result.out_of_fy_count == 0


def test_select_documents_includes_both_fy_boundary_dates_and_excludes_the_days_outside() -> None:
    documents = [
        _tagged(1, "2025-06-30"),
        _tagged(2, "2025-07-01"),
        _tagged(3, "2026-06-30"),
        _tagged(4, "2026-07-01"),
    ]
    client = make_client(lambda request: filtered_documents_response(request, documents))
    result = select_documents(client, tag_id=7, fy_start=FY_START, fy_end=FY_END)
    assert [doc.id for doc in result.documents] == [2, 3]
    assert result.total_tagged == 4
    assert result.out_of_fy_count == 2


def test_select_documents_raises_when_the_server_ignores_the_tag_filter() -> None:
    documents = [
        _tagged(1, "2025-10-01"),
        _tagged(2, "2025-11-01", tag_id=99),
        document_payload(3, title="Private title", created="2025-12-01T00:00:00Z", tags=[]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        params = httpx.QueryParams(
            {k: v for k, v in request.url.params.items() if k != "tags__id__all"}
        )
        found = [d for d in documents if paperless_filter_matches(d, params)]
        return json_response(page_payload(found))

    client = make_client(handler)
    with pytest.raises(StartupError) as exc_info:
        select_documents(client, tag_id=7, fy_start=FY_START, fy_end=FY_END)
    message = str(exc_info.value)
    assert "tag id 7" in message
    assert "2 do not carry the tag" in message
    assert "0 were created outside" in message
    assert "ignored a filter" in message
    assert "Private title" not in message


def test_select_documents_raises_when_the_server_ignores_the_created_window() -> None:
    documents = [
        _tagged(1, "2025-10-01"),
        _tagged(2, "2025-06-30"),
        _tagged(3, "2026-07-01"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        params = httpx.QueryParams(
            {k: v for k, v in request.url.params.items() if not k.startswith("created__")}
        )
        found = [d for d in documents if paperless_filter_matches(d, params)]
        return json_response(page_payload(found))

    client = make_client(handler)
    with pytest.raises(StartupError) as exc_info:
        select_documents(client, tag_id=7, fy_start=FY_START, fy_end=FY_END)
    message = str(exc_info.value)
    assert "tag id 7" in message
    assert "0 do not carry the tag" in message
    assert "2 were created outside 2025-07-01 to 2026-06-30" in message
    assert "ignored a filter" in message


def test_select_documents_raises_count_guard_when_tagged_total_is_below_selected() -> None:
    documents = [_tagged(1, "2025-10-01"), _tagged(2, "2025-11-01")]

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("page_size") == "1" and params.get("fields") == "id":
            return json_response(page_payload([{"id": 1}], count=1))
        return filtered_documents_response(request, documents)

    client = make_client(handler)
    with pytest.raises(CountGuardError) as exc_info:
        select_documents(client, tag_id=7, fy_start=FY_START, fy_end=FY_END)
    message = str(exc_info.value)
    assert "1 documents carry tag id 7" in message
    assert "2 selected" in message
    assert "re-run" in message
