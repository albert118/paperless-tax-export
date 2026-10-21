"""A read-only HTTP client for the Paperless-ngx API.

The client is GET-only by construction: every operation here issues a GET and
there are no write methods anywhere. The API token and headers are never
logged and never appear in error messages. ``next`` links that carry their own
authority are re-anchored to the configured base URL before they are followed,
so a token-bearing request never goes to a host the operator did not
configure.
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
        self._origin_rewrites: list[str] = []
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

    @property
    def origin_rewrites(self) -> tuple[str, ...]:
        """Human-readable records of ``next`` links re-anchored to the base origin.

        Read-only: the audit surfaces these as non-fatal notes so a
        misconfigured server is warned about instead of hidden.
        """
        return tuple(self._origin_rewrites)

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
        tag_id: int | None = None,
        correspondent_id: int | None = None,
        created_from: str,
        created_to: str,
    ) -> list[Document]:
        """All documents created within the inclusive window.

        Narrowed to *tag_id* and/or *correspondent_id* when given; at least one
        of the two is required so the call never lists the whole library.
        """
        if tag_id is None and correspondent_id is None:
            raise ValueError("list_documents needs a tag_id or a correspondent_id")
        params: dict[str, Any] = {
            "created__gte": created_from,
            "created__lte": created_to,
            "fields": _DOCUMENT_FIELDS,
        }
        if tag_id is not None:
            params["tags__id__all"] = str(tag_id)
        if correspondent_id is not None:
            params["correspondents__id"] = str(correspondent_id)
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
        subsequent requests follow the ``next`` link, re-anchored to the
        configured base URL. The loop guards against a repeated ``next`` URL,
        runaway paging, and a final count that disagrees with the first page's
        reported total.
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

        A link that carries its own authority — an absolute ``http(s)://``
        URL, a protocol-relative ``//host/…`` URL, or one that differs only by
        scheme — is re-anchored to the origin the client was configured with,
        keeping the path and query, so the request (and the Authorization
        header) always goes to the configured host. Links without an authority
        are resolved against the base URL the normal way.
        """
        parsed = httpx.URL(next_url)
        base = self._client.base_url
        if parsed.host is None:
            return str(base.join(next_url))
        rewritten = self._reanchor_to_base(parsed, base)
        if str(rewritten) != str(parsed):
            self._origin_rewrites.append(
                f"server advertised {next_url}; re-anchored to {rewritten}"
            )
        return str(rewritten)

    def _reanchor_to_base(self, next_url: httpx.URL, base: httpx.URL) -> httpx.URL:
        """Rebuild *next_url* on *base*'s origin, keeping its path and query.

        Paperless builds absolute links from its own notion of its base URL,
        which can lag the configured one: an ``http`` scheme behind a TLS
        proxy, a different host name, or both. Following such a link verbatim
        would both fail (the proxy answers 301) and ship the API token to a
        host the operator did not configure. The base URL's path prefix is
        kept unless the advertised path already carries it, so a base URL
        served below a proxy subpath keeps working.
        """
        path = next_url.path or "/"
        base_path = base.path
        if base_path not in ("", "/") and not (
            path == base_path or path.startswith(base_path.rstrip("/") + "/")
        ):
            path = base_path.rstrip("/") + "/" + path.lstrip("/")
        return base.copy_with(
            scheme=base.scheme,
            host=base.host,
            port=base.port,
            path=path,
            query=next_url.query,
        )

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
        # The full request URL (scheme, host, path, query) identifies the
        # request; the token travels in a header and is never part of it.
        url = str(response.request.url)
        location = response.headers.get("Location")
        redirected = f"; redirected to {location}" if location else ""
        if status in (401, 403):
            raise TaxExportAuthError(
                f"Paperless rejected the API token (HTTP {status} for {url}){redirected}"
            )
        if status == 406:
            raise ApiVersionError(
                f"Paperless does not serve API version 10 (HTTP 406 for {url}){redirected}"
            )
        raise HttpStatusError(
            f"Paperless returned HTTP {status} for {response.request.method} {url}{redirected}"
        )
