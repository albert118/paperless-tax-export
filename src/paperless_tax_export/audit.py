"""Diagnostic audit of a live Paperless instance against the export settings.

``run_audit`` is a diagnostic, not a gate: a half-configured instance (a missing
tag, zero matching documents, missing fields, zero coverage) still produces a
report and the CLI exits 0. The audit never calls ``resolve_tag`` or
``resolve_fields``, so it reports lenient findings instead of exiting on
configuration mismatches. Paperless permissions are per object type, so the
report records how many tags, correspondents, document types and storage paths
the token can see, plus the base URL and UTC timestamp the audit ran against
and any non-fatal ``notes`` (such as the client's record of ``next`` links it
re-anchored to the configured origin).

The optional list reads are resilient: a tags, custom-fields, storage-paths,
correspondents or document-types fetch that raises an auth, status or
transport error is recorded in ``unavailable`` and treated as an empty list,
and the audit continues. The documents fetch (only made when exactly one tag
matches) stays fatal and keeps the exit-code mapping to 3; bad flags and
unreadable or empty token files map to exit 2.

Sanitisation (structural, not a scrub)
--------------------------------------
``dump_json`` output is the only sanctioned committed data path. ``AuditReport``
holds aggregates only: no per-document title, document id, date, custom-field
value, ``archived_file_name`` string or monetary value may enter the report.
The one document-derived aggregate is the ``archived_file_name`` first/second
path-segment count summary. Those segment strings are rendered only by
``render()`` for the owner's console; ``dump_json`` emits only the integer counts
of documents with and without an archived version. The base
URL, run timestamp, visibility counts, the ``unavailable`` endpoint list and
the value-neutral ``notes`` observations (including the client's
origin-rewrite records) are structural metadata and never carry the API token.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.errors import (
    ApiVersionError,
    HttpStatusError,
    TaxExportAuthError,
    TransportError,
)
from paperless_tax_export.models import (
    Correspondent,
    CustomFieldDef,
    Document,
    DocumentType,
    Tag,
)


@dataclass(frozen=True, slots=True)
class TagFinding:
    """One tag whose name matched the requested deduction tag."""

    name: str
    document_count: int


@dataclass(frozen=True, slots=True)
class SelectOption:
    """One ``id -> label`` option declared on a select custom field."""

    id: str
    label: str


@dataclass(frozen=True, slots=True)
class CustomFieldInfo:
    """Metadata for one custom-field definition (no document-derived values)."""

    id: int
    name: str
    data_type: str
    document_count: int
    default_currency: str | None
    select_options: tuple[SelectOption, ...]


@dataclass(frozen=True, slots=True)
class FieldCoverage:
    """Per-field value coverage over the FY-scoped deduction documents."""

    field_id: int
    field_name: str
    present_count: int
    null_count: int
    absent_count: int


@dataclass(frozen=True, slots=True)
class StoragePathInfo:
    """One storage-path definition (informational only)."""

    id: int
    name: str
    path: str
    document_count: int


@dataclass(frozen=True, slots=True)
class SegmentCount:
    """A distinct ``archived_file_name`` path segment and how many documents use it."""

    segment: str
    count: int


@dataclass(frozen=True, slots=True)
class IbkrFindings:
    """How the IBKR correspondent lines up with the configured name.

    IBKR selection is correspondent-only; the ``tax-information`` tag is
    general tax metadata and is reported separately as ``TagMetadata``.
    """

    correspondent_name: str
    correspondent_exists: bool
    correspondent_document_count: int
    trade_confirmation_types: tuple[str, ...]
    realized_gains_types: tuple[str, ...]
    correspondent_candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TagMetadata:
    """General metadata about one configured tag name.

    The ``tax-information`` tag is general tax metadata, not an IBKR-specific
    marker, so the audit reports it for what it is instead of folding it into
    the IBKR findings.
    """

    name: str
    exists: bool
    document_count: int
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExpectedFieldInfo:
    """Whether one expected field name exists and how well it is populated."""

    name: str
    required: bool
    exists: bool
    data_type: str | None
    coverage: FieldCoverage | None


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Aggregated audit findings; holds no per-document strings (see module docs)."""

    tag_name: str
    fy_start: str
    fy_end: str
    tag_matches: tuple[TagFinding, ...]
    deduction_document_count: int
    custom_fields: tuple[CustomFieldInfo, ...]
    coverage: tuple[FieldCoverage, ...]
    storage_paths: tuple[StoragePathInfo, ...]
    archived_first_segments: tuple[SegmentCount, ...]
    archived_second_segments: tuple[SegmentCount, ...]
    ibkr: IbkrFindings
    tax_metadata_tag: TagMetadata
    expected_fields: tuple[ExpectedFieldInfo, ...]
    api_version: str | None
    server_version: str | None
    base_url: str = ""
    recorded_at: str = ""
    tags_visible: int = 0
    correspondents_visible: int = 0
    document_types_visible: int = 0
    storage_paths_visible: int = 0
    unavailable: tuple[str, ...] = ()
    tag_candidates: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    archived_version_count: int = 0
    no_archived_version_count: int = 0


def run_audit(
    client: PaperlessClient,
    *,
    tag: str,
    fy_start: str,
    fy_end: str,
    tax_info_tag: str,
    ibkr_correspondent: str,
    field_amount: str | None,
    field_gst: str | None,
    field_work_use: str | None,
    base_url: str = "",
) -> AuditReport:
    """Gather lenient diagnostic findings from the live Paperless instance.

    Every list endpoint is fetched through the client. The optional reads —
    tags, custom fields, storage paths, correspondents and document types —
    survive client failures: a raised ``TaxExportError`` is recorded in
    ``unavailable`` and the endpoint is treated as empty, so the audit keeps
    going and reports what a half-visible token could actually see. The
    documents fetch (only made when exactly one tag matches) stays fatal.
    Missing tags, missing fields and zero coverage are reported, never raised.
    A ``None`` expected field name means the setting is not configured; it is
    reported as such by the renderer and never appears as "missing from
    Paperless". The client's record of re-anchored ``next`` links is carried
    into ``notes`` as non-fatal observations.
    """
    recorded_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    unavailable: list[str] = []
    all_tags = _optional_read(client.list_tags, "tags", unavailable)
    matched_tags = [tag_def for tag_def in all_tags if tag_def.name == tag]
    tag_matches = tuple(
        TagFinding(name=tag_def.name, document_count=tag_def.document_count)
        for tag_def in matched_tags
    )
    tag_candidates = _near_miss_names((tag_def.name for tag_def in all_tags), tag)

    documents: tuple[Document, ...] = ()
    if len(matched_tags) == 1:
        documents = tuple(
            client.list_documents(
                tag_id=matched_tags[0].id,
                created_from=fy_start,
                created_to=fy_end,
            )
        )

    fields = _optional_read(client.list_custom_fields, "custom_fields", unavailable)
    custom_fields = tuple(
        sorted((_field_info(field) for field in fields), key=lambda info: info.name)
    )
    coverage = tuple(
        sorted(
            (_coverage(field, documents) for field in fields),
            key=lambda cov: cov.field_name,
        )
    )
    coverage_by_field_id = {cov.field_id: cov for cov in coverage}

    storage_path_rows = _optional_read(client.list_storage_paths, "storage_paths", unavailable)
    storage_paths = tuple(
        sorted(
            (
                StoragePathInfo(
                    id=storage_path.id,
                    name=storage_path.name,
                    path=storage_path.path,
                    document_count=storage_path.document_count,
                )
                for storage_path in storage_path_rows
            ),
            key=lambda info: (info.name, info.id),
        )
    )

    archived = _archived_segments(documents)

    correspondents = _optional_read(client.list_correspondents, "correspondents", unavailable)
    document_types = _optional_read(client.list_document_types, "document_types", unavailable)
    ibkr = _ibkr_findings(
        correspondents=correspondents,
        document_types=document_types,
        ibkr_correspondent=ibkr_correspondent,
    )
    tax_metadata = _tag_metadata(name=tax_info_tag, tags=all_tags)

    fields_by_name = {field.name: field for field in fields}
    expected_fields = _expected_fields(
        fields_by_name=fields_by_name,
        coverage_by_field_id=coverage_by_field_id,
        field_amount=field_amount,
        field_gst=field_gst,
        field_work_use=field_work_use,
    )

    return AuditReport(
        tag_name=tag,
        fy_start=fy_start,
        fy_end=fy_end,
        tag_matches=tag_matches,
        deduction_document_count=len(documents),
        custom_fields=custom_fields,
        coverage=coverage,
        storage_paths=storage_paths,
        archived_first_segments=archived.first,
        archived_second_segments=archived.second,
        archived_version_count=archived.counted,
        no_archived_version_count=archived.none,
        ibkr=ibkr,
        tax_metadata_tag=tax_metadata,
        expected_fields=expected_fields,
        api_version=client.api_version,
        server_version=client.server_version,
        base_url=base_url,
        recorded_at=recorded_at,
        tags_visible=len(all_tags),
        correspondents_visible=len(correspondents),
        document_types_visible=len(document_types),
        storage_paths_visible=len(storage_path_rows),
        unavailable=tuple(unavailable),
        tag_candidates=tag_candidates,
        notes=tuple(client.origin_rewrites),
    )


def _optional_read[T](
    read: Callable[[], Sequence[T]], label: str, unavailable: list[str]
) -> tuple[T, ...]:
    """Read an optional endpoint through *read*, or record the failure.

    A client failure the token can recover from (auth, status or transport:
    see ``_OPTIONAL_READ_RECOVERABLE``) is appended to *unavailable* and the
    endpoint is treated as empty so the audit continues instead of dying on one
    optional object type the token cannot see. Guard failures stay fatal.
    """
    try:
        return tuple(read())
    except _OPTIONAL_READ_RECOVERABLE as exc:
        unavailable.append(f"{label}: {exc}")
        return ()


# Client failures an optional read can absorb: auth (401/403), API version
# (406), other status errors and transport errors all mean "this token cannot
# read this object type" or "the server did not answer". Guard failures
# (pagination integrity and count guards) stay fatal: they are client-visible
# bugs, not visibility problems, and keep their exit-code mapping.
_OPTIONAL_READ_RECOVERABLE: tuple[type[Exception], ...] = (
    TaxExportAuthError,
    ApiVersionError,
    HttpStatusError,
    TransportError,
)


def _normalise_name(name: str) -> str:
    """Normalise *name* for near-miss comparison: casefold and collapse separators.

    Hyphens and underscores are treated as spaces so ``tax-deductible`` and
    ``tax deductible`` compare equal and are reported as candidates.
    """
    return " ".join(name.casefold().replace("-", " ").replace("_", " ").split())


def _near_miss_names(names: Iterable[str], target: str) -> tuple[str, ...]:
    """Stored names that normalise to *target* but do not match it exactly.

    Used for reporting only: a near-miss is a candidate for the owner to
    compare, never an automatic match.
    """
    needle = _normalise_name(target)
    candidates = [name for name in names if name != target and _normalise_name(name) == needle]
    return tuple(sorted(candidates))


def _field_info(field: CustomFieldDef) -> CustomFieldInfo:
    """Build the report's metadata for one custom-field definition."""
    options: list[SelectOption] = []
    if field.data_type == "select":
        for raw in field.select_options():
            raw_id: Any = raw.get("id")
            label: Any = raw.get("label")
            if raw_id is None or not isinstance(label, str):
                continue
            options.append(SelectOption(id=str(raw_id), label=label))
    options.sort(key=lambda option: (option.id, option.label))
    return CustomFieldInfo(
        id=field.id,
        name=field.name,
        data_type=field.data_type,
        document_count=field.document_count,
        default_currency=field.default_currency(),
        select_options=tuple(options),
    )


def _coverage(field: CustomFieldDef, documents: Sequence[Document]) -> FieldCoverage:
    """Count present / present-null / absent values of *field* over *documents*."""
    present = 0
    null = 0
    absent = 0
    for document in documents:
        if not document.has_field(field.id):
            absent += 1
        elif document.field_value(field.id) is not None:
            present += 1
        else:
            null += 1
    return FieldCoverage(
        field_id=field.id,
        field_name=field.name,
        present_count=present,
        null_count=null,
        absent_count=absent,
    )


@dataclass(frozen=True, slots=True)
class ArchivedSegments:
    """First/second ``archived_file_name`` segment counts plus their denominators.

    ``counted + none`` always equals the number of documents examined.
    """

    first: tuple[SegmentCount, ...]
    second: tuple[SegmentCount, ...]
    counted: int
    none: int


def _archived_segments(documents: Sequence[Document]) -> ArchivedSegments:
    """Distinct first and second ``archived_file_name`` segments with counts.

    A document with a null or empty ``archived_file_name`` is tallied in ``none``
    rather than skipped, so the counts always reconcile with the document total.
    """
    first: dict[str, int] = {}
    second: dict[str, int] = {}
    counted = 0
    for document in documents:
        archived = document.archived_file_name
        if not archived:
            continue
        counted += 1
        parts = archived.split("/")
        first[parts[0]] = first.get(parts[0], 0) + 1
        # Single-segment paths add to the first segment only, so the second-segment
        # counts can sum to less than ``counted``.
        if len(parts) > 1:
            second[parts[1]] = second.get(parts[1], 0) + 1
    return ArchivedSegments(
        first=_segment_counts(first),
        second=_segment_counts(second),
        counted=counted,
        none=len(documents) - counted,
    )


def _segment_counts(counter: dict[str, int]) -> tuple[SegmentCount, ...]:
    return tuple(
        sorted(
            (SegmentCount(segment=segment, count=count) for segment, count in counter.items()),
            key=lambda item: (-item.count, item.segment),
        )
    )


def _ibkr_findings(
    *,
    correspondents: Sequence[Correspondent],
    document_types: Sequence[DocumentType],
    ibkr_correspondent: str,
) -> IbkrFindings:
    """Aggregate the IBKR correspondent checks and near-miss counts."""
    correspondent_matches = [
        correspondent
        for correspondent in correspondents
        if correspondent.name == ibkr_correspondent
    ]

    trade_confirmation = tuple(
        sorted(
            type_def.name
            for type_def in document_types
            if "trade confirmation" in type_def.name.lower()
        )
    )
    realized_gains = tuple(
        sorted(
            type_def.name
            for type_def in document_types
            if "realized gains summary" in type_def.name.lower()
        )
    )

    return IbkrFindings(
        correspondent_name=ibkr_correspondent,
        correspondent_exists=bool(correspondent_matches),
        correspondent_document_count=sum(
            correspondent.document_count for correspondent in correspondent_matches
        ),
        trade_confirmation_types=trade_confirmation,
        realized_gains_types=realized_gains,
        correspondent_candidates=_near_miss_names(
            (correspondent.name for correspondent in correspondents),
            ibkr_correspondent,
        ),
    )


def _tag_metadata(*, name: str, tags: Sequence[Tag]) -> TagMetadata:
    """General metadata about the configured *name* tag (exact match plus near-misses)."""
    matches = [tag_def for tag_def in tags if tag_def.name == name]
    return TagMetadata(
        name=name,
        exists=bool(matches),
        document_count=sum(tag_def.document_count for tag_def in matches),
        candidates=_near_miss_names((tag_def.name for tag_def in tags), name),
    )


def _expected_fields(
    *,
    fields_by_name: dict[str, CustomFieldDef],
    coverage_by_field_id: dict[int, FieldCoverage],
    field_amount: str | None,
    field_gst: str | None,
    field_work_use: str | None,
) -> tuple[ExpectedFieldInfo, ...]:
    """Check the expected field names by exact name against the live fields.

    A ``None`` name means the setting is not configured; those fields are
    skipped so they are never reported as "missing from Paperless".
    """
    result: list[ExpectedFieldInfo] = []
    for name, required in (
        (field_amount, True),
        (field_gst, False),
        (field_work_use, False),
    ):
        if name is None:
            continue
        field = fields_by_name.get(name)
        result.append(
            ExpectedFieldInfo(
                name=name,
                required=required,
                exists=field is not None,
                data_type=field.data_type if field is not None else None,
                coverage=(coverage_by_field_id.get(field.id) if field is not None else None),
            )
        )
    return tuple(result)


def render(report: AuditReport) -> str:
    """Console text for the audit report (the owner sees this, not git)."""
    lines = ["Paperless audit", "===============", ""]

    if report.unavailable or report.notes:
        lines.append("Warnings")
        lines.append("--------")
        lines.extend(f"  {entry}" for entry in report.unavailable)
        if report.notes:
            lines.append("  Notes:")
            lines.extend(f"    {note}" for note in report.notes)
        lines.append("")

    lines.append(f"Deduction tag {report.tag_name!r}:")
    if report.tags_visible == 0:
        lines.append(f"  no tags visible to this token — {_visibility_hint(report)}")
    elif not report.tag_matches:
        line = f"  no tag named {report.tag_name!r} among {report.tags_visible} visible"
        if report.tag_candidates:
            line += f"; candidates: {_names_or_none(report.tag_candidates)}"
        lines.append(line)
    else:
        for match in report.tag_matches:
            lines.append(f"  - {match.name!r}: {match.document_count} document(s)")
        if len(report.tag_matches) != 1:
            lines.append(
                f"  note: {len(report.tag_matches)} tags share this name; documents were"
                " not scoped to a single tag"
            )

    lines.append("")
    lines.append(
        f"FY {report.fy_start} to {report.fy_end}: "
        f"{report.deduction_document_count} deduction document(s)"
    )

    lines.append("")
    lines.append("Custom fields (sorted by name):")
    if report.custom_fields:
        for field in report.custom_fields:
            parts = [
                f"#{field.id}",
                repr(field.name),
                f"[{field.data_type}]",
                f"{field.document_count} document(s)",
            ]
            if field.default_currency is not None:
                parts.append(f"default currency {field.default_currency}")
            if field.select_options:
                options = ", ".join(
                    f"{option.id}={option.label}" for option in field.select_options
                )
                parts.append(f"options: {options}")
            lines.append("  - " + " ".join(parts))
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Custom-field coverage over the FY deduction documents:")
    if report.coverage:
        for cov in report.coverage:
            lines.append(
                f"  - {cov.field_name!r} (#{cov.field_id}): {cov.present_count} present, "
                f"{cov.null_count} null, {cov.absent_count} absent"
            )
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Storage paths (informational — selection is created-only):")
    if report.storage_paths:
        for path in report.storage_paths:
            lines.append(
                f"  - #{path.id} {path.name!r} {path.path!r}: {path.document_count} document(s)"
            )
    else:
        lines.append("  (none)")

    if report.deduction_document_count:
        lines.append("")
        lines.append(
            "archived_file_name segments across the FY deduction documents (console only):"
        )
        lines.append(
            f"  {report.archived_version_count} with an archived version, "
            f"{report.no_archived_version_count} with no archived version "
            f"(of {report.deduction_document_count})"
        )
        if report.archived_version_count:
            lines.append("  first segment:")
            lines.extend(_segment_lines(report.archived_first_segments))
            lines.append("  second segment:")
            lines.extend(_segment_lines(report.archived_second_segments))
        else:
            lines.append("  no archived version: no segments to report")

    ibkr = report.ibkr
    lines.append("")
    lines.append("IBKR identifiers:")
    if ibkr.correspondent_exists:
        lines.append(
            f"  correspondent {ibkr.correspondent_name!r}: exists with "
            f"{ibkr.correspondent_document_count} document(s)"
        )
    elif report.correspondents_visible == 0:
        lines.append(
            f"  correspondent {ibkr.correspondent_name!r}: no correspondents visible"
            f" to this token — {_visibility_hint(report)}"
        )
    else:
        line = (
            f"  correspondent {ibkr.correspondent_name!r}: no correspondent named "
            f"{ibkr.correspondent_name!r} among {report.correspondents_visible} visible"
        )
        if ibkr.correspondent_candidates:
            line += f"; candidates: {_names_or_none(ibkr.correspondent_candidates)}"
        lines.append(line)
    lines.append(
        "  document types matching 'trade confirmation': "
        f"{_names_or_none(ibkr.trade_confirmation_types)}"
    )
    lines.append(
        "  document types matching 'realized gains summary': "
        f"{_names_or_none(ibkr.realized_gains_types)}"
    )

    tax = report.tax_metadata_tag
    lines.append("")
    lines.append("Tax-information tag (general tax metadata, not IBKR-specific):")
    if tax.exists:
        lines.append(f"  tag {tax.name!r}: exists with {tax.document_count} document(s)")
    elif report.tags_visible == 0:
        lines.append(
            f"  tag {tax.name!r}: no tags visible to this token — {_visibility_hint(report)}"
        )
    else:
        line = f"  tag {tax.name!r}: no tag named {tax.name!r} among {report.tags_visible} visible"
        if tax.candidates:
            line += f"; candidates: {_names_or_none(tax.candidates)}"
        lines.append(line)

    lines.append("")
    lines.append("Expected field names (from the Settings defaults):")
    for info in report.expected_fields:
        marker = " (required)" if info.required else ""
        if info.exists:
            data_type = info.data_type or "unknown"
            coverage_text = "no coverage"
            if info.coverage is not None:
                coverage_text = (
                    f"coverage {info.coverage.present_count} present / "
                    f"{info.coverage.null_count} null / {info.coverage.absent_count} absent"
                )
            lines.append(f"  - {info.name!r}{marker}: exists ({data_type}), {coverage_text}")
        else:
            lines.append(f"  - {info.name!r}{marker}: missing from Paperless")
    lines.append("  Note: Settings.field_date and Settings.field_supplier may be None;")
    lines.append("  a None field is not configured and is never reported as missing.")
    lines.append("  The row date falls back to the document `created` date and the")
    lines.append("  supplier to the document correspondent.")

    lines.append("")
    lines.append(f"Paperless API version: {report.api_version or 'unknown'}")
    lines.append(f"Paperless server version: {report.server_version or 'unknown'}")
    return "\n".join(lines)


def _segment_lines(segments: Sequence[SegmentCount]) -> list[str]:
    return [f"    {segment.segment!r} x{segment.count}" for segment in segments]


def _names_or_none(names: Sequence[str]) -> str:
    return ", ".join(repr(name) for name in names) if names else "none"


def _visibility_hint(report: AuditReport) -> str:
    """Explain why an object list was empty, without guessing past the evidence."""
    if report.unavailable:
        return "see the warnings above"
    return "check permissions"


def dump_json(report: AuditReport) -> str:
    """Deterministic JSON text of the audit; counts and metadata only.

    This is the only sanctioned committed data path: no document title, id,
    date, custom-field value, ``archived_file_name`` string or amount is ever
    part of the payload (see the module docstring for the full rule). The
    base URL, run timestamp, visibility counts, the ``unavailable`` endpoint
    list and the value-neutral ``notes`` observations (including the client's
    recorded origin rewrites) are metadata and never carry the API token.
    """
    payload: dict[str, Any] = {
        "tag": report.tag_name,
        "fy_start": report.fy_start,
        "fy_end": report.fy_end,
        "deduction_document_count": report.deduction_document_count,
        "archived_version_count": report.archived_version_count,
        "no_archived_version_count": report.no_archived_version_count,
        "tag_matches": [
            {"name": match.name, "document_count": match.document_count}
            for match in report.tag_matches
        ],
        "custom_fields": [
            {
                "id": field.id,
                "name": field.name,
                "data_type": field.data_type,
                "document_count": field.document_count,
                "default_currency": field.default_currency,
                "select_options": [
                    {"id": option.id, "label": option.label} for option in field.select_options
                ],
            }
            for field in report.custom_fields
        ],
        "coverage": [
            {
                "field_id": cov.field_id,
                "field_name": cov.field_name,
                "present_count": cov.present_count,
                "null_count": cov.null_count,
                "absent_count": cov.absent_count,
            }
            for cov in report.coverage
        ],
        "storage_paths": [
            {
                "id": path.id,
                "name": path.name,
                "path": path.path,
                "document_count": path.document_count,
            }
            for path in report.storage_paths
        ],
        "ibkr": {
            "correspondent": report.ibkr.correspondent_name,
            "correspondent_exists": report.ibkr.correspondent_exists,
            "correspondent_document_count": report.ibkr.correspondent_document_count,
            "trade_confirmation_types": list(report.ibkr.trade_confirmation_types),
            "realized_gains_types": list(report.ibkr.realized_gains_types),
        },
        "tax_metadata_tag": {
            "name": report.tax_metadata_tag.name,
            "exists": report.tax_metadata_tag.exists,
            "document_count": report.tax_metadata_tag.document_count,
            "candidates": list(report.tax_metadata_tag.candidates),
        },
        "expected_fields": [
            {
                "name": info.name,
                "required": info.required,
                "exists": info.exists,
                "data_type": info.data_type,
                "coverage": (
                    {
                        "field_id": info.coverage.field_id,
                        "field_name": info.coverage.field_name,
                        "present_count": info.coverage.present_count,
                        "null_count": info.coverage.null_count,
                        "absent_count": info.coverage.absent_count,
                    }
                    if info.coverage is not None
                    else None
                ),
            }
            for info in report.expected_fields
        ],
        "api_version": report.api_version,
        "server_version": report.server_version,
        "base_url": report.base_url,
        "recorded_at": report.recorded_at,
        "tags_visible": report.tags_visible,
        "correspondents_visible": report.correspondents_visible,
        "document_types_visible": report.document_types_visible,
        "storage_paths_visible": report.storage_paths_visible,
        "unavailable": list(report.unavailable),
        "notes": list(report.notes),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
