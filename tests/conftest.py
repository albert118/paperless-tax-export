"""Shared helpers for the library unit tests.

All client tests stay offline by wiring ``httpx.MockTransport`` into
``PaperlessClient``; httpx's MockTransport never opens a socket, which keeps
``pytest --disable-socket`` happy without touching real Paperless.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from paperless_tax_export.client import PaperlessClient

BASE_URL = "http://paperless.test"
TOKEN = "test-token"
WFH_VARIABLES = (
    "WFH_HOURS",
    "WFH_RATE_PER_HOUR",
    "WFH_INTERNET_PCT",
    "WFH_PHONE_PCT",
    "WFH_ENERGY_AMOUNT",
)


@pytest.fixture(autouse=True)
def _no_estimate_variables_in_the_real_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """TAX_MARGINAL_RATE and WFH_* exported in the developer's shell must not change test output."""
    monkeypatch.delenv("TAX_MARGINAL_RATE", raising=False)
    for name in WFH_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def paperless_filter_matches(document: dict[str, object], params: httpx.QueryParams) -> bool:
    """Apply only the filters Paperless declares and ignore every other parameter.

    Paperless's django-filter ``DocumentFilterSet`` silently drops query parameters it does
    not declare, so a misspelt filter name returns unfiltered documents.
    """
    created = str(document["created"])[:10]
    if "created__gte" in params and created < params["created__gte"]:
        return False
    if "created__lte" in params and created > params["created__lte"]:
        return False
    if "correspondent__id" in params and document["correspondent"] != int(
        params["correspondent__id"]
    ):
        return False
    if "document_type__id" in params and document["document_type"] != int(
        params["document_type__id"]
    ):
        return False
    tag_ids = document["tags"]
    assert isinstance(tag_ids, list)
    return not ("tags__id__all" in params and int(params["tags__id__all"]) not in tag_ids)


def filtered_documents_response(
    request: httpx.Request, documents: list[dict[str, object]]
) -> httpx.Response:
    """Serve ``/api/documents/`` like Paperless: filter strictly, and answer the count probe.

    The one-row ``fields=id`` probe the client uses for counting reports the filtered total.
    """
    params = request.url.params
    found = [document for document in documents if paperless_filter_matches(document, params)]
    if params.get("page_size") == "1" and params.get("fields") == "id":
        return json_response(
            page_payload([{"id": document["id"]} for document in found[:1]], count=len(found))
        )
    return json_response(page_payload(found))


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    page_size: int = 100,
) -> PaperlessClient:
    """Build a PaperlessClient wired to an offline ``httpx.MockTransport``."""
    return PaperlessClient(
        BASE_URL,
        TOKEN,
        page_size=page_size,
        transport=httpx.MockTransport(handler),
    )


def json_response(
    payload: object,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a JSON response, by default with the Paperless version headers."""
    if headers is None:
        headers = {"X-Api-Version": "10", "X-Version": "2024.12.1"}
    return httpx.Response(status_code, json=payload, headers=headers)


def page_payload(
    results: list[dict[str, object]],
    *,
    count: int | None = None,
    next_url: str | None = None,
) -> dict[str, object]:
    """Build a single Paperless page of results."""
    return {
        "count": count if count is not None else len(results),
        "next": next_url,
        "previous": None,
        "results": results,
    }


def tag_payload(tag_id: int, name: str, document_count: int = 0) -> dict[str, object]:
    """Build a Paperless tag object."""
    return {"id": tag_id, "name": name, "document_count": document_count}


def custom_field_payload(
    field_id: int,
    name: str,
    *,
    data_type: str = "string",
    extra_data: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a Paperless custom-field definition object."""
    return {
        "id": field_id,
        "name": name,
        "data_type": data_type,
        "extra_data": extra_data or {},
        "document_count": 0,
    }


def correspondent_payload(
    correspondent_id: int, name: str, document_count: int = 0
) -> dict[str, object]:
    """Build a Paperless correspondent object."""
    return {"id": correspondent_id, "name": name, "document_count": document_count}


def document_type_payload(type_id: int, name: str, document_count: int = 0) -> dict[str, object]:
    """Build a Paperless document-type object."""
    return {"id": type_id, "name": name, "document_count": document_count}


def storage_path_payload(
    path_id: int, name: str, path: str, document_count: int = 0
) -> dict[str, object]:
    """Build a Paperless storage-path object."""
    return {
        "id": path_id,
        "name": name,
        "path": path,
        "document_count": document_count,
    }


def document_payload(
    doc_id: int,
    *,
    title: str = "Statement",
    created: str = "2025-10-01T00:00:00Z",
    modified: str | None = None,
    tags: list[int] | None = None,
    correspondent: int | None = None,
    document_type: int | None = None,
    storage_path: int | None = None,
    archived_file_name: str | None = None,
    custom_fields: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a Paperless document object with the fields the tool reads."""
    return {
        "id": doc_id,
        "title": title,
        "created": created,
        "modified": modified if modified is not None else created,
        "tags": [1] if tags is None else tags,
        "correspondent": correspondent,
        "document_type": document_type,
        "storage_path": storage_path,
        "custom_fields": custom_fields or [],
        "archived_file_name": archived_file_name,
    }
