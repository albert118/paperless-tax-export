"""A read-only HTTP client for the Paperless-ngx API.

The client is GET-only by construction: every operation here issues a GET and
there are no write methods anywhere. The API token and headers are never
logged and never appear in error messages.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

from paperless_tax_export import __version__
from paperless_tax_export.errors import (
    ApiVersionError,
    HttpStatusError,
    PaginationIntegrityError,
    TaxExportAuthError,
    TransportError,
)
from paperless_tax_export.models import (
    Correspondent,
    CustomFieldDef,
    Document,
    DocumentType,
    Page,
    StoragePath,
    Tag,
)

T = TypeVar("T")

# Beyond this many pages the pagination loop is treated as malformed input.
_MAX_PAGES = 1000

_DOCUMENT_FIELDS = (
    "id,title,created,modified,tags,correspondent,document_type,"
    "storage_path,custom_fields,archived_file_name"
)


class _CountPage(BaseModel):
    """The minimal shape of a count-only documents response."""

    model_config = ConfigDict(extra="ignore")

    count: int


class PaperlessClient:
    """A small, typed client for the Paperless-ngx REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        page_size: int = 100,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/json; version=10",
            "Authorization": f"Token {token}",
            "User-Agent": f"paperless-tax-export/{__version__}",
        }
        self._page_size = page_size
        self._api_version: str | None = None
        self._server_version: str | None = None
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @property
    def api_version(self) -> str | None:
        """The Paperless API version from the first response, if any."""
        return self._api_version

    @property
    def server_version(self) -> str | None:
        """The Paperless release version from the first response, if any."""
        return self._server_version

    def __enter__(self) -> PaperlessClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def list_documents(
        self,
        *,
        tag_id: int,
        created_from: str,
        created_to: str,
    ) -> list[Document]:
        """All documents with *tag_id* created within the inclusive window."""
        params = {
            "tags__id__all": str(tag_id),
            "created__gte": created_from,
            "created__lte": created_to,
            "fields": _DOCUMENT_FIELDS,
        }
        return self._paginate("/api/documents/", TypeAdapter(Page[Document]), params=params)

    def count_documents(self, *, tag_id: int) -> int:
        """The total number of documents carrying *tag_id*, from one request."""
        response = self._request(
            "GET",
            "/api/documents/",
            params={
                "tags__id__all": str(tag_id),
                "ordering": "id",
                "page_size": "1",
                "fields": "id",
            },
        )
        page = _CountPage.model_validate(response.json())
        return page.count

    def list_tags(self) -> list[Tag]:
        return self._paginate("/api/tags/", TypeAdapter(Page[Tag]))

    def list_custom_fields(self) -> list[CustomFieldDef]:
        return self._paginate("/api/custom_fields/", TypeAdapter(Page[CustomFieldDef]))

    def list_correspondents(self) -> list[Correspondent]:
        return self._paginate("/api/correspondents/", TypeAdapter(Page[Correspondent]))

    def list_document_types(self) -> list[DocumentType]:
        return self._paginate("/api/document_types/", TypeAdapter(Page[DocumentType]))

    def list_storage_paths(self) -> list[StoragePath]:
        return self._paginate("/api/storage_paths/", TypeAdapter(Page[StoragePath]))

    def _paginate(
        self,
        url: str,
        page_adapter: TypeAdapter[Page[T]],
        *,
        params: dict[str, Any] | None = None,
    ) -> list[T]:
        """Fetch every page for *url* and return the accumulated items.

        The first page asks for ``ordering=id`` and the configured page size;
        subsequent requests follow the ``next`` link verbatim. The loop guards
        against a repeated ``next`` URL, runaway paging, and a final count that
        disagrees with the first page's reported total.
        """
        first_params = {"ordering": "id", "page_size": str(self._page_size)}
        if params is not None:
            first_params.update(params)
        response = self._request("GET", url, params=first_params)
        page = page_adapter.validate_python(response.json())
        expected_count = page.count
        results: list[T] = list(page.results)
        seen_next: set[str] = set()
        next_url = page.next
        pages = 1
        while next_url is not None:
            if next_url in seen_next:
                raise PaginationIntegrityError(f"Paperless repeated the 'next' URL {next_url!r}")
            seen_next.add(next_url)
            pages += 1
            if pages > _MAX_PAGES:
                raise PaginationIntegrityError(f"pagination exceeded {_MAX_PAGES} pages")
            response = self._request("GET", self._resolve_next(next_url))
            page = page_adapter.validate_python(response.json())
            results.extend(page.results)
            next_url = page.next
        if len(results) != expected_count:
            raise PaginationIntegrityError(
                f"Paperless returned {len(results)} items but reported {expected_count} total"
            )
        return results

    def _resolve_next(self, next_url: str) -> str:
        """Return *next_url* for use as a request URL.

        Absolute URLs pass through verbatim; relative ones are resolved
        against the base URL the client was configured with.
        """
        if next_url.startswith(("http://", "https://")):
            return next_url
        return str(self._client.base_url.join(next_url))

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = self._send(method, url, params=params)
        target = self._redirect_target(response)
        if target is not None:
            # The resolved target carries its own query; follow at most once.
            response = self._send("GET", target)
        self._capture_versions(response)
        self._raise_for_status(response)
        return response

    def _send(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(method, url, params=params)
        except httpx.TimeoutException as exc:
            raise TransportError("the Paperless server timed out") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TransportError(f"could not reach the Paperless server: {exc}") from exc

    def _redirect_target(self, response: httpx.Response) -> httpx.URL | None:
        """The one redirect hop this client is willing to follow, if any.

        httpx does not follow redirects. We follow a single ``301``/``308`` hop
        to the ``Location`` only when it stays on the same origin and the path
        merely gains or loses a trailing slash, so a live instance's
        slash-normalising redirect barely changes the request. Any other
        redirect — a different status, a different origin, or a loop back to
        the identical path — is reported by ``_raise_for_status`` instead of
        being followed, and a token-bearing request is never forwarded to
        another origin.
        """
        if response.status_code not in (301, 308):
            return None
        location = response.headers.get("Location")
        if not location:
            return None
        target = response.request.url.join(location)
        request_url = response.request.url
        if (target.scheme, target.host, target.port) != (
            request_url.scheme,
            request_url.host,
            request_url.port,
        ):
            return None
        if target.path.rstrip("/") != request_url.path.rstrip("/"):
            return None
        if target.path == request_url.path:
            return None
        return target

    def _capture_versions(self, response: httpx.Response) -> None:
        if self._api_version is None:
            self._api_version = response.headers.get("X-Api-Version")
        if self._server_version is None:
            self._server_version = response.headers.get("X-Version")

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        path = response.request.url.path
        location = response.headers.get("Location")
        redirected = f"; redirected to {location}" if location else ""
        if status in (401, 403):
            raise TaxExportAuthError(
                f"Paperless rejected the API token (HTTP {status} for {path}){redirected}"
            )
        if status == 406:
            raise ApiVersionError(
                f"Paperless does not serve API version 10 (HTTP 406 for {path}){redirected}"
            )
        raise HttpStatusError(
            f"Paperless returned HTTP {status} for {response.request.method} {path}{redirected}"
        )
