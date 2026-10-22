"""Tests for bills.csv: selection, kinds, amounts, bytes, the WFH scenarios and the wiring."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from conftest import (
    correspondent_payload,
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

from paperless_tax_export.bills import BILLS_COLUMNS, BillRow, render_bills_bytes
from paperless_tax_export.cli import main
from paperless_tax_export.env_config import EnvSources, resolve_wfh_inputs
from paperless_tax_export.errors import SettingsError, StartupError
from paperless_tax_export.export import ExportResult, run_export
from paperless_tax_export.settings import DEFAULT_BILL_KIND_TAGS, Settings, WfhInputs
from paperless_tax_export.wfh_estimate import build_bills_section

BOM = b"\xef\xbb\xbf"
DEDUCTION_TAG, ELECTRICITY, INTERNET, PHONE, GAS, OTHER = 1, 2, 3, 4, 5, 6
BILL_TYPE, RECEIPT_TYPE = 7, 6
TAGS = [
    tag_payload(DEDUCTION_TAG, "tax deductible"),
    tag_payload(ELECTRICITY, "electricity"),
    tag_payload(INTERNET, "internet"),
    tag_payload(PHONE, "phone"),
    tag_payload(GAS, "gas"),
    tag_payload(OTHER, "garden"),
]
FIELDS = [
    custom_field_payload(10, "Transaction Amount", data_type="monetary"),
    custom_field_payload(13, "Transaction Date", data_type="date"),
]
TYPES = [document_type_payload(RECEIPT_TYPE, "Receipt"), document_type_payload(BILL_TYPE, "bill")]
MISSING_TYPE_WARNING = "document type 'bill' not found in Paperless; bills.csv has no rows"


def bill(
    doc_id: int,
    *,
    kind: int | None = INTERNET,
    amount: str | None = "100.00",
    created: str = "2025-10-01T00:00:00Z",
    title: str | None = None,
    tags: list[int] | None = None,
    extra: list[dict[str, object]] | None = None,
    document_type: int = BILL_TYPE,
    with_date: bool = True,
) -> dict[str, object]:
    """A bill document; by default its Transaction Date field equals its created date."""
    fields: list[dict[str, object]] = [] if amount is None else [{"field": 10, "value": amount}]
    if with_date and extra is None:
        extra = [{"field": 13, "value": created[:10]}]
    return document_payload(
        doc_id,
        title=title or f"Bill {doc_id}",
        created=created,
        tags=tags if tags is not None else ([] if kind is None else [kind]),
        correspondent=5,
        document_type=document_type,
        custom_fields=[*fields, *(extra or [])],
    )


def make_handler(
    documents: list[dict[str, object]],
    *,
    types: list[dict[str, object]] | None = None,
    ignore: str | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the run endpoints; *ignore* names a query parameter the 'server' drops."""
    served_types = TYPES if types is None else types

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags/":
            return json_response(page_payload(list(TAGS)))
        if path == "/api/custom_fields/":
            return json_response(page_payload(list(FIELDS)))
        if path == "/api/correspondents/":
            return json_response(page_payload([correspondent_payload(5, "Utility Co")]))
        if path == "/api/document_types/":
            return json_response(page_payload(list(served_types)))
        if path == "/api/documents/":
            if ignore is None:
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
    ignore: str | None = None,
) -> ExportResult:
    handler = make_handler(documents, types=types, ignore=ignore)
    return run_export(
        settings or Settings(), make_client(handler), output=tmp_path / "out", dry_run=dry_run
    )


def bill_warnings(result: ExportResult) -> tuple[str, ...]:
    """The bill warnings only; other warnings (investments) are not under test here."""
    return tuple(warning for warning in result.warnings if "bill" in warning)


def table(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig"), newline="")))


def bills(tmp_path: Path) -> list[dict[str, str]]:
    return table(tmp_path / "out" / "bills.csv")


def estimate(tmp_path: Path) -> dict[tuple[str, str], dict[str, str]]:
    rows = table(tmp_path / "out" / "estimate.csv")
    return {(row["section"], row["label"]): row for row in rows}


# --- selection ---------------------------------------------------------------


def test_only_bill_type_documents_created_in_the_fy_are_listed(tmp_path: Path) -> None:
    documents = [
        bill(1, created="2025-07-01T00:00:00Z"),
        bill(2, created="2026-06-30T23:59:59Z"),
        bill(3, created="2025-06-30T23:59:59Z"),
        bill(4, created="2026-07-01T00:00:00Z"),
        bill(5, document_type=RECEIPT_TYPE),
    ]
    result = run(tmp_path, documents)
    assert [row["paperless_document_id"] for row in bills(tmp_path)] == ["1", "2"]
    assert result.bills_count == 2


@pytest.mark.parametrize("ignored", ["document_type__id", "created__gte", "created__lte"])
def test_a_server_that_ignores_a_filter_is_a_startup_error_and_writes_nothing(
    tmp_path: Path, ignored: str
) -> None:
    documents = [
        bill(1),
        bill(2, document_type=RECEIPT_TYPE),
        bill(3, created="2024-01-01T00:00:00Z"),
        bill(4, created="2027-01-01T00:00:00Z"),
    ]
    with pytest.raises(StartupError) as raised:
        run(tmp_path, documents, ignore=ignored)
    assert "Bill" not in str(raised.value)
    assert "document type 'bill'" in str(raised.value)
    assert not (tmp_path / "out").exists()


def test_the_ignored_filter_guard_exits_4_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(
        monkeypatch,
        make_handler([bill(1), bill(2, document_type=RECEIPT_TYPE)], ignore="document_type__id"),
    )
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 4
    assert _written(output) == []


def test_a_missing_bill_type_warns_once_and_writes_a_header_only_file(tmp_path: Path) -> None:
    result = run(tmp_path, [], types=[document_type_payload(RECEIPT_TYPE, "Receipt")])
    assert bill_warnings(result) == (MISSING_TYPE_WARNING,)
    content = (tmp_path / "out" / "bills.csv").read_bytes()
    assert content == BOM + ",".join(BILLS_COLUMNS).encode() + b"\r\n"
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["bills_count"] == 0
    assert manifest["outputs"]["bills.csv"]["rows"] == 0


def test_two_document_types_with_the_name_bill_are_a_startup_error(tmp_path: Path) -> None:
    types = [*TYPES, document_type_payload(9, "bill")]
    with pytest.raises(StartupError, match="at most one document type named 'bill', found 2"):
        run(tmp_path, [bill(1)], types=types)
    assert not (tmp_path / "out").exists()


def test_duplicate_bill_types_exit_4_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler([bill(1)], types=[*TYPES, document_type_payload(9, "bill")]))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 4
    assert _written(output) == []


def test_the_document_type_name_is_a_setting(tmp_path: Path) -> None:
    types = [document_type_payload(8, "Utility bill")]
    documents = [bill(1, document_type=8)]
    result = run(tmp_path, documents, Settings(bill_document_type="Utility bill"), types=types)
    assert result.bills_count == 1
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parameters"]["bill_document_type"] == "Utility bill"


# --- kinds -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "kind", "notes"),
    [
        ([ELECTRICITY], "electricity", ""),
        ([INTERNET], "internet", ""),
        ([PHONE], "phone", ""),
        ([GAS, OTHER], "gas", ""),
        ([], "", "no kind tag"),
        ([OTHER], "", "no kind tag"),
        ([PHONE, INTERNET], "", "several kind tags: internet; phone"),
        ([GAS, ELECTRICITY, INTERNET], "", "several kind tags: electricity; gas; internet"),
    ],
)
def test_kind_comes_from_the_kind_tags(
    tmp_path: Path, tags: list[int], kind: str, notes: str
) -> None:
    run(tmp_path, [bill(1, tags=tags)])
    (row,) = bills(tmp_path)
    assert (row["kind"], row["notes_for_agent"]) == (kind, notes)


def test_kind_tag_names_are_configurable(tmp_path: Path) -> None:
    kind_tags = {**DEFAULT_BILL_KIND_TAGS, "gas": "garden"}
    run(tmp_path, [bill(1, tags=[OTHER]), bill(2, tags=[GAS])], Settings(bill_kind_tags=kind_tags))
    rows = bills(tmp_path)
    assert [(row["kind"], row["notes_for_agent"]) for row in rows] == [
        ("gas", ""),
        ("", "no kind tag"),
    ]


@pytest.mark.parametrize(
    "kind_tags",
    [
        {"electricity": "e", "internet": "i", "phone": "p"},
        {**DEFAULT_BILL_KIND_TAGS, "water": "w"},
        {**DEFAULT_BILL_KIND_TAGS, "gas": " "},
        {**DEFAULT_BILL_KIND_TAGS, "gas": "phone"},
    ],
)
def test_settings_reject_bad_kind_tags(kind_tags: dict[str, str]) -> None:
    with pytest.raises(SettingsError, match="bill_kind_tags"):
        Settings(bill_kind_tags=kind_tags)


def test_settings_reject_an_empty_bill_type_name() -> None:
    with pytest.raises(SettingsError, match="bill_document_type"):
        Settings(bill_document_type=" ")


# --- date and amount ---------------------------------------------------------


def test_date_is_the_transaction_date_when_it_parses_else_created(tmp_path: Path) -> None:
    documents = [
        bill(1, extra=[{"field": 13, "value": "2025-09-15"}]),
        bill(2, extra=[{"field": 13, "value": "15/09/2025"}]),
        bill(3, with_date=False),
    ]
    run(tmp_path, documents)
    rows = {row["paperless_document_id"]: row for row in bills(tmp_path)}
    assert (rows["1"]["date"], rows["1"]["notes_for_agent"]) == ("2025-09-15", "")
    assert (rows["2"]["date"], rows["2"]["notes_for_agent"]) == ("2025-10-01", "date from created")
    assert (rows["3"]["date"], rows["3"]["notes_for_agent"]) == ("2025-10-01", "date from created")


def test_rows_are_sorted_by_date_then_document_id(tmp_path: Path) -> None:
    documents = [
        bill(5, created="2025-08-01T00:00:00Z"),
        bill(2, created="2025-11-01T00:00:00Z"),
        bill(9, created="2025-11-01T00:00:00Z"),
        bill(7, created="2025-12-01T00:00:00Z", extra=[{"field": 13, "value": "2025-07-20"}]),
    ]
    run(tmp_path, documents)
    assert [row["paperless_document_id"] for row in bills(tmp_path)] == ["7", "5", "2", "9"]


@pytest.mark.parametrize(
    ("amount", "shown", "currency", "notes"),
    [
        ("100.00", "100.00", "AUD", ""),
        ("1,234.5", "1234.50", "AUD", ""),
        ("USD10.00", "10.00", "USD", ""),
        (None, "", "", "amount missing"),
        ("n/a", "", "", "amount missing"),
        ("-5.00", "-5.00", "AUD", "negative amount"),
        ("0.00", "0.00", "AUD", "zero amount"),
    ],
)
def test_amount_and_currency_follow_the_deduction_rules(
    tmp_path: Path, amount: str | None, shown: str, currency: str, notes: str
) -> None:
    run(tmp_path, [bill(1, amount=amount)])
    (row,) = bills(tmp_path)
    assert (row["amount_inc_gst"], row["currency"], row["notes_for_agent"]) == (
        shown,
        currency,
        notes,
    )


def test_notes_are_joined_in_a_fixed_order(tmp_path: Path) -> None:
    run(tmp_path, [bill(1, kind=None, amount=None, tags=[DEDUCTION_TAG], with_date=False)])
    (row,) = bills(tmp_path)
    assert row["notes_for_agent"] == (
        "amount missing; date from created; no kind tag; "
        "also in deductions.csv; excluded from WFH totals"
    )


# --- byte contract -----------------------------------------------------------


def test_bytes_follow_the_csv_contract_and_guard_formulas(tmp_path: Path) -> None:
    run(tmp_path, [bill(1, title="=SUM(1)", amount="12.5", with_date=False)])
    content = (tmp_path / "out" / "bills.csv").read_bytes()
    assert content.startswith(BOM)
    text = content.decode("utf-8-sig")
    assert text.splitlines()[0] == (
        "paperless_document_id,date,correspondent,title,kind,amount_inc_gst,currency,"
        "notes_for_agent"
    )
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert (
        text.splitlines()[1]
        == "1,2025-10-01,Utility Co,'=SUM(1),internet,12.50,AUD,date from created"
    )


def test_render_is_deterministic_for_the_same_rows() -> None:
    row = BillRow(1, "2025-10-01", "Utility Co", "t", "gas", Decimal("1.5"), "AUD", "")
    assert render_bills_bytes([row]) == render_bills_bytes([row])


# --- also a deduction --------------------------------------------------------


def test_a_bill_with_the_deduction_tag_warns_stays_listed_and_is_left_out_of_the_sums(
    tmp_path: Path,
) -> None:
    documents = [
        bill(1, kind=INTERNET, amount="100.00"),
        bill(2, kind=INTERNET, amount="40.00", tags=[INTERNET, DEDUCTION_TAG]),
    ]
    result = run(tmp_path, documents)
    assert bill_warnings(result) == (
        "document 2 is a bill and carries the deduction tag; it stays in deductions.csv or "
        "review.csv and is excluded from the bill totals and WFH figures in estimate.csv",
    )
    rows = {row["paperless_document_id"]: row for row in bills(tmp_path)}
    assert rows["2"]["notes_for_agent"] == "also in deductions.csv; excluded from WFH totals"
    assert rows["2"]["amount_inc_gst"] == "40.00"
    figures = estimate(tmp_path)
    assert figures[("bills", "total")]["count"] == "1"
    assert figures[("bills", "total")]["amount_aud"] == "100.00"
    assert figures[("bills", "internet")]["amount_aud"] == "100.00"
    assert figures[("wfh_evidence", "internet")]["count"] == "1"
    deductions = table(tmp_path / "out" / "deductions.csv")
    assert [row["paperless_document_id"] for row in deductions] == ["2"]
    assert result.estimate_deductions_aud == Decimal("40.00")
    assert result.bills_aud == Decimal("100.00")


def test_negative_and_zero_bills_are_listed_but_not_counted(tmp_path: Path) -> None:
    documents = [
        bill(1, amount="30.00"),
        bill(2, amount="-10.00"),
        bill(3, amount="0.00"),
        bill(4, amount=None),
    ]
    result = run(tmp_path, documents)
    assert result.bills_count == 4
    figures = estimate(tmp_path)
    assert figures[("bills", "total")]["count"] == "2"  # the amount-less bill still counts
    assert figures[("bills", "total")]["amount_aud"] == "30.00"
    assert figures[("wfh_evidence", "internet")]["count"] == "2"


# --- estimate rows -----------------------------------------------------------


def _row(
    doc_id: int, kind: str, amount: str | None, currency: str = "AUD", *, deduction: bool = False
) -> BillRow:
    return BillRow(
        doc_id,
        "2025-10-01",
        "Utility Co",
        f"Bill {doc_id}",
        kind,
        None if amount is None else Decimal(amount),
        currency if amount is not None else "",
        "",
        deduction,
    )


def _section(
    rows: list[BillRow],
    wfh: WfhInputs | None = None,
    *,
    base: str = "0",
    rates: dict[str, Decimal] | None = None,
) -> dict[tuple[str, str], dict[str, object]]:
    result = build_bills_section(rows, rates or {}, wfh or WfhInputs(), Decimal(base))
    return {
        (row.section, row.label): {
            "count": row.count,
            "amount": row.amount_aud,
            "notes": row.notes,
        }
        for row in result.rows
    }


ALL_KINDS = [
    _row(1, "electricity", "200.00"),
    _row(2, "gas", "50.50"),
    _row(3, "internet", "100.00"),
    _row(4, "internet", "20.50"),
    _row(5, "phone", "40.00"),
    _row(6, "", "9.99"),
]


def test_bill_totals_by_kind_in_a_fixed_order() -> None:
    result = build_bills_section(ALL_KINDS, {}, WfhInputs(), Decimal(0))
    bill_rows = [
        (row.label, row.count, row.amount_aud) for row in result.rows if row.section == "bills"
    ]
    assert bill_rows == [
        ("total", 6, Decimal("420.99")),
        ("electricity", 1, Decimal("200.00")),
        ("internet", 2, Decimal("120.50")),
        ("phone", 1, Decimal("40.00")),
        ("gas", 1, Decimal("50.50")),
        ("(no kind)", 1, Decimal("9.99")),
    ]
    assert result.total_aud == Decimal("420.99")
    assert [row.section for row in result.rows] == [
        *["bills"] * 6,
        *["wfh_evidence"] * 4,
        "wfh_fixed_rate",
        *["wfh_actual_cost"] * 4,
        "wfh_note",
    ]


def test_foreign_bills_convert_with_a_rate_and_are_excluded_without_one() -> None:
    rows = [
        _row(1, "phone", "10.00", "USD"),
        _row(2, "phone", "5.00", "EUR"),
        _row(3, "phone", "1.00"),
    ]
    result = build_bills_section(rows, {"USD": Decimal("1.5")}, WfhInputs(), Decimal(0))
    figures = {(row.section, row.label): row for row in result.rows}
    assert figures[("bills", "phone")].count == 3
    assert figures[("bills", "phone")].amount_aud == Decimal("16.00")
    assert figures[("bills", "phone")].notes == "excluded, no rate: EUR 1"
    assert result.unconverted_currencies == ("EUR",)


def test_evidence_counts_and_no_bill_found() -> None:
    figures = _section(
        [_row(1, "gas", "10.00"), _row(2, "electricity", "10.00"), _row(3, "phone", "1")]
    )
    assert figures[("wfh_evidence", "internet")] == {
        "count": 0,
        "amount": None,
        "notes": "no bill found",
    }
    assert figures[("wfh_evidence", "phone")]["count"] == 1
    assert figures[("wfh_evidence", "phone")]["notes"] == ""
    assert figures[("wfh_evidence", "energy")]["count"] == 2
    assert "not tracked" in str(figures[("wfh_evidence", "stationery")]["notes"])
    assert figures[("wfh_evidence", "stationery")]["count"] is None


def test_fixed_rate_is_hours_times_rate() -> None:
    figures = _section(ALL_KINDS, WfhInputs(hours=Decimal("100"), rate_per_hour=Decimal("0.70")))
    assert figures[("wfh_fixed_rate", "hours x rate")]["amount"] == Decimal("70.00")


@pytest.mark.parametrize(
    ("hours", "rate", "expected"),
    [
        ("0.5", "0.01", "0.01"),  # 0.005 rounds half up (half-even would give 0.00)
        ("1.5", "0.01", "0.02"),
        ("3", "0.335", "1.01"),  # 1.005
        ("0", "0.70", "0.00"),
    ],
)
def test_fixed_rate_rounds_half_up(hours: str, rate: str, expected: str) -> None:
    figures = _section([], WfhInputs(hours=Decimal(hours), rate_per_hour=Decimal(rate)))
    assert figures[("wfh_fixed_rate", "hours x rate")]["amount"] == Decimal(expected)


@pytest.mark.parametrize(
    "wfh",
    [WfhInputs(), WfhInputs(hours=Decimal("10")), WfhInputs(rate_per_hour=Decimal("0.7"))],
)
def test_fixed_rate_needs_both_variables(wfh: WfhInputs) -> None:
    figures = _section(ALL_KINDS, wfh, base="500.00")
    row = figures[("wfh_fixed_rate", "hours x rate")]
    assert row["amount"] is None
    assert row["notes"] == "not computed: set WFH_HOURS and WFH_RATE_PER_HOUR"
    assert ("wfh_scenarios", "base deductions + fixed rate") not in figures


FULL = WfhInputs(
    hours=Decimal("100"),
    rate_per_hour=Decimal("0.70"),
    internet_pct=Decimal("50"),
    phone_pct=Decimal("25"),
    energy_amount=Decimal("30.00"),
)


def test_actual_cost_with_every_variable_set() -> None:
    figures = _section(ALL_KINDS, FULL, base="300.00")
    assert figures[("wfh_actual_cost", "internet")]["amount"] == Decimal("60.25")  # 120.50 x 50%
    assert figures[("wfh_actual_cost", "phone")]["amount"] == Decimal("10.00")  # 40.00 x 25%
    assert figures[("wfh_actual_cost", "energy")]["amount"] == Decimal("30.00")
    assert figures[("wfh_actual_cost", "total")]["amount"] == Decimal("100.25")
    assert figures[("wfh_actual_cost", "total")]["notes"] == ""


def test_both_scenarios_add_to_the_base_and_never_replace_it() -> None:
    figures = _section(ALL_KINDS, FULL, base="300.00")
    note = "illustrative; one method per year; not added to the base total"
    assert figures[("wfh_scenarios", "base deductions + fixed rate")] == {
        "count": None,
        "amount": Decimal("370.00"),
        "notes": note,
    }
    assert figures[("wfh_scenarios", "base deductions + actual cost")] == {
        "count": None,
        "amount": Decimal("400.25"),
        "notes": note,
    }
    assert "not also claimed in deductions.csv" in str(
        figures[("wfh_note", "illustrative only")]["notes"]
    )


@pytest.mark.parametrize(
    ("missing", "variable", "total", "missing_label"),
    [
        ("internet_pct", "WFH_INTERNET_PCT", "40.00", "internet"),
        ("phone_pct", "WFH_PHONE_PCT", "90.25", "phone"),
        ("energy_amount", "WFH_ENERGY_AMOUNT", "70.25", "energy"),
    ],
)
def test_actual_cost_with_one_variable_missing_counts_only_the_computed_rows(
    missing: str, variable: str, total: str, missing_label: str
) -> None:
    wfh = WfhInputs(**{**_fields(FULL), missing: None})
    figures = _section(ALL_KINDS, wfh, base="300.00")
    row = figures[("wfh_actual_cost", missing_label)]
    assert row["amount"] is None
    assert row["notes"] == f"not computed: set {variable}"
    assert figures[("wfh_actual_cost", "total")]["amount"] == Decimal(total)
    assert (
        figures[("wfh_actual_cost", "total")]["notes"] == f"partial; not computed: set {variable}"
    )
    scenario = figures[("wfh_scenarios", "base deductions + actual cost")]
    assert scenario["amount"] == Decimal(total) + Decimal("300.00")
    assert str(scenario["notes"]).endswith(f"partial; not computed: set {variable}")


def _fields(wfh: WfhInputs) -> dict[str, Decimal | None]:
    return {
        "hours": wfh.hours,
        "rate_per_hour": wfh.rate_per_hour,
        "internet_pct": wfh.internet_pct,
        "phone_pct": wfh.phone_pct,
        "energy_amount": wfh.energy_amount,
    }


def test_actual_cost_with_nothing_set_has_blank_rows_and_no_scenario() -> None:
    figures = _section(ALL_KINDS)
    total = figures[("wfh_actual_cost", "total")]
    assert total["amount"] is None
    assert total["notes"] == "not computed: set WFH_INTERNET_PCT, WFH_PHONE_PCT, WFH_ENERGY_AMOUNT"
    assert not [key for key in figures if key[0] == "wfh_scenarios"]


def test_actual_cost_percentage_rounds_half_up() -> None:
    wfh = WfhInputs(internet_pct=Decimal("50"))
    figures = _section([_row(1, "internet", "10.01")], wfh)
    assert figures[("wfh_actual_cost", "internet")]["amount"] == Decimal("5.01")  # 5.005


def test_percentage_bounds_zero_and_one_hundred_compute() -> None:
    zero = _section(ALL_KINDS, WfhInputs(internet_pct=Decimal("0")))
    full = _section(ALL_KINDS, WfhInputs(internet_pct=Decimal("100")))
    assert zero[("wfh_actual_cost", "internet")]["amount"] == Decimal("0.00")
    assert full[("wfh_actual_cost", "internet")]["amount"] == Decimal("120.50")


def test_unconverted_internet_bills_are_noted_on_the_actual_cost_row() -> None:
    rows = [_row(1, "internet", "10.00"), _row(2, "internet", "5.00", "EUR")]
    figures = _section(rows, WfhInputs(internet_pct=Decimal("50")))
    row = figures[("wfh_actual_cost", "internet")]
    assert row["amount"] == Decimal("5.00")
    assert "excluded, no rate: EUR 1" in str(row["notes"])


def test_wfh_figures_never_change_the_deductions_pending_or_saving(tmp_path: Path) -> None:
    deduction = document_payload(
        20, tags=[DEDUCTION_TAG], correspondent=5, custom_fields=[{"field": 10, "value": "200.00"}]
    )
    documents = [deduction, bill(1, amount="100.00")]
    plain = run(tmp_path / "plain", documents, Settings(marginal_rate=Decimal("0.3")))
    with_wfh = run(tmp_path / "wfh", documents, Settings(marginal_rate=Decimal("0.3"), wfh=FULL))
    assert plain.estimate_deductions_aud == with_wfh.estimate_deductions_aud == Decimal("200.00")
    assert plain.estimate_pending_aud == with_wfh.estimate_pending_aud
    assert plain.estimated_saving == with_wfh.estimated_saving == Decimal("60.00")
    figures = estimate(tmp_path / "wfh")
    assert figures[("deductions", "total")]["amount_aud"] == "200.00"
    assert figures[("wfh_scenarios", "base deductions + fixed rate")]["amount_aud"] == "270.00"


def test_estimate_row_order_is_fixed(tmp_path: Path) -> None:
    run(tmp_path, [bill(1)], Settings(marginal_rate=Decimal("0.3"), wfh=FULL))
    sections = [row["section"] for row in table(tmp_path / "out" / "estimate.csv")]
    unique = list(dict.fromkeys(sections))
    assert unique == [
        "estimate",
        "deductions",
        "bills",
        "wfh_evidence",
        "wfh_fixed_rate",
        "wfh_actual_cost",
        "wfh_scenarios",
        "wfh_note",
        "wfh_hours_ceiling",
        "pending",
        "pending_no_amount",
        "tax_saving",
    ]


# --- variables ---------------------------------------------------------------


def _sources(environ: dict[str, str], dotenv: dict[str, str] | None = None) -> EnvSources:
    return EnvSources(environ=environ, dotenv=dotenv or {}, env_file=Path("paperless.env"))


def test_unset_variables_are_none() -> None:
    assert resolve_wfh_inputs(_sources({})) == WfhInputs()


def test_valid_variables_are_decimals_from_strings() -> None:
    wfh = resolve_wfh_inputs(
        _sources(
            {
                "WFH_HOURS": "612.5",
                "WFH_RATE_PER_HOUR": " 0.70 ",
                "WFH_INTERNET_PCT": "100",
                "WFH_PHONE_PCT": "0",
                "WFH_ENERGY_AMOUNT": "0",
            }
        )
    )
    assert wfh == WfhInputs(
        hours=Decimal("612.5"),
        rate_per_hour=Decimal("0.70"),
        internet_pct=Decimal("100"),
        phone_pct=Decimal("0"),
        energy_amount=Decimal("0"),
    )


@pytest.mark.parametrize(
    "name",
    ["WFH_HOURS", "WFH_RATE_PER_HOUR", "WFH_INTERNET_PCT", "WFH_PHONE_PCT", "WFH_ENERGY_AMOUNT"],
)
@pytest.mark.parametrize("value", ["", " ", "abc", "-1", "1e3", "NaN", "50%", "1,5"])
def test_bad_variables_name_the_variable_and_source_but_not_the_value(
    name: str, value: str
) -> None:
    with pytest.raises(SettingsError) as raised:
        resolve_wfh_inputs(_sources({name: value}))
    message = str(raised.value)
    assert name in message
    assert f"environment variable {name}" in message
    assert value.strip() == "" or value not in message


@pytest.mark.parametrize("name", ["WFH_INTERNET_PCT", "WFH_PHONE_PCT"])
def test_percentage_above_one_hundred_is_an_error(name: str) -> None:
    with pytest.raises(SettingsError, match=name):
        resolve_wfh_inputs(_sources({name: "101"}))
    with pytest.raises(SettingsError, match=name):
        resolve_wfh_inputs(_sources({name: "100.01"}))


def test_a_fraction_style_percentage_is_taken_literally() -> None:
    assert resolve_wfh_inputs(_sources({"WFH_INTERNET_PCT": "0.5"})).internet_pct == Decimal("0.5")


def test_environment_wins_over_the_dotenv_file_and_errors_name_the_file() -> None:
    sources = _sources({"WFH_HOURS": "10"}, {"WFH_HOURS": "99", "WFH_PHONE_PCT": "x"})
    with pytest.raises(SettingsError, match=r"WFH_PHONE_PCT in env file 'paperless.env'"):
        resolve_wfh_inputs(sources)
    ok = _sources({"WFH_HOURS": "10"}, {"WFH_HOURS": "99"})
    assert resolve_wfh_inputs(ok).hours == Decimal("10")
    assert resolve_wfh_inputs(_sources({}, {"WFH_HOURS": "99"})).hours == Decimal("99")


def test_settings_reject_out_of_range_wfh_inputs() -> None:
    with pytest.raises(SettingsError, match="hours"):
        WfhInputs(hours=Decimal("-1"))
    with pytest.raises(SettingsError, match="phone_pct"):
        WfhInputs(phone_pct=Decimal("101"))
    with pytest.raises(SettingsError, match="internet_pct"):
        WfhInputs(internet_pct=Decimal("NaN"))


# --- command line ------------------------------------------------------------


def test_bad_variable_exits_2_before_any_file_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler([bill(1)]))
    monkeypatch.setenv("WFH_HOURS", "lots")
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 2
    err = capsys.readouterr().err
    assert "WFH_HOURS" in err
    assert "lots" not in err
    assert _written(output) == []


def test_there_are_no_wfh_flags(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(_run_cli(tmp_path, tmp_path / "out", "--wfh-hours", "10"))
    assert raised.value.code == 2


def test_variables_come_from_the_dotenv_file_and_are_recorded_as_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler([bill(1)]))
    env_file = tmp_path / "paperless.env"
    env_file.write_text(
        "WFH_HOURS=100\nWFH_RATE_PER_HOUR=0.70\nWFH_INTERNET_PCT=50\n", encoding="utf-8"
    )
    monkeypatch.setenv("WFH_PHONE_PCT", "25.0")
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--env-file", str(env_file))) == 0
    parameters = json.loads((output / "manifest.json").read_text(encoding="utf-8"))["parameters"]
    assert parameters["wfh_hours"] == "100"
    assert parameters["wfh_rate_per_hour"] == "0.70"
    assert parameters["wfh_internet_pct"] == "50"
    assert parameters["wfh_phone_pct"] == "25.0"
    assert parameters["wfh_energy_amount"] is None


def test_manifest_records_the_bill_settings_canonically(tmp_path: Path) -> None:
    run(tmp_path, [bill(1)])
    text = (tmp_path / "out" / "manifest.json").read_text(encoding="utf-8")
    parameters = json.loads(text)["parameters"]
    assert parameters["bill_document_type"] == "bill"
    assert parameters["bill_kind_tags"] == DEFAULT_BILL_KIND_TAGS
    assert list(parameters["bill_kind_tags"]) == sorted(DEFAULT_BILL_KIND_TAGS)
    assert all(
        parameters[f"wfh_{name}"] is None
        for name in ("hours", "rate_per_hour", "internet_pct", "phone_pct", "energy_amount")
    )
    outputs = sorted(json.loads(text)["outputs"])
    assert outputs == [
        "bills.csv",
        "deductions.csv",
        "estimate.csv",
        "investments.csv",
        "review.csv",
    ]


def test_two_runs_are_byte_identical_and_a_wfh_change_changes_the_manifest(
    tmp_path: Path,
) -> None:
    documents = [bill(1), bill(2, kind=PHONE, amount="USD4.00")]
    run(tmp_path / "a", documents, Settings(wfh=FULL))
    run(tmp_path / "b", documents, Settings(wfh=FULL))
    run(tmp_path / "c", documents, Settings(wfh=WfhInputs(hours=Decimal("1"))))
    for name in ("bills.csv", "estimate.csv", "manifest.json"):
        assert (tmp_path / "a" / "out" / name).read_bytes() == (
            tmp_path / "b" / "out" / name
        ).read_bytes()
    assert (tmp_path / "a" / "out" / "manifest.json").read_bytes() != (
        tmp_path / "c" / "out" / "manifest.json"
    ).read_bytes()


def test_console_prints_the_bills_line_after_the_estimate_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    documents = [bill(1, amount="100.00"), bill(2, amount="-1.00"), bill(3, amount="20.50")]
    _install(monkeypatch, make_handler(documents))
    assert main(_run_cli(tmp_path, tmp_path / "out")) == 0
    lines = capsys.readouterr().out.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("estimate:"))
    assert lines[index + 1] == "bills: 3 listed, 2 counted, AUD 120.50"


def test_console_has_no_bills_line_without_bills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler([]))
    assert main(_run_cli(tmp_path, tmp_path / "out")) == 0
    assert "bills:" not in capsys.readouterr().out


def test_dry_run_plans_bills_csv_writes_nothing_and_matches_a_real_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler([bill(1), bill(2, tags=[INTERNET, DEDUCTION_TAG])]))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--dry-run")) == 0
    dry = capsys.readouterr()
    assert _written(output) == []
    assert "bills.csv would write" in dry.out
    assert main(_run_cli(tmp_path, output)) == 0
    real = capsys.readouterr()
    assert (
        dry.out.replace("would write", "written to").replace("dry run: nothing was written\n", "")
        == real.out
    )
    assert dry.err == real.err
    assert "document 2 is a bill" in real.err


def test_the_files_are_planned_in_the_documented_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler([bill(1)]))
    assert main(_run_cli(tmp_path, tmp_path / "out", "--dry-run")) == 0
    out = capsys.readouterr().out
    planned = [
        line.split(" would write ")[0] for line in out.splitlines() if " would write " in line
    ]
    assert planned == [
        "deductions.csv",
        "review.csv",
        "investments.csv",
        "bills.csv",
        "estimate.csv",
        "manifest.json",
    ]


def test_a_guard_failure_writes_no_bills_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler([bill(1)]))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--expected-count", "9")) == 4
    assert _written(output) == []


def test_warnings_come_after_the_existing_ones_in_a_fixed_order(tmp_path: Path) -> None:
    documents = [
        bill(2, tags=[INTERNET, DEDUCTION_TAG], amount="USD5.00"),
        bill(1, tags=[INTERNET, DEDUCTION_TAG]),
    ]
    result = run(tmp_path, documents)
    assert [warning.split(" is a bill")[0] for warning in result.warnings[-3:-1]] == [
        "document 1",
        "document 2",
    ]
    assert result.warnings[-1] == "document type 'salary' not found in Paperless; no payslip hours"
    assert result.warnings[0].startswith("correspondent 'Interactive Brokers' not found")
    assert result.warnings[1].startswith("no exchange rate for USD")
    assert MISSING_TYPE_WARNING not in result.warnings


def test_bills_in_a_foreign_currency_without_a_rate_join_the_missing_rate_warning(
    tmp_path: Path,
) -> None:
    result = run(tmp_path, [bill(1, amount="USD5.00")])
    assert any(warning.startswith("no exchange rate for USD") for warning in result.warnings)
    figures = estimate(tmp_path)
    assert figures[("bills", "total")]["amount_aud"] == "0.00"
    assert figures[("bills", "total")]["notes"] == "excluded, no rate: USD 1"


# --- review revision ---------------------------------------------------------


def test_a_deduction_tagged_bill_with_a_review_code_is_excluded_and_warned(
    tmp_path: Path,
) -> None:
    documents = [
        bill(1, kind=INTERNET, amount="100.00"),
        bill(2, kind=INTERNET, amount="0.00", tags=[INTERNET, DEDUCTION_TAG]),
    ]
    result = run(tmp_path, documents)
    assert bill_warnings(result) == (
        "document 2 is a bill and carries the deduction tag; it stays in deductions.csv or "
        "review.csv and is excluded from the bill totals and WFH figures in estimate.csv",
    )
    rows = {row["paperless_document_id"]: row for row in bills(tmp_path)}
    assert rows["2"]["notes_for_agent"] == (
        "zero amount; also in deductions.csv; excluded from WFH totals"
    )
    review = table(tmp_path / "out" / "review.csv")
    assert [row["paperless_document_id"] for row in review] == ["2"]
    figures = estimate(tmp_path)
    assert figures[("bills", "total")]["count"] == "1"
    assert figures[("bills", "total")]["amount_aud"] == "100.00"
    assert figures[("wfh_evidence", "internet")]["count"] == "1"
    assert (result.bills_count, result.bills_counted) == (2, 1)


@pytest.mark.parametrize("kind", ["internet", "phone"])
def test_actual_cost_row_says_when_no_bills_were_found_and_the_total_is_partial(
    kind: str,
) -> None:
    wfh = WfhInputs(
        internet_pct=Decimal("50"), phone_pct=Decimal("25"), energy_amount=Decimal("30.00")
    )
    other = "phone" if kind == "internet" else "internet"
    figures = _section([_row(1, other, "100.00")], wfh, base="300.00")
    row = figures[("wfh_actual_cost", kind)]
    assert row["amount"] == Decimal("0.00")
    assert row["notes"] == f"no {kind} bills found"
    total = figures[("wfh_actual_cost", "total")]
    assert total["notes"] == f"partial; no {kind} bills found"
    scenario = figures[("wfh_scenarios", "base deductions + actual cost")]
    assert scenario["notes"] == (
        f"illustrative; one method per year; not added to the base total; "
        f"partial; no {kind} bills found"
    )
    assert scenario["amount"] == Decimal(total["amount"]) + Decimal("300.00")  # type: ignore[arg-type]


def test_actual_cost_total_combines_missing_variables_and_empty_kinds() -> None:
    wfh = WfhInputs(internet_pct=Decimal("50"), energy_amount=Decimal("30.00"))
    figures = _section([], wfh)
    assert figures[("wfh_actual_cost", "total")]["notes"] == (
        "partial; not computed: set WFH_PHONE_PCT; no internet bills found"
    )


def test_the_wfh_note_is_attributed_to_the_owner() -> None:
    figures = _section([])
    notes = str(figures[("wfh_note", "illustrative only")]["notes"])
    assert notes.startswith("per your own notes: ")


@pytest.mark.parametrize(
    "settings_kwargs",
    [
        {"bill_kind_tags": {**DEFAULT_BILL_KIND_TAGS, "gas": "tax deductible"}},
        {"bill_kind_tags": {**DEFAULT_BILL_KIND_TAGS, "gas": "asset-reviewed"}},
        {
            "deduction_tag": "internet",
        },
        {"asset_reviewed_tag": "phone"},
    ],
)
def test_settings_reject_a_kind_tag_that_is_the_deduction_or_reviewed_tag(
    settings_kwargs: dict[str, object],
) -> None:
    with pytest.raises(SettingsError, match="deduction_tag or asset_reviewed_tag"):
        Settings(**settings_kwargs)  # type: ignore[arg-type]


def test_a_disabled_reviewed_tag_does_not_clash_with_kind_tags() -> None:
    Settings(asset_reviewed_tag=None)


def test_missing_kind_tags_warn_once_when_bills_are_selected(tmp_path: Path) -> None:
    kind_tags = {**DEFAULT_BILL_KIND_TAGS, "gas": "Gas", "phone": "mobile"}
    result = run(tmp_path, [bill(1)], Settings(bill_kind_tags=kind_tags))
    assert bill_warnings(result) == (
        "bill kind tags not found in Paperless: Gas, mobile; names match case-sensitively "
        "and bills cannot get those kinds",
    )


def test_missing_kind_tags_are_silent_without_bills_or_when_all_exist(tmp_path: Path) -> None:
    kind_tags = {**DEFAULT_BILL_KIND_TAGS, "gas": "Gas"}
    assert bill_warnings(run(tmp_path / "a", [], Settings(bill_kind_tags=kind_tags))) == ()
    assert bill_warnings(run(tmp_path / "b", [bill(1)])) == ()


def test_kind_tag_names_match_case_sensitively(tmp_path: Path) -> None:
    kind_tags = {**DEFAULT_BILL_KIND_TAGS, "internet": "Internet"}
    run(tmp_path, [bill(1, tags=[INTERNET])], Settings(bill_kind_tags=kind_tags))
    (row,) = bills(tmp_path)
    assert (row["kind"], row["notes_for_agent"]) == ("", "no kind tag")
