"""High-level export run: resolve, select, classify, then write both CSVs.

``run_export`` is the run layer behind the ``run`` subcommand. Every check
(startup resolution and the count guard) completes before anything touches
disk: both CSV files are rendered in memory and the output directory is only
created once the classifications are proven complete, so a failing run leaves
no partial files behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from paperless_tax_export.classify import ClassifyContext, classify_documents
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.csv_output import DEDUCTIONS_COLUMNS, REVIEW_COLUMNS, write_csv
from paperless_tax_export.errors import CountGuardError
from paperless_tax_export.selection import resolve_fields, resolve_tag, select_documents
from paperless_tax_export.settings import Settings


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Counts of one successful export run; the files already exist."""

    selected_count: int
    accepted_count: int
    review_count: int
    output_dir: Path


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

    # Every check has passed: only now render and write the files.
    deductions_rows = tuple(classification.accepted)
    review_rows = tuple(classification.review)
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "deductions.csv", rows=deductions_rows, columns=DEDUCTIONS_COLUMNS)
    write_csv(output / "review.csv", rows=review_rows, columns=REVIEW_COLUMNS)

    return ExportResult(
        selected_count=selected,
        accepted_count=len(deductions_rows),
        review_count=len(review_rows),
        output_dir=output,
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
