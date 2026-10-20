"""Shared helpers for the library unit tests.

All client tests stay offline by wiring ``httpx.MockTransport`` into
``PaperlessClient``; httpx's MockTransport never opens a socket, which keeps
``pytest --disable-socket`` happy without touching real Paperless.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from paperless_tax_export.client import PaperlessClient

BASE_URL = "http://paperless.test"
TOKEN = "test-token"


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
    custom_fields: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a Paperless document object with the fields the tool reads."""
    return {
        "id": doc_id,
        "title": title,
        "created": created,
        "modified": created,
        "tags": [1],
        "correspondent": None,
        "document_type": None,
        "storage_path": None,
        "custom_fields": custom_fields or [],
        "archived_file_name": None,
    }
