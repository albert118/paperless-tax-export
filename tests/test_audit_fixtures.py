"""Contract tests for the recorded audit fixtures.

Every file under ``tests/fixtures/records`` is a real ``dump_json`` output. These
tests keep the recordings honest: canonical JSON, the same nested schema
``dump_json`` emits today (with additive growth allowed — an older recording may
lack keys newer versions added), and no document-derived data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperless_tax_export.audit import (
    AuditReport,
    CustomFieldInfo,
    ExpectedFieldInfo,
    FieldCoverage,
    IbkrFindings,
    StoragePathInfo,
    TagFinding,
    TagMetadata,
    dump_json,
)
from paperless_tax_export.settings import Settings

_RECORDS_DIR = Path(__file__).parent / "fixtures" / "records"
_RECORDS = sorted(_RECORDS_DIR.glob("*.json"))
_RECORD_IDS = [path.name for path in _RECORDS]
_PARTIAL_FAILURE_RECORD = _RECORDS_DIR / "audit-live-2026-10-20T1527Z.json"


def _minimal_report() -> AuditReport:
    """A report with one exemplar item per list section, to derive the schema.

    The exemplar items make every nested level of the current ``dump_json``
    output non-empty, so the recursive subset check actually compares the keys
    inside list items rather than skipping empty collections.
    """
    coverage = FieldCoverage(
        field_id=1,
        field_name="Transaction Amount",
        present_count=1,
        null_count=0,
        absent_count=0,
    )
    return AuditReport(
        tag_name="tax deductible",
        fy_start="2025-07-01",
        fy_end="2026-06-30",
        tag_matches=(TagFinding(name="tax deductible", document_count=1),),
        deduction_document_count=1,
        custom_fields=(
            CustomFieldInfo(
                id=1,
                name="Transaction Amount",
                data_type="monetary",
                document_count=1,
                default_currency=None,
                select_options=(),
            ),
        ),
        coverage=(coverage,),
        storage_paths=(StoragePathInfo(id=1, name="FY26", path="2025/07", document_count=1),),
        archived_first_segments=(),
        archived_second_segments=(),
        ibkr=IbkrFindings(
            correspondent_name="Interactive Brokers",
            correspondent_exists=False,
            correspondent_document_count=0,
            trade_confirmation_types=(),
            realized_gains_types=(),
        ),
        tax_metadata_tag=TagMetadata(name="tax-information", exists=False, document_count=0),
        expected_fields=(
            ExpectedFieldInfo(
                name="Transaction Amount",
                required=True,
                exists=True,
                data_type="monetary",
                coverage=coverage,
            ),
        ),
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
    """A recorded JSON must be a nested subset of today's ``dump_json`` output.

    Additive schema growth is allowed: an older recording legitimately lacks
    keys (and list items, where both sides have items, are compared by
    structure) newer versions of ``dump_json`` added. A key removal, rename or
    re-shape, on the other hand, leaves a recorded key unknown to today's
    ``dump_json`` and fails here.
    """
    recorded = json.loads(path.read_text(encoding="utf-8"))
    current = json.loads(dump_json(_minimal_report()))

    _assert_nested_subset(recorded, current, "$")


@pytest.mark.parametrize("path", _RECORDS, ids=_RECORD_IDS)
def test_recorded_audit_holds_no_document_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    for banned in ("archived_file_name", "document_ids", '"title"'):
        assert banned not in text


def test_partial_failure_recording_documents_the_audit_robustness_facts() -> None:
    """The maintainer's partial-failure recording, when present, must say what it claims.

    The recording is added by the maintainer, not this branch, so the test
    skips cleanly when the file is not in the checkout.
    """
    if not _PARTIAL_FAILURE_RECORD.exists():
        pytest.skip(f"{_PARTIAL_FAILURE_RECORD.name} is not in this checkout yet")
    recorded = json.loads(_PARTIAL_FAILURE_RECORD.read_text(encoding="utf-8"))

    assert recorded["unavailable"], "expected at least one unavailable endpoint"
    assert any("correspondents" in entry for entry in recorded["unavailable"])
    assert recorded["correspondents_visible"] == 0
    assert recorded["tag_matches"], "expected the deduction tag among the matches"
    assert any(match["name"] == Settings().deduction_tag for match in recorded["tag_matches"])
    assert recorded["deduction_document_count"] == 13
    assert recorded["tax_metadata_tag"]["exists"] is True


def _assert_nested_subset(recorded: object, current: object, path: str) -> None:
    """Fail unless *recorded* is a nested subset of *current* at *path*."""
    if recorded is None:
        # ``null`` is a leaf: positions like ``expected_fields[].coverage`` are
        # nullable in the current schema, so a recorded null is always valid.
        return
    if isinstance(current, dict):
        assert isinstance(recorded, dict), f"{path}: expected a dict, got {type(recorded).__name__}"
        unknown = sorted(set(recorded) - set(current))
        assert not unknown, f"{path}: keys unknown to the current schema: {unknown}"
        for key, recorded_value in recorded.items():
            _assert_nested_subset(recorded_value, current[key], f"{path}.{key}")
        return
    if isinstance(current, list):
        assert isinstance(recorded, list), f"{path}: expected a list, got {type(recorded).__name__}"
        if recorded and current:
            for index, item in enumerate(recorded):
                _assert_nested_subset(item, current[0], f"{path}[{index}]")
        return
    # Any other leaf value (a count, name, URL, timestamp): not cross-checked
    # between recordings — the schema asserts its presence, not its value.
