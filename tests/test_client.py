"""Tests for PaperlessClient HTTP behaviour."""

from __future__ import annotations

import httpx
import pytest
from conftest import (
    BASE_URL,
    TOKEN,
    document_payload,
    json_response,
    make_client,
    page_payload,
    tag_payload,
)

from paperless_tax_export import __version__
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.errors import (
    ApiVersionError,
    HttpStatusError,
    PaginationIntegrityError,
    TaxExportAuthError,
    TransportError,
)
from paperless_tax_export.models import Tag

_DOCUMENT_FIELDS = (
    "id,title,created,modified,tags,correspondent,document_type,"
    "storage_path,custom_fields,archived_file_name"
)


def test_requests_carry_the_expected_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return json_response(page_payload([]))

    client = make_client(handler)
    client.list_tags()
    request = captured[0]
    assert request.headers["Accept"] == "application/json; version=10"
    assert request.headers["Authorization"] == f"Token {TOKEN}"
    assert request.headers["User-Agent"] == f"paperless-tax-export/{__version__}"


def test_list_documents_sends_tag_dates_ordering_fields_and_page_size() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update({key: value for key, value in request.url.params.items()})
        return json_response(page_payload([document_payload(1)], count=1))

    client = make_client(handler)
    documents = client.list_documents(
        tag_id=7,
        created_from="2025-07-01",
        created_to="2026-06-30",
    )
    assert [doc.id for doc in documents] == [1]
    assert captured["tags__id__all"] == "7"
    assert captured["created__gte"] == "2025-07-01"
    assert captured["created__lte"] == "2026-06-30"
    assert captured["ordering"] == "id"
    assert captured["page_size"] == "100"
    assert captured["fields"] == _DOCUMENT_FIELDS


def test_client_follows_next_and_returns_all_documents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return json_response(
                {
                    "count": 2,
                    "next": None,
                    "previous": None,
                    "results": [document_payload(2)],
                }
            )
        return json_response(
            {
                "count": 2,
                "next": f"{BASE_URL}/api/documents/?page=2",
                "previous": None,
                "results": [document_payload(1)],
            }
        )

    client = make_client(handler)
    documents = client.list_documents(tag_id=1, created_from="a", created_to="b")
    assert [doc.id for doc in documents] == [1, 2]


def test_absolute_next_url_is_followed_verbatim() -> None:
    followed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        followed.append(str(request.url))
        if request.url.params.get("page") == "2":
            return json_response(page_payload([document_payload(2)], count=2))
        return json_response(
            {
                "count": 2,
                "next": f"{BASE_URL}/api/documents/?page=2",
                "previous": None,
                "results": [document_payload(1)],
            }
        )

    client = make_client(handler)
    client.list_documents(tag_id=1, created_from="a", created_to="b")
    assert followed[1] == f"{BASE_URL}/api/documents/?page=2"


def test_relative_next_url_is_resolved_against_the_base_url() -> None:
    followed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        followed.append(str(request.url))
        if request.url.params.get("page") == "2":
            return json_response(page_payload([document_payload(2)], count=2))
        return json_response(
            {
                "count": 2,
                "next": "/api/documents/?page=2",
                "previous": None,
                "results": [document_payload(1)],
            }
        )

    client = make_client(handler)
    client.list_documents(tag_id=1, created_from="a", created_to="b")
    assert followed[1] == f"{BASE_URL}/api/documents/?page=2"


def test_pagination_count_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "count": 3,
                "next": None,
                "previous": None,
                "results": [tag_payload(1, "a"), tag_payload(2, "b")],
            }
        )

    client = make_client(handler)
    with pytest.raises(PaginationIntegrityError):
        client.list_tags()


def test_repeated_next_url_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return json_response(
                {
                    "count": 2,
                    "next": f"{BASE_URL}/api/tags/?page=2",
                    "previous": None,
                    "results": [tag_payload(2, "b")],
                }
            )
        return json_response(
            {
                "count": 2,
                "next": f"{BASE_URL}/api/tags/?page=2",
                "previous": None,
                "results": [tag_payload(1, "a")],
            }
        )

    client = make_client(handler)
    with pytest.raises(PaginationIntegrityError):
        client.list_tags()


def test_pagination_beyond_1000_pages_raises() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return json_response(
            {
                "count": 1,
                "next": f"{BASE_URL}/api/tags/?page={len(calls) + 1}",
                "previous": None,
                "results": [],
            }
        )

    client = make_client(handler)
    with pytest.raises(PaginationIntegrityError):
        client.list_tags()
    assert len(calls) == 1000


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, TaxExportAuthError),
        (403, TaxExportAuthError),
        (406, ApiVersionError),
        (500, HttpStatusError),
    ],
)
def test_non_success_statuses_are_mapped(status_code: int, error_type: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({}, status_code=status_code)

    client = make_client(handler)
    with pytest.raises(error_type):
        client.list_tags()


def test_mapped_errors_carry_the_network_exit_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({}, status_code=401)

    client = make_client(handler)
    with pytest.raises(TaxExportAuthError) as exc_info:
        client.list_tags()
    assert exc_info.value.exit_code == 3
    message = str(exc_info.value)
    assert "Token" not in message
    assert "Authorization" not in message


def test_transport_failures_are_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    with pytest.raises(TransportError):
        client.list_tags()


def test_timeout_failures_are_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = make_client(handler)
    with pytest.raises(TransportError):
        client.list_tags()


def test_api_versions_are_captured_from_response_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([]))

    client = make_client(handler)
    assert client.api_version is None
    assert client.server_version is None
    client.list_tags()
    assert client.api_version == "10"
    assert client.server_version == "2024.12.1"


def test_count_documents_uses_a_single_request_and_returns_the_total() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update({key: value for key, value in request.url.params.items()})
        return json_response({"count": 8, "next": None, "previous": None, "results": [{"id": 1}]})

    client = make_client(handler)
    assert client.count_documents(tag_id=4) == 8
    assert captured["tags__id__all"] == "4"
    assert captured["page_size"] == "1"
    assert captured["fields"] == "id"


def test_list_tags_returns_typed_tag_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            page_payload([tag_payload(1, "tax-deductible", document_count=5)], count=1)
        )

    client = make_client(handler)
    tags = client.list_tags()
    assert len(tags) == 1
    assert isinstance(tags[0], Tag)
    assert tags[0].id == 1
    assert tags[0].document_count == 5


def test_client_context_manager_enters_and_exits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([]))

    with make_client(handler) as client:
        client.list_tags()
        assert client.api_version == "10"
        assert isinstance(client, PaperlessClient)


def test_client_close_is_present_and_idempotent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(page_payload([]))

    client = make_client(handler)
    client.close()
    client.close()
