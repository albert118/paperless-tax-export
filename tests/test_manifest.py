"""Tests for manifest.json, the count guards, --expected-count and --dry-run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from conftest import (
    BASE_URL,
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

from paperless_tax_export import __version__
from paperless_tax_export.cli import main
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.errors import CountGuardError
from paperless_tax_export.export import run_export
from paperless_tax_export.settings import Settings

TAG_ID = 1
IBKR_ID = 8
OUTPUT_FILES = ("deductions.csv", "review.csv", "investments.csv", "manifest.json")


def _documents() -> list[dict[str, object]]:
    """Two in-FY deduction documents (one accepted, one review), one out of FY, one IBKR."""
    return [
        document_payload(
            1,
            title="Receipt A",
            correspondent=5,
            custom_fields=[{"field": 10, "value": "100.00"}],
        ),
        document_payload(
            2,
            title="Refund B",
            correspondent=5,
            custom_fields=[{"field": 10, "value": "-5.00"}],
        ),
        document_payload(
            3,
            title="Old receipt",
            created="2024-01-01T00:00:00Z",
            correspondent=5,
            custom_fields=[{"field": 10, "value": "9.00"}],
        ),
        document_payload(
            4,
            title="Statement",
            tags=[],
            correspondent=IBKR_ID,
            document_type=6,
        ),
    ]


def make_handler(
    documents: list[dict[str, object]] | None = None,
    *,
    fault: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve every run endpoint, filtering documents like Paperless; optionally break paging."""
    all_documents = documents if documents is not None else _documents()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == "/api/tags/":
            return json_response(page_payload([tag_payload(TAG_ID, "tax deductible")]))
        if path == "/api/custom_fields/":
            return json_response(
                page_payload([custom_field_payload(10, "Transaction Amount", data_type="monetary")])
            )
        if path == "/api/correspondents/":
            return json_response(
                page_payload(
                    [
                        correspondent_payload(5, "Alpha Supplies"),
                        correspondent_payload(IBKR_ID, "Interactive Brokers"),
                    ]
                )
            )
        if path == "/api/document_types/":
            return json_response(page_payload([document_type_payload(6, "Statement")]))
        if path == "/api/documents/":
            if params.get("page_size") == "1" and params.get("fields") == "id":
                tagged = [d for d in all_documents if paperless_filter_matches(d, params)]
                return json_response(page_payload([{"id": 1}] if tagged else [], count=len(tagged)))
            found = [d for d in all_documents if paperless_filter_matches(d, params)]
            if fault is not None and "tags__id__all" in params:
                return _faulty_page(found, fault, params.get("page"))
            return json_response(page_payload(found))
        return json_response({}, status_code=404)

    return handler


def _faulty_page(found: list[dict[str, object]], fault: str, page: str | None) -> httpx.Response:
    """Page one reports ``len(found) + 1`` documents; the later pages break as *fault* says."""
    next_url = f"{BASE_URL}/api/documents/?page=2"
    reported = len(found) + 1
    if page is None:
        # A missing "next" strands the extra document the count promised.
        return json_response(
            page_payload(
                found[:1], count=reported, next_url=None if fault == "no_next" else next_url
            )
        )
    if fault == "lost_page":
        return json_response(page_payload(found[1:], count=reported, next_url=None))
    return json_response(page_payload(found[1:], count=reported, next_url=next_url))


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


def _run_cli(tmp_path: Path, output: Path, *extra: str, name: str = "token.txt") -> list[str]:
    token = tmp_path / name
    token.write_text("test-token", encoding="utf-8")
    return ["run", "--token-file", str(token), "--output", str(output), *extra]


def _written(output: Path) -> list[str]:
    return sorted(path.name for path in output.glob("*")) if output.exists() else []


# --- paging integrity on run -------------------------------------------------


@pytest.mark.parametrize("fault", ["lost_page", "repeated_page", "no_next"])
def test_paging_failure_exits_non_zero_and_writes_nothing(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _install(monkeypatch, make_handler(fault=fault))
    output = tmp_path / "out"
    code = main(_run_cli(tmp_path, output))
    assert code == 4
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


def test_paging_failure_leaves_an_existing_output_directory_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler(fault="lost_page"))
    output = tmp_path / "out"
    output.mkdir()
    assert main(_run_cli(tmp_path, output)) == 4
    assert _written(output) == []


# --- --expected-count --------------------------------------------------------


def test_expected_count_mismatch_exits_non_zero_names_both_numbers_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _install(monkeypatch, make_handler())
    output = tmp_path / "out"
    code = main(_run_cli(tmp_path, output, "--expected-count", "7"))
    err = capsys.readouterr().err
    assert code == 4
    assert "expected 7" in err
    assert "reported 2" in err
    assert not output.exists()


def test_expected_count_mismatch_writes_nothing_into_an_existing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler())
    output = tmp_path / "out"
    output.mkdir()
    assert main(_run_cli(tmp_path, output, "--expected-count", "3")) == 4
    assert _written(output) == []


def test_expected_count_match_writes_all_files_and_records_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler())
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--expected-count", "2")) == 0
    assert _written(output) == sorted(OUTPUT_FILES)
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["expected_count"] == 2


def test_expected_count_is_null_in_the_manifest_when_not_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler())
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 0
    assert (
        json.loads((output / "manifest.json").read_text(encoding="utf-8"))["expected_count"] is None
    )


def test_run_export_reads_expected_count_from_settings(tmp_path: Path) -> None:
    client = make_client(make_handler())
    with pytest.raises(CountGuardError, match=r"expected 9 .* reported 2"):
        run_export(Settings(expected_count=9), client, output=tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_negative_expected_count_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler())
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--expected-count", "-1")) == 2
    assert not output.exists()


# --- --dry-run ---------------------------------------------------------------


def test_dry_run_writes_nothing_and_its_counts_match_a_real_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _install(monkeypatch, make_handler())
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--dry-run")) == 0
    dry = capsys.readouterr().out.splitlines()
    assert not output.exists()
    assert dry[-1] == "dry run: nothing was written"

    assert main(_run_cli(tmp_path, output)) == 0
    real = capsys.readouterr().out.splitlines()
    assert _written(output) == sorted(OUTPUT_FILES)

    count_lines = [line for line in dry if "accepted" in line or line.startswith("tagged")]
    assert count_lines == [line for line in real if "accepted" in line or line.startswith("tagged")]
    assert count_lines[0] == "1 accepted, 1 review (2 deduction documents)"
    assert count_lines[1] == "tagged 3, out of FY 1, selected in FY 2, investments 1"
    planned = [line.split(" would write ")[0] for line in dry if " would write " in line]
    assert planned == list(OUTPUT_FILES[:3]) + ["manifest.json"]


def test_dry_run_still_applies_the_expected_count_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler())
    assert main(_run_cli(tmp_path, tmp_path / "out", "--dry-run", "--expected-count", "5")) == 4


def test_dry_run_still_applies_the_paging_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler(fault="no_next"))
    assert main(_run_cli(tmp_path, tmp_path / "out", "--dry-run")) == 4


def test_dry_run_returns_the_manifest_without_creating_the_directory(tmp_path: Path) -> None:
    result = run_export(
        Settings(), make_client(make_handler()), output=tmp_path / "out", dry_run=True
    )
    assert result.dry_run
    assert [file.name for file in result.files][-1] == "manifest.json"
    assert not (tmp_path / "out").exists()


# --- manifest ----------------------------------------------------------------


def _manifest(output: Path) -> dict[str, object]:
    loaded = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_manifest_hashes_and_row_counts_match_the_files_on_disk(tmp_path: Path) -> None:
    output = tmp_path / "out"
    run_export(Settings(), make_client(make_handler()), output=output)
    outputs = _manifest(output)["outputs"]
    assert isinstance(outputs, dict)
    assert sorted(outputs) == ["deductions.csv", "investments.csv", "review.csv"]
    for name, entry in outputs.items():
        data = (output / name).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["rows"] == data.decode("utf-8-sig").count("\r\n") - 1


def test_manifest_records_versions_parameters_and_counts(tmp_path: Path) -> None:
    output = tmp_path / "out"
    run_export(Settings(), make_client(make_handler()), output=output)
    manifest = _manifest(output)
    assert manifest["tool_version"] == __version__
    assert manifest["paperless"] == {"server_version": "2024.12.1", "api_version": "10"}
    assert manifest["counts"] == {
        "tagged_total": 3,
        "out_of_fy": 1,
        "selected_in_fy": 2,
        "accepted": 1,
        "review": 1,
        "investments_count": 1,
    }
    parameters = manifest["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["fy_start"] == "2025-07-01"
    assert parameters["deduction_tag"] == "tax deductible"


@pytest.mark.parametrize(
    ("setting", "recorded"), [("asset-reviewed", "asset-reviewed"), (None, None)]
)
def test_manifest_records_the_reviewed_tag_parameter_deterministically(
    setting: str | None, recorded: str | None, tmp_path: Path
) -> None:
    settings = Settings(asset_reviewed_tag=setting)
    manifests = []
    for name in ("first", "second"):
        output = tmp_path / name
        run_export(settings, make_client(make_handler()), output=output)
        manifests.append((output / "manifest.json").read_bytes())
    assert manifests[0] == manifests[1]
    text = manifests[0].decode("utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    parameters = json.loads(text)["parameters"]
    assert "asset_reviewed_tag" in parameters
    assert parameters["asset_reviewed_tag"] == recorded


def test_manifest_counts_satisfy_the_accepted_plus_review_invariant(tmp_path: Path) -> None:
    output = tmp_path / "out"
    run_export(Settings(), make_client(make_handler()), output=output)
    counts = _manifest(output)["counts"]
    assert isinstance(counts, dict)
    assert counts["accepted"] + counts["review"] == counts["selected_in_fy"]
    assert counts["tagged_total"] == counts["selected_in_fy"] + counts["out_of_fy"]


def test_identical_data_gives_a_byte_identical_manifest(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    run_export(Settings(), make_client(make_handler()), output=first)
    run_export(Settings(), make_client(make_handler()), output=second)
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()


def test_manifest_is_canonical_json_without_a_timestamp(tmp_path: Path) -> None:
    output = tmp_path / "out"
    run_export(Settings(), make_client(make_handler()), output=output)
    raw = (output / "manifest.json").read_bytes()
    text = raw.decode("utf-8")
    assert b"\r" not in raw
    assert text.endswith("}\n")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    assert "time" not in text.replace("tool_version", "")


def test_a_failed_csv_write_leaves_no_manifest_even_from_an_earlier_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    run_export(Settings(), make_client(make_handler()), output=output)
    original = Path.write_bytes

    def failing(self: Path, data: bytes) -> int:
        if self.name == "review.csv":
            raise OSError("disk full")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing)
    with pytest.raises(OSError, match="disk full"):
        run_export(Settings(), make_client(make_handler()), output=output)
    assert not (output / "manifest.json").exists()
