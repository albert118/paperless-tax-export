"""Command-line interface for paperless-tax-export.

The tool is read-only toward Paperless: it only ever reads documents with GET
requests and never writes back. ``main`` maps every ``TaxExportError`` to its
``exit_code`` so a failure never escapes as a traceback; argparse usage errors
still exit 2 with ``SystemExit``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from paperless_tax_export import __version__
from paperless_tax_export.audit import dump_json, render, run_audit
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.env_config import (
    EnvSources,
    build_sources,
    resolve_base_url,
    resolve_marginal_rate,
    resolve_token,
    resolve_wfh_inputs,
)
from paperless_tax_export.errors import EXIT_NETWORK, EXIT_OK, TaxExportError, VerificationError
from paperless_tax_export.export import ExportResult, run_export, unconfigured_field_notes
from paperless_tax_export.settings import Settings
from paperless_tax_export.verify import verify_directory


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line arguments and run the requested subcommand."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(parser, args)
    except TaxExportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


def _build_parser() -> argparse.ArgumentParser:
    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="paperless-tax-export",
        description=(
            "Export a financial year of Paperless-ngx documents to"
            " accountant-ready CSVs (read-only)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser(
        "audit",
        help="audit the live Paperless instance against the export settings",
        description=(
            "Diagnose how the live Paperless instance lines up with the export"
            " settings. The audit is not a gate: findings (a missing tag, zero"
            " documents, missing fields) are reported and the exit code stays 0"
            " unless nothing at all came back and every optional read failed"
            " (then it is 3, so a bad token or unreachable server is never a"
            " successful audit)."
        ),
    )
    audit.add_argument(
        "--base-url",
        default=None,
        help=(f"Paperless base URL; overrides PAPERLESS_BASE_URL (default: {settings.base_url})"),
    )
    audit.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help=(
            "file holding the Paperless API token; overrides PAPERLESS_TOKEN."
            " There is no --token flag, so the token never appears in the process"
            f" list (default: PAPERLESS_TOKEN, else {settings.token_file})"
        ),
    )
    audit.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "dotenv file supplying PAPERLESS_TOKEN and PAPERLESS_BASE_URL; real"
            " environment variables win over its entries. Equivalent to setting"
            " PAPERLESS_ENV_FILE. A .env in the working directory is never loaded"
        ),
    )
    audit.add_argument(
        "--tag",
        default=settings.deduction_tag,
        help=f"deduction tag to audit (default: {settings.deduction_tag})",
    )
    audit.add_argument(
        "--fy-start",
        default=settings.fy_start,
        help=f"financial-year start, ISO date (default: {settings.fy_start})",
    )
    audit.add_argument(
        "--fy-end",
        default=settings.fy_end,
        help=f"financial-year end, ISO date (default: {settings.fy_end})",
    )
    audit.add_argument(
        "--tax-info-tag",
        default=settings.tax_info_tag,
        help=(
            "general tax-metadata tag name, reported with a caveat that it is"
            f" not IBKR-specific (default: {settings.tax_info_tag})"
        ),
    )
    audit.add_argument(
        "--ibkr-correspondent",
        default=settings.ibkr_correspondent,
        help=f"IBKR correspondent name (default: {settings.ibkr_correspondent})",
    )
    audit.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="directory the audit JSON is written into (default: output/)",
    )
    audit.add_argument(
        "--json-name",
        default="audit.json",
        help="audit JSON file name inside --output (default: audit.json)",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="classify one financial year and write the export CSVs",
        description=(
            "Classify the deduction-tagged documents for one financial year,"
            " write deductions.csv (rows that pass every check) and review.csv"
            " (rows with reason codes a human still has to decide on), plus"
            " investments.csv (the investing correspondent's documents for the year),"
            " bills.csv (the utilities and bills of the year) and"
            " estimate.csv (an estimate of total deductions, not tax advice)."
        ),
    )
    run_parser.add_argument(
        "--base-url",
        default=None,
        help=(f"Paperless base URL; overrides PAPERLESS_BASE_URL (default: {settings.base_url})"),
    )
    run_parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help=(
            "file holding the Paperless API token; overrides PAPERLESS_TOKEN."
            " There is no --token flag, so the token never appears in the process"
            f" list (default: PAPERLESS_TOKEN, else {settings.token_file})"
        ),
    )
    run_parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "dotenv file supplying PAPERLESS_TOKEN and PAPERLESS_BASE_URL; real"
            " environment variables win over its entries. Equivalent to setting"
            " PAPERLESS_ENV_FILE. A .env in the working directory is never loaded"
        ),
    )
    run_parser.add_argument(
        "--tag",
        default=settings.deduction_tag,
        help=f"deduction tag to select (default: {settings.deduction_tag})",
    )
    run_parser.add_argument(
        "--asset-reviewed-tag",
        default=settings.asset_reviewed_tag,
        help=(
            "Paperless tag that marks an over-300 AUD document as reviewed and claimed in"
            f" full, clearing AMOUNT_OVER_300 (default: {settings.asset_reviewed_tag})"
        ),
    )
    run_parser.add_argument(
        "--fy-start",
        default=settings.fy_start,
        help=f"financial-year start, ISO date (default: {settings.fy_start})",
    )
    run_parser.add_argument(
        "--fy-end",
        default=settings.fy_end,
        help=f"financial-year end, ISO date (default: {settings.fy_end})",
    )
    run_parser.add_argument(
        "--ibkr-correspondent",
        default=settings.ibkr_correspondent,
        help=f"IBKR correspondent name (default: {settings.ibkr_correspondent})",
    )
    run_parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="directory the output files are written into (default: output/)",
    )
    run_parser.add_argument(
        "--expected-count",
        type=int,
        default=settings.expected_count,
        help=(
            "number of documents the Paperless admin view shows for the tag with the"
            " financial-year created filter applied; a different count exits non-zero"
            " and writes nothing"
        ),
    )
    run_parser.add_argument(
        "--rates",
        type=Path,
        default=None,
        help=(
            "CSV file (header currency,rate_to_aud) of exchange rates to AUD used to convert"
            " foreign amounts in estimate.csv; without it foreign amounts are left out of the"
            " AUD figures. The marginal tax rate comes from TAX_MARGINAL_RATE, not a flag"
        ),
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every guard and print the counts and planned files, but write nothing",
    )
    output_default: Path = run_parser.get_default("output")
    verify_parser = subparsers.add_parser(
        "verify",
        help="check a finished output directory against its manifest, offline",
        description=(
            "Recompute the SHA-256 and data-row count of every file listed in"
            " manifest.json and compare them with the manifest, then check the"
            " manifest's counts against the files. Needs no token and no network."
            " It catches accidental edits and a corrupted copy; it does not prove"
            " the files match Paperless and it is not tamper-proof. Exit 0 when"
            " everything matches, 4 on any mismatch or missing listed file, 2 when"
            " manifest.json is missing, unreadable or malformed."
        ),
    )
    verify_parser.add_argument(
        "--output",
        type=Path,
        default=output_default,
        help=f"directory holding manifest.json and the output files (default: {output_default}/)",
    )
    return parser


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command == "audit":
        sources = build_sources(args.env_file, os.environ)
        settings = _settings_from_args(args, sources)
        return _run_audit_command(
            settings, resolve_token(args.token_file, settings.token_file, sources), args
        )
    if args.command == "run":
        sources = build_sources(args.env_file, os.environ)
        settings = _settings_from_run_args(args, sources)
        return _run_export_command(
            settings, resolve_token(args.token_file, settings.token_file, sources), args
        )
    if args.command == "verify":
        return _run_verify_command(args)
    parser.error(f"unknown command {args.command!r}")


def _settings_from_args(args: argparse.Namespace, sources: EnvSources) -> Settings:
    """Validate the audit flags by building a frozen :class:`Settings`."""
    return Settings(
        base_url=resolve_base_url(args.base_url, Settings().base_url, sources),
        deduction_tag=args.tag,
        tax_info_tag=args.tax_info_tag,
        ibkr_correspondent=args.ibkr_correspondent,
        asset_reviewed_tag=None,
        fy_start=args.fy_start,
        fy_end=args.fy_end,
    )


def _settings_from_run_args(args: argparse.Namespace, sources: EnvSources) -> Settings:
    """Validate the run flags by building a frozen :class:`Settings`."""
    return Settings(
        base_url=resolve_base_url(args.base_url, Settings().base_url, sources),
        deduction_tag=args.tag,
        ibkr_correspondent=args.ibkr_correspondent,
        asset_reviewed_tag=args.asset_reviewed_tag,
        fy_start=args.fy_start,
        fy_end=args.fy_end,
        expected_count=args.expected_count,
        marginal_rate=resolve_marginal_rate(sources),
        rates_file=args.rates,
        wfh=resolve_wfh_inputs(sources),
    )


def _run_audit_command(settings: Settings, token: str, args: argparse.Namespace) -> int:
    with PaperlessClient(
        settings.base_url,
        token,
        page_size=settings.page_size,
        timeout=settings.timeout_seconds,
    ) as client:
        report = run_audit(
            client,
            tag=settings.deduction_tag,
            fy_start=settings.fy_start,
            fy_end=settings.fy_end,
            tax_info_tag=settings.tax_info_tag,
            ibkr_correspondent=settings.ibkr_correspondent,
            field_amount=settings.field_amount,
            field_gst=settings.field_gst,
            field_work_use=settings.field_work_use,
            base_url=settings.base_url,
        )
    print(render(report))
    if args.json_name:
        json_path = args.output / args.json_name
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(dump_json(report), encoding="utf-8")
    if (
        report.unavailable
        and report.tags_visible == 0
        and report.correspondents_visible == 0
        and report.document_types_visible == 0
        and report.storage_paths_visible == 0
        and not report.custom_fields
    ):
        # Every optional read failed and nothing at all came back — not even a
        # custom field: a bad token or an unreachable server must not look
        # like a successful audit.
        return EXIT_NETWORK
    return EXIT_OK


def _run_export_command(settings: Settings, token: str, args: argparse.Namespace) -> int:
    """Run the export and print the short summary (counts and once-notes)."""
    with PaperlessClient(
        settings.base_url,
        token,
        page_size=settings.page_size,
        timeout=settings.timeout_seconds,
    ) as client:
        result = run_export(settings, client, output=args.output, dry_run=args.dry_run)
    for note in unconfigured_field_notes(settings):
        print(f"note: {note}")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(
        f"{result.accepted_count} accepted, {result.review_count} review "
        f"({result.selected_count} deduction documents)"
    )
    print(_estimate_summary(result))
    if result.bills_count:
        print(
            f"bills: {result.bills_count} listed, {result.bills_counted} counted, "
            f"AUD {result.bills_aud:.2f}"
        )
    if result.payslip_documents:
        print(_payslips_summary(result))
    print(
        f"tagged {result.tagged_total}, out of FY {result.out_of_fy_count}, "
        f"selected in FY {result.selected_count}, investments {result.investments_count}"
    )
    verb = "would write" if result.dry_run else "written to"
    for file in result.files:
        print(f"{file.name} {verb} {result.output_dir / file.name}")
    if result.dry_run:
        print("dry run: nothing was written")
    return EXIT_OK


def _estimate_summary(result: ExportResult) -> str:
    line = (
        f"estimate: deductions AUD {result.estimate_deductions_aud:.2f}, "
        f"pending AUD {result.estimate_pending_aud:.2f}"
    )
    if result.estimated_saving is not None:
        line += f", estimated saving AUD {result.estimated_saving:.2f}"
    return line


def _payslips_summary(result: ExportResult) -> str:
    ceiling = "not computed" if result.payslip_ceiling is None else f"{result.payslip_ceiling:.2f}"
    return (
        f"payslips: {result.payslips_usable} usable, {result.payslips_unusable} unusable; "
        f"WFH hours ceiling {ceiling}"
    )


def _run_verify_command(args: argparse.Namespace) -> int:
    """Print the verification report; a failed check exits 4 through ``VerificationError``."""
    report = verify_directory(args.output)
    for line in report.lines:
        print(line)
    if not report.ok:
        raise VerificationError(f"{report.problems} verification problem(s) in {args.output}")
    return EXIT_OK
