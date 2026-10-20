"""Diagnostic audit of a live Paperless instance against the export settings.

``run_audit`` is a diagnostic, not a gate: a half-configured instance (a missing
tag, zero matching documents, missing fields, zero coverage) still produces a
report and the CLI exits 0. The audit never calls ``resolve_tag`` or
``resolve_fields``, so it reports lenient findings instead of exiting on
configuration mismatches. Client failures keep their existing mapping
(auth/network/status -> exit 3); bad flags and unreadable or empty token files
map to exit 2.

Sanitisation (structural, not a scrub)
--------------------------------------
``dump_json`` output is the only sanctioned committed data path. ``AuditReport``
holds aggregates only: no per-document title, document id, date, custom-field
value, ``archived_file_name`` string or monetary value may enter the report.
The one document-derived aggregate is the ``archived_file_name`` first/second
path-segment count summary, which ``dump_json`` never emits; those segment
strings are rendered only by ``render()`` for the owner's console.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from paperless_tax_export.client import PaperlessClient
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
    """How the IBKR identifiers line up with the configured names."""

    tag_name: str
    tag_exists: bool
    tag_document_count: int
    correspondent_name: str
    correspondent_exists: bool
    correspondent_document_count: int
    trade_confirmation_types: tuple[str, ...]
    realized_gains_types: tuple[str, ...]
    correspondent_without_tag: int
    tag_with_other_correspondent: int


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
    expected_fields: tuple[ExpectedFieldInfo, ...]
    api_version: str | None
    server_version: str | None


def run_audit(
    client: PaperlessClient,
    *,
    tag: str,
    fy_start: str,
    fy_end: str,
    ibkr_tag: str,
    ibkr_correspondent: str,
    field_amount: str,
    field_gst: str,
    field_work_use: str,
) -> AuditReport:
    """Gather lenient diagnostic findings from the live Paperless instance.

    Every list endpoint is fetched through the client, so auth/network/status
    failures keep their existing exception mapping. Missing tags, missing
    fields and zero coverage are reported, never raised.
    """
    all_tags = client.list_tags()
    matched_tags = [tag_def for tag_def in all_tags if tag_def.name == tag]
    tag_matches = tuple(
        TagFinding(name=tag_def.name, document_count=tag_def.document_count)
        for tag_def in matched_tags
    )

    documents: tuple[Document, ...] = ()
    if len(matched_tags) == 1:
        documents = tuple(
            client.list_documents(
                tag_id=matched_tags[0].id,
                created_from=fy_start,
                created_to=fy_end,
            )
        )

    fields = client.list_custom_fields()
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

    storage_paths = tuple(
        sorted(
            (
                StoragePathInfo(
                    id=storage_path.id,
                    name=storage_path.name,
                    path=storage_path.path,
                    document_count=storage_path.document_count,
                )
                for storage_path in client.list_storage_paths()
            ),
            key=lambda info: (info.name, info.id),
        )
    )

    first_segments, second_segments = _archived_segments(documents)

    ibkr = _ibkr_findings(
        tags=all_tags,
        correspondents=client.list_correspondents(),
        document_types=client.list_document_types(),
        documents=documents,
        ibkr_tag=ibkr_tag,
        ibkr_correspondent=ibkr_correspondent,
    )

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
        archived_first_segments=first_segments,
        archived_second_segments=second_segments,
        ibkr=ibkr,
        expected_fields=expected_fields,
        api_version=client.api_version,
        server_version=client.server_version,
    )


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


def _archived_segments(
    documents: Sequence[Document],
) -> tuple[tuple[SegmentCount, ...], tuple[SegmentCount, ...]]:
    """Distinct first and second ``archived_file_name`` segments with counts."""
    first: dict[str, int] = {}
    second: dict[str, int] = {}
    for document in documents:
        archived = document.archived_file_name
        if not archived:
            continue
        parts = archived.split("/")
        first[parts[0]] = first.get(parts[0], 0) + 1
        if len(parts) > 1:
            second[parts[1]] = second.get(parts[1], 0) + 1
    return _segment_counts(first), _segment_counts(second)


def _segment_counts(counter: dict[str, int]) -> tuple[SegmentCount, ...]:
    return tuple(
        sorted(
            (SegmentCount(segment=segment, count=count) for segment, count in counter.items()),
            key=lambda item: (-item.count, item.segment),
        )
    )


def _ibkr_findings(
    *,
    tags: Sequence[Tag],
    correspondents: Sequence[Correspondent],
    document_types: Sequence[DocumentType],
    documents: Sequence[Document],
    ibkr_tag: str,
    ibkr_correspondent: str,
) -> IbkrFindings:
    """Aggregate the IBKR identifier checks and near-miss counts."""
    tag_matches = [tag_def for tag_def in tags if tag_def.name == ibkr_tag]
    ibkr_tag_ids = frozenset(tag_def.id for tag_def in tag_matches)
    correspondent_matches = [
        correspondent
        for correspondent in correspondents
        if correspondent.name == ibkr_correspondent
    ]
    correspondent_ids = frozenset(correspondent.id for correspondent in correspondent_matches)

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

    correspondent_without_tag = 0
    tag_with_other_correspondent = 0
    for document in documents:
        has_ibkr_tag = bool(ibkr_tag_ids.intersection(document.tags))
        has_target_correspondent = (
            document.correspondent is not None and document.correspondent in correspondent_ids
        )
        if has_target_correspondent and not has_ibkr_tag:
            correspondent_without_tag += 1
        if has_ibkr_tag and not has_target_correspondent:
            tag_with_other_correspondent += 1

    return IbkrFindings(
        tag_name=ibkr_tag,
        tag_exists=bool(tag_matches),
        tag_document_count=sum(tag_def.document_count for tag_def in tag_matches),
        correspondent_name=ibkr_correspondent,
        correspondent_exists=bool(correspondent_matches),
        correspondent_document_count=sum(
            correspondent.document_count for correspondent in correspondent_matches
        ),
        trade_confirmation_types=trade_confirmation,
        realized_gains_types=realized_gains,
        correspondent_without_tag=correspondent_without_tag,
        tag_with_other_correspondent=tag_with_other_correspondent,
    )


def _expected_fields(
    *,
    fields_by_name: dict[str, CustomFieldDef],
    coverage_by_field_id: dict[int, FieldCoverage],
    field_amount: str,
    field_gst: str,
    field_work_use: str,
) -> tuple[ExpectedFieldInfo, ...]:
    """Check the expected field names by exact name against the live fields."""
    result: list[ExpectedFieldInfo] = []
    for name, required in (
        (field_amount, True),
        (field_gst, False),
        (field_work_use, False),
    ):
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

    lines.append(f"Deduction tag {report.tag_name!r}:")
    if not report.tag_matches:
        lines.append("  no matching tags — the FY deduction set is empty")
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

    if report.archived_first_segments or report.archived_second_segments:
        lines.append("")
        lines.append(
            "archived_file_name segments across the FY deduction documents (console only):"
        )
        lines.append("  first segment:")
        lines.extend(_segment_lines(report.archived_first_segments))
        lines.append("  second segment:")
        lines.extend(_segment_lines(report.archived_second_segments))

    ibkr = report.ibkr
    lines.append("")
    lines.append("IBKR identifiers:")
    if ibkr.tag_exists:
        lines.append(f"  tag {ibkr.tag_name!r}: exists with {ibkr.tag_document_count} document(s)")
    else:
        lines.append(f"  tag {ibkr.tag_name!r}: missing")
    if ibkr.correspondent_exists:
        lines.append(
            f"  correspondent {ibkr.correspondent_name!r}: exists with "
            f"{ibkr.correspondent_document_count} document(s)"
        )
    else:
        lines.append(f"  correspondent {ibkr.correspondent_name!r}: missing")
    lines.append(
        "  document types matching 'trade confirmation': "
        f"{_names_or_none(ibkr.trade_confirmation_types)}"
    )
    lines.append(
        "  document types matching 'realized gains summary': "
        f"{_names_or_none(ibkr.realized_gains_types)}"
    )
    lines.append(
        f"  in the FY deduction set: {ibkr.correspondent_without_tag} document(s) with "
        f"correspondent {ibkr.correspondent_name!r} but without tag {ibkr.tag_name!r}"
    )
    lines.append(
        f"  in the FY deduction set: {ibkr.tag_with_other_correspondent} document(s) "
        f"with tag {ibkr.tag_name!r} but a different correspondent"
    )

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
    lines.append("  Note: Settings.field_date and Settings.field_supplier are None by")
    lines.append("  default; the row date falls back to the document `created` date and")
    lines.append("  the supplier to the document correspondent.")

    lines.append("")
    lines.append(f"Paperless API version: {report.api_version or 'unknown'}")
    lines.append(f"Paperless server version: {report.server_version or 'unknown'}")
    return "\n".join(lines)


def _segment_lines(segments: Sequence[SegmentCount]) -> list[str]:
    return [f"    {segment.segment!r} x{segment.count}" for segment in segments]


def _names_or_none(names: Sequence[str]) -> str:
    return ", ".join(repr(name) for name in names) if names else "none"


def dump_json(report: AuditReport) -> str:
    """Deterministic JSON text of the audit; counts and metadata only.

    This is the only sanctioned committed data path: no document title, id,
    date, custom-field value, ``archived_file_name`` string or amount is ever
    part of the payload (see the module docstring for the full rule).
    """
    payload: dict[str, Any] = {
        "tag": report.tag_name,
        "fy_start": report.fy_start,
        "fy_end": report.fy_end,
        "deduction_document_count": report.deduction_document_count,
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
            "tag": report.ibkr.tag_name,
            "tag_exists": report.ibkr.tag_exists,
            "tag_document_count": report.ibkr.tag_document_count,
            "correspondent": report.ibkr.correspondent_name,
            "correspondent_exists": report.ibkr.correspondent_exists,
            "correspondent_document_count": report.ibkr.correspondent_document_count,
            "trade_confirmation_types": list(report.ibkr.trade_confirmation_types),
            "realized_gains_types": list(report.ibkr.realized_gains_types),
            "correspondent_without_tag": report.ibkr.correspondent_without_tag,
            "tag_with_other_correspondent": report.ibkr.tag_with_other_correspondent,
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
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
