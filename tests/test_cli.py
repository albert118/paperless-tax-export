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
            return json_response(
                page_payload([document_payload(7, title="Receipt", created="2025-10-01T00:00:00Z")])
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


def test_client_401_returns_3_without_leaking_the_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    secret = "secret-token-12345"
    token_file = _write_token(tmp_path, secret)
    _install_mock_client(monkeypatch, lambda request: json_response({}, status_code=401))
    code = main(["audit", "--token-file", str(token_file)])
    captured = capsys.readouterr()
    assert code == 3
    combined = captured.out + captured.err
    assert secret not in combined


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
