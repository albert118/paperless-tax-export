"""Contract tests for the recorded audit fixtures.

Every file under ``tests/fixtures/records`` is a real ``dump_json`` output. These
tests keep the recordings honest: canonical JSON, the same top-level schema
``dump_json`` emits today, and no document-derived data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperless_tax_export.audit import AuditReport, IbkrFindings, dump_json

_RECORDS_DIR = Path(__file__).parent / "fixtures" / "records"
_RECORDS = sorted(_RECORDS_DIR.glob("*.json"))
_RECORD_IDS = [path.name for path in _RECORDS]


def _minimal_report() -> AuditReport:
    """A report with empty collections, used only to derive the JSON schema."""
    return AuditReport(
        tag_name="tax deductible",
        fy_start="2025-07-01",
        fy_end="2026-06-30",
        tag_matches=(),
        deduction_document_count=0,
        custom_fields=(),
        coverage=(),
        storage_paths=(),
        archived_first_segments=(),
        archived_second_segments=(),
        ibkr=IbkrFindings(
            tag_name="tax-information",
            tag_exists=False,
            tag_document_count=0,
            correspondent_name="Interactive Brokers",
            correspondent_exists=False,
            correspondent_document_count=0,
            trade_confirmation_types=(),
            realized_gains_types=(),
            correspondent_without_tag=0,
            tag_with_other_correspondent=0,
        ),
        expected_fields=(),
        api_version="10",
        server_version="3.3.2",
    )


def test_there_is_at_least_one_recorded_audit() -> None:
    assert _RECORDS, f"no recorded audit fixtures in {_RECORDS_DIR}"


@pytest.mark.parametrize("path", _RECORDS, ids=_RECORD_IDS)
def test_recorded_audit_is_canonical_json(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n" == text


@pytest.mark.parametrize("path", _RECORDS, ids=_RECORD_IDS)
def test_recorded_audit_matches_the_current_schema(path: Path) -> None:
    recorded = json.loads(path.read_text(encoding="utf-8"))
    current = json.loads(dump_json(_minimal_report()))

    assert set(recorded) == set(current)


@pytest.mark.parametrize("path", _RECORDS, ids=_RECORD_IDS)
def test_recorded_audit_holds_no_document_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    for banned in ("archived_file_name", "document_ids", '"title"'):
        assert banned not in text
