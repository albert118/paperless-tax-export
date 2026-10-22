"""Tests for the offline ``verify`` subcommand and its pure check functions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import make_client
from test_manifest import make_handler

from paperless_tax_export.cli import main
from paperless_tax_export.export import run_export
from paperless_tax_export.manifest import ManifestCounts
from paperless_tax_export.settings import Settings
from paperless_tax_export.verify import (
    ExpectedFile,
    check_consistency,
    check_file,
    count_data_rows,
)

BOM = b"\xef\xbb\xbf"
HEADER = b"id,title\r\n"
CSV_FILES = ("deductions.csv", "review.csv", "investments.csv", "bills.csv", "estimate.csv")


@pytest.fixture
def output(tmp_path: Path) -> Path:
    """A real ``run_export`` output directory: 1 accepted, 1 review, 1 investments row."""
    directory = tmp_path / "out"
    run_export(Settings(), make_client(make_handler()), output=directory)
    return directory


def _manifest(output: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    return payload


def _write_manifest(output: Path, payload: object) -> None:
    (output / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _edit_manifest(output: Path, edit: Any) -> None:
    payload = _manifest(output)
    edit(payload)
    _write_manifest(output, payload)


def _verify(output: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    code = main(["verify", "--output", str(output), *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _craft_directory(directory: Path, files: dict[str, bytes], counts: dict[str, int]) -> None:
    """A manifest-consistent directory built from raw bytes (rows counted by the csv module)."""
    directory.mkdir()
    outputs = {}
    for name, content in files.items():
        (directory / name).write_bytes(content)
        outputs[name] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "rows": count_data_rows(content),
        }
    _write_manifest(directory, {"counts": counts, "outputs": outputs})


def _counts(accepted: int, review: int, investments: int) -> dict[str, int]:
    return {
        "tagged_total": accepted + review,
        "out_of_fy": 0,
        "selected_in_fy": accepted + review,
        "accepted": accepted,
        "review": review,
        "investments_count": investments,
        "bills_count": 0,
    }


# --- success -----------------------------------------------------------------


def test_real_run_output_verifies_clean(output: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _verify(output, capsys)
    assert code == 0
    assert err == ""
    lines = out.splitlines()
    assert [line for line in lines if line.startswith("OK ")] == [
        f"OK {name}" for name in sorted(CSV_FILES)
    ]
    assert "verify OK: 5 files match the manifest" in lines
    assert "note: not listed in the manifest: manifest.json" in lines


def test_manifest_with_the_reviewed_tag_parameter_verifies_ok(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "asset_reviewed_tag" in _manifest(output)["parameters"]
    code, _, err = _verify(output, capsys)
    assert code == 0
    assert err == ""


def test_manifest_without_the_reviewed_tag_parameter_still_verifies_ok(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_manifest(output, lambda payload: payload["parameters"].pop("asset_reviewed_tag"))
    assert "asset_reviewed_tag" not in _manifest(output)["parameters"]
    code, out, err = _verify(output, capsys)
    assert code == 0
    assert err == ""
    assert "verify OK" in out


def test_default_output_directory_is_the_one_run_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    run_export(Settings(), make_client(make_handler()), output=Path("output"))
    assert main(["verify"]) == 0
    assert "verify OK" in capsys.readouterr().out


def test_unlisted_files_only_produce_a_note(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "audit.json").write_text("{}", encoding="utf-8")
    code, out, _ = _verify(output, capsys)
    assert code == 0
    notes = [line for line in out.splitlines() if line.startswith("note:")]
    assert notes == ["note: not listed in the manifest: audit.json, manifest.json"]


def test_verify_needs_no_network_settings_env_or_token(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("PAPERLESS_TOKEN", "PAPERLESS_BASE_URL", "PAPERLESS_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no secrets/paperless-token.txt here

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("verify must not read the environment or contact Paperless")

    monkeypatch.setattr("paperless_tax_export.cli.PaperlessClient", forbidden)
    monkeypatch.setattr("paperless_tax_export.cli.build_sources", forbidden)
    monkeypatch.setattr("paperless_tax_export.cli.resolve_token", forbidden)
    code, out, _ = _verify(output, capsys)
    assert code == 0
    assert "verify OK" in out


# --- mismatches (exit 4) -----------------------------------------------------


def test_changed_byte_in_a_csv_is_a_hash_mismatch(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = output / "deductions.csv"
    content = path.read_bytes()
    assert b"100.00" in content
    path.write_bytes(content.replace(b"100.00", b"100.01"))
    code, out, err = _verify(output, capsys)
    assert code == 4
    assert "error:" in err
    mismatch = next(line for line in out.splitlines() if line.startswith("MISMATCH"))
    assert mismatch.startswith("MISMATCH deductions.csv: expected sha256 ")
    assert "(differs: sha256)" in mismatch
    assert "OK review.csv" in out
    assert "verify FAILED: 1 problem(s); 4 of 5 files OK" in out


def test_row_count_differing_from_the_manifest_is_a_row_mismatch(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def edit(payload: dict[str, Any]) -> None:
        payload["outputs"]["review.csv"]["rows"] += 1

    _edit_manifest(output, edit)
    code, out, _ = _verify(output, capsys)
    assert code == 4
    mismatch = next(line for line in out.splitlines() if line.startswith("MISMATCH"))
    assert mismatch.startswith("MISMATCH review.csv:")
    assert "rows 2," in mismatch
    assert mismatch.endswith("rows 1 (differs: rows)")


def test_hash_and_rows_both_differing_are_both_named(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "investments.csv").write_bytes(BOM + HEADER)
    code, out, _ = _verify(output, capsys)
    assert code == 4
    assert "(differs: sha256, rows)" in out


def test_deleted_listed_file_fails_without_a_traceback(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "review.csv").unlink()
    code, out, err = _verify(output, capsys)
    assert code == 4
    assert "MISSING review.csv: listed in the manifest but not found" in out
    assert "Traceback" not in err


def test_counts_disagreeing_with_the_files_fail_each_check(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def edit(payload: dict[str, Any]) -> None:
        payload["counts"].update(accepted=0, review=2, investments_count=3)

    _edit_manifest(output, edit)
    code, out, _ = _verify(output, capsys)
    assert code == 4
    failures = [line for line in out.splitlines() if line.startswith("CHECK FAILED")]
    assert failures == [
        "CHECK FAILED counts.accepted: manifest says 0, deductions.csv has 1 data rows",
        "CHECK FAILED counts.review: manifest says 2, review.csv has 1 data rows",
        "CHECK FAILED counts.investments_count: manifest says 3, investments.csv has 1 data rows",
    ]


def test_accepted_plus_review_not_equal_to_selected_in_fy_fails(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_manifest(output, lambda payload: payload["counts"].update(selected_in_fy=7))
    code, out, _ = _verify(output, capsys)
    assert code == 4
    failures = [line for line in out.splitlines() if line.startswith("CHECK FAILED")]
    assert failures == [
        "CHECK FAILED counts.accepted + counts.review == counts.selected_in_fy: 1 + 1 != 7"
    ]


def test_output_never_contains_file_contents(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "deductions.csv").write_bytes(b"Receipt A,secret supplier\r\n")
    code, out, err = _verify(output, capsys)
    assert code == 4
    for secret in ("Receipt A", "secret supplier", "Refund B"):
        assert secret not in out
        assert secret not in err


# --- unusable manifest (exit 2) ----------------------------------------------


def test_missing_manifest_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _verify(tmp_path, capsys)
    assert code == 2
    assert out == ""
    assert "manifest.json not found" in err


def test_missing_output_directory_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _verify(tmp_path / "nope", capsys)
    assert code == 2
    assert "manifest.json not found" in err


def test_unreadable_manifest_is_a_usage_error(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "manifest.json").unlink()
    (output / "manifest.json").mkdir()  # reading a directory fails on every platform
    code, _, err = _verify(output, capsys)
    assert code == 2
    assert "manifest.json is not readable" in err


@pytest.mark.parametrize("raw", [b"{not json", b"", b"\xff\xfe\x00"])
def test_invalid_json_is_a_usage_error_that_does_not_echo_the_content(
    raw: bytes, output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "manifest.json").write_bytes(raw)
    code, _, err = _verify(output, capsys)
    assert code == 2
    assert "manifest.json is not valid JSON" in err
    assert "not json" not in err


def _drop(path: tuple[str, ...]) -> Any:
    def edit(payload: dict[str, Any]) -> None:
        node = payload
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return edit


def _set(path: tuple[str, ...], value: object) -> Any:
    def edit(payload: dict[str, Any]) -> None:
        node = payload
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return edit


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (_drop(("outputs",)), "lacks an 'outputs' object"),
        (_drop(("counts",)), "lacks a 'counts' object"),
        (_set(("outputs",), []), "lacks an 'outputs' object"),
        (_set(("counts",), "many"), "lacks a 'counts' object"),
        (_drop(("counts", "accepted")), "'counts.accepted' must be a non-negative integer"),
        (_set(("counts", "review"), "1"), "'counts.review' must be a non-negative integer"),
        (
            _set(("counts", "investments_count"), True),
            "'counts.investments_count' must be a non-negative integer",
        ),
        (_set(("counts", "selected_in_fy"), -1), "'counts.selected_in_fy' must be a non-negative"),
        (_set(("outputs", "review.csv", "rows"), "1"), "'outputs.review.csv.rows' must be"),
        (_set(("outputs", "review.csv", "rows"), 1.5), "'outputs.review.csv.rows' must be"),
        (_set(("outputs", "review.csv", "sha256"), 7), "'outputs.review.csv.sha256' must be"),
        (_set(("outputs", "review.csv", "sha256"), "abc"), "'outputs.review.csv.sha256' must be"),
        (_set(("outputs", "review.csv"), "x"), "'outputs.review.csv' must be an object"),
        (_drop(("outputs", "deductions.csv")), "does not list deductions.csv"),
        (_set(("outputs", "../escape.csv"), {"sha256": "0" * 64, "rows": 0}), "not a file name"),
    ],
)
def test_wrong_manifest_shape_is_a_usage_error(
    edit: Any, message: str, output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_manifest(output, edit)
    code, out, err = _verify(output, capsys)
    assert code == 2
    assert out == ""
    assert message in err


def test_manifest_from_0_2_x_is_a_usage_error_naming_the_missing_file(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def to_0_2_x(payload: dict[str, Any]) -> None:
        payload["outputs"]["ibkr-documents.csv"] = payload["outputs"].pop("investments.csv")
        payload["counts"]["ibkr_count"] = payload["counts"].pop("investments_count")

    _edit_manifest(output, to_0_2_x)
    code, out, err = _verify(output, capsys)
    assert code == 2
    assert out == ""
    assert "'outputs' does not list investments.csv" in err
    assert "Traceback" not in err


def test_manifest_without_the_estimate_is_a_usage_error_naming_the_missing_file(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_manifest(output, _drop(("outputs", "estimate.csv")))
    code, out, err = _verify(output, capsys)
    assert code == 2
    assert out == ""
    assert "'outputs' does not list estimate.csv" in err
    assert "Traceback" not in err


def test_manifest_without_the_bills_file_is_a_usage_error_naming_it(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def to_estimate_only(payload: dict[str, Any]) -> None:
        del payload["outputs"]["bills.csv"]
        del payload["counts"]["bills_count"]

    _edit_manifest(output, to_estimate_only)
    code, out, err = _verify(output, capsys)
    assert code == 2
    assert out == ""
    assert "'outputs' does not list bills.csv" in err
    assert "Traceback" not in err


def test_changed_bills_file_is_a_hash_mismatch(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = output / "bills.csv"
    path.write_bytes(path.read_bytes() + b"9,2025-10-01,,Extra,gas,1.00,AUD,\r\n")
    code, out, _ = _verify(output, capsys)
    assert code == 4
    assert "MISMATCH bills.csv" in out
    assert "(differs: sha256, rows)" in out


def test_bills_count_that_disagrees_with_the_file_fails(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_manifest(output, _set(("counts", "bills_count"), 4))
    code, out, _ = _verify(output, capsys)
    assert code == 4
    assert "CHECK FAILED counts.bills_count: manifest says 4, bills.csv has 0 data rows" in out


def test_changed_estimate_is_a_hash_mismatch(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = output / "estimate.csv"
    path.write_bytes(path.read_bytes().replace(b"100.00", b"100.01"))
    code, out, _ = _verify(output, capsys)
    assert code == 4
    assert "MISMATCH estimate.csv" in out
    assert "(differs: sha256)" in out


def test_manifest_with_the_old_count_key_is_a_usage_error_naming_the_new_key(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def rename_count(payload: dict[str, Any]) -> None:
        payload["counts"]["ibkr_count"] = payload["counts"].pop("investments_count")

    _edit_manifest(output, rename_count)
    code, out, err = _verify(output, capsys)
    assert code == 2
    assert out == ""
    assert "'counts.investments_count' must be a non-negative integer" in err


@pytest.mark.parametrize(
    "name",
    [
        "..",
        ".",
        "/etc/passwd",
        "C:foo",
        "a" + chr(0) + "b",
        "dir/x.csv",
        r"dir\x.csv",
        "x.csv:stream",
        "review.csv.",
        "review.csv..",
        "review.csv ",
        " review.csv",
    ],
)
def test_unsafe_listed_names_are_a_usage_error(
    name: str, output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_manifest(output, _set(("outputs", name), {"sha256": "0" * 64, "rows": 0}))
    code, out, err = _verify(output, capsys)
    assert code == 2
    assert out == ""
    assert "not a file name" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("name", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "NUL.txt"])
def test_reserved_device_names_never_read_as_ok(
    name: str, output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = hashlib.sha256(b"").hexdigest()
    _edit_manifest(output, _set(("outputs", name), {"sha256": empty, "rows": 0}))
    code, out, err = _verify(output, capsys)
    assert code == 4
    assert f"OK {name}" not in out
    assert any(
        line.startswith((f"MISSING {name}", f"UNREADABLE {name}")) for line in out.splitlines()
    )
    assert "Traceback" not in out + err


@pytest.mark.parametrize(
    "name",
    [
        "review.csv::$DATA",
        "\treview.csv",
        "review.csv\t",
        "\nreview.csv",
        "review.csv\n",
        "\u00a0review.csv",
        "review.csv\u00a0",
    ],
    ids=[
        "ads",
        "leading-tab",
        "trailing-tab",
        "leading-newline",
        "trailing-newline",
        "leading-nbsp",
        "trailing-nbsp",
    ],
)
def test_padded_or_stream_listed_names_are_a_usage_error(
    name: str, output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_manifest(output, _set(("outputs", name), {"sha256": "0" * 64, "rows": 0}))
    code, out, err = _verify(output, capsys)
    assert code == 2
    assert out == ""
    assert "not a file name" in err
    assert "Traceback" not in err


def test_dangling_symlink_listed_as_a_csv_is_unreadable_not_a_traceback(
    output: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "review.csv").unlink()
    try:
        (output / "review.csv").symlink_to(tmp_path / "does-not-exist.csv")
    except OSError, NotImplementedError:
        pytest.skip("this OS refuses to create symlinks without privileges")
    code, out, err = _verify(output, capsys)
    assert code == 4
    assert "UNREADABLE review.csv" in out
    assert "Traceback" not in out + err


def test_directory_named_like_a_csv_is_unreadable_not_ok(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (output / "review.csv").unlink()
    (output / "review.csv").mkdir()
    code, out, _ = _verify(output, capsys)
    assert code == 4
    assert "UNREADABLE review.csv: is not a regular file" in out


def test_uppercase_hex_in_the_manifest_is_accepted_when_it_matches(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def edit(payload: dict[str, Any]) -> None:
        entry = payload["outputs"]["review.csv"]
        entry["sha256"] = entry["sha256"].upper()

    _edit_manifest(output, edit)
    code, out, _ = _verify(output, capsys)
    assert code == 0
    assert "OK review.csv" in out


def test_symlinked_listed_file_is_a_failure(
    output: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "elsewhere.csv"
    target.write_bytes((output / "review.csv").read_bytes())
    (output / "review.csv").unlink()
    try:
        (output / "review.csv").symlink_to(target)
    except OSError, NotImplementedError:
        pytest.skip("this OS refuses to create symlinks without privileges")
    code, out, _ = _verify(output, capsys)
    assert code == 4
    assert "UNREADABLE review.csv: is a symbolic link" in out


def test_unlistable_directory_is_a_note_not_a_traceback(
    output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(_directory: Path) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", refuse)
    code, out, err = _verify(output, capsys)
    assert code == 0
    assert "note: could not list the directory: Permission denied" in out
    assert "Traceback" not in err


def test_manifest_that_is_not_an_object_is_a_usage_error(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_manifest(output, [1, 2])
    code, _, err = _verify(output, capsys)
    assert code == 2
    assert "must hold a JSON object" in err


# --- pure functions ----------------------------------------------------------


def test_quoted_multi_line_cell_counts_as_one_row() -> None:
    content = BOM + b'id,title\r\n1,"two\r\nlines"\r\n2,plain\r\n'
    assert content.count(b"\r\n") == 4
    assert count_data_rows(content) == 2


def test_multi_line_cell_directory_verifies_with_the_csv_row_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deductions = BOM + b'id,title\r\n1,"two\nlines"\r\n'
    directory = tmp_path / "crafted"
    _craft_directory(
        directory,
        {
            "deductions.csv": deductions,
            "review.csv": BOM + HEADER,
            "investments.csv": BOM + HEADER,
            "bills.csv": BOM + HEADER,
            "estimate.csv": BOM + HEADER,
        },
        _counts(accepted=1, review=0, investments=0),
    )
    code, out, _ = _verify(directory, capsys)
    assert code == 0
    assert "OK deductions.csv" in out


def test_header_only_and_empty_files_have_zero_rows() -> None:
    assert count_data_rows(BOM + HEADER) == 0
    assert count_data_rows(b"") == 0


def test_bytes_that_are_not_utf8_are_not_countable() -> None:
    with pytest.raises(ValueError, match="UTF-8 CSV"):
        count_data_rows(b"id\r\n\xff\xfe\r\n")


def test_check_file_reports_unreadable_rows_as_a_row_mismatch() -> None:
    content = b"id\r\n\xff\r\n"
    expected = ExpectedFile(sha256=hashlib.sha256(content).hexdigest(), rows=1)
    result = check_file("x.csv", expected, content)
    assert not result.ok
    assert result.rows is None
    assert result.line.endswith("rows unreadable (differs: rows)")


def test_check_file_ok_when_hash_and_rows_match() -> None:
    content = BOM + HEADER + b"1,a\r\n"
    expected = ExpectedFile(sha256=hashlib.sha256(content).hexdigest(), rows=1)
    result = check_file("x.csv", expected, content)
    assert result.ok
    assert result.line == "OK x.csv"
    assert result.rows == 1


def test_check_consistency_skips_files_without_a_measured_row_count() -> None:
    counts = ManifestCounts(
        tagged_total=2,
        out_of_fy=0,
        selected_in_fy=2,
        accepted=1,
        review=1,
        investments_count=0,
        bills_count=0,
    )
    assert check_consistency(counts, {}) == ()
    assert check_consistency(counts, {"deductions.csv": 1, "review.csv": 1}) == ()
