"""Tests for the ``download`` command: verify gate, integrity, names and the zip."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from conftest import (
    ChunkedStream,
    document_payload,
    json_response,
    make_client,
    streamed_response,
)
from test_manifest import IBKR_ID, _documents, _install, make_handler
from test_verify import _counts, _craft_directory

from paperless_tax_export.cli import main
from paperless_tax_export.client import (
    DOWNLOAD_CONNECT_TIMEOUT_SECONDS,
    DOWNLOAD_TIMEOUT_SECONDS,
    parse_content_disposition_filename,
)
from paperless_tax_export.download import (
    DownloadPlan,
    derive_extension,
    execute_download,
    prepare_download,
    sanitise_name,
    write_atomically,
)
from paperless_tax_export.errors import (
    DownloadError,
    HttpStatusError,
    TaxExportAuthError,
    TransportError,
    VerificationError,
)
from paperless_tax_export.export import run_export
from paperless_tax_export.settings import Settings
from paperless_tax_export.verify import VerifyReport, verify_directory

SECRET = "tok-9f3a7c1d-DISTINCTIVE"
PDF = {
    "Content-Disposition": 'attachment; filename="receipt.PDF"',
    "Content-Type": "application/pdf",
}
SAFE_ARCNAME = re.compile(r"^(deductions|investments)/[0-9]+_[A-Za-z0-9._-]+\.[a-z0-9]+$")


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("PAPERLESS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the temp directory so leftovers can be seen."""
    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(directory))
    return directory


@dataclass
class Served:
    """What the fake Paperless serves for one document id."""

    body: bytes
    checksum: str | None = None
    original_file_name: str | None = None
    headers: dict[str, str] = field(default_factory=lambda: dict(PDF))
    status: int = 200
    metadata_status: int = 200


def md5(body: bytes) -> str:
    return hashlib.md5(body, usedforsecurity=False).hexdigest()


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class FakePaperless:
    """A GET-only fake of the metadata and download endpoints that records every request."""

    def __init__(self, served: dict[int, Served]) -> None:
        self.served = served
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        match = re.fullmatch(r"/api/documents/(\d+)/(download/)?", request.url.path)
        if match is None or int(match.group(1)) not in self.served:
            return json_response({}, status_code=404)
        spec = self.served[int(match.group(1))]
        if match.group(2) is None:
            if spec.metadata_status != 200:
                return json_response({}, status_code=spec.metadata_status)
            return json_response(
                {
                    "id": int(match.group(1)),
                    "checksum": spec.checksum,
                    "original_file_name": spec.original_file_name,
                }
            )
        assert request.url.params["original"] == "true"
        if spec.status != 200:
            return httpx.Response(spec.status)
        return streamed_response(spec.body, headers=spec.headers, chunk_size=7)

    def downloads_of(self, document_id: int) -> int:
        return sum(
            1 for r in self.requests if r.url.path == f"/api/documents/{document_id}/download/"
        )


RECEIPT_1 = b"%PDF-1.7 receipt one " * 20
RECEIPT_4 = b"%PDF-1.7 statement four " * 30


def default_served() -> dict[int, Served]:
    return {
        1: Served(RECEIPT_1, checksum=md5(RECEIPT_1)),
        4: Served(RECEIPT_4, checksum=sha256(RECEIPT_4)),
    }


@pytest.fixture
def output(tmp_path: Path) -> Path:
    """A real run output: id 1 in deductions, id 4 in investments."""
    directory = tmp_path / "out"
    run_export(Settings(), make_client(make_handler()), output=directory)
    return directory


def _token(tmp_path: Path, content: str = "test-token") -> Path:
    path = tmp_path / "token.txt"
    path.write_text(content, encoding="utf-8")
    return path


def _download(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakePaperless,
    *extra: str,
) -> int:
    _install(monkeypatch, fake)
    return main(
        ["download", "--output", str(output), "--token-file", str(_token(tmp_path)), *extra]
    )


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# --- happy path --------------------------------------------------------------


def test_happy_path_writes_exactly_the_designed_zip(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakePaperless(default_served())
    assert _download(output, tmp_path, monkeypatch, fake) == 0

    pack = output / "tax-pack.zip"
    with zipfile.ZipFile(pack) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        for info in archive.infolist():
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 3
            assert info.external_attr == 0o644 << 16
            assert info.extra == b""
            assert info.comment == b""
        assert archive.comment == b""
    assert names == [
        "deductions.csv",
        "deductions/1_2025-10-01_Alpha_Supplies.pdf",
        "downloads.json",
        "investments.csv",
        "investments/4_2025-10-01_Interactive_Brokers.pdf",
    ]
    contents = _read_zip(pack)
    assert contents["deductions.csv"] == (output / "deductions.csv").read_bytes()
    assert contents["investments.csv"] == (output / "investments.csv").read_bytes()
    assert contents["deductions/1_2025-10-01_Alpha_Supplies.pdf"] == RECEIPT_1
    assert contents["investments/4_2025-10-01_Interactive_Brokers.pdf"] == RECEIPT_4

    downloads = json.loads(contents["downloads.json"])
    assert [(e["set"], e["document_id"]) for e in downloads] == [
        ("deductions", 1),
        ("investments", 4),
    ]
    for entry in downloads:
        assert sha256(contents[entry["arcname"]]) == entry["sha256"]
        assert entry["bytes"] == len(contents[entry["arcname"]])
        assert entry["checksum_result"] == "match"
    assert contents["downloads.json"].endswith(b"\n")
    assert b"\r" not in contents["downloads.json"]

    out = capsys.readouterr().out.splitlines()
    assert "deductions: 1 receipts" in out
    assert "investments: 1 receipts" in out
    assert f"tax-pack.zip written to {pack} (2 receipts)" in out
    assert f"sha256 {sha256(pack.read_bytes())}" in out
    assert {r.method for r in fake.requests} == {"GET"}


def test_two_runs_give_byte_identical_zips(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _download(output, tmp_path, monkeypatch, FakePaperless(default_served())) == 0
    first = (output / "tax-pack.zip").read_bytes()
    assert _download(output, tmp_path, monkeypatch, FakePaperless(default_served())) == 0
    assert (output / "tax-pack.zip").read_bytes() == first


def test_a_successful_run_replaces_an_existing_zip(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (output / "tax-pack.zip").write_bytes(b"old")
    assert _download(output, tmp_path, monkeypatch, FakePaperless(default_served())) == 0
    assert zipfile.is_zipfile(output / "tax-pack.zip")
    assert not [name for name in _names(output) if name.startswith(".tax-pack-")]


# --- verify gate -------------------------------------------------------------


def test_tampered_csv_exits_4_with_no_request_and_no_zip(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _edit_valid_row(output / "deductions.csv", b"Alpha Supplies", b"Alpha Supplier")
    fake = FakePaperless(default_served())
    assert _download(output, tmp_path, monkeypatch, fake) == 4
    assert "verification problem" in capsys.readouterr().err
    assert fake.requests == []
    assert not (output / "tax-pack.zip").exists()


def _edit_valid_row(path: Path, old: bytes, new: bytes) -> None:
    """Change a cell so the CSV still parses and keeps its ids; only the hash differs."""
    content = path.read_bytes()
    assert old in content
    path.write_bytes(content.replace(old, new, 1))


def test_csv_changed_after_the_gate_is_refused(
    output: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = output / "deductions.csv"
    real_verify = verify_directory
    real_read = Path.read_bytes

    def swapped(path: Path) -> bytes:
        content = real_read(path)
        if path == original:
            return content.replace(b"Alpha Supplies", b"Alpha Supplier")
        return content

    def verify_then_swap(directory: Path) -> VerifyReport:
        report = real_verify(directory)
        monkeypatch.setattr(Path, "read_bytes", swapped)
        return report

    monkeypatch.setattr("paperless_tax_export.download.verify_directory", verify_then_swap)
    with pytest.raises(VerificationError, match="changed after the verify check"):
        prepare_download(output)


def test_unusable_manifest_exits_2_with_no_request(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (output / "manifest.json").write_text("{", encoding="utf-8")
    fake = FakePaperless(default_served())
    assert _download(output, tmp_path, monkeypatch, fake) == 2
    assert fake.requests == []


def test_missing_manifest_exits_2_with_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    fake = FakePaperless(default_served())
    assert _download(empty, tmp_path, monkeypatch, fake) == 2
    assert fake.requests == []


# --- all or nothing ----------------------------------------------------------


@pytest.mark.parametrize(("status", "code"), [(404, 3), (403, 3), (500, 3)])
def test_one_failed_document_writes_nothing_and_keeps_an_existing_zip(
    status: int,
    code: int,
    output: Path,
    tmp_path: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    served[4].status = status
    (output / "tax-pack.zip").write_bytes(b"previous pack")
    before = _names(output)
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == code
    err = capsys.readouterr().err
    assert "document 4" in err
    assert f"HTTP {status}" in err
    assert "Receipt" not in err
    assert (output / "tax-pack.zip").read_bytes() == b"previous pack"
    assert _names(output) == before
    assert list(scratch.iterdir()) == []


def test_a_failed_first_run_leaves_no_zip_and_no_temp_file(
    output: Path, tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = default_served()
    served[4].metadata_status = 500
    before = _names(output)
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 3
    assert _names(output) == before
    assert list(scratch.iterdir()) == []


def test_a_timeout_is_a_network_error(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _install(monkeypatch, handler)
    code = main(["download", "--output", str(output), "--token-file", str(_token(tmp_path))])
    assert code == 3
    assert not (output / "tax-pack.zip").exists()


# --- integrity ---------------------------------------------------------------


@pytest.mark.parametrize("stored", [md5(b"other"), sha256(b"other")])
def test_checksum_mismatch_fails_that_document(
    stored: str,
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    served[1].checksum = stored
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 4
    assert "document 1: the download does not match its checksum" in capsys.readouterr().err
    assert not (output / "tax-pack.zip").exists()


def test_an_empty_download_always_fails(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    served[1] = Served(b"", checksum=None)
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 4
    assert "document 1: the download was empty" in capsys.readouterr().err
    assert not (output / "tax-pack.zip").exists()


def test_a_content_length_that_disagrees_with_the_body_fails(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = default_served()
    served[1].headers = {**PDF, "Content-Length": str(len(RECEIPT_1) + 5)}
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 4
    assert not (output / "tax-pack.zip").exists()


@pytest.mark.parametrize("stored", [None, "", "abc123", "z" * 32])
def test_a_missing_or_unknown_checksum_is_recorded_as_unchecked(
    stored: str | None, output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = default_served()
    served[1].checksum = stored
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 0
    downloads = json.loads(_read_zip(output / "tax-pack.zip")["downloads.json"])
    assert {e["document_id"]: e["checksum_result"] for e in downloads} == {
        1: "unchecked",
        4: "match",
    }


# --- overlap -----------------------------------------------------------------


def test_a_document_in_both_sets_warns_once_and_lands_in_both_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    documents = [
        *_documents(),
        document_payload(
            5,
            title="Both",
            correspondent=IBKR_ID,
            custom_fields=[{"field": 10, "value": "20.00"}],
        ),
    ]
    directory = tmp_path / "both"
    run_export(Settings(), make_client(make_handler(documents)), output=directory)
    body = b"%PDF both"
    served = {**default_served(), 5: Served(body, checksum=md5(body))}
    fake = FakePaperless(served)
    assert _download(directory, tmp_path, monkeypatch, fake) == 0
    err = capsys.readouterr().err
    assert err.count("warning: document 5 is in both deductions.csv and investments.csv") == 1
    assert fake.downloads_of(5) == 1
    contents = _read_zip(directory / "tax-pack.zip")
    copies = [name for name in contents if re.match(r"(deductions|investments)/5_", name)]
    assert len(copies) == 2
    assert {contents[name] for name in copies} == {body}
    assert {name.split("/")[0] for name in copies} == {"deductions", "investments"}


# --- names -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alpha Supplies", "Alpha_Supplies"),
        ("../x", "x"),
        ("a/b", "a_b"),
        ("a\\b", "a_b"),
        ("CON", "CON"),
        ("..", "unknown"),
        ("", "unknown"),
        ("日本語", "unknown"),
        ("café", "caf"),
        ("a..b", "a.b"),
        ("__a___b__", "a_b"),
        ("C:\\x", "C_x"),
        ("x" * 500, "x" * 60),
        ("a" * 59 + "!b", "a" * 59),
    ],
)
def test_sanitise_name(raw: str, expected: str) -> None:
    assert sanitise_name(raw, limit=60) == expected


def test_hostile_names_produce_safe_unique_entries(tmp_path: Path) -> None:
    names = ["../x", "a/b", "CON", "日本語", "x" * 500, "", "..\\..\\evil", "C:\\boot"]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["paperless_document_id", "date", "supplier_name"])
    for number, name in enumerate(names, start=10):
        writer.writerow([number, "2025-10-01", name])
    directory = tmp_path / "hostile"
    _craft_directory(
        directory,
        {
            "deductions.csv": buffer.getvalue().encode("utf-8-sig"),
            "review.csv": b"id\r\n",
            "investments.csv": b"paperless_document_id,created,correspondent\r\n",
            "bills.csv": b"paperless_document_id\r\n",
            "estimate.csv": b"section\r\n",
        },
        _counts(len(names), 0, 0),
    )
    plan = prepare_download(directory)
    served = {10 + i: Served(f"%PDF {i}".encode()) for i in range(len(names))}
    result = execute_download(plan, make_client(FakePaperless(served)))
    arcnames = [entry.arcname for entry in result.entries]
    assert len(set(arcnames)) == len(names)
    for arcname in arcnames:
        assert SAFE_ARCNAME.match(arcname), arcname
        assert ".." not in arcname
        assert len(arcname.split("/")[1]) <= len("12_2025-10-01_") + 60 + len(".pdf")
    assert _names(directory).count("tax-pack.zip") == 1


def test_duplicate_ids_in_one_csv_fail_before_any_request(tmp_path: Path) -> None:
    directory = tmp_path / "dup"
    _craft_directory(
        directory,
        {
            "deductions.csv": (
                b"paperless_document_id,date,supplier_name\r\n1,2025-10-01,A\r\n1,2025-10-02,B\r\n"
            ),
            "review.csv": b"id\r\n",
            "investments.csv": b"paperless_document_id,created,correspondent\r\n",
            "bills.csv": b"paperless_document_id\r\n",
            "estimate.csv": b"section\r\n",
        },
        _counts(2, 0, 0),
    )
    with pytest.raises(DownloadError, match="document 1 more than once"):
        prepare_download(directory)


# --- extensions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("disposition", "original", "content_type", "expected"),
    [
        ("scan.PDF", None, None, ".pdf"),
        ("scan.PDF", "other.png", "image/jpeg", ".pdf"),
        (None, "photo.JPEG", "application/pdf", ".jpeg"),
        (None, "noext", "application/pdf", ".pdf"),
        (None, None, "image/jpeg; charset=binary", ".jpg"),
        (None, None, "image/png", ".png"),
        (None, None, "message/rfc822", ".eml"),
        (None, None, "text/plain", ".txt"),
        (None, None, "application/octet-stream", None),
        (None, None, None, None),
        ("a/b/c.tar.gz", None, None, ".gz"),
        ("weird.p/d f", None, None, None),
        ("x.p d$f", None, None, ".pdf"),
        ("x." + "a" * 40, None, None, "." + "a" * 10),
        ("x.é", None, None, None),
    ],
)
def test_derive_extension(
    disposition: str | None, original: str | None, content_type: str | None, expected: str | None
) -> None:
    assert derive_extension(disposition, original, content_type) == expected


def test_an_unknown_type_uses_bin_and_warns(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    served[1].headers = {"Content-Type": "application/octet-stream"}
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 0
    assert "warning: document 1: no file type found, using .bin" in capsys.readouterr().err
    assert "deductions/1_2025-10-01_Alpha_Supplies.bin" in _read_zip(output / "tax-pack.zip")


def test_the_stored_original_name_supplies_the_extension(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = default_served()
    served[1].headers = {}
    served[1].original_file_name = "Scan 001.JPG"
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 0
    assert "deductions/1_2025-10-01_Alpha_Supplies.jpg" in _read_zip(output / "tax-pack.zip")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("attachment; filename*=UTF-8''na%C3%AFve%20scan.pdf", "naïve scan.pdf"),
        ('attachment; filename="a.pdf"', "a.pdf"),
        ("attachment; filename=a.pdf", "a.pdf"),
        ("attachment; filename=\"a.pdf\"; filename*=UTF-8''b.png", "b.png"),
        ('attachment; filename="a \\"q\\".pdf"', 'a "q".pdf'),
        ("attachment; filename*=bogus-charset''x.pdf", "x.pdf"),
        ("attachment", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_content_disposition_filename(header: str | None, expected: str | None) -> None:
    assert parse_content_disposition_filename(header) == expected


# --- dry run -----------------------------------------------------------------


def test_dry_run_makes_no_request_and_creates_nothing(
    output: Path,
    tmp_path: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakePaperless(default_served())
    _install(monkeypatch, fake)
    before = _names(output)
    assert main(["download", "--output", str(output), "--dry-run"]) == 0
    assert fake.requests == []
    assert _names(output) == before
    assert list(scratch.iterdir()) == []
    assert not (tmp_path / "secrets").exists()
    out = capsys.readouterr().out.splitlines()
    assert "deductions 1 deductions/1_2025-10-01_Alpha_Supplies" in out
    assert "investments 4 investments/4_2025-10-01_Interactive_Brokers" in out
    assert "deductions: 1 receipts" in out
    assert out[-1].startswith("dry run: 2 documents")


def test_dry_run_still_runs_the_verify_gate(
    output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_valid_row(output / "investments.csv", b"Interactive Brokers", b"Interactive Brokerz")
    assert main(["download", "--output", str(output), "--dry-run"]) == 4
    assert "verification problem" in capsys.readouterr().err


# --- secrets -----------------------------------------------------------------


@pytest.mark.parametrize("fail", [False, True])
def test_the_token_never_appears_in_output_or_files(
    fail: bool,
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    if fail:
        served[4].status = 403
    _install(monkeypatch, FakePaperless(served))
    monkeypatch.setenv("PAPERLESS_TOKEN", SECRET)
    code = main(["download", "--output", str(output)])
    captured = capsys.readouterr()
    assert code == (3 if fail else 0)
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    for path in output.iterdir():
        assert SECRET.encode() not in path.read_bytes(), path.name


def test_the_token_is_sent_only_as_a_header(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakePaperless(default_served())
    _install(monkeypatch, fake)
    monkeypatch.setenv("PAPERLESS_TOKEN", SECRET)
    assert main(["download", "--output", str(output)]) == 0
    for request in fake.requests:
        assert request.headers["Authorization"] == f"Token {SECRET}"
        assert SECRET not in str(request.url)


# --- client ------------------------------------------------------------------


def test_a_large_body_is_streamed_in_chunks_and_never_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = bytes(range(256)) * 400  # 102,400 bytes
    stream = ChunkedStream(body, chunk_size=10_240)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"Content-Length": str(len(body))}, stream=stream)

    def forbidden(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("the response body was read whole")

    monkeypatch.setattr(httpx.Response, "read", forbidden)
    destination = tmp_path / "dest.bin"
    result = make_client(handler).download_original(7, destination)
    assert stream.chunks_served == 10
    assert destination.read_bytes() == body
    assert result.size == len(body)
    assert result.sha256 == sha256(body)
    assert result.md5 == md5(body)
    assert result.content_length == len(body)
    assert seen[0].extensions["timeout"]["read"] == DOWNLOAD_TIMEOUT_SECONDS == 300.0
    assert seen[0].extensions["timeout"]["connect"] == DOWNLOAD_CONNECT_TIMEOUT_SECONDS == 30.0
    assert DOWNLOAD_TIMEOUT_SECONDS > 30.0
    assert seen[0].headers["Accept-Encoding"] == "identity"


def test_the_download_client_reports_headers_and_tolerates_their_absence(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abc")

    result = make_client(handler).download_original(7, tmp_path / "d")
    assert result.file_name is None
    assert result.content_type is None
    assert result.content_length == 3


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, TaxExportAuthError),
        (403, TaxExportAuthError),
        (404, HttpStatusError),
        (503, HttpStatusError),
    ],
)
def test_download_statuses_map_to_client_errors_naming_the_document(
    status: int, error: type[Exception], tmp_path: Path
) -> None:
    client = make_client(lambda request: httpx.Response(status))
    with pytest.raises(error, match=rf"document 9: .*HTTP {status}"):
        client.download_original(9, tmp_path / "d")
    with pytest.raises(error, match=rf"document 9: .*HTTP {status}"):
        client.get_document_file_info(9)


def test_a_transport_failure_maps_to_a_transport_error_without_the_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom http://internal.example/secret", request=request)

    client = make_client(handler)
    with pytest.raises(TransportError) as info:
        client.download_original(9, tmp_path / "d")
    assert "document 9" in str(info.value)
    assert "internal.example" not in str(info.value)


def test_the_metadata_request_asks_for_the_checksum_and_original_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response({"id": 3, "checksum": "ab", "original_file_name": "x.pdf"})

    info = make_client(handler).get_document_file_info(3)
    assert (info.checksum, info.original_file_name) == ("ab", "x.pdf")
    assert seen[0].url.path == "/api/documents/3/"
    assert seen[0].url.params["fields"] == "id,checksum,original_file_name"


@pytest.mark.parametrize("payload", [{}, {"checksum": 5, "original_file_name": ["x"]}])
def test_odd_metadata_gives_none_not_an_error(payload: dict[str, object]) -> None:
    info = make_client(lambda request: json_response(payload)).get_document_file_info(3)
    assert (info.checksum, info.original_file_name) == (None, None)


@pytest.mark.parametrize("response", [httpx.Response(200, content=b"nope"), json_response([1])])
def test_unusable_metadata_is_a_download_error(response: httpx.Response) -> None:
    with pytest.raises(DownloadError, match="document 3"):
        make_client(lambda request: response).get_document_file_info(3)


# --- atomic write ------------------------------------------------------------


def _write_new(path: Path) -> None:
    path.write_bytes(b"new")


def test_write_atomically_replaces_the_target_and_removes_the_temp(tmp_path: Path) -> None:
    target = tmp_path / "pack.zip"
    target.write_bytes(b"old")
    digest = write_atomically(target, _write_new)
    assert target.read_bytes() == b"new"
    assert digest == sha256(b"new")
    assert _names(tmp_path) == ["pack.zip"]


def test_write_atomically_failure_keeps_the_target_and_removes_the_temp(tmp_path: Path) -> None:
    target = tmp_path / "pack.zip"
    target.write_bytes(b"old")

    def broken(path: Path) -> None:
        path.write_bytes(b"half")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        write_atomically(target, broken)
    assert target.read_bytes() == b"old"
    assert _names(tmp_path) == ["pack.zip"]


def test_a_failed_replace_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "pack.zip"

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr("os.replace", refuse)
    with pytest.raises(PermissionError):
        write_atomically(target, _write_new)
    assert _names(tmp_path) == []


def test_plan_exposes_unique_ids_for_an_overlap(tmp_path: Path) -> None:
    plan: DownloadPlan = prepare_download(_overlap_directory(tmp_path))
    assert plan.unique_ids == (5,)
    assert len(plan.documents) == 2


def _overlap_directory(tmp_path: Path) -> Path:
    documents: list[dict[str, object]] = [
        document_payload(5, correspondent=IBKR_ID, custom_fields=[{"field": 10, "value": "20.00"}])
    ]
    directory = tmp_path / "overlap"
    run_export(Settings(), make_client(make_handler(documents)), output=directory)
    return directory


# --- unchecked downloads -----------------------------------------------------


def test_the_checksum_line_counts_matches_and_unchecked(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _download(output, tmp_path, monkeypatch, FakePaperless(default_served())) == 0
    captured = capsys.readouterr()
    assert "checksum: 2 match, 0 unchecked" in captured.out.splitlines()
    assert "not checked" not in captured.err


def test_unchecked_documents_are_counted_and_warned_about_by_id(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    served[1].checksum = None
    assert _download(output, tmp_path, monkeypatch, FakePaperless(served)) == 0
    captured = capsys.readouterr()
    assert "checksum: 1 match, 1 unchecked" in captured.out.splitlines()
    assert "warning: 1 document(s) were not checked against a Paperless checksum" in captured.err
    assert captured.err.strip().endswith(": 1")
    assert (output / "tax-pack.zip").exists()


def test_require_checksum_fails_on_an_unchecked_document_and_writes_no_zip(
    output: Path,
    tmp_path: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    served[4].checksum = ""
    (output / "tax-pack.zip").write_bytes(b"previous pack")
    before = _names(output)
    code = _download(output, tmp_path, monkeypatch, FakePaperless(served), "--require-checksum")
    assert code == 4
    err = capsys.readouterr().err
    assert "no usable Paperless checksum" in err
    assert "1 document(s): 4" in err
    assert "Interactive" not in err
    assert (output / "tax-pack.zip").read_bytes() == b"previous pack"
    assert _names(output) == before
    assert list(scratch.iterdir()) == []


def test_require_checksum_succeeds_when_every_checksum_matches(
    output: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _download(
        output, tmp_path, monkeypatch, FakePaperless(default_served()), "--require-checksum"
    )
    assert code == 0
    assert zipfile.is_zipfile(output / "tax-pack.zip")


# --- interruption and cleanup ------------------------------------------------


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_write_atomically_cleans_up_after_any_exception(
    failure: type[BaseException], tmp_path: Path
) -> None:
    target = tmp_path / "pack.zip"
    target.write_bytes(b"old")

    def broken(path: Path) -> None:
        path.write_bytes(b"half")
        raise failure("stop")

    with pytest.raises(failure):
        write_atomically(target, broken)
    assert target.read_bytes() == b"old"
    assert _names(tmp_path) == ["pack.zip"]


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_a_zip_that_fails_part_way_leaves_nothing_behind(
    failure: type[BaseException],
    output: Path,
    tmp_path: Path,
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(path: Path, *, members: object) -> None:
        path.write_bytes(b"PK partial")
        raise failure("stop")

    monkeypatch.setattr("paperless_tax_export.download._write_zip", broken)
    (output / "tax-pack.zip").write_bytes(b"previous pack")
    before = _names(output)
    _install(monkeypatch, FakePaperless(default_served()))
    argv = ["download", "--output", str(output), "--token-file", str(_token(tmp_path))]
    with pytest.raises(failure):
        main(argv)
    assert (output / "tax-pack.zip").read_bytes() == b"previous pack"
    assert _names(output) == before
    assert list(scratch.iterdir()) == []


# --- limits and warnings -----------------------------------------------------


def _craft_deductions(tmp_path: Path, name: str, rows: list[list[object]]) -> Path:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["paperless_document_id", "date", "supplier_name"])
    writer.writerows(rows)
    directory = tmp_path / name
    _craft_directory(
        directory,
        {
            "deductions.csv": buffer.getvalue().encode("utf-8-sig"),
            "review.csv": b"id\r\n",
            "investments.csv": b"paperless_document_id,created,correspondent\r\n",
            "bills.csv": b"paperless_document_id\r\n",
            "estimate.csv": b"section\r\n",
        },
        _counts(len(rows), 0, 0),
    )
    return directory


def test_the_date_fragment_is_stripped_and_capped(tmp_path: Path) -> None:
    directory = _craft_deductions(tmp_path, "dates", [[1, "../../x", "A"], [2, "9" * 100, "A"]])
    stems = [document.stem for document in prepare_download(directory).documents]
    assert stems == ["deductions/1_x_A", "deductions/2_" + "9" * 20 + "_A"]


@pytest.mark.parametrize("raw_id", ["9" * 19, "9" * 5000, "-1", "1.5", "0x10"])
def test_an_oversized_or_malformed_id_is_a_download_error(tmp_path: Path, raw_id: str) -> None:
    directory = _craft_deductions(tmp_path, "ids", [[raw_id, "2025-10-01", "A"]])
    with pytest.raises(DownloadError, match="not a document id"):
        prepare_download(directory)


def test_an_empty_pack_warns_on_stderr_and_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _craft_deductions(tmp_path, "empty-pack", [])
    fake = FakePaperless({})
    assert _download(directory, tmp_path, monkeypatch, fake) == 0
    assert "list no documents" in capsys.readouterr().err
    assert fake.requests == []
    assert sorted(_read_zip(directory / "tax-pack.zip")) == [
        "deductions.csv",
        "downloads.json",
        "investments.csv",
    ]


# --- nits: every unchecked id, id forms, overlap ------------------------------


def _many_documents(tmp_path: Path, count: int) -> tuple[Path, dict[int, Served]]:
    ids = range(100, 100 + count)
    directory = _craft_deductions(tmp_path, "many", [[i, "2025-10-01", "A"] for i in ids])
    return directory, {i: Served(b"%PDF", checksum=None) for i in ids}


def test_require_checksum_reports_every_unchecked_id_once_capped_at_twenty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory, served = _many_documents(tmp_path, 25)
    fake = FakePaperless(served)
    assert _download(directory, tmp_path, monkeypatch, fake, "--require-checksum") == 4
    err = capsys.readouterr().err
    assert err.count("error:") == 1
    assert "25 document(s)" in err
    assert ", ".join(str(i) for i in range(100, 120)) + " and 5 more" in err
    assert "120" not in err
    assert fake.downloads_of(124) == 1
    assert not (directory / "tax-pack.zip").exists()


def test_the_unchecked_warning_caps_its_ids_but_downloads_json_keeps_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory, served = _many_documents(tmp_path, 25)
    assert _download(directory, tmp_path, monkeypatch, FakePaperless(served)) == 0
    captured = capsys.readouterr()
    assert "checksum: 0 match, 25 unchecked" in captured.out.splitlines()
    assert "25 document(s) were not checked" in captured.err
    assert "and 5 more" in captured.err
    assert "124" not in captured.err
    downloads = json.loads(_read_zip(directory / "tax-pack.zip")["downloads.json"])
    assert len(downloads) == 25
    assert {e["checksum_result"] for e in downloads} == {"unchecked"}


def test_a_checksum_mismatch_fails_with_or_without_require_checksum(
    output: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = default_served()
    served[1].checksum = md5(b"other")
    code = _download(output, tmp_path, monkeypatch, FakePaperless(served), "--require-checksum")
    assert code == 4
    assert "document 1: the download does not match its checksum" in capsys.readouterr().err
    assert not (output / "tax-pack.zip").exists()


def test_require_checksum_is_ignored_by_dry_run(
    output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePaperless(default_served())
    _install(monkeypatch, fake)
    argv = ["download", "--output", str(output), "--dry-run", "--require-checksum"]
    assert main(argv) == 0
    assert fake.requests == []
    assert "dry run: 2 documents" in capsys.readouterr().out


def test_the_empty_pack_warning_also_shows_in_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _craft_deductions(tmp_path, "empty-dry", [])
    assert main(["download", "--output", str(directory), "--dry-run"]) == 0
    assert "list no documents" in capsys.readouterr().err
    assert not (directory / "tax-pack.zip").exists()


def test_id_zero_and_an_eighteen_digit_id_are_accepted(tmp_path: Path) -> None:
    big = "9" * 18
    directory = _craft_deductions(
        tmp_path, "edge-ids", [["0", "2025-10-01", "A"], [big, "2025-10-01", "A"]]
    )
    ids = [document.document_id for document in prepare_download(directory).documents]
    assert ids == [0, int(big)]


def test_a_leading_zero_id_duplicates_the_plain_id(tmp_path: Path) -> None:
    directory = _craft_deductions(
        tmp_path, "zero-dup", [["01", "2025-10-01", "A"], ["1", "2025-10-02", "B"]]
    )
    with pytest.raises(DownloadError, match="document 1 more than once"):
        prepare_download(directory)


def test_non_ascii_digits_are_not_a_document_id(tmp_path: Path) -> None:
    directory = _craft_deductions(tmp_path, "arabic", [["١٢", "2025-10-01", "A"]])
    with pytest.raises(DownloadError, match="not a document id"):
        prepare_download(directory)


def test_the_same_unchecked_document_in_both_sets_is_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _overlap_directory(tmp_path)
    served = {5: Served(b"%PDF both", checksum=None)}
    assert _download(directory, tmp_path, monkeypatch, FakePaperless(served)) == 0
    captured = capsys.readouterr()
    assert "checksum: 0 match, 1 unchecked" in captured.out.splitlines()
    assert "1 document(s) were not checked" in captured.err
    assert captured.err.count("warning: 1 document(s)") == 1
