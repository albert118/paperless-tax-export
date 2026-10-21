"""Tests for the investments file: IBKR correspondent selection, client filter, CSV, run layer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

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
    paperless_filter_matches,
    tag_payload,
)

from paperless_tax_export.cli import main
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.csv_output import (
    INVESTMENTS_COLUMNS,
    InvestmentRow,
    write_investments_csv,
)
from paperless_tax_export.errors import StartupError
from paperless_tax_export.export import run_export
from paperless_tax_export.parsing import ReviewCode
from paperless_tax_export.selection import select_ibkr_documents
from paperless_tax_export.settings import Settings

IBKR_ID = 8
DEDUCTION_TAG_ID = 1
HEADER = "paperless_document_id,title,created,document_type,correspondent\r\n"


def make_handler(
    *,
    correspondents: list[dict[str, object]],
    documents: list[dict[str, object]],
    tags: list[dict[str, object]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that applies the tag, correspondent and created filters like Paperless."""
    all_tags = tags if tags is not None else [tag_payload(DEDUCTION_TAG_ID, "tax deductible")]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == "/api/tags/":
            return json_response(page_payload(list(all_tags)))
        if path == "/api/custom_fields/":
            return json_response(
                page_payload([custom_field_payload(10, "Transaction Amount", data_type="monetary")])
            )
        if path == "/api/correspondents/":
            return json_response(page_payload(list(correspondents)))
        if path == "/api/document_types/":
            return json_response(page_payload([document_type_payload(6, "Statement")]))
        if path == "/api/documents/":
            found = [
                document for document in documents if paperless_filter_matches(document, params)
            ]
            return json_response(page_payload(found))
        return json_response({}, status_code=404)

    return handler


def ibkr_document(
    doc_id: int,
    *,
    created: str = "2025-10-01",
    tags: list[int] | None = None,
    custom_fields: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return document_payload(
        doc_id,
        created=f"{created}T00:00:00Z",
        correspondent=IBKR_ID,
        document_type=6,
        tags=[] if tags is None else tags,
        custom_fields=custom_fields,
    )


def ibkr_correspondents() -> list[dict[str, object]]:
    return [
        correspondent_payload(5, "Alpha Supplies"),
        correspondent_payload(IBKR_ID, "Interactive Brokers"),
    ]


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


# --- client -----------------------------------------------------------------


def test_list_documents_maps_correspondent_id_to_correspondent_id() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params.items()))
        return json_response(page_payload([document_payload(1)]))

    make_client(handler).list_documents(
        correspondent_id=8, created_from="2025-07-01", created_to="2026-06-30"
    )
    assert captured["correspondent__id"] == "8"
    assert "correspondents__id" not in captured
    assert "tags__id__all" not in captured
    assert captured["created__gte"] == "2025-07-01"
    assert captured["created__lte"] == "2026-06-30"


def test_strict_paperless_filter_returns_only_the_correspondent_documents() -> None:
    documents = [
        ibkr_document(1),
        document_payload(2, created="2025-10-01T00:00:00Z", correspondent=5),
        document_payload(3, created="2025-10-01T00:00:00Z", correspondent=None),
    ]
    sent: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params)
        found = [d for d in documents if paperless_filter_matches(d, request.url.params)]
        return json_response(page_payload(found))

    listed = make_client(handler).list_documents(
        correspondent_id=IBKR_ID, created_from="2025-07-01", created_to="2026-06-30"
    )
    assert [document.id for document in listed] == [1]
    assert "correspondent__id" in sent[0]


def test_strict_mock_ignores_the_misspelt_plural_filter_name() -> None:
    params = httpx.QueryParams({"correspondents__id": str(IBKR_ID)})
    other = document_payload(2, created="2025-10-01T00:00:00Z", correspondent=5)
    assert paperless_filter_matches(other, params) is True


def test_list_documents_without_tag_or_correspondent_is_rejected() -> None:
    client = make_client(lambda request: json_response(page_payload([])))
    with pytest.raises(ValueError, match="tag_id or a correspondent_id"):
        client.list_documents(created_from="2025-07-01", created_to="2026-06-30")


# --- selection --------------------------------------------------------------


def select(
    documents: list[dict[str, object]], correspondents: list[dict[str, object]] | None = None
) -> tuple[bool, tuple[int, ...]]:
    client = make_client(
        make_handler(
            correspondents=correspondents if correspondents is not None else ibkr_correspondents(),
            documents=documents,
        )
    )
    found, selected = select_ibkr_documents(
        client, "Interactive Brokers", "2025-07-01", "2026-06-30"
    )
    return found, tuple(document.id for document in selected)


def test_select_lists_only_exact_correspondent_documents_in_fy_sorted() -> None:
    documents = [
        ibkr_document(30, created="2026-03-01"),
        ibkr_document(20, created="2025-10-01"),
        ibkr_document(10, created="2025-10-01"),
        document_payload(99, created="2025-10-01T00:00:00Z", correspondent=5),
        document_payload(98, created="2025-10-01T00:00:00Z", correspondent=None),
    ]
    assert select(documents) == (True, (10, 20, 30))


@pytest.mark.parametrize(
    ("created", "included"),
    [
        ("2025-06-30", False),
        ("2025-07-01", True),
        ("2026-06-30", True),
        ("2026-07-01", False),
    ],
)
def test_select_fy_boundaries(created: str, included: bool) -> None:
    found, ids = select([ibkr_document(1, created=created)])
    assert found is True
    assert ids == ((1,) if included else ())


def test_select_missing_correspondent_returns_false_and_no_documents() -> None:
    assert select([ibkr_document(1)], [correspondent_payload(5, "Alpha Supplies")]) == (False, ())


def test_select_requires_an_exact_name_match() -> None:
    near_misses = [
        correspondent_payload(IBKR_ID, "Interactive Brokers LLC"),
        correspondent_payload(9, "interactive brokers"),
    ]
    assert select([ibkr_document(1)], near_misses) == (False, ())


def test_select_duplicate_correspondent_names_are_a_startup_error() -> None:
    duplicates = [
        correspondent_payload(IBKR_ID, "Interactive Brokers"),
        correspondent_payload(9, "Interactive Brokers"),
    ]
    with pytest.raises(StartupError, match="Interactive Brokers"):
        select([ibkr_document(1)], duplicates)


def ignoring_filter_handler(
    documents: list[dict[str, object]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Like make_handler, but the documents endpoint ignores every filter."""
    inner = make_handler(correspondents=ibkr_correspondents(), documents=documents)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/":
            return json_response(page_payload(list(documents)))
        return inner(request)

    return handler


def test_select_fails_when_the_server_ignores_the_correspondent_filter() -> None:
    documents = [
        ibkr_document(1),
        document_payload(
            2, title="Secret supplier", created="2025-10-01T00:00:00Z", correspondent=5
        ),
        document_payload(3, created="2025-10-01T00:00:00Z", correspondent=None),
    ]
    client = make_client(ignoring_filter_handler(documents))
    with pytest.raises(StartupError) as excinfo:
        select_ibkr_documents(client, "Interactive Brokers", "2025-07-01", "2026-06-30")
    message = str(excinfo.value)
    assert "Interactive Brokers" in message
    assert "2 of 3" in message
    assert "ignored the correspondent filter" in message
    assert "Secret supplier" not in message


def test_run_exits_4_and_writes_nothing_when_the_server_ignores_the_correspondent_filter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    documents = [
        ibkr_document(1, tags=[DEDUCTION_TAG_ID]),
        document_payload(2, created="2025-10-01T00:00:00Z"),
    ]
    transport = httpx.MockTransport(ignoring_filter_handler(documents))

    def build(
        base_url: str, token: str, *, page_size: int = 100, timeout: float = 30.0
    ) -> PaperlessClient:
        return PaperlessClient(
            base_url, token, page_size=page_size, timeout=timeout, transport=transport
        )

    monkeypatch.setattr("paperless_tax_export.cli.PaperlessClient", build)
    token = tmp_path / "token.txt"
    token.write_text("test-token", encoding="utf-8")
    output = tmp_path / "out"
    code = main(["run", "--token-file", str(token), "--output", str(output)])
    assert code == 4
    assert "ignored the correspondent filter" in capsys.readouterr().err
    assert not output.exists()


# --- CSV --------------------------------------------------------------------


def test_investments_header_has_no_tags_column() -> None:
    assert INVESTMENTS_COLUMNS == (
        "paperless_document_id",
        "title",
        "created",
        "document_type",
        "correspondent",
    )


def test_investments_csv_with_no_rows_is_header_only(tmp_path: Path) -> None:
    path = tmp_path / "investments.csv"
    write_investments_csv(path, rows=[])
    assert path.read_bytes() == b"\xef\xbb\xbf" + HEADER.encode("utf-8")
    assert tuple(HEADER.strip().split(",")) == INVESTMENTS_COLUMNS


def test_investments_csv_sorts_rows_and_guards_text(tmp_path: Path) -> None:
    path = tmp_path / "investments.csv"
    rows = [
        InvestmentRow(2, "=HYPERLINK(1)", "2025-10-01", "Statement", "Interactive Brokers"),
        InvestmentRow(1, "Plain", "2025-10-01", None, None),
        InvestmentRow(3, "Earlier", "2025-08-01", "Statement", "Interactive Brokers"),
    ]
    write_investments_csv(path, rows=rows)
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert read_text(path) == (
        HEADER
        + "3,Earlier,2025-08-01,Statement,Interactive Brokers\r\n"
        + "1,Plain,2025-10-01,,\r\n"
        + "2,'=HYPERLINK(1),2025-10-01,Statement,Interactive Brokers\r\n"
    )


# --- run layer --------------------------------------------------------------


def test_run_export_writes_the_investments_file(tmp_path: Path) -> None:
    documents = [
        ibkr_document(2, created="2026-01-02", tags=[7]),
        ibkr_document(1, created="2025-09-01", tags=[]),
        ibkr_document(3, created="2026-07-01"),
    ]
    tags = [tag_payload(DEDUCTION_TAG_ID, "tax deductible"), tag_payload(7, "tax-information")]
    client = make_client(
        make_handler(correspondents=ibkr_correspondents(), documents=documents, tags=tags)
    )
    result = run_export(Settings(), client, output=tmp_path)
    assert result.investments_count == 2
    assert result.warnings == ()
    assert read_text(tmp_path / "investments.csv") == (
        HEADER
        + "1,Statement,2025-09-01,Statement,Interactive Brokers\r\n"
        + "2,Statement,2026-01-02,Statement,Interactive Brokers\r\n"
    )


def test_document_with_both_signals_is_in_both_outputs_with_one_warning(tmp_path: Path) -> None:
    both = ibkr_document(
        4,
        tags=[DEDUCTION_TAG_ID],
        custom_fields=[{"field": 10, "value": "50.00"}],
    )
    client = make_client(make_handler(correspondents=ibkr_correspondents(), documents=[both]))
    result = run_export(Settings(), client, output=tmp_path)
    assert result.warnings == (
        "document 4 carries the deduction tag and the 'Interactive Brokers' correspondent; "
        "it appears in both outputs",
    )
    assert (result.selected_count, result.accepted_count, result.review_count) == (1, 1, 0)
    assert "4,2025-10-01" in read_text(tmp_path / "deductions.csv")
    assert "4,Statement,2025-10-01" in read_text(tmp_path / "investments.csv")
    assert ReviewCode.__members__.get("BOTH_DEDUCTIBLE_AND_IBKR") is None
    assert "BOTH_DEDUCTIBLE_AND_IBKR" not in read_text(tmp_path / "review.csv")


def test_missing_correspondent_warns_and_writes_a_header_only_index(tmp_path: Path) -> None:
    client = make_client(
        make_handler(
            correspondents=[correspondent_payload(5, "Alpha Supplies")],
            documents=[document_payload(1, correspondent=5)],
        )
    )
    result = run_export(Settings(), client, output=tmp_path)
    assert result.investments_count == 0
    assert len(result.warnings) == 1
    assert "'Interactive Brokers'" in result.warnings[0]
    assert (tmp_path / "investments.csv").read_bytes() == b"\xef\xbb\xbf" + HEADER.encode()


def test_ibkr_correspondent_name_comes_from_settings(tmp_path: Path) -> None:
    correspondents = [correspondent_payload(IBKR_ID, "IB Australia")]
    client = make_client(make_handler(correspondents=correspondents, documents=[ibkr_document(1)]))
    result = run_export(Settings(ibkr_correspondent="IB Australia"), client, output=tmp_path)
    assert result.investments_count == 1


def test_identical_data_gives_byte_identical_investments_file(tmp_path: Path) -> None:
    documents = [ibkr_document(2, tags=[1]), ibkr_document(1, created="2025-08-01")]

    def run(output: Path) -> bytes:
        client: PaperlessClient = make_client(
            make_handler(correspondents=ibkr_correspondents(), documents=documents)
        )
        run_export(Settings(), client, output=output)
        return (output / "investments.csv").read_bytes()

    assert run(tmp_path / "first") == run(tmp_path / "second")


# --- CLI --------------------------------------------------------------------


def _install(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = httpx.MockTransport(handler)

    def build(
        base_url: str, token: str, *, page_size: int = 100, timeout: float = 30.0
    ) -> PaperlessClient:
        return PaperlessClient(
            base_url, token, page_size=page_size, timeout=timeout, transport=transport
        )

    monkeypatch.setattr("paperless_tax_export.cli.PaperlessClient", build)


def test_cli_missing_correspondent_prints_one_warning_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("test-token", encoding="utf-8")
    handler = make_handler(
        correspondents=[correspondent_payload(5, "Alpha Supplies")], documents=[]
    )
    _install(monkeypatch, handler)
    output = tmp_path / "out"
    code = main(["run", "--token-file", str(token_file), "--output", str(output)])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err.count("warning:") == 1
    assert "'Interactive Brokers'" in captured.err
    assert "investments.csv written to" in captured.out
    assert (output / "investments.csv").read_bytes() == b"\xef\xbb\xbf" + HEADER.encode()
