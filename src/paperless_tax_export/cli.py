"""Command-line interface for paperless-tax-export."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from paperless_tax_export import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line arguments and run the export.

    The tool is read-only toward Paperless: it only ever reads documents
    with GET requests and never writes back.
    """
    parser = argparse.ArgumentParser(
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
    parser.parse_args(argv)
    return 0
