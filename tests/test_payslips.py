"""Tests for the payslip hours ceiling: reading, coverage, sums, the check, wiring and privacy."""

from __future__ import annotations

import csv
import datetime
import io
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from conftest import (
    PAYSLIP_FIELD_PAYLOADS,
    custom_field_payload,
    document_payload,
    document_type_payload,
    filtered_documents_response,
    json_response,
    make_client,
    page_payload,
    paperless_filter_matches,
    tag_payload,
)
from test_manifest import _install, _run_cli, _written

from paperless_tax_export.cli import main
from paperless_tax_export.errors import StartupError
from paperless_tax_export.export import ExportResult, run_export
from paperless_tax_export.payslips import (
    Payslip,
    PayslipReading,
    build_payslip_section,
)
from paperless_tax_export.settings import Settings, WfhInputs

SALARY_TYPE, BILL_TYPE = 8, 7
START, END, ORDINARY, ANNUAL, PERSONAL, HOLIDAY = 31, 32, 33, 34, 35, 36
TYPES = [document_type_payload(BILL_TYPE, "bill"), document_type_payload(SALARY_TYPE, "salary")]
FIELDS = [
    custom_field_payload(10, "Transaction Amount", data_type="monetary"),
    *PAYSLIP_FIELD_PAYLOADS,
]
SECTION = "wfh_hours_ceiling"
MISSING_TYPE_WARNING = "document type 'salary' not found in Paperless; no payslip hours"
SECRET_TITLE = "Payslip-Title-Marker-7731"
SECRET_CONTENT = "Document-Content-Marker-9154"


def slip(
    doc_id: int,
    *,
    start: object = "2025-07-01",
    end: object = "2025-07-31",
    ordinary: object = "100",
    annual: object = None,
    personal: object = None,
    holiday: object = None,
    created: str = "2025-08-01T00:00:00Z",
    omit: tuple[int, ...] = (),
    document_type: int = SALARY_TYPE,
) -> dict[str, object]:
    """A payslip document; a ``None`` leave value means no entry, ``omit`` drops any entry."""
    entries = {
        START: start,
        END: end,
        ORDINARY: ordinary,
        ANNUAL: annual,
        PERSONAL: personal,
        HOLIDAY: holiday,
    }
    custom_fields = [
        {"field": field_id, "value": value}
        for field_id, value in entries.items()
        if field_id not in omit and (value is not None or field_id in (START, END, ORDINARY))
    ]
    payload = document_payload(
        doc_id,
        title=SECRET_TITLE,
        created=created,
        tags=[],
        document_type=document_type,
        custom_fields=custom_fields,
    )
    payload["content"] = SECRET_CONTENT
    return payload


def make_handler(
    documents: list[dict[str, object]],
    *,
    types: list[dict[str, object]] | None = None,
    fields: list[dict[str, object]] | None = None,
    ignore: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the run endpoints; *ignore* names a parameter the 'server' drops on payslip lists."""
    served_types = TYPES if types is None else types
    served_fields = FIELDS if fields is None else fields

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(page_payload([tag_payload(1, "tax deductible")]))
        if path == "/api/custom_fields/":
            return json_response(page_payload(list(served_fields)))
        if path == "/api/correspondents/":
            return json_response(page_payload([]))
        if path == "/api/document_types/":
            return json_response(page_payload(list(served_types)))
        if path == "/api/documents/":
            if ignore is None or request.url.params.get("document_type__id") != str(SALARY_TYPE):
                return filtered_documents_response(request, documents)
            params = request.url.params.remove(ignore)
            found = [d for d in documents if paperless_filter_matches(d, params)]
            return json_response(page_payload(found))
        return json_response({}, status_code=404)

    return handler


def run(
    tmp_path: Path,
    documents: list[dict[str, object]],
    settings: Settings | None = None,
    *,
    dry_run: bool = False,
    types: list[dict[str, object]] | None = None,
    fields: list[dict[str, object]] | None = None,
    ignore: str | None = None,
) -> ExportResult:
    handler = make_handler(documents, types=types, fields=fields, ignore=ignore)
    return run_export(
        settings or Settings(), make_client(handler), output=tmp_path / "out", dry_run=dry_run
    )


def payslip_warnings(result: ExportResult) -> tuple[str, ...]:
    return tuple(w for w in result.warnings if "payslip" in w or "'salary'" in w)


def estimate(tmp_path: Path) -> dict[tuple[str, str], dict[str, str]]:
    text = (tmp_path / "out" / "estimate.csv").read_bytes().decode("utf-8-sig")
    rows = csv.DictReader(io.StringIO(text, newline=""))
    return {(row["section"], row["label"]): row for row in rows}


def ceiling_rows(tmp_path: Path) -> dict[str, dict[str, str]]:
    return {
        label: row for (section, label), row in estimate(tmp_path).items() if section == SECTION
    }


def monthly(count: int = 12, skip: tuple[int, ...] = ()) -> list[dict[str, object]]:
    """Calendar-month payslips from July 2025; ``skip`` drops months by index."""
    documents = []
    for index in range(count):
        if index in skip:
            continue
        year, month = divmod(6 + index, 12)
        first = datetime.date(2025 + year, month + 1, 1)
        nxt = datetime.date(2025 + year + (month + 1) // 12, (month + 1) % 12 + 1, 1)
        last = nxt - datetime.timedelta(days=1)
        documents.append(
            slip(
                100 + index,
                start=first.isoformat(),
                end=last.isoformat(),
                created=f"{last.isoformat()}T00:00:00Z",
            )
        )
    return documents


# --- coverage and gap math ---------------------------------------------------


def test_a_full_year_of_monthly_periods_has_no_gaps_and_no_overlaps(tmp_path: Path) -> None:
    result = run(tmp_path, monthly())
    rows = ceiling_rows(tmp_path)
    assert result.payslips_usable == 12
    assert rows["coverage gaps"]["count"] == "0"
    assert rows["coverage gaps"]["notes"] == ""
    assert rows["overlapping days"]["count"] == "0"
    assert rows["period coverage"]["notes"] == (
        "first period start 2025-07-01; last period end 2026-06-30; FY 2025-07-01 to 2026-06-30"
    )
    assert rows["payslips"]["count"] == "12"
    assert rows["payslips"]["notes"] == ""


def test_a_missing_month_in_the_middle_is_one_gap(tmp_path: Path) -> None:
    run(tmp_path, monthly(skip=(3,)))  # October 2025
    rows = ceiling_rows(tmp_path)
    assert rows["coverage gaps"]["count"] == "31"
    assert rows["coverage gaps"]["notes"] == "2025-10-01..2025-10-31"


def test_gaps_at_both_ends_of_the_year_are_listed(tmp_path: Path) -> None:
    run(tmp_path, [slip(1, start="2025-07-08", end="2026-06-23", created="2026-06-24T00:00:00Z")])
    rows = ceiling_rows(tmp_path)
    assert rows["coverage gaps"]["count"] == "14"
    assert rows["coverage gaps"]["notes"] == "2025-07-01..2025-07-07, 2026-06-24..2026-06-30"


def test_overlapping_periods_count_each_doubly_covered_day_once(tmp_path: Path) -> None:
    documents = [
        slip(1, start="2025-07-01", end="2025-07-20"),
        slip(2, start="2025-07-15", end="2026-06-30"),
        slip(3, start="2025-07-18", end="2025-07-19"),  # a third layer on two of the six days
    ]
    run(tmp_path, documents)
    rows = ceiling_rows(tmp_path)
    assert rows["overlapping days"]["count"] == "6"  # 15 to 20 July
    assert rows["coverage gaps"]["count"] == "0"


def test_one_period_covering_the_whole_year(tmp_path: Path) -> None:
    run(tmp_path, [slip(1, start="2025-07-01", end="2026-06-30")])
    rows = ceiling_rows(tmp_path)
    assert (rows["coverage gaps"]["count"], rows["overlapping days"]["count"]) == ("0", "0")


def test_periods_outside_the_year_are_clipped_to_it(tmp_path: Path) -> None:
    documents = [
        slip(1, start="2025-06-15", end="2025-07-10", created="2025-07-11T00:00:00Z"),
        slip(2, start="2025-07-11", end="2026-07-15", created="2026-06-30T00:00:00Z"),
    ]
    run(tmp_path, documents)
    rows = ceiling_rows(tmp_path)
    assert rows["coverage gaps"]["count"] == "0"
    assert rows["overlapping days"]["count"] == "0"
    assert rows["payslips"]["notes"] == "2 period(s) extend outside the FY; hours counted in full"
    assert rows["period coverage"]["notes"].startswith(
        "first period start 2025-06-15; last period end 2026-07-15"
    )


def test_a_period_wholly_outside_the_year_leaves_the_year_uncovered(tmp_path: Path) -> None:
    run(tmp_path, [slip(1, start="2024-01-01", end="2024-01-31")])
    rows = ceiling_rows(tmp_path)
    assert rows["coverage gaps"]["count"] == "365"
    assert rows["coverage gaps"]["notes"] == "2025-07-01..2026-06-30"


def test_at_most_five_gap_ranges_are_listed_then_a_count(tmp_path: Path) -> None:
    documents = []
    for index in range(8):
        first = datetime.date(2025, 7, 2) + datetime.timedelta(days=3 * index)
        documents.append(
            slip(
                index + 1,
                start=first.isoformat(),
                end=(first + datetime.timedelta(days=1)).isoformat(),
            )
        )
    run(tmp_path, documents)
    rows = ceiling_rows(tmp_path)
    trailing = (datetime.date(2026, 6, 30) - datetime.date(2025, 7, 25)).days + 1
    assert rows["coverage gaps"]["count"] == str(1 + 7 + trailing)
    assert rows["coverage gaps"]["notes"] == (
        "2025-07-01..2025-07-01, 2025-07-04..2025-07-04, 2025-07-07..2025-07-07, "
        "2025-07-10..2025-07-10, 2025-07-13..2025-07-13, and 4 more"
    )


# --- hours, ceiling and leave defaults ---------------------------------------


def two_payslips() -> list[dict[str, object]]:
    return [
        slip(1, ordinary="76", annual="7.6", personal="0"),
        slip(
            2,
            start="2025-08-01",
            end="2025-08-31",
            ordinary="76.5",
            personal="3.8",
            holiday="7.6",
            created="2025-09-01T00:00:00Z",
        ),
    ]


def test_hours_sums_and_the_ceiling_match_hand_computed_values(tmp_path: Path) -> None:
    result = run(tmp_path, two_payslips())
    rows = ceiling_rows(tmp_path)
    assert rows["ordinary hours"]["amount"] == "152.50"  # 76 + 76.5
    assert rows["ordinary hours"]["notes"] == "hours"
    assert rows["leave hours"]["amount"] == "19.00"  # 7.6 + 3.8 + 7.6
    assert rows["leave hours"]["notes"] == (
        "hours; annual 7.60; personal 3.80; public holiday 7.60"
    )
    assert rows["ceiling"]["amount"] == "133.50"  # 152.5 - 19
    assert rows["ceiling"]["currency"] == ""
    assert result.payslip_ceiling == Decimal("133.5")


def test_leave_fields_with_no_entry_count_as_zero(tmp_path: Path) -> None:
    result = run(tmp_path, [slip(1, ordinary="80")])
    rows = ceiling_rows(tmp_path)
    assert result.payslips_usable == 1
    assert rows["leave hours"]["amount"] == "0.00"
    assert rows["ceiling"]["amount"] == "80.00"


def test_a_json_number_is_read_through_its_string_form(tmp_path: Path) -> None:
    result = run(tmp_path, [slip(1, ordinary=76.5, annual=7)])
    assert result.payslip_ceiling == Decimal("69.5")


def test_a_negative_ceiling_is_shown_with_a_note(tmp_path: Path) -> None:
    run(tmp_path, [slip(1, ordinary="10", annual="20")])
    row = ceiling_rows(tmp_path)["ceiling"]
    assert row["amount"] == "-10.00"
    assert "negative" in row["notes"]


def test_the_ceiling_row_says_it_bounds_and_does_not_prove(tmp_path: Path) -> None:
    run(tmp_path, [slip(1)])
    assert "bounds WFH hours, proves none" in ceiling_rows(tmp_path)["ceiling"]["notes"]


# --- unusable payslips -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        slip(9, omit=(ORDINARY,)),
        slip(9, omit=(START,)),
        slip(9, omit=(END,)),
        slip(9, start="2025-13-01"),
        slip(9, start="01/07/2025"),
        slip(9, end="2025-02-30"),
        slip(9, start="2025-07-31", end="2025-07-01"),
        slip(9, ordinary="-1"),
        slip(9, annual="-0.5"),
        slip(9, ordinary="744.01"),
        slip(9, holiday="745"),
        slip(9, ordinary="abc"),
        slip(9, ordinary=""),
        slip(9, ordinary="1e2"),
        slip(9, ordinary="NaN"),
        slip(9, ordinary={"a": 1}),
    ],
)
def test_an_unusable_payslip_is_left_out_of_the_sums_and_counted(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    result = run(tmp_path, [slip(1, ordinary="50"), bad])
    rows = ceiling_rows(tmp_path)
    assert result.payslips_usable == 1
    assert result.payslips_unusable == 1
    assert rows["ordinary hours"]["amount"] == "50.00"
    assert rows["payslips"]["notes"] == "unusable: 1"
    warnings = payslip_warnings(result)
    assert len(warnings) == 1
    assert "document ids 9" in warnings[0]


def test_a_null_value_is_unusable_even_for_a_leave_field(tmp_path: Path) -> None:
    null_leave = slip(9)
    custom_fields = null_leave["custom_fields"]
    assert isinstance(custom_fields, list)
    custom_fields.append({"field": ANNUAL, "value": None})
    result = run(tmp_path, [null_leave])
    assert result.payslips_usable == 0
    assert result.payslips_unusable == 1


def test_744_hours_is_accepted_per_field(tmp_path: Path) -> None:
    result = run(tmp_path, [slip(1, ordinary="744", annual="744")])
    assert result.payslips_usable == 1
    assert result.payslip_ceiling == Decimal(0)


def test_unusable_ids_are_capped_at_twenty(tmp_path: Path) -> None:
    documents = [slip(index, ordinary="abc") for index in range(1, 23)]
    result = run(tmp_path, documents)
    (warning,) = payslip_warnings(result)
    assert "22 payslip(s) unusable" in warning
    assert warning.endswith(f"{', '.join(str(i) for i in range(1, 21))} and 2 more")


def test_without_usable_payslips_the_section_is_one_not_computed_row(tmp_path: Path) -> None:
    result = run(tmp_path, [slip(1, ordinary="abc")])
    rows = ceiling_rows(tmp_path)
    assert list(rows) == ["ceiling"]
    assert rows["ceiling"]["notes"] == "not computed: no usable payslips"
    assert result.payslip_ceiling is None


def test_no_payslip_documents_is_not_computed_and_not_a_warning(tmp_path: Path) -> None:
    result = run(tmp_path, [])
    assert payslip_warnings(result) == ()
    assert ceiling_rows(tmp_path)["ceiling"]["notes"] == "not computed: no usable payslips"


# --- the claimed-hours check -------------------------------------------------


def check_note(tmp_path: Path, claimed: str | None, documents: list[dict[str, object]]) -> str:
    wfh = WfhInputs() if claimed is None else WfhInputs(hours=Decimal(claimed))
    run(tmp_path, documents, Settings(wfh=wfh))
    rows = ceiling_rows(tmp_path)
    return rows["wfh_hours_check"]["notes"] if "wfh_hours_check" in rows else "(no row)"


def test_claimed_hours_equal_to_the_ceiling_are_within_it(tmp_path: Path) -> None:
    assert check_note(tmp_path, "133.5", two_payslips()) == (
        "claimed WFH hours <= ceiling; 0.00 hours below it"
    )


def test_claimed_hours_below_the_ceiling_show_the_difference(tmp_path: Path) -> None:
    assert check_note(tmp_path, "100", two_payslips()) == (
        "claimed WFH hours <= ceiling; 33.50 hours below it"
    )


def test_claimed_hours_above_the_ceiling_are_flagged(tmp_path: Path) -> None:
    assert check_note(tmp_path, "140", two_payslips()) == "exceeds the ceiling by 6.50 hours"


def test_no_check_row_without_wfh_hours(tmp_path: Path) -> None:
    assert check_note(tmp_path, None, two_payslips()) == "(no row)"


def test_the_check_says_not_computed_without_usable_payslips(tmp_path: Path) -> None:
    assert check_note(tmp_path, "10", []) == "not computed: no usable payslips"


def test_the_check_is_not_computed_when_a_leave_field_is_missing(tmp_path: Path) -> None:
    fields = [field for field in FIELDS if field["name"] != "Annual Leave Hours"]
    run(tmp_path, two_payslips(), Settings(wfh=WfhInputs(hours=Decimal(10))), fields=fields)
    rows = ceiling_rows(tmp_path)
    assert rows["wfh_hours_check"]["notes"] == "not computed: no ceiling"


# --- type and field resolution -----------------------------------------------


def test_a_missing_type_warns_and_the_section_is_not_computed(tmp_path: Path) -> None:
    result = run(tmp_path, [], types=[document_type_payload(BILL_TYPE, "bill")])
    assert payslip_warnings(result) == (MISSING_TYPE_WARNING,)
    assert ceiling_rows(tmp_path)["ceiling"]["notes"] == "not computed: no payslip document type"
    assert result.payslip_documents == 0


def test_two_types_named_salary_are_a_startup_error(tmp_path: Path) -> None:
    types = [*TYPES, document_type_payload(9, "salary")]
    with pytest.raises(StartupError, match="at most one document type named 'salary'"):
        run(tmp_path, [], types=types)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("ignored", ["document_type__id", "created__gte", "created__lte"])
def test_a_server_that_ignores_a_filter_is_a_startup_error(tmp_path: Path, ignored: str) -> None:
    documents = [
        slip(1),
        slip(2, document_type=BILL_TYPE),
        slip(3, created="2024-01-01T00:00:00Z"),
        slip(4, created="2027-01-01T00:00:00Z"),
    ]
    with pytest.raises(StartupError) as raised:
        run(tmp_path, documents, ignore=ignored)
    assert SECRET_TITLE not in str(raised.value)
    assert "document type 'salary'" in str(raised.value)
    assert not (tmp_path / "out").exists()


def test_the_guard_exits_4_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(
        monkeypatch,
        make_handler([slip(1), slip(2, document_type=BILL_TYPE)], ignore="document_type__id"),
    )
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 4
    assert _written(output) == []


def test_only_payslips_created_in_the_year_are_read(tmp_path: Path) -> None:
    documents = [
        slip(1, created="2025-07-01T00:00:00Z"),
        slip(2, created="2026-06-30T23:59:59Z"),
        slip(3, created="2025-06-30T23:59:59Z"),
        slip(4, created="2026-07-01T00:00:00Z"),
        slip(5, document_type=BILL_TYPE),
    ]
    result = run(tmp_path, documents)
    assert result.payslip_documents == 2
    assert result.payslips_usable == 2


def test_a_missing_optional_field_warns_by_name_and_leave_is_not_computed(
    tmp_path: Path,
) -> None:
    fields = [field for field in FIELDS if field["name"] != "Annual Leave Hours"]
    result = run(tmp_path, two_payslips(), fields=fields)
    (warning,) = payslip_warnings(result)
    assert "'Annual Leave Hours'" in warning
    assert "'Personal Leave Hours'" not in warning
    rows = ceiling_rows(tmp_path)
    assert result.payslips_usable == 2
    assert rows["ordinary hours"]["amount"] == "152.50"
    assert rows["leave hours"]["notes"].startswith(
        "not computed: custom field 'Annual Leave Hours'"
    )
    assert rows["ceiling"]["amount"] == ""
    assert result.payslip_ceiling is None


def test_a_missing_required_field_makes_every_payslip_unusable_without_failing(
    tmp_path: Path,
) -> None:
    fields = [field for field in FIELDS if field["name"] != "Ordinary Hours"]
    result = run(tmp_path, two_payslips(), fields=fields)
    warnings = payslip_warnings(result)
    assert "'Ordinary Hours'" in warnings[0]
    assert "2 payslip(s) unusable" in warnings[1]
    assert result.payslips_usable == 0
    assert result.payslips_unusable == 2
    assert ceiling_rows(tmp_path)["ceiling"]["notes"] == (
        "not computed: a required payslip custom field is missing in Paperless"
    )


def test_custom_names_are_resolved_from_settings(tmp_path: Path) -> None:
    renamed = dict(Settings().payslip_fields, ordinary_hours="Paid Hours")
    fields = [
        custom_field_payload(33, "Paid Hours", data_type="float") if f["id"] == 33 else f
        for f in FIELDS
    ]
    result = run(
        tmp_path, [slip(1, ordinary="40")], Settings(payslip_fields=renamed), fields=fields
    )
    assert payslip_warnings(result) == ()
    assert result.payslip_ceiling == Decimal(40)


def test_the_warnings_follow_the_bill_warnings_in_a_fixed_order(tmp_path: Path) -> None:
    fields = [field for field in FIELDS if field["name"] != "Annual Leave Hours"]
    result = run(tmp_path, [slip(1, ordinary="abc")], fields=fields)
    assert [w.split(";")[0].split(":")[0] for w in result.warnings[-2:]] == [
        "payslip custom field(s) not found in Paperless",
        "1 payslip(s) unusable (missing or invalid dates or hours), not counted in the hours",
    ]


# --- estimate placement ------------------------------------------------------


def test_the_section_sits_between_the_wfh_rows_and_pending(tmp_path: Path) -> None:
    run(tmp_path, two_payslips(), Settings(marginal_rate=Decimal("0.3")))
    text = (tmp_path / "out" / "estimate.csv").read_bytes().decode("utf-8-sig")
    sections = list(dict.fromkeys(row["section"] for row in csv.DictReader(io.StringIO(text))))
    assert sections[sections.index("wfh_note") + 1 :] == [
        SECTION,
        "pending",
        "pending_no_amount",
        "tax_saving",
    ]


def test_the_rows_come_in_a_fixed_order(tmp_path: Path) -> None:
    run(tmp_path, two_payslips(), Settings(wfh=WfhInputs(hours=Decimal(10))))
    assert list(ceiling_rows(tmp_path)) == [
        "payslips",
        "period coverage",
        "coverage gaps",
        "overlapping days",
        "ordinary hours",
        "leave hours",
        "ceiling",
        "wfh_hours_check",
    ]


def test_payslips_change_no_other_estimate_figure(tmp_path: Path) -> None:
    settings = Settings(marginal_rate=Decimal("0.3"), wfh=WfhInputs(hours=Decimal(10)))
    plain = run(tmp_path / "plain", [], settings)
    with_slips = run(tmp_path / "slips", two_payslips(), settings)
    assert plain.estimate_deductions_aud == with_slips.estimate_deductions_aud
    assert plain.estimate_pending_aud == with_slips.estimate_pending_aud
    assert plain.estimated_saving == with_slips.estimated_saving
    other = {k: v for k, v in estimate(tmp_path / "slips").items() if k[0] != SECTION}
    baseline = {k: v for k, v in estimate(tmp_path / "plain").items() if k[0] != SECTION}
    assert other == baseline


def test_build_section_is_pure_over_the_reading() -> None:
    reading = PayslipReading(
        type_found=True,
        document_count=1,
        usable=(
            Payslip(
                1,
                datetime.date(2025, 7, 1),
                datetime.date(2026, 6, 30),
                Decimal("1976"),
                Decimal("152"),
                Decimal("38"),
                Decimal("30.4"),
            ),
        ),
    )
    section = build_payslip_section(reading, Settings(), None)
    assert section.ceiling == Decimal("1755.6")
    assert section.usable_count == 1


# --- manifest, determinism, dry run, console ---------------------------------


def test_manifest_records_the_count_and_the_parameters(tmp_path: Path) -> None:
    run(tmp_path, [*two_payslips(), slip(9, ordinary="abc")])
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["payslips_count"] == 2
    assert manifest["parameters"]["payslip_document_type"] == "salary"
    assert manifest["parameters"]["payslip_fields"] == {
        "annual_leave_hours": "Annual Leave Hours",
        "ordinary_hours": "Ordinary Hours",
        "period_end": "Pay Period End",
        "period_start": "Pay Period Start",
        "personal_leave_hours": "Personal Leave Hours",
        "public_holiday_hours": "Public Holiday Hours",
    }
    assert "payslips.csv" not in manifest["outputs"]


def test_two_runs_give_byte_identical_files(tmp_path: Path) -> None:
    settings = Settings(wfh=WfhInputs(hours=Decimal("120.5")))
    run(tmp_path / "a", two_payslips(), settings)
    run(tmp_path / "b", list(reversed(two_payslips())), settings)
    names = sorted(path.name for path in (tmp_path / "a" / "out").iterdir())
    assert names
    for name in names:
        assert (tmp_path / "a" / "out" / name).read_bytes() == (
            tmp_path / "b" / "out" / name
        ).read_bytes()


def test_a_dry_run_writes_nothing_but_computes_the_ceiling(tmp_path: Path) -> None:
    result = run(tmp_path, two_payslips(), dry_run=True)
    assert not (tmp_path / "out").exists()
    assert result.payslip_ceiling == Decimal("133.5")


def test_old_manifest_without_payslips_count_exits_2_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(tmp_path, two_payslips())
    manifest_path = tmp_path / "out" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    del payload["counts"]["payslips_count"]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["verify", "--output", str(tmp_path / "out")]) == 2
    assert "counts.payslips_count" in capsys.readouterr().err


def test_console_prints_one_payslips_line_after_the_bills_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler([*two_payslips(), slip(9, ordinary="abc")]))
    assert main(_run_cli(tmp_path, tmp_path / "out")) == 0
    out = capsys.readouterr().out
    assert "payslips: 2 usable, 1 unusable; WFH hours ceiling 133.50\n" in out
    assert out.count("payslips:") == 1


def test_console_says_not_computed_when_there_is_no_ceiling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler([slip(9, ordinary="abc")]))
    assert main(_run_cli(tmp_path, tmp_path / "out")) == 0
    assert "payslips: 0 usable, 1 unusable; WFH hours ceiling not computed" in (
        capsys.readouterr().out
    )


def test_console_prints_no_payslips_line_without_payslip_documents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler([]))
    assert main(_run_cli(tmp_path, tmp_path / "out")) == 0
    assert "payslips:" not in capsys.readouterr().out


# --- privacy -----------------------------------------------------------------


def test_titles_and_content_never_reach_the_console_or_any_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    documents = [*two_payslips(), slip(9, ordinary="abc"), slip(10, start="2025-13-45")]
    _install(monkeypatch, make_handler(documents))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 0
    captured = capsys.readouterr()
    for marker in (SECRET_TITLE, SECRET_CONTENT):
        assert marker not in captured.out
        assert marker not in captured.err
        for path in output.iterdir():
            assert marker.encode() not in path.read_bytes()
    assert "document ids 9, 10" in captured.err


# --- settings ----------------------------------------------------------------


def test_the_settings_defaults_are_the_owner_decisions() -> None:
    settings = Settings()
    assert settings.payslip_document_type == "salary"
    assert settings.payslip_fields == {
        "period_start": "Pay Period Start",
        "period_end": "Pay Period End",
        "ordinary_hours": "Ordinary Hours",
        "annual_leave_hours": "Annual Leave Hours",
        "personal_leave_hours": "Personal Leave Hours",
        "public_holiday_hours": "Public Holiday Hours",
    }
