"""Tests for the command-line interface."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from conftest import (
    correspondent_payload,
    custom_field_payload,
    document_payload,
    document_type_payload,
    filtered_documents_response,
    json_response,
    page_payload,
    storage_path_payload,
    tag_payload,
)

from paperless_tax_export import __version__
from paperless_tax_export.cli import main
from paperless_tax_export.client import PaperlessClient


def _write_token(tmp_path: Path, content: str = "test-token") -> Path:
    token_file = tmp_path / "token.txt"
    token_file.write_text(content, encoding="utf-8")
    return token_file


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)

    def build(
        base_url: str,
        token: str,
        *,
        page_size: int = 100,
        timeout: float = 30.0,
    ) -> PaperlessClient:
        return PaperlessClient(
            base_url,
            token,
            page_size=page_size,
            timeout=timeout,
            transport=transport,
        )

    monkeypatch.setattr("paperless_tax_export.cli.PaperlessClient", build)


def _audit_handler() -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(
                page_payload(
                    [
                        tag_payload(1, "tax-deductible", document_count=2),
                        tag_payload(2, "tax-information", document_count=1),
                    ]
                )
            )
        if path == "/api/documents/":
            return filtered_documents_response(
                request, [document_payload(7, title="Receipt", created="2025-10-01T00:00:00Z")]
            )
        if path == "/api/custom_fields/":
            return json_response(
                page_payload([custom_field_payload(10, "Gross Amount", data_type="monetary")])
            )
        if path == "/api/storage_paths/":
            return json_response(
                page_payload([storage_path_payload(3, "FY26", "2025/07/", document_count=2)])
            )
        if path == "/api/correspondents/":
            return json_response(page_payload([correspondent_payload(5, "Interactive Brokers")]))
        if path == "/api/document_types/":
            return json_response(page_payload([document_type_payload(6, "Trade Confirmation")]))
        return json_response({}, status_code=404)

    return handler


def _run_handler() -> Callable[[httpx.Request], httpx.Response]:
    """A handler serving every endpoint the run subcommand needs."""
    document = document_payload(
        1,
        title="Receipt",
        correspondent=5,
        custom_fields=[{"field": 10, "value": "100.00"}],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(page_payload([tag_payload(1, "tax deductible", document_count=1)]))
        if path == "/api/custom_fields/":
            return json_response(
                page_payload([custom_field_payload(10, "Transaction Amount", data_type="monetary")])
            )
        if path == "/api/correspondents/":
            return json_response(page_payload([correspondent_payload(5, "Alpha Supplies")]))
        if path == "/api/document_types/":
            return json_response(page_payload([document_type_payload(6, "Receipt")]))
        if path == "/api/documents/":
            return filtered_documents_response(request, [document])
        return json_response({}, status_code=404)

    return handler


def test_version_exits_zero_and_prints_the_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2


def test_bad_top_level_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--not-a-real-flag"])
    assert exc_info.value.code == 2


def test_bad_audit_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["audit", "--not-a-real-flag"])
    assert exc_info.value.code == 2


def test_missing_token_file_returns_2_and_names_the_path(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.txt"
    code = main(["audit", "--token-file", str(missing)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert str(missing) in captured.err


def test_empty_token_file_returns_2_and_never_leaks_content(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("", encoding="utf-8")
    code = main(["audit", "--token-file", str(token_file)])
    captured = capsys.readouterr()
    assert code == 2
    assert str(token_file) in captured.err
    assert captured.out == ""


def test_client_401_is_reported_as_warnings_and_a_failure_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret = "secret-token-12345"
    token_file = _write_token(tmp_path, secret)
    output = tmp_path / "out"
    _install_mock_client(monkeypatch, lambda request: json_response({}, status_code=401))
    code = main(["audit", "--token-file", str(token_file), "--output", str(output)])
    captured = capsys.readouterr()
    # Every optional read failed and nothing came back, so this is not success.
    assert code == 3
    combined = captured.out + captured.err
    assert secret not in combined
    assert "Warnings" in captured.out
    assert "no tags visible to this token" in captured.out
    assert (output / "audit.json").exists()


def test_pagination_count_mismatch_returns_4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = _write_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "count": 3,
                "next": None,
                "previous": None,
                "results": [tag_payload(1, "a"), tag_payload(2, "b")],
            }
        )

    _install_mock_client(monkeypatch, handler)
    code = main(["audit", "--token-file", str(token_file)])
    assert code == 4


def test_successful_run_writes_audit_json_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    token_file = _write_token(tmp_path)
    _install_mock_client(monkeypatch, _audit_handler())
    output = tmp_path / "out"
    code = main(["audit", "--token-file", str(token_file), "--output", str(output)])
    captured = capsys.readouterr()
    assert code == 0
    assert "Paperless audit" in captured.out
    json_path = output / "audit.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["deduction_document_count"] >= 0
    assert data["api_version"] == "10"


def test_partial_read_failure_with_visible_things_elsewhere_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """One failed optional read does not force exit 3 while tags were visible."""
    token_file = _write_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(page_payload([tag_payload(1, "tax-deductible", document_count=2)]))
        if path == "/api/correspondents/":
            return json_response({}, status_code=500)
        if path == "/api/custom_fields/":
            return json_response(page_payload([]))
        if path == "/api/storage_paths/":
            return json_response(page_payload([]))
        if path == "/api/document_types/":
            return json_response(page_payload([]))
        return json_response({}, status_code=404)

    _install_mock_client(monkeypatch, handler)
    code = main(["audit", "--token-file", str(token_file), "--output", str(tmp_path / "out")])
    assert code == 0
    assert "correspondents" in capsys.readouterr().out


def test_one_failed_read_with_readable_custom_fields_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Tags failed and the object lists are empty, but a custom field is readable: not exit 3."""
    token_file = _write_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response({}, status_code=500)
        if path == "/api/custom_fields/":
            return json_response(page_payload([custom_field_payload(10, "Gross Amount")]))
        if path == "/api/storage_paths/":
            return json_response(page_payload([]))
        if path == "/api/correspondents/":
            return json_response(page_payload([]))
        if path == "/api/document_types/":
            return json_response(page_payload([]))
        return json_response({}, status_code=404)

    _install_mock_client(monkeypatch, handler)
    code = main(["audit", "--token-file", str(token_file), "--output", str(tmp_path / "out")])
    assert code == 0
    assert "tags:" in capsys.readouterr().out


def test_all_optional_reads_failed_and_nothing_came_back_returns_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Every optional read failed and nothing came back: exit 3, never a clean pass."""
    token_file = _write_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({}, status_code=500)

    _install_mock_client(monkeypatch, handler)
    code = main(["audit", "--token-file", str(token_file), "--output", str(tmp_path / "out")])
    assert code == 3
    captured = capsys.readouterr()
    assert "Warnings" in captured.out


def test_run_missing_token_file_returns_2_and_names_the_path(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.txt"
    code = main(["run", "--token-file", str(missing)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert str(missing) in captured.err


def test_bad_run_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--not-a-real-flag"])
    assert exc_info.value.code == 2


def test_run_success_writes_csvs_and_prints_the_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    token_file = _write_token(tmp_path)
    _install_mock_client(monkeypatch, _run_handler())
    output = tmp_path / "out"
    code = main(["run", "--token-file", str(token_file), "--output", str(output)])
    captured = capsys.readouterr()
    assert code == 0
    assert "1 accepted, 0 review (1 deduction documents)" in captured.out
    assert "deductions.csv written to" in captured.out
    assert "review.csv written to" in captured.out
    assert "GST" not in captured.out
    assert "work-use" not in captured.out
    assert "note: supplier_name is the document correspondent" in captured.out
    assert (output / "deductions.csv").exists()
    assert (output / "review.csv").exists()
    assert not (output / "audit.json").exists()


def test_run_missing_deduction_tag_returns_4(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    token_file = _write_token(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags/":
            return json_response(page_payload([tag_payload(9, "income")]))
        return json_response({}, status_code=404)

    _install_mock_client(monkeypatch, handler)
    code = main(["run", "--token-file", str(token_file), "--output", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 4
    assert captured.out == ""
    assert "tax deductible" in captured.err
    assert not (tmp_path / "out").exists()


def _run_handler_with_documents(
    documents_handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """The run handler, with ``/api/documents/`` served by *documents_handler*."""
    base = _run_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/":
            return documents_handler(request)
        return base(request)

    return handler


def _without_params(request: httpx.Request, *, prefix: str) -> httpx.Request:
    kept = [(key, value) for key, value in request.url.params.items() if not key.startswith(prefix)]
    return httpx.Request(request.method, request.url.copy_with(params=kept))


@pytest.mark.parametrize(
    ("ignored_prefix", "documents", "expected"),
    [
        (
            "tags__id__all",
            [
                document_payload(1, tags=[1], custom_fields=[{"field": 10, "value": "1.00"}]),
                document_payload(2, tags=[9], custom_fields=[{"field": 10, "value": "2.00"}]),
            ],
            "1 do not carry the tag",
        ),
        (
            "created__",
            [
                document_payload(1, created="2025-10-01T00:00:00Z"),
                document_payload(2, created="2026-07-01T00:00:00Z"),
            ],
            "1 were created outside",
        ),
    ],
    ids=["tag-filter", "created-window"],
)
def test_run_returns_4_and_writes_nothing_when_the_server_ignores_a_filter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ignored_prefix: str,
    documents: list[dict[str, object]],
    expected: str,
) -> None:
    token_file = _write_token(tmp_path)

    def documents_handler(request: httpx.Request) -> httpx.Response:
        return filtered_documents_response(
            _without_params(request, prefix=ignored_prefix), documents
        )

    _install_mock_client(monkeypatch, _run_handler_with_documents(documents_handler))
    output = tmp_path / "out"
    code = main(["run", "--token-file", str(token_file), "--output", str(output)])
    captured = capsys.readouterr()
    assert code == 4
    assert captured.out == ""
    assert expected in captured.err
    assert "ignored a filter" in captured.err
    assert not output.exists()


def test_run_returns_4_and_writes_nothing_when_the_tagged_count_is_below_the_selected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    token_file = _write_token(tmp_path)
    documents = [document_payload(1), document_payload(2)]

    def documents_handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("page_size") == "1" and params.get("fields") == "id":
            return json_response(page_payload([{"id": 1}], count=1))
        return filtered_documents_response(request, documents)

    _install_mock_client(monkeypatch, _run_handler_with_documents(documents_handler))
    output = tmp_path / "out"
    code = main(["run", "--token-file", str(token_file), "--output", str(output)])
    captured = capsys.readouterr()
    assert code == 4
    assert captured.out == ""
    assert "1 documents carry tag id 1" in captured.err
    assert "2 selected" in captured.err
    assert not output.exists()


def test_run_accepts_documents_on_both_fy_boundary_dates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = _write_token(tmp_path)
    documents = [
        document_payload(
            doc_id,
            created=f"{created}T00:00:00Z",
            correspondent=5,
            custom_fields=[{"field": 10, "value": "1.00"}],
        )
        for doc_id, created in (
            (1, "2025-06-30"),
            (2, "2025-07-01"),
            (3, "2026-06-30"),
            (4, "2026-07-01"),
        )
    ]

    _install_mock_client(
        monkeypatch,
        _run_handler_with_documents(
            lambda request: filtered_documents_response(request, documents)
        ),
    )
    output = tmp_path / "out"
    code = main(["run", "--token-file", str(token_file), "--output", str(output)])
    assert code == 0
    deductions = (output / "deductions.csv").read_text(encoding="utf-8-sig")
    assert [line.split(",")[0] for line in deductions.splitlines()[1:]] == ["2", "3"]
    assert "2,2025-07-01" in deductions
    assert "3,2026-06-30" in deductions
