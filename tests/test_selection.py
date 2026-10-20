"""Tests for tag and custom-field resolution and FY selection."""

from __future__ import annotations

import httpx
import pytest
from conftest import (
    custom_field_payload,
    document_payload,
    json_response,
    make_client,
    page_payload,
    tag_payload,
)

from paperless_tax_export.errors import StartupError
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


def test_select_documents_counts_out_of_fy_documents_without_returning_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("page_size") == "1" and params.get("fields") == "id":
            return json_response(
                {"count": 8, "next": None, "previous": None, "results": [{"id": 1}]}
            )
        return json_response(
            {
                "count": 3,
                "next": None,
                "previous": None,
                "results": [
                    document_payload(1, created="2025-07-01T00:00:00Z"),
                    document_payload(2, created="2026-01-15T00:00:00Z"),
                    document_payload(3, created="2026-06-30T00:00:00Z"),
                ],
            }
        )

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

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("page_size") == "1" and params.get("fields") == "id":
            return json_response(
                {"count": 1, "next": None, "previous": None, "results": [{"id": 1}]}
            )
        document_params.update({key: value for key, value in params.items()})
        return json_response(page_payload([document_payload(1)], count=1))

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
