"""High-level export run: resolve, select, classify, then write the CSVs.

``run_export`` is the run layer behind the ``run`` subcommand. Every check
(startup resolution, the count guard and the IBKR selection) completes before
anything touches disk: the output directory is only created once the
classifications are proven complete, so a failing run leaves no partial files
behind.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from paperless_tax_export.classify import ClassifyContext, classify_documents
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.csv_output import (
    DEDUCTIONS_COLUMNS,
    REVIEW_COLUMNS,
    IbkrRow,
    write_csv,
    write_ibkr_csv,
)
from paperless_tax_export.errors import CountGuardError
from paperless_tax_export.models import Document
from paperless_tax_export.selection import (
    resolve_fields,
    resolve_tag,
    select_documents,
    select_ibkr_documents,
)
from paperless_tax_export.settings import Settings


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Counts and warnings of one successful export run; the files already exist.

    ``warnings`` are non-fatal findings in a fixed, deterministic order; the
    CLI prints them once.
    """

    selected_count: int
    accepted_count: int
    review_count: int
    ibkr_count: int
    output_dir: Path
    warnings: tuple[str, ...] = ()


def run_export(settings: Settings, client: PaperlessClient, *, output: Path) -> ExportResult:
    """Run one classification/export pass against the live Paperless instance.

    The deduction tag is resolved strictly (exactly one tag with that exact
    name), ``field_amount`` is a required custom field and every other
    configured field is optional (a missing one maps to ``None``). Context
    maps come from one fetch each of tags, correspondents and document types.
    """
    deduction_tag = resolve_tag(client, settings.deduction_tag)

    required = [settings.field_amount] if settings.field_amount is not None else []
    optional = [
        name
        for name in (
            settings.field_gst,
            settings.field_work_use,
            settings.field_date,
            settings.field_supplier,
        )
        if name is not None
    ]
    resolved = resolve_fields(client, required=required, optional=optional)
    amount_field = resolved[settings.field_amount] if settings.field_amount is not None else None
    gst_field = resolved[settings.field_gst] if settings.field_gst is not None else None
    work_use_field = (
        resolved[settings.field_work_use] if settings.field_work_use is not None else None
    )
    date_field = resolved[settings.field_date] if settings.field_date is not None else None
    supplier_field = (
        resolved[settings.field_supplier] if settings.field_supplier is not None else None
    )

    all_tags = client.list_tags()
    correspondents = client.list_correspondents()
    document_types = client.list_document_types()

    income_names = set(settings.income_tags)
    context = ClassifyContext(
        settings=settings,
        amount_field=amount_field,
        gst_field=gst_field,
        work_use_field=work_use_field,
        date_field=date_field,
        supplier_field=supplier_field,
        correspondents={correspondent.id: correspondent.name for correspondent in correspondents},
        document_types={type_def.id: type_def.name for type_def in document_types},
        tags={tag.id: tag.name for tag in all_tags},
        income_tag_ids=frozenset(tag.id for tag in all_tags if tag.name in income_names),
    )

    selection = select_documents(
        client,
        tag_id=deduction_tag.id,
        fy_start=settings.fy_start,
        fy_end=settings.fy_end,
    )
    documents = selection.documents
    classification = classify_documents(documents, context)

    selected = len(documents)
    if len(classification.accepted) + len(classification.review) != selected:
        raise CountGuardError(
            "classifier returned "
            f"{len(classification.accepted) + len(classification.review)} rows for "
            f"{selected} selected documents"
        )

    ibkr_found, ibkr_documents = select_ibkr_documents(
        client, settings.ibkr_correspondent, settings.fy_start, settings.fy_end
    )
    warnings = _ibkr_warnings(settings.ibkr_correspondent, ibkr_found, documents, ibkr_documents)
    ibkr_rows = tuple(_ibkr_row(document, context) for document in ibkr_documents)

    # Every check has passed: only now render and write the files.
    deductions_rows = tuple(classification.accepted)
    review_rows = tuple(classification.review)
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "deductions.csv", rows=deductions_rows, columns=DEDUCTIONS_COLUMNS)
    write_csv(output / "review.csv", rows=review_rows, columns=REVIEW_COLUMNS)
    write_ibkr_csv(output / "ibkr-documents.csv", rows=ibkr_rows)

    return ExportResult(
        selected_count=selected,
        accepted_count=len(deductions_rows),
        review_count=len(review_rows),
        ibkr_count=len(ibkr_rows),
        output_dir=output,
        warnings=warnings,
    )


def _ibkr_row(document: Document, context: ClassifyContext) -> IbkrRow:
    document_type = document.document_type
    correspondent = document.correspondent
    return IbkrRow(
        document_id=document.id,
        title=document.title,
        created=document.created[:10],  # Paperless timestamps are ISO 8601
        document_type=None if document_type is None else context.document_types.get(document_type),
        correspondent=None if correspondent is None else context.correspondents.get(correspondent),
        tags=tuple(context.tags[tag_id] for tag_id in document.tags if tag_id in context.tags),
    )


def _ibkr_warnings(
    correspondent_name: str,
    found: bool,
    deduction_documents: Sequence[Document],
    ibkr_documents: Sequence[Document],
) -> tuple[str, ...]:
    """Non-fatal findings about the IBKR index, one line each, in stable order."""
    if not found:
        return (
            f"correspondent {correspondent_name!r} not found in Paperless; "
            "ibkr-documents.csv has no rows",
        )
    deduction_ids = {document.id for document in deduction_documents}
    both = sorted(document.id for document in ibkr_documents if document.id in deduction_ids)
    return tuple(
        f"document {document_id} carries the deduction tag and the "
        f"{correspondent_name!r} correspondent; it appears in both outputs"
        for document_id in both
    )


def unconfigured_field_notes(settings: Settings) -> tuple[str, ...]:
    """The settings-level gaps, noted once per run (never per row or per code).

    These sentences are fixed, deterministic, and order-stable; the CLI prints
    them once to stdout so the owner knows which columns are intentionally
    blank.
    """
    notes: list[str] = []
    if settings.field_amount is None:
        notes.append("amount field not configured; every row gets MISSING_AMOUNT")
    if settings.field_gst is None:
        notes.append("GST field not configured; GST column left blank")
    if settings.field_work_use is None:
        notes.append("work-use field not configured; work-use column left blank")
    if settings.field_date is None:
        notes.append("date field not configured; row date falls back to the document created date")
    if settings.field_supplier is None:
        notes.append(
            "supplier field not configured; supplier falls back to the document correspondent"
        )
    return tuple(notes)
