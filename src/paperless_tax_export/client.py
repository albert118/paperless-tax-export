"""A read-only HTTP client for the Paperless-ngx API.

The client is GET-only by construction: every operation here issues a GET and
there are no write methods anywhere. The API token and headers are never
logged and never appear in error messages. ``next`` links that carry their own
authority are re-anchored to the configured base URL before they are followed,
so a token-bearing request never goes to a host the operator did not
configure.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar
from urllib.parse import unquote

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

from paperless_tax_export import __version__
from paperless_tax_export.errors import (
    ApiVersionError,
    DownloadError,
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


# Originals can be large scans; the JSON calls' 30 second timeout is too short for them.
# httpx applies this per read, not to the whole transfer.
DOWNLOAD_TIMEOUT_SECONDS = 300.0
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_DOCUMENT_FILE_FIELDS = "id,checksum,original_file_name"

_QUOTED_PAIR_RE = re.compile(r"\\(.)")
_DISPOSITION_STAR_RE = re.compile(r"(?:^|;)\s*filename\*\s*=\s*([^;]+)", re.IGNORECASE)
_DISPOSITION_PLAIN_RE = re.compile(
    r'(?:^|;)\s*filename\s*=\s*("(?:[^"\\]|\\.)*"|[^;]+)', re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class DocumentFileInfo:
    """What Paperless stores about a document's original file; either may be ``None``."""

    document_id: int
    checksum: str | None
    original_file_name: str | None


@dataclass(frozen=True, slots=True)
class DownloadedOriginal:
    """The facts measured while an original streamed to disk.

    ``content_length`` is ``None`` when the header is absent, unparseable or the
    body was content-encoded (the header then counts encoded bytes).
    """

    document_id: int
    size: int
    sha256: str
    md5: str
    file_name: str | None
    content_type: str | None
    content_length: int | None


def parse_content_disposition_filename(header: str | None) -> str | None:
    """The file name in a ``Content-Disposition`` value, or ``None``.

    ``filename*=UTF-8''...`` (RFC 5987, percent-decoded) wins over ``filename="..."``.
    Anything unparseable gives ``None``; the caller falls back to other sources.
    """
    if not header:
        return None
    star = _DISPOSITION_STAR_RE.search(header)
    if star is not None:
        parts = star.group(1).strip().split("'", 2)
        if len(parts) == 3:
            charset = parts[0] or "utf-8"
            try:
                decoded = unquote(parts[2], encoding=charset, errors="replace")
            except LookupError:
                decoded = unquote(parts[2], errors="replace")
            if decoded:
                return decoded
    plain = _DISPOSITION_PLAIN_RE.search(header)
    if plain is None:
        return None
    value = plain.group(1).strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        value = _QUOTED_PAIR_RE.sub(lambda match: match.group(1), value[1:-1])
    return value or None


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
        document_type_id: int | None = None,
        created_from: str,
        created_to: str,
    ) -> list[Document]:
        """All documents created within the inclusive window.

        Narrowed to *tag_id*, *correspondent_id* and/or *document_type_id* when
        given; at least one is required so the call never lists the whole library.
        """
        if tag_id is None and correspondent_id is None and document_type_id is None:
            raise ValueError(
                "list_documents needs a tag_id, a correspondent_id or a document_type_id"
            )
        params: dict[str, Any] = {
            "created__gte": created_from,
            "created__lte": created_to,
            "fields": _DOCUMENT_FIELDS,
        }
        if tag_id is not None:
            params["tags__id__all"] = str(tag_id)
        if correspondent_id is not None:
            params["correspondent__id"] = str(correspondent_id)
        if document_type_id is not None:
            params["document_type__id"] = str(document_type_id)
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

    def get_document_file_info(self, document_id: int) -> DocumentFileInfo:
        """The stored checksum and original file name of one document, from one GET."""
        response = self._send(
            "GET",
            f"/api/documents/{document_id}/",
            params={"fields": _DOCUMENT_FILE_FIELDS},
        )
        self._raise_for_document_status(response, document_id)
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise DownloadError(f"document {document_id}: the metadata was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise DownloadError(f"document {document_id}: the metadata was not a JSON object")
        checksum = payload.get("checksum")
        original_name = payload.get("original_file_name")
        return DocumentFileInfo(
            document_id=document_id,
            checksum=checksum if isinstance(checksum, str) else None,
            original_file_name=original_name if isinstance(original_name, str) else None,
        )

    def download_original(self, document_id: int, dest_path: Path) -> DownloadedOriginal:
        """Stream the original upload of one document into *dest_path*.

        The body is written in chunks and never held whole. SHA-256 and MD5 (the
        two hashes Paperless has used for ``checksum``) are computed on the way.
        A non-2xx status, a transport failure or a timeout raises one of the
        client errors, naming the document id and status only.
        """
        sha256 = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        size = 0
        try:
            with self._client.stream(
                "GET",
                f"/api/documents/{document_id}/download/",
                params={"original": "true"},
                headers={"Accept": "*/*", "Accept-Encoding": "identity"},
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            ) as response:
                self._raise_for_document_status(response, document_id)
                with dest_path.open("wb") as handle:
                    for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                        handle.write(chunk)
                        sha256.update(chunk)
                        md5.update(chunk)
                        size += len(chunk)
                headers = response.headers
        except httpx.TimeoutException as exc:
            raise TransportError(f"document {document_id}: the Paperless server timed out") from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"document {document_id}: transfer failed ({type(exc).__name__})"
            ) from exc
        return DownloadedOriginal(
            document_id=document_id,
            size=size,
            sha256=sha256.hexdigest(),
            md5=md5.hexdigest(),
            file_name=parse_content_disposition_filename(headers.get("Content-Disposition")),
            content_type=headers.get("Content-Type"),
            content_length=_plain_content_length(headers),
        )

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

    def _raise_for_document_status(self, response: httpx.Response, document_id: int) -> None:
        """Map a non-2xx status to a client error naming the document id and status only."""
        if response.is_success:
            return
        status = response.status_code
        if status in (401, 403):
            raise TaxExportAuthError(
                f"document {document_id}: Paperless refused the request (HTTP {status})"
            )
        raise HttpStatusError(f"document {document_id}: Paperless returned HTTP {status}")


def _plain_content_length(headers: httpx.Headers) -> int | None:
    encoding = headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        return None
    raw = headers.get("Content-Length")
    if raw is None or not raw.strip().isdecimal():
        return None
    return int(raw)
