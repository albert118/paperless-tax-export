"""Deterministic ``manifest.json`` rendering.

The manifest records what a run was asked to do and what it produced: the tool
and Paperless versions, the parameters, the counts, the expected count and a
SHA-256 plus data-row count per output file. It carries no timestamp, so two
runs over identical data give byte-identical manifests. The JSON is canonical
(sorted keys, two-space indent, trailing newline), like ``audit.dump_json``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from paperless_tax_export import __version__
from paperless_tax_export.settings import Settings

DEDUCTIONS_FILE = "deductions.csv"
REVIEW_FILE = "review.csv"
INVESTMENTS_FILE = "investments.csv"
BILLS_FILE = "bills.csv"
ESTIMATE_FILE = "estimate.csv"
MANIFEST_FILE = "manifest.json"


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One output file to write: its name, exact bytes and data-row count."""

    name: str
    content: bytes
    rows: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestCounts:
    """The run counts the manifest reports."""

    tagged_total: int
    out_of_fy: int
    selected_in_fy: int
    accepted: int
    review: int
    investments_count: int
    bills_count: int


def render_manifest(
    settings: Settings,
    *,
    counts: ManifestCounts,
    files: tuple[PlannedFile, ...],
    server_version: str | None,
    api_version: str | None,
    rates_file_sha256: str | None = None,
) -> bytes:
    """Canonical manifest bytes (UTF-8, LF, trailing newline)."""
    payload: dict[str, Any] = {
        "tool_version": __version__,
        "paperless": {"server_version": server_version, "api_version": api_version},
        "parameters": {
            "deduction_tag": settings.deduction_tag,
            "fy_start": settings.fy_start,
            "fy_end": settings.fy_end,
            "ibkr_correspondent": settings.ibkr_correspondent,
            "income_tags": list(settings.income_tags),
            "asset_reviewed_tag": settings.asset_reviewed_tag,
            "default_currency": settings.default_currency,
            "field_amount": settings.field_amount,
            "field_gst": settings.field_gst,
            "field_work_use": settings.field_work_use,
            "field_date": settings.field_date,
            "field_supplier": settings.field_supplier,
            "marginal_rate": None
            if settings.marginal_rate is None
            else f"{settings.marginal_rate:f}",
            "rates_file_sha256": rates_file_sha256,
            "bill_document_type": settings.bill_document_type,
            "bill_kind_tags": dict(sorted(settings.bill_kind_tags.items())),
            "wfh_hours": _typed(settings.wfh.hours),
            "wfh_rate_per_hour": _typed(settings.wfh.rate_per_hour),
            "wfh_internet_pct": _typed(settings.wfh.internet_pct),
            "wfh_phone_pct": _typed(settings.wfh.phone_pct),
            "wfh_energy_amount": _typed(settings.wfh.energy_amount),
        },
        "counts": {
            "tagged_total": counts.tagged_total,
            "out_of_fy": counts.out_of_fy,
            "selected_in_fy": counts.selected_in_fy,
            "accepted": counts.accepted,
            "review": counts.review,
            "investments_count": counts.investments_count,
            "bills_count": counts.bills_count,
        },
        "expected_count": settings.expected_count,
        "outputs": {file.name: {"sha256": file.sha256, "rows": file.rows} for file in files},
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _typed(value: Decimal | None) -> str | None:
    """A decimal as typed (``0.70`` stays ``0.70``), or ``None`` when the variable is unset."""
    return None if value is None else f"{value:f}"
