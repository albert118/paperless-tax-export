"""Command-line interface for paperless-tax-export.

The tool is read-only toward Paperless: it only ever reads documents with GET
requests and never writes back. ``main`` maps every ``TaxExportError`` to its
``exit_code`` so a failure never escapes as a traceback; argparse usage errors
still exit 2 with ``SystemExit``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from paperless_tax_export import __version__
from paperless_tax_export.audit import dump_json, render, run_audit
from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.errors import EXIT_OK, SettingsError, TaxExportError
from paperless_tax_export.export import run_export, unconfigured_field_notes
from paperless_tax_export.settings import Settings


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
            " documents, missing fields) are reported and the exit code stays 0."
        ),
    )
    audit.add_argument(
        "--base-url",
        default=settings.base_url,
        help=(f"Paperless base URL (default: {settings.base_url})"),
    )
    audit.add_argument(
        "--token-file",
        type=Path,
        default=settings.token_file,
        help=(
            "file holding the Paperless API token; the token is read from this"
            " file only — there is no --token flag or environment variable"
            f" (default: {settings.token_file})"
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
        "--ibkr-tag",
        default=settings.ibkr_tag,
        help=f"IBKR tag name (default: {settings.ibkr_tag})",
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
            " (rows with reason codes a human still has to decide on)."
        ),
    )
    run_parser.add_argument(
        "--base-url",
        default=settings.base_url,
        help=(f"Paperless base URL (default: {settings.base_url})"),
    )
    run_parser.add_argument(
        "--token-file",
        type=Path,
        default=settings.token_file,
        help=(
            "file holding the Paperless API token; the token is read from this"
            " file only — there is no --token flag or environment variable"
            f" (default: {settings.token_file})"
        ),
    )
    run_parser.add_argument(
        "--tag",
        default=settings.deduction_tag,
        help=f"deduction tag to select (default: {settings.deduction_tag})",
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
        "--output",
        type=Path,
        default=Path("output"),
        help="directory the CSV files are written into (default: output/)",
    )
    return parser


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.command == "audit":
        return _run_audit_command(_settings_from_args(args), args)
    if args.command == "run":
        return _run_export_command(_settings_from_run_args(args), args)
    parser.error(f"unknown command {args.command!r}")


def _settings_from_args(args: argparse.Namespace) -> Settings:
    """Validate the audit flags by building a frozen :class:`Settings`."""
    return Settings(
        base_url=args.base_url,
        token_file=args.token_file,
        deduction_tag=args.tag,
        ibkr_tag=args.ibkr_tag,
        ibkr_correspondent=args.ibkr_correspondent,
        fy_start=args.fy_start,
        fy_end=args.fy_end,
    )


def _settings_from_run_args(args: argparse.Namespace) -> Settings:
    """Validate the run flags by building a frozen :class:`Settings`."""
    return Settings(
        base_url=args.base_url,
        token_file=args.token_file,
        deduction_tag=args.tag,
        fy_start=args.fy_start,
        fy_end=args.fy_end,
    )


def _run_audit_command(settings: Settings, args: argparse.Namespace) -> int:
    token = _read_token(settings.token_file)
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
            ibkr_tag=settings.ibkr_tag,
            ibkr_correspondent=settings.ibkr_correspondent,
            field_amount=settings.field_amount,
            field_gst=settings.field_gst,
            field_work_use=settings.field_work_use,
        )
    print(render(report))
    if args.json_name:
        json_path = args.output / args.json_name
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(dump_json(report), encoding="utf-8")
    return EXIT_OK


def _run_export_command(settings: Settings, args: argparse.Namespace) -> int:
    """Run the export and print the short summary (counts and once-notes)."""
    token = _read_token(settings.token_file)
    with PaperlessClient(
        settings.base_url,
        token,
        page_size=settings.page_size,
        timeout=settings.timeout_seconds,
    ) as client:
        result = run_export(settings, client, output=args.output)
    for note in unconfigured_field_notes(settings):
        print(f"note: {note}")
    print(
        f"{result.accepted_count} accepted, {result.review_count} review "
        f"({result.selected_count} deduction documents)"
    )
    print(f"deductions.csv written to {result.output_dir / 'deductions.csv'}")
    print(f"review.csv written to {result.output_dir / 'review.csv'}")
    return EXIT_OK


def _read_token(token_file: Path) -> str:
    """Read and strip the API token from *token_file*.

    The token comes from a file only. Errors name the path but never the file's
    content, and an empty file is rejected.
    """
    try:
        content = token_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"cannot read token file '{token_file}': {exc}") from exc
    token = content.strip()
    if not token:
        raise SettingsError(f"token file '{token_file}' is empty")
    return token
