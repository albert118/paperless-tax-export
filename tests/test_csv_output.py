"""Tests for the deterministic CSV byte contract."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paperless_tax_export.classify import ClassifiedRow
from paperless_tax_export.csv_output import (
    DEDUCTIONS_COLUMNS,
    INVESTMENTS_COLUMNS,
    REVIEW_COLUMNS,
    write_csv,
)
from paperless_tax_export.parsing import ReviewCode

BOM = "\N{ZERO WIDTH NO-BREAK SPACE}"  # U+FEFF, the UTF-8 byte order mark


def make_row(
    doc_id: int,
    *,
    date: str = "2025-10-01",
    supplier_name: str = "Acme Pty Ltd",
    description: str = "Grocery run",
    amount: Decimal | None = Decimal("123.40"),
    currency: str = "AUD",
    document_type: str = "Receipt",
    notes: str = "",
    title: str = "Grocery run",
    correspondent: str = "Coles",
    created: str = "2025-10-01",
    modified: str = "2025-10-01",
    tags: tuple[str, ...] = (),
    codes: tuple[ReviewCode, ...] = (),
    raw_amount: str = "",
    raw_date: str = "",
) -> ClassifiedRow:
    """A fully populated, accepted-style row with per-test overrides."""
    return ClassifiedRow(
        document_id=doc_id,
        date=date,
        supplier_name=supplier_name,
        description=description,
        amount_inc_gst=amount,
        currency=currency,
        document_type=document_type,
        document_modified=modified,
        notes_for_agent=notes,
        title=title,
        correspondent=correspondent,
        created=created,
        tags=tags,
        reason_codes=codes,
        raw_amount=raw_amount,
        raw_date=raw_date,
    )


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def header_of(path: Path) -> str:
    return read_bytes(path).decode("utf-8-sig").split("\r\n")[0]


def first_data_cells(path: Path) -> list[str]:
    return read_bytes(path).decode("utf-8-sig").split("\r\n")[1].split(",")


def test_output_has_bom_and_crlf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(path, rows=[make_row(1)], columns=REVIEW_COLUMNS)
    data = read_bytes(path)
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert data.endswith(b"\r\n")


def test_header_row_lists_review_columns(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(path, rows=[make_row(1)], columns=REVIEW_COLUMNS)
    assert header_of(path) == ",".join(name for name, _kind in REVIEW_COLUMNS)


def test_negative_decimal_is_not_formula_guarded(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(
        path,
        rows=[make_row(1, amount=Decimal("-12.34"))],
        columns=REVIEW_COLUMNS,
    )
    text = read_bytes(path).decode("utf-8-sig")
    assert "-12.34" in text
    assert "'-12.34" not in text


def test_review_text_cells_are_formula_guarded(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    row = make_row(1, correspondent="@mention", title="-hyphen", notes="=cmd")
    write_csv(path, rows=[row], columns=REVIEW_COLUMNS)
    cells = first_data_cells(path)
    assert cells[3] == "'@mention"
    assert cells[4] == "'-hyphen"
    assert cells[9] == "'=cmd"


def test_deductions_text_cells_are_formula_guarded(tmp_path: Path) -> None:
    path = tmp_path / "deductions.csv"
    row = make_row(1, supplier_name="=SUM(A1)", description="+formula", document_type="\t")
    write_csv(path, rows=[row], columns=DEDUCTIONS_COLUMNS)
    cells = first_data_cells(path)
    assert cells[2] == "'=SUM(A1)"
    assert cells[3] == "'+formula"
    assert cells[6] == "'\t"


def test_null_cells_render_blank(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(
        path,
        rows=[make_row(1, amount=None, document_type="")],
        columns=REVIEW_COLUMNS,
    )
    cells = first_data_cells(path)
    assert len(cells) == len(REVIEW_COLUMNS)
    assert cells[5] == ""  # amount_inc_gst
    assert cells[6] == "AUD"  # currency is a separate column
    assert cells[7] == ""  # document_type


def test_currency_follows_the_amount_and_is_blank_without_one(tmp_path: Path) -> None:
    path = tmp_path / "deductions.csv"
    write_csv(path, rows=[make_row(1, amount=None, currency="")], columns=DEDUCTIONS_COLUMNS)
    cells = first_data_cells(path)
    assert cells[4] == ""  # amount_inc_gst
    assert cells[5] == ""  # currency


def test_every_currency_is_written_with_two_decimals(tmp_path: Path) -> None:
    path = tmp_path / "deductions.csv"
    rows = [
        make_row(1, amount=Decimal("1500"), currency="JPY"),
        make_row(2, amount=Decimal("5.5"), currency="USD"),
    ]
    write_csv(path, rows=rows, columns=DEDUCTIONS_COLUMNS)
    lines = read_bytes(path).decode("utf-8-sig").split("\r\n")
    assert lines[1].split(",")[4:6] == ["1500.00", "JPY"]
    assert lines[2].split(",")[4:6] == ["5.50", "USD"]


def test_rows_are_sorted_by_date_then_document_id(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    rows = [
        make_row(9, date="2025-10-01"),
        make_row(1, date="2025-07-01"),
        make_row(3, date="2025-10-01"),
    ]
    write_csv(path, rows=rows, columns=REVIEW_COLUMNS)
    text = read_bytes(path).decode("utf-8-sig")
    lines = text.split("\r\n")[1:-1]
    assert lines[0].startswith("1,")
    assert lines[1].startswith("3,")
    assert lines[2].startswith("9,")


def test_reason_codes_are_joined_with_semicolons(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(
        path,
        rows=[
            make_row(
                1,
                codes=(ReviewCode.NEGATIVE_AMOUNT, ReviewCode.INCOME_TAGGED),
                amount=Decimal("-5.00"),
            )
        ],
        columns=REVIEW_COLUMNS,
    )
    assert first_data_cells(path)[1] == "NEGATIVE_AMOUNT;INCOME_TAGGED"


def _two_row_fixture() -> list[ClassifiedRow]:
    return [
        make_row(
            7,
            date="2025-10-01",
            description="Grocery run",
            amount=Decimal("123.40"),
            document_type="Receipt",
            title="Grocery run",
            correspondent="Coles",
            created="2025-10-01",
            modified="2025-10-01",
            tags=("personal", "low-value"),
            raw_amount="123.40",
            raw_date="2025-10-01",
        ),
        make_row(
            2,
            date="2025-07-15",
            supplier_name="=SUM(A1)",
            description="Invoice",
            amount=Decimal("-12.34"),
            currency="USD",
            document_type="",
            notes="amount missing",
            title="Invoice",
            correspondent="",
            created="2025-07-15",
            modified="2025-07-15",
            tags=("income",),
            codes=(ReviewCode.NEGATIVE_AMOUNT, ReviewCode.INCOME_TAGGED),
            raw_amount="-12.34",
            raw_date="",
        ),
    ]


def test_full_expected_review_bytes_for_a_two_row_fixture(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(path, rows=_two_row_fixture(), columns=REVIEW_COLUMNS)

    row_two = (
        "2,NEGATIVE_AMOUNT;INCOME_TAGGED,2025-07-15,,Invoice,-12.34,USD,,2025-07-15,amount missing"
    )
    row_seven = "7,,2025-10-01,Coles,Grocery run,123.40,AUD,Receipt,2025-10-01,"
    expected = BOM + "\r\n".join([EXPECTED_REVIEW_HEADER, row_two, row_seven]) + "\r\n"
    assert read_bytes(path) == expected.encode("utf-8")


def test_full_expected_deductions_bytes_for_a_two_row_fixture(tmp_path: Path) -> None:
    path = tmp_path / "deductions.csv"
    write_csv(path, rows=_two_row_fixture(), columns=DEDUCTIONS_COLUMNS)

    row_two = "2,2025-07-15,'=SUM(A1),Invoice,-12.34,USD,,amount missing"
    row_seven = "7,2025-10-01,Acme Pty Ltd,Grocery run,123.40,AUD,Receipt,"
    expected = BOM + "\r\n".join([EXPECTED_DEDUCTIONS_HEADER, row_two, row_seven]) + "\r\n"
    assert read_bytes(path) == expected.encode("utf-8")


def test_deductions_file_uses_the_deduction_column_contract(tmp_path: Path) -> None:
    path = tmp_path / "deductions.csv"
    write_csv(path, rows=[make_row(1)], columns=DEDUCTIONS_COLUMNS)
    header = header_of(path)
    assert header == ",".join(name for name, _kind in DEDUCTIONS_COLUMNS)
    assert "title" not in header
    assert "reason_codes" not in header


EXPECTED_DEDUCTIONS_HEADER = (
    "paperless_document_id,date,supplier_name,description,amount_inc_gst,currency,"
    "document_type,notes_for_agent"
)
EXPECTED_REVIEW_HEADER = (
    "paperless_document_id,reason_codes,date,correspondent,title,amount_inc_gst,"
    "currency,document_type,created,notes_for_agent"
)
EXPECTED_INVESTMENTS_HEADER = "paperless_document_id,title,created,document_type,correspondent"

REMOVED_FROM_DEDUCTIONS = ("gst_amount", "work_use_pct", "document_modified")
REMOVED_FROM_REVIEW = (
    "supplier_name",
    "description",
    "gst_amount",
    "work_use_pct",
    "document_modified",
    "tags",
    "raw_amount",
    "raw_date",
)
REMOVED_EARLIER = (
    "category_label",
    "claim_type",
    "apportionment_basis",
    "reimbursed",
    "wfh_method",
    "asset_cost_inc_gst",
    "asset_set_id",
)


def test_deductions_header_is_pinned() -> None:
    assert ",".join(name for name, _kind in DEDUCTIONS_COLUMNS) == EXPECTED_DEDUCTIONS_HEADER


def test_review_header_is_pinned() -> None:
    assert ",".join(name for name, _kind in REVIEW_COLUMNS) == EXPECTED_REVIEW_HEADER


def test_investments_header_is_pinned() -> None:
    assert ",".join(INVESTMENTS_COLUMNS) == EXPECTED_INVESTMENTS_HEADER


def test_dropped_columns_are_absent_from_the_written_headers(tmp_path: Path) -> None:
    deductions = tmp_path / "deductions.csv"
    review = tmp_path / "review.csv"
    write_csv(deductions, rows=[make_row(1)], columns=DEDUCTIONS_COLUMNS)
    write_csv(review, rows=[make_row(1)], columns=REVIEW_COLUMNS)
    deductions_header = set(header_of(deductions).split(","))
    review_header = set(header_of(review).split(","))
    assert not set(REMOVED_FROM_DEDUCTIONS + REMOVED_EARLIER) & deductions_header
    assert not set(REMOVED_FROM_REVIEW + REMOVED_EARLIER) & review_header


def test_duplicate_write_is_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    rows = [make_row(1), make_row(2, amount=Decimal("-3.00"), tags=("x",))]
    write_csv(first, rows=rows, columns=REVIEW_COLUMNS)
    write_csv(second, rows=rows, columns=REVIEW_COLUMNS)
    assert read_bytes(first) == read_bytes(second)
