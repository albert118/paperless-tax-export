"""High-level export run: resolve, select, classify, then write the CSVs.

``run_export`` is the run layer behind the ``run`` subcommand. Every check
(startup resolution, the count guard and the investments selection) completes before
anything touches disk: the output directory is only created once the
classifications are proven complete, so a failing run leaves no partial files
behind. ``manifest.json`` is rendered from the bytes of the other outputs and
written last. ``dry_run`` runs the same guards and rendering but writes nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from paperless_tax_export.bills import (
    BillRow,
    bills_also_deductions,
    build_bill_rows,
    render_bills_bytes,
)
from paperless_tax_export.classify import Classification, ClassifyContext, classify_documents
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.csv_output import (
    DEDUCTIONS_COLUMNS,
    REVIEW_COLUMNS,
    InvestmentRow,
    render_csv_bytes,
    render_investments_csv_bytes,
)
from paperless_tax_export.errors import CountGuardError
from paperless_tax_export.estimate import (
    Estimate,
    Rates,
    build_estimate,
    load_rates,
    render_estimate_bytes,
)
from paperless_tax_export.manifest import (
    BILLS_FILE,
    DEDUCTIONS_FILE,
    ESTIMATE_FILE,
    INVESTMENTS_FILE,
    MANIFEST_FILE,
    REVIEW_FILE,
    ManifestCounts,
    PlannedFile,
    render_manifest,
)
from paperless_tax_export.models import Document
from paperless_tax_export.parsing import ReviewCode
from paperless_tax_export.payslips import (
    build_payslip_section,
    read_payslips,
)
from paperless_tax_export.selection import (
    resolve_fields,
    resolve_tag,
    select_documents,
    select_documents_by_type,
    select_ibkr_documents,
)
from paperless_tax_export.settings import Settings
from paperless_tax_export.wfh_estimate import make_bills_section


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Counts and warnings of one successful export run.

    ``files`` lists every output, ``manifest.json`` last. They exist on disk
    unless ``dry_run`` is true. ``warnings`` are non-fatal findings in a fixed,
    deterministic order; the CLI prints them once.
    """

    selected_count: int
    accepted_count: int
    review_count: int
    investments_count: int
    output_dir: Path
    tagged_total: int = 0
    out_of_fy_count: int = 0
    files: tuple[PlannedFile, ...] = ()
    dry_run: bool = False
    warnings: tuple[str, ...] = ()
    estimate_deductions_aud: Decimal = Decimal(0)
    estimate_pending_aud: Decimal = Decimal(0)
    estimated_saving: Decimal | None = None
    bills_count: int = 0
    bills_counted: int = 0
    bills_aud: Decimal = Decimal(0)
    payslip_documents: int = 0
    payslips_usable: int = 0
    payslips_unusable: int = 0
    payslip_ceiling: Decimal | None = None


def run_export(
    settings: Settings,
    client: PaperlessClient,
    *,
    output: Path,
    dry_run: bool = False,
) -> ExportResult:
    """Run one classification/export pass against the live Paperless instance.

    The deduction tag is resolved strictly (exactly one tag with that exact
    name), ``field_amount`` is a required custom field and every other
    configured field is optional (a missing one maps to ``None``). Context
    maps come from one fetch each of tags, correspondents and document types.

    ``settings.expected_count``, when set, must equal the number of documents
    selected for the FY. That number is the server-reported FY-scoped count:
    the client's paging check has already proven the accumulated results equal
    the ``count`` Paperless reported. A mismatch raises ``CountGuardError``
    before anything is written. With ``dry_run`` every guard runs and the
    result is returned, but the output directory is not created.
    """
    rates = Rates() if settings.rates_file is None else load_rates(settings.rates_file)
    deduction_tag = resolve_tag(client, settings.deduction_tag)

    required = [settings.field_amount] if settings.field_amount is not None else []
    optional = [name for name in (settings.field_date, settings.field_supplier) if name is not None]
    optional.extend(settings.payslip_fields.values())
    resolved = resolve_fields(client, required=required, optional=optional)
    amount_field = resolved[settings.field_amount] if settings.field_amount is not None else None
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
        date_field=date_field,
        supplier_field=supplier_field,
        correspondents={correspondent.id: correspondent.name for correspondent in correspondents},
        document_types={type_def.id: type_def.name for type_def in document_types},
        tags={tag.id: tag.name for tag in all_tags},
        income_tag_ids=frozenset(tag.id for tag in all_tags if tag.name in income_names),
        asset_reviewed_tag_ids=frozenset(
            tag.id
            for tag in all_tags
            if settings.asset_reviewed_tag is not None and tag.name == settings.asset_reviewed_tag
        ),
    )

    selection = select_documents(
        client,
        tag_id=deduction_tag.id,
        fy_start=settings.fy_start,
        fy_end=settings.fy_end,
    )
    documents = selection.documents
    expected = settings.expected_count
    if expected is not None and expected != len(documents):
        raise CountGuardError(
            f"expected {expected} documents in the financial year but Paperless "
            f"reported {len(documents)} for tag {settings.deduction_tag!r}"
        )
    classification = classify_documents(documents, context)

    selected = len(documents)
    if len(classification.accepted) + len(classification.review) != selected:
        raise CountGuardError(
            "classifier returned "
            f"{len(classification.accepted) + len(classification.review)} rows for "
            f"{selected} selected documents"
        )

    investments_found, investment_documents = select_ibkr_documents(
        client, settings.ibkr_correspondent, settings.fy_start, settings.fy_end
    )
    bills_found, bill_documents = select_documents_by_type(
        client, document_types, settings.bill_document_type, settings.fy_start, settings.fy_end
    )
    bill_rows = build_bill_rows(bill_documents, context, deduction_tag.id)
    payslips_found, payslip_documents = select_documents_by_type(
        client, document_types, settings.payslip_document_type, settings.fy_start, settings.fy_end
    )
    payslips = build_payslip_section(
        read_payslips(payslips_found, payslip_documents, settings, resolved),
        settings,
        settings.wfh.hours,
    )
    estimate = build_estimate(
        classification.accepted,
        classification.review,
        rates.by_currency,
        settings.marginal_rate,
        make_bills_section(bill_rows, rates.by_currency, settings.wfh),
        payslips.rows,
    )
    warnings = (
        *_investments_warnings(
            settings.ibkr_correspondent, investments_found, documents, investment_documents
        ),
        *_asset_reviewed_tag_warnings(settings, context, classification),
        *_missing_rate_warnings(estimate),
        *_bills_warnings(settings, bills_found, bill_rows, context.tags.values()),
        *payslips.warnings,
    )
    investment_rows = tuple(_investment_row(document, context) for document in investment_documents)

    # Every check has passed: render every file in memory, manifest last.
    deductions_rows = tuple(classification.accepted)
    review_rows = tuple(classification.review)
    csv_files = (
        PlannedFile(
            DEDUCTIONS_FILE,
            render_csv_bytes(deductions_rows, DEDUCTIONS_COLUMNS),
            len(deductions_rows),
        ),
        PlannedFile(REVIEW_FILE, render_csv_bytes(review_rows, REVIEW_COLUMNS), len(review_rows)),
        PlannedFile(
            INVESTMENTS_FILE, render_investments_csv_bytes(investment_rows), len(investment_rows)
        ),
        PlannedFile(BILLS_FILE, render_bills_bytes(bill_rows), len(bill_rows)),
        PlannedFile(ESTIMATE_FILE, render_estimate_bytes(estimate), len(estimate.rows)),
    )
    manifest_bytes = render_manifest(
        settings,
        counts=ManifestCounts(
            tagged_total=selection.total_tagged,
            out_of_fy=selection.out_of_fy_count,
            selected_in_fy=selected,
            accepted=len(deductions_rows),
            review=len(review_rows),
            investments_count=len(investment_rows),
            bills_count=len(bill_rows),
            payslips_count=payslips.usable_count,
        ),
        files=csv_files,
        server_version=client.server_version,
        api_version=client.api_version,
        rates_file_sha256=rates.sha256,
    )
    manifest_file = PlannedFile(MANIFEST_FILE, manifest_bytes, rows=0)
    if not dry_run:
        _write_files(output, csv_files, manifest_file)

    return ExportResult(
        selected_count=selected,
        accepted_count=len(deductions_rows),
        review_count=len(review_rows),
        investments_count=len(investment_rows),
        output_dir=output,
        tagged_total=selection.total_tagged,
        out_of_fy_count=selection.out_of_fy_count,
        files=(*csv_files, manifest_file),
        dry_run=dry_run,
        warnings=warnings,
        estimate_deductions_aud=estimate.deductions_aud,
        estimate_pending_aud=estimate.pending_aud,
        estimated_saving=estimate.tax_saving,
        bills_count=len(bill_rows),
        bills_counted=sum(1 for row in bill_rows if row.counts_toward_wfh),
        bills_aud=estimate.bills_aud,
        payslip_documents=payslips.document_count,
        payslips_usable=payslips.usable_count,
        payslips_unusable=payslips.unusable_count,
        payslip_ceiling=payslips.ceiling,
    )


def _write_files(
    output: Path, csv_files: Sequence[PlannedFile], manifest_file: PlannedFile
) -> None:
    """Write the CSVs, then the manifest, so a failure never leaves a fresh manifest.

    A manifest from an earlier run is removed first: it would otherwise sit
    next to CSVs it no longer describes if a write fails part-way.
    """
    output.mkdir(parents=True, exist_ok=True)
    (output / manifest_file.name).unlink(missing_ok=True)
    for file in csv_files:
        (output / file.name).write_bytes(file.content)
    (output / manifest_file.name).write_bytes(manifest_file.content)


def _investment_row(document: Document, context: ClassifyContext) -> InvestmentRow:
    document_type = document.document_type
    correspondent = document.correspondent
    return InvestmentRow(
        document_id=document.id,
        title=document.title,
        created=document.created[:10],  # Paperless timestamps are ISO 8601
        document_type=None if document_type is None else context.document_types.get(document_type),
        correspondent=None if correspondent is None else context.correspondents.get(correspondent),
    )


def _investments_warnings(
    correspondent_name: str,
    found: bool,
    deduction_documents: Sequence[Document],
    investment_documents: Sequence[Document],
) -> tuple[str, ...]:
    """Non-fatal findings about the investments index, one line each, in stable order."""
    if not found:
        return (
            f"correspondent {correspondent_name!r} not found in Paperless; "
            "investments.csv has no rows",
        )
    deduction_ids = {document.id for document in deduction_documents}
    both = sorted(document.id for document in investment_documents if document.id in deduction_ids)
    return tuple(
        f"document {document_id} carries the deduction tag and the "
        f"{correspondent_name!r} correspondent; it appears in both outputs"
        for document_id in both
    )


def _asset_reviewed_tag_warnings(
    settings: Settings, context: ClassifyContext, classification: Classification
) -> tuple[str, ...]:
    """Warn once when AMOUNT_OVER_300 rows exist but the reviewed tag cannot clear them."""
    tag_name = settings.asset_reviewed_tag
    if tag_name is None or context.asset_reviewed_tag_ids:
        return ()
    if not any(ReviewCode.AMOUNT_OVER_300 in row.reason_codes for row in classification.review):
        return ()
    return (
        f"asset-reviewed tag {tag_name!r} not found in Paperless; "
        "AMOUNT_OVER_300 rows cannot be cleared until it exists",
    )


def _missing_rate_warnings(estimate: Estimate) -> tuple[str, ...]:
    """Warn once, listing every currency with amounts but no rate (``run --rates FILE``)."""
    if not estimate.unconverted_currencies:
        return ()
    return (
        f"no exchange rate for {', '.join(estimate.unconverted_currencies)}; those amounts are "
        "excluded from the AUD figures in estimate.csv (supply them with run --rates FILE)",
    )


def _bills_warnings(
    settings: Settings,
    found: bool,
    bill_rows: Sequence[BillRow],
    tag_names: Collection[str],
) -> tuple[str, ...]:
    """Non-fatal findings about the bills, in stable order.

    A missing type, then kind tags missing from Paperless (only when bills are listed), then
    one line per bill that also carries the deduction tag.
    """
    missing = (
        ()
        if found
        else (
            f"document type {settings.bill_document_type!r} not found in Paperless; "
            "bills.csv has no rows",
        )
    )
    absent = sorted(set(settings.bill_kind_tags.values()).difference(tag_names))
    missing_tags = (
        (
            f"bill kind tags not found in Paperless: {', '.join(absent)}; names match "
            "case-sensitively and bills cannot get those kinds",
        )
        if bill_rows and absent
        else ()
    )
    overlaps = tuple(
        f"document {document_id} is a bill and carries the deduction tag; it stays in "
        "deductions.csv or review.csv and is excluded from the bill totals and WFH figures "
        "in estimate.csv"
        for document_id in bills_also_deductions(bill_rows)
    )
    return (*missing, *missing_tags, *overlaps)


def unconfigured_field_notes(settings: Settings) -> tuple[str, ...]:
    """The settings-level gaps, noted once per run (never per row or per code).

    These sentences are fixed, deterministic, and order-stable; the CLI prints
    them once to stdout so the owner knows which columns are intentionally
    blank.
    """
    notes: list[str] = []
    if settings.field_amount is None:
        notes.append("amount field not configured; every row gets MISSING_AMOUNT")
    if settings.field_date is None:
        notes.append("date field not configured; row date falls back to the document created date")
    if settings.field_supplier is None:
        notes.append(
            "supplier_name is the document correspondent; no separate supplier custom field"
            " is configured (the default)"
        )
    return tuple(notes)
