"""Pack the original files of a finished run into one deterministic zip.

``prepare_download`` gates on :func:`verify_directory` (so the ids come from the CSVs
exactly as the run wrote them), reads the ids and naming fields from
``deductions.csv`` and ``investments.csv`` and plans one entry per document and set.
``execute_download`` fetches each original once, checks it against Paperless's
stored ``checksum`` and writes ``tax-pack.zip`` with a temp file and ``os.replace``,
so a failed run leaves no zip and an existing one untouched.

The zip is not encrypted (``zipfile`` cannot write encrypted archives) and holds the
real receipts. Messages name document ids, never titles or contents.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath

from paperless_tax_export.client import (
    DocumentFileInfo,
    DownloadedOriginal,
    PaperlessClient,
)
from paperless_tax_export.errors import DownloadError, VerificationError
from paperless_tax_export.manifest import DEDUCTIONS_FILE, INVESTMENTS_FILE, MANIFEST_FILE
from paperless_tax_export.verify import parse_manifest, verify_directory

PACK_NAME = "tax-pack.zip"
DOWNLOADS_FILE = "downloads.json"
DEDUCTIONS_SET = "deductions"
INVESTMENTS_SET = "investments"

CHECKSUM_MATCH = "match"
CHECKSUM_UNCHECKED = "unchecked"

_MAX_NAME_CHARS = 60
_MAX_DATE_CHARS = 20
_MAX_ID_DIGITS = 18
_MAX_EXTENSION_CHARS = 10
_UNKNOWN = "unknown"
_FALLBACK_EXTENSION = ".bin"
_COPY_CHUNK_BYTES = 64 * 1024
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_CREATE_SYSTEM = 3
_ZIP_EXTERNAL_ATTR = 0o644 << 16

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")
_UNDERSCORE_RUN_RE = re.compile(r"_{2,}")
_DOT_RUN_RE = re.compile(r"\.{2,}")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(rf"^[0-9]{{1,{_MAX_ID_DIGITS}}}$")

_CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "message/rfc822": ".eml",
    "text/plain": ".txt",
}

# (set, CSV file, id column, date column, name column)
_SOURCES = (
    (DEDUCTIONS_SET, DEDUCTIONS_FILE, "paperless_document_id", "date", "supplier_name"),
    (INVESTMENTS_SET, INVESTMENTS_FILE, "paperless_document_id", "created", "correspondent"),
)


@dataclass(frozen=True, slots=True)
class PlannedDocument:
    """One zip entry to produce, from one CSV row (raw naming fields)."""

    set_name: str
    document_id: int
    date: str
    name: str

    @property
    def stem(self) -> str:
        """The archive path without its extension, e.g. ``deductions/12_2025-10-01_Acme``."""
        date = sanitise_name(self.date, limit=_MAX_DATE_CHARS)
        name = sanitise_name(self.name, limit=_MAX_NAME_CHARS)
        return f"{self.set_name}/{self.document_id}_{date}_{name}"


@dataclass(frozen=True, slots=True)
class DownloadPlan:
    """Everything known before the first request."""

    directory: Path
    documents: tuple[PlannedDocument, ...]
    warnings: tuple[str, ...]
    deductions_csv: bytes
    investments_csv: bytes

    @property
    def unique_ids(self) -> tuple[int, ...]:
        return tuple(sorted({document.document_id for document in self.documents}))


@dataclass(frozen=True, slots=True)
class PackEntry:
    """One receipt in the finished zip."""

    set_name: str
    document_id: int
    arcname: str
    size: int
    sha256: str
    checksum_result: str


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """What ``execute_download`` wrote."""

    pack_path: Path
    entries: tuple[PackEntry, ...]
    warnings: tuple[str, ...]
    zip_sha256: str

    @property
    def unchecked_ids(self) -> tuple[int, ...]:
        """Ids whose download had no usable stored checksum to compare with."""
        return tuple(
            sorted({e.document_id for e in self.entries if e.checksum_result == CHECKSUM_UNCHECKED})
        )

    @property
    def matched_count(self) -> int:
        """Distinct documents whose bytes matched the stored checksum."""
        return len({e.document_id for e in self.entries if e.checksum_result == CHECKSUM_MATCH})

    def count_for(self, set_name: str) -> int:
        return sum(1 for entry in self.entries if entry.set_name == set_name)


# --- names -------------------------------------------------------------------


def sanitise_name(value: str, *, limit: int | None = None) -> str:
    """A file-name-safe fragment: ``[A-Za-z0-9._-]`` only, never empty, no ``..``.

    Other characters become ``_``, runs of ``_`` and ``.`` collapse, leading and
    trailing ``_``, ``.`` and spaces go, then the text is cut to *limit* characters
    and stripped again. An empty result becomes ``unknown``.
    """
    text = _UNSAFE_CHARS_RE.sub("_", value)
    text = _DOT_RUN_RE.sub(".", _UNDERSCORE_RUN_RE.sub("_", text))
    text = text.strip("_. ")
    if limit is not None:
        text = text[:limit].strip("_. ")
    return text or _UNKNOWN


def _clean_extension(candidate: str) -> str | None:
    """``.pdf`` from a file name's suffix: lower-case letters and digits only, or ``None``."""
    base = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    suffix = os.path.splitext(base)[1].removeprefix(".")
    cleaned = "".join(char for char in suffix.lower() if char.isascii() and char.isalnum())
    return f".{cleaned[:_MAX_EXTENSION_CHARS]}" if cleaned else None


def derive_extension(
    disposition_name: str | None,
    original_file_name: str | None,
    content_type: str | None,
) -> str | None:
    """The extension from the response file name, the stored original name, then the type."""
    for name in (disposition_name, original_file_name):
        if name:
            extension = _clean_extension(name)
            if extension is not None:
                return extension
    if content_type:
        return _CONTENT_TYPE_EXTENSIONS.get(content_type.split(";", 1)[0].strip().lower())
    return None


# --- planning ----------------------------------------------------------------


def prepare_download(directory: Path) -> DownloadPlan:
    """Run the verify gate, then plan the entries. Makes no request and writes nothing.

    Raises ``ManifestError`` (exit 2) for an unusable manifest and
    ``VerificationError`` (exit 4) when a listed file no longer matches it.
    """
    report = verify_directory(directory)
    if not report.ok:
        raise VerificationError(
            f"{report.problems} verification problem(s) in {directory}; run verify for details"
        )
    expected = _manifest_hashes(directory)
    contents: dict[str, bytes] = {}
    documents: list[PlannedDocument] = []
    for set_name, file_name, id_column, date_column, name_column in _SOURCES:
        contents[file_name] = _read_csv_file(directory / file_name)
        if hashlib.sha256(contents[file_name]).hexdigest() != expected[file_name]:
            raise VerificationError(f"{file_name} changed after the verify check")
        documents.extend(
            _read_rows(
                contents[file_name], file_name, set_name, (id_column, date_column, name_column)
            )
        )
    documents.sort(key=lambda document: (document.set_name, document.document_id))
    return DownloadPlan(
        directory=directory,
        documents=tuple(documents),
        warnings=_plan_warnings(documents),
        deductions_csv=contents[DEDUCTIONS_FILE],
        investments_csv=contents[INVESTMENTS_FILE],
    )


def _manifest_hashes(directory: Path) -> dict[str, str]:
    """The manifest's SHA-256 for each CSV that goes into the pack."""
    try:
        manifest = parse_manifest((directory / MANIFEST_FILE).read_bytes())
    except OSError as exc:
        raise DownloadError(f"cannot read {MANIFEST_FILE}: {exc.strerror or 'I/O error'}") from exc
    return {name: manifest.outputs[name].sha256 for name in (DEDUCTIONS_FILE, INVESTMENTS_FILE)}


def _read_csv_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DownloadError(f"cannot read {path.name}: {exc.strerror or 'I/O error'}") from exc


def _read_rows(
    content: bytes, file_name: str, set_name: str, columns: tuple[str, str, str]
) -> list[PlannedDocument]:
    id_column, date_column, name_column = columns
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        rows = list(reader)
        header = reader.fieldnames or []
    except (UnicodeDecodeError, csv.Error) as exc:
        raise DownloadError(f"{file_name} is not readable as a UTF-8 CSV") from exc
    for column in columns:
        if column not in header:
            raise DownloadError(f"{file_name} has no '{column}' column")
    documents: list[PlannedDocument] = []
    seen: set[int] = set()
    for number, row in enumerate(rows, start=2):
        raw_id = (row.get(id_column) or "").strip()
        if _ID_RE.match(raw_id) is None:
            raise DownloadError(f"{file_name} line {number}: '{id_column}' is not a document id")
        document_id = int(raw_id)
        if document_id in seen:
            raise DownloadError(f"{file_name} lists document {document_id} more than once")
        seen.add(document_id)
        documents.append(
            PlannedDocument(
                set_name=set_name,
                document_id=document_id,
                date=row.get(date_column) or "",
                name=row.get(name_column) or "",
            )
        )
    return documents


def _plan_warnings(documents: list[PlannedDocument]) -> tuple[str, ...]:
    warnings = list(_overlap_warnings(documents))
    if not documents:
        warnings.append(
            f"{DEDUCTIONS_FILE} and {INVESTMENTS_FILE} list no documents;"
            f" the pack will hold only the CSVs and {DOWNLOADS_FILE}"
        )
    return tuple(warnings)


def _overlap_warnings(documents: list[PlannedDocument]) -> tuple[str, ...]:
    in_deductions = {d.document_id for d in documents if d.set_name == DEDUCTIONS_SET}
    in_investments = {d.document_id for d in documents if d.set_name == INVESTMENTS_SET}
    return tuple(
        f"document {document_id} is in both {DEDUCTIONS_FILE} and {INVESTMENTS_FILE}"
        for document_id in sorted(in_deductions & in_investments)
    )


# --- integrity ---------------------------------------------------------------


def check_integrity(info: DocumentFileInfo, downloaded: DownloadedOriginal) -> str:
    """``match`` or ``unchecked``; raises ``DownloadError`` when the bytes are wrong.

    An empty body and a body that disagrees with a present Content-Length always
    fail. A 32-hex stored checksum is compared as MD5, a 64-hex one as SHA-256;
    a missing or other-length value leaves the size checks as the only guard.
    """
    document_id = downloaded.document_id
    if downloaded.size == 0:
        raise DownloadError(f"document {document_id}: the download was empty")
    if downloaded.content_length is not None and downloaded.content_length != downloaded.size:
        raise DownloadError(
            f"document {document_id}: received {downloaded.size} bytes but"
            f" Content-Length said {downloaded.content_length}"
        )
    stored = (info.checksum or "").strip().lower()
    if _MD5_RE.match(stored):
        actual = downloaded.md5
    elif _SHA256_RE.match(stored):
        actual = downloaded.sha256
    else:
        return CHECKSUM_UNCHECKED
    if actual != stored:
        raise DownloadError(f"document {document_id}: the download does not match its checksum")
    return CHECKSUM_MATCH


# --- execution ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Fetched:
    path: Path
    extension: str
    size: int
    sha256: str
    checksum_result: str


def execute_download(
    plan: DownloadPlan, client: PaperlessClient, *, require_checksum: bool = False
) -> DownloadResult:
    """Download every planned original and write ``tax-pack.zip`` all or nothing.

    With *require_checksum*, a document with no usable stored checksum fails the run.
    """
    warnings: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="paperless-tax-export-") as scratch:
            fetched = _fetch_all(plan, client, Path(scratch), warnings, require_checksum)
            entries = _build_entries(plan, fetched)
            pack_path, zip_sha256 = _write_pack(plan, entries)
    except OSError as exc:
        raise DownloadError(f"cannot write {PACK_NAME}: {exc.strerror or 'I/O error'}") from exc
    return DownloadResult(
        pack_path=pack_path,
        entries=tuple(entry for entry, _ in entries),
        warnings=tuple(warnings),
        zip_sha256=zip_sha256,
    )


def _fetch_all(
    plan: DownloadPlan,
    client: PaperlessClient,
    scratch: Path,
    warnings: list[str],
    require_checksum: bool,
) -> dict[int, _Fetched]:
    fetched: dict[int, _Fetched] = {}
    for document_id in plan.unique_ids:
        info = client.get_document_file_info(document_id)
        destination = scratch / f"{document_id}.download"
        downloaded = client.download_original(document_id, destination)
        result = check_integrity(info, downloaded)
        if require_checksum and result == CHECKSUM_UNCHECKED:
            raise DownloadError(
                f"document {document_id}: no usable Paperless checksum to compare with"
                " (--require-checksum)"
            )
        extension = derive_extension(
            downloaded.file_name, info.original_file_name, downloaded.content_type
        )
        if extension is None:
            warnings.append(
                f"document {document_id}: no file type found, using {_FALLBACK_EXTENSION}"
            )
            extension = _FALLBACK_EXTENSION
        fetched[document_id] = _Fetched(
            path=destination,
            extension=extension,
            size=downloaded.size,
            sha256=downloaded.sha256,
            checksum_result=result,
        )
    return fetched


def _build_entries(
    plan: DownloadPlan, fetched: dict[int, _Fetched]
) -> list[tuple[PackEntry, Path]]:
    entries: list[tuple[PackEntry, Path]] = []
    for document in plan.documents:
        item = fetched[document.document_id]
        entry = PackEntry(
            set_name=document.set_name,
            document_id=document.document_id,
            arcname=f"{document.stem}{item.extension}",
            size=item.size,
            sha256=item.sha256,
            checksum_result=item.checksum_result,
        )
        entries.append((entry, item.path))
    return entries


def render_downloads_json(entries: list[PackEntry]) -> bytes:
    """Canonical ``downloads.json``: sorted keys, two-space indent, no timestamp."""
    payload = [
        {
            "set": entry.set_name,
            "document_id": entry.document_id,
            "arcname": entry.arcname,
            "bytes": entry.size,
            "sha256": entry.sha256,
            "checksum_result": entry.checksum_result,
        }
        for entry in sorted(entries, key=lambda item: (item.set_name, item.document_id))
    ]
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_pack(plan: DownloadPlan, entries: list[tuple[PackEntry, Path]]) -> tuple[Path, str]:
    """Build the zip in a temp file beside the target and ``os.replace`` it in place."""
    downloads_json = render_downloads_json([entry for entry, _ in entries])
    members: dict[str, bytes | Path] = {entry.arcname: path for entry, path in entries}
    members[DEDUCTIONS_FILE] = plan.deductions_csv
    members[INVESTMENTS_FILE] = plan.investments_csv
    members[DOWNLOADS_FILE] = downloads_json
    target = plan.directory / PACK_NAME
    zip_sha256 = write_atomically(target, partial(_write_zip, members=members))
    return target, zip_sha256


def write_atomically(target: Path, writer: Callable[[Path], None]) -> str:
    """Run *writer* on a temp file next to *target*, then replace *target* with it.

    Returns the SHA-256 of the written file. If *writer* or the replace fails, the
    temp file is removed and *target* is left as it was.
    """
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".tax-pack-", suffix=".tmp")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        writer(temp_path)
        digest = _sha256_of_file(temp_path)
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return digest


def _write_zip(path: Path, *, members: dict[str, bytes | Path]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for arcname in sorted(members):
            source = members[arcname]
            info = _zip_info(arcname)
            if isinstance(source, bytes):
                archive.writestr(info, source)
                continue
            info.file_size = source.stat().st_size
            with source.open("rb") as reader, archive.open(info, "w") as writer:
                shutil.copyfileobj(reader, writer, _COPY_CHUNK_BYTES)


def _zip_info(arcname: str) -> zipfile.ZipInfo:
    if PurePosixPath(arcname).is_absolute() or ".." in arcname or "\\" in arcname:
        raise DownloadError("an archive path was not safe")
    info = zipfile.ZipInfo(arcname, date_time=_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = _ZIP_CREATE_SYSTEM
    info.external_attr = _ZIP_EXTERNAL_ATTR
    return info


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
