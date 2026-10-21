"""Offline check of a finished output directory against its ``manifest.json``.

``verify_directory`` recomputes the SHA-256 and the data-row count of every file
the manifest lists and compares both with the manifest, then checks the manifest's
own counts against the files and against each other. It needs no token and no
network and reads no settings. It catches accidental edits and a corrupted copy;
it does not prove the files match Paperless, and it is not tamper-proof (whoever
can edit a CSV can regenerate the manifest too).

The check functions are pure: they take bytes and parsed values and return text.
Only :func:`verify_directory` touches the disk. Output never includes file
contents, only names, hashes and counts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from paperless_tax_export.errors import ManifestError
from paperless_tax_export.manifest import (
    DEDUCTIONS_FILE,
    IBKR_FILE,
    MANIFEST_FILE,
    REVIEW_FILE,
    ManifestCounts,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_REQUIRED_OUTPUTS = (DEDUCTIONS_FILE, REVIEW_FILE, IBKR_FILE)


@dataclass(frozen=True, slots=True)
class ExpectedFile:
    """What the manifest records for one output file."""

    sha256: str
    rows: int


@dataclass(frozen=True, slots=True)
class ParsedManifest:
    """The parts of ``manifest.json`` that verification uses."""

    outputs: Mapping[str, ExpectedFile]
    counts: ManifestCounts


@dataclass(frozen=True, slots=True)
class FileCheck:
    """Result of comparing one listed file with the manifest.

    ``rows`` is the measured data-row count, or ``None`` when the bytes are not
    readable as a UTF-8 CSV.
    """

    line: str
    ok: bool
    rows: int | None = None


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Everything ``verify`` prints, in order, and how many checks failed."""

    lines: tuple[str, ...]
    problems: int

    @property
    def ok(self) -> bool:
        return self.problems == 0


def count_data_rows(content: bytes) -> int:
    """Data rows of a CSV file (header excluded), counted with the ``csv`` module.

    Decoding is ``utf-8-sig`` and parsing uses ``newline=""`` semantics, so a
    quoted cell holding a line break counts once. Raises ``ValueError`` when the
    bytes are not UTF-8 or the CSV cannot be parsed.
    """
    try:
        text = content.decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""))
        return max(sum(1 for _ in reader) - 1, 0)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("not readable as a UTF-8 CSV") from exc


def parse_manifest(raw: bytes) -> ParsedManifest:
    """Validate the manifest shape; errors name the problem, never the contents."""
    try:
        payload: object = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ManifestError(f"{MANIFEST_FILE} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"{MANIFEST_FILE} must hold a JSON object")
    return ParsedManifest(
        outputs=_parse_outputs(payload.get("outputs")),
        counts=_parse_counts(payload.get("counts")),
    )


def _parse_outputs(value: object) -> dict[str, ExpectedFile]:
    if not isinstance(value, dict):
        raise ManifestError(f"{MANIFEST_FILE} lacks an 'outputs' object")
    outputs: dict[str, ExpectedFile] = {}
    for name, entry in value.items():
        if not _is_plain_file_name(name):
            raise ManifestError(f"{MANIFEST_FILE} 'outputs' has an entry that is not a file name")
        if not isinstance(entry, dict):
            raise ManifestError(f"{MANIFEST_FILE} 'outputs.{name}' must be an object")
        digest = entry.get("sha256")
        rows = entry.get("rows")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise ManifestError(f"{MANIFEST_FILE} 'outputs.{name}.sha256' must be 64 hex digits")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise ManifestError(
                f"{MANIFEST_FILE} 'outputs.{name}.rows' must be a non-negative integer"
            )
        outputs[name] = ExpectedFile(sha256=digest.lower(), rows=rows)
    for required in _REQUIRED_OUTPUTS:
        if required not in outputs:
            raise ManifestError(f"{MANIFEST_FILE} 'outputs' does not list {required}")
    return outputs


def _parse_counts(value: object) -> ManifestCounts:
    if not isinstance(value, dict):
        raise ManifestError(f"{MANIFEST_FILE} lacks a 'counts' object")
    parsed: dict[str, int] = {}
    for field in fields(ManifestCounts):
        count = value.get(field.name)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ManifestError(
                f"{MANIFEST_FILE} 'counts.{field.name}' must be a non-negative integer"
            )
        parsed[field.name] = count
    return ManifestCounts(**parsed)


def _is_plain_file_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and "/" not in name
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
    )


def check_file(name: str, expected: ExpectedFile, content: bytes) -> FileCheck:
    """Compare the bytes of one listed file with the manifest's hash and row count."""
    actual_sha = hashlib.sha256(content).hexdigest()
    try:
        actual_rows: int | None = count_data_rows(content)
    except ValueError:
        actual_rows = None
    differs = []
    if actual_sha != expected.sha256:
        differs.append("sha256")
    if actual_rows != expected.rows:
        differs.append("rows")
    if not differs:
        return FileCheck(f"OK {name}", ok=True, rows=actual_rows)
    shown_rows = "unreadable" if actual_rows is None else str(actual_rows)
    line = (
        f"MISMATCH {name}: expected sha256 {expected.sha256} rows {expected.rows}, "
        f"actual sha256 {actual_sha} rows {shown_rows} (differs: {', '.join(differs)})"
    )
    return FileCheck(line, ok=False, rows=actual_rows)


def check_consistency(counts: ManifestCounts, rows_by_file: Mapping[str, int]) -> tuple[str, ...]:
    """Failure lines for the manifest's counts against the files and each other.

    A file absent from *rows_by_file* (missing or unreadable) is skipped here;
    :func:`verify_directory` already reports it.
    """
    claims = (
        ("accepted", counts.accepted, DEDUCTIONS_FILE),
        ("review", counts.review, REVIEW_FILE),
        ("ibkr_count", counts.ibkr_count, IBKR_FILE),
    )
    failures = [
        f"CHECK FAILED counts.{field}: manifest says {claimed}, {name} has "
        f"{rows_by_file[name]} data rows"
        for field, claimed, name in claims
        if name in rows_by_file and rows_by_file[name] != claimed
    ]
    if counts.accepted + counts.review != counts.selected_in_fy:
        failures.append(
            "CHECK FAILED counts.accepted + counts.review == counts.selected_in_fy: "
            f"{counts.accepted} + {counts.review} != {counts.selected_in_fy}"
        )
    return tuple(failures)


def verify_directory(directory: Path) -> VerifyReport:
    """Verify the output directory; raises ``ManifestError`` on an unusable manifest."""
    manifest = parse_manifest(_read_manifest(directory))
    lines: list[str] = []
    rows_by_file: dict[str, int] = {}
    file_problems = 0
    for name, expected in sorted(manifest.outputs.items()):
        try:
            content = _read_listed_file(directory / name)
        except FileNotFoundError:
            lines.append(f"MISSING {name}: listed in the manifest but not found")
            file_problems += 1
            continue
        except _UnreadableFileError as exc:
            lines.append(f"UNREADABLE {name}: {exc}")
            file_problems += 1
            continue
        result = check_file(name, expected, content)
        lines.append(result.line)
        file_problems += 0 if result.ok else 1
        if result.rows is not None:
            rows_by_file[name] = result.rows
    failures = check_consistency(manifest.counts, rows_by_file)
    lines.extend(failures)
    try:
        unlisted = _unlisted_names(directory, manifest.outputs)
    except OSError as exc:
        lines.append(f"note: could not list the directory: {exc.strerror or type(exc).__name__}")
    else:
        if unlisted:
            lines.append(f"note: not listed in the manifest: {', '.join(unlisted)}")
    problems = file_problems + len(failures)
    total = len(manifest.outputs)
    if problems:
        lines.append(
            f"verify FAILED: {problems} problem(s); {total - file_problems} of {total} files OK"
        )
    else:
        lines.append(f"verify OK: {total} files match the manifest")
    return VerifyReport(lines=tuple(lines), problems=problems)


class _UnreadableFileError(Exception):
    """A listed entry exists but cannot be verified; the message is the reason."""


def _read_listed_file(path: Path) -> bytes:
    """Bytes of a listed output: a regular file, not a symlink, device or directory.

    Raises ``FileNotFoundError`` when nothing is there and ``_UnreadableFileError``
    for every other reason the entry cannot be read.
    """
    if path.is_symlink():
        raise _UnreadableFileError("is a symbolic link; verify does not follow links")
    if not path.exists():
        raise FileNotFoundError(path.name)
    if not path.is_file():
        raise _UnreadableFileError("is not a regular file")
    try:
        return path.read_bytes()
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        raise _UnreadableFileError(str(reason)) from exc


def _read_manifest(directory: Path) -> bytes:
    try:
        return (directory / MANIFEST_FILE).read_bytes()
    except FileNotFoundError as exc:
        raise ManifestError(f"{MANIFEST_FILE} not found in {directory}") from exc
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise ManifestError(f"{MANIFEST_FILE} is not readable in {directory}: {reason}") from exc


def _unlisted_names(directory: Path, listed: Collection[str]) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if entry.name not in listed)
