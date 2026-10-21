"""Tests for the deterministic CSV byte contract."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from paperless_tax_export.classify import ClassifiedRow
from paperless_tax_export.csv_output import (
    DEDUCTIONS_COLUMNS,
    REVIEW_COLUMNS,
    write_csv,
)
from paperless_tax_export.parsing import ReviewCode

BOM = "\ufeff"


def make_row(
    doc_id: int,
    *,
    date: str = "2025-10-01",
    supplier_name: str = "Acme Pty Ltd",
    description: str = "Grocery run",
    amount: Decimal | None = Decimal("123.40"),
    gst: Decimal | None = Decimal("0.00"),
    document_type: str = "Receipt",
    work_use: float | None = 100.0,
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
        gst_amount=gst,
        document_type=document_type,
        work_use_pct=work_use,
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
    header = read_bytes(path).decode("utf-8-sig").split("\r\n")[0]
    assert header == ",".join(name for name, _kind in REVIEW_COLUMNS)


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


def test_text_cells_are_formula_guarded(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    row = make_row(
        1,
        supplier_name="=SUM(A1)",
        description="+formula",
        correspondent="@mention",
        title="-hyphen",
        tags=("\t",),
    )
    write_csv(path, rows=[row], columns=REVIEW_COLUMNS)
    text = read_bytes(path).decode("utf-8-sig")
    assert "'=SUM(A1)" in text
    assert "'+formula" in text
    assert "'@mention" in text
    assert "'-hyphen" in text
    assert "'\t" in text


def test_null_cells_render_blank(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(
        path,
        rows=[make_row(1, amount=None, gst=None, work_use=None, document_type="")],
        columns=REVIEW_COLUMNS,
    )
    text = read_bytes(path).decode("utf-8-sig")
    line = text.split("\r\n")[1]
    cells = line.split(",")
    assert cells[4] == ""  # amount_inc_gst
    assert cells[5] == ""  # gst_amount
    assert cells[7] == ""  # work_use_pct
    assert cells[6] == ""  # document_type


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


def test_tags_and_reason_codes_use_their_own_separators(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(
        path,
        rows=[
            make_row(
                1,
                tags=("low-value", "personal"),
                codes=(ReviewCode.NEGATIVE_AMOUNT, ReviewCode.INCOME_TAGGED),
                amount=Decimal("-5.00"),
            )
        ],
        columns=REVIEW_COLUMNS,
    )
    text = read_bytes(path).decode("utf-8-sig")
    assert "low-value; personal" in text
    assert "NEGATIVE_AMOUNT;INCOME_TAGGED" in text


def test_work_use_pct_renders_from_the_raw_float(tmp_path: Path) -> None:
    path = tmp_path / "review.csv"
    write_csv(path, rows=[make_row(1, work_use=100.0)], columns=REVIEW_COLUMNS)
    text = read_bytes(path).decode("utf-8-sig")
    line = text.split("\r\n")[1]
    assert line.split(",")[7] == "100.00"


def test_full_expected_bytes_for_a_two_row_fixture(tmp_path: Path) -> None:
    rows = [
        make_row(
            7,
            date="2025-10-01",
            description="Grocery run",
            amount=Decimal("123.40"),
            gst=Decimal("0.00"),
            document_type="Receipt",
            work_use=100.0,
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
            gst=None,
            document_type="",
            work_use=None,
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
    path = tmp_path / "review.csv"
    write_csv(path, rows=rows, columns=REVIEW_COLUMNS)

    header = ",".join(name for name, _kind in REVIEW_COLUMNS)
    row_two = (
        "2,2025-07-15,'=SUM(A1),Invoice,-12.34,,,,2025-07-15,amount missing,"
        "Invoice,,2025-07-15,income,NEGATIVE_AMOUNT;INCOME_TAGGED,'-12.34,"
    )
    row_seven = (
        "7,2025-10-01,Acme Pty Ltd,Grocery run,123.40,0.00,Receipt,100.00,"
        "2025-10-01,,Grocery run,Coles,2025-10-01,low-value; personal,,123.40,2025-10-01"
    )
    expected = BOM + "\r\n".join([header, row_two, row_seven]) + "\r\n"
    assert read_bytes(path) == expected.encode("utf-8")


def test_deductions_file_uses_the_deduction_column_contract(tmp_path: Path) -> None:
    path = tmp_path / "deductions.csv"
    write_csv(path, rows=[make_row(1)], columns=DEDUCTIONS_COLUMNS)
    text = read_bytes(path).decode("utf-8-sig")
    header = text.split("\r\n")[0]
    assert header == ",".join(name for name, _kind in DEDUCTIONS_COLUMNS)
    assert "title" not in header
    assert "reason_codes" not in header


EXPECTED_DEDUCTIONS_HEADER = (
    "paperless_document_id,date,supplier_name,description,amount_inc_gst,gst_amount,"
    "document_type,work_use_pct,document_modified,notes_for_agent"
)
REMOVED_COLUMNS = (
    "category_label",
    "claim_type",
    "apportionment_basis",
    "reimbursed",
    "wfh_method",
    "asset_cost_inc_gst",
    "asset_set_id",
)


def test_deductions_columns_are_pinned() -> None:
    assert ",".join(name for name, _kind in DEDUCTIONS_COLUMNS) == EXPECTED_DEDUCTIONS_HEADER
    assert len(DEDUCTIONS_COLUMNS) == 10


def test_removed_columns_are_absent_from_both_headers(tmp_path: Path) -> None:
    for filename, columns in (
        ("deductions.csv", DEDUCTIONS_COLUMNS),
        ("review.csv", REVIEW_COLUMNS),
    ):
        path = tmp_path / filename
        write_csv(path, rows=[make_row(1)], columns=columns)
        header = read_bytes(path).decode("utf-8-sig").split("\r\n")[0].split(",")
        assert "work_use_pct" in header
        assert not set(REMOVED_COLUMNS) & set(header)


def test_duplicate_write_is_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    rows = [make_row(1), make_row(2, amount=Decimal("-3.00"), tags=("x",))]
    write_csv(first, rows=rows, columns=REVIEW_COLUMNS)
    write_csv(second, rows=rows, columns=REVIEW_COLUMNS)
    assert read_bytes(first) == read_bytes(second)
