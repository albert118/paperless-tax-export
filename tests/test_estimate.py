"""Tests for estimate.csv: rates file, marginal rate, arithmetic, bytes and the run wiring."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import document_payload
from test_csv_output import make_row
from test_manifest import _install, _run_cli, make_handler

from paperless_tax_export.classify import ClassifiedRow
from paperless_tax_export.cli import main
from paperless_tax_export.env_config import EnvSources, resolve_marginal_rate
from paperless_tax_export.errors import EstimateError, SettingsError
from paperless_tax_export.estimate import (
    ESTIMATE_COLUMNS,
    Estimate,
    build_estimate,
    load_rates,
    parse_rates,
    render_estimate_bytes,
)
from paperless_tax_export.settings import Settings

BOM = b"\xef\xbb\xbf"
RATES = {"USD": Decimal("1.5"), "EUR": Decimal("1.6")}
RATES_CSV = "currency,rate_to_aud\r\nUSD,1.5\r\nEUR,1.6\r\n"


def _row(
    doc_id: int,
    amount: str | None,
    *,
    currency: str = "AUD",
    date: str = "2025-10-05",
    document_type: str = "Receipt",
) -> ClassifiedRow:
    return make_row(
        doc_id,
        amount=None if amount is None else Decimal(amount),
        currency=currency if amount is not None else "",
        date=date,
        document_type=document_type,
    )


def _estimate(
    accepted: list[ClassifiedRow],
    review: list[ClassifiedRow] | None = None,
    rates: dict[str, Decimal] | None = None,
    marginal_rate: Decimal | None = None,
) -> Estimate:
    return build_estimate(accepted, review or [], RATES if rates is None else rates, marginal_rate)


def _by_key(estimate: Estimate) -> dict[tuple[str, str], tuple[str, ...]]:
    """``(section, label) -> (count, currency, amount, rate, amount_aud, notes)`` as CSV text."""
    return {(r[0], r[1]): tuple(r[2:]) for r in _parsed(render_estimate_bytes(estimate))[1:]}


def _parsed(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"), newline="")))


def _aud(estimate: Estimate, section: str, label: str) -> str:
    return _by_key(estimate)[(section, label)][4]


# --- totals ------------------------------------------------------------------


def test_totals_mix_aud_converted_foreign_and_unconverted_currencies() -> None:
    accepted = [
        _row(1, "100.00"),
        _row(2, "500.00"),  # an over-300 item cleared by the reviewed tag counts in full
        _row(3, "10.00", currency="USD"),
        _row(4, "1000.00", currency="JPY"),
    ]
    review = [
        _row(5, "20.00"),
        _row(6, "-4.00", currency="USD"),
        _row(7, None),
        _row(8, "50.00", currency="JPY"),
    ]
    estimate = _estimate(accepted, review)
    table = _by_key(estimate)

    assert estimate.deductions_aud == Decimal("615.00")
    assert estimate.pending_aud == Decimal("14.00")
    assert estimate.unconverted_currencies == ("JPY",)
    assert table[("deductions", "total")] == (
        "4",
        "AUD",
        "",
        "",
        "615.00",
        "excluded, no rate: JPY 1",
    )
    assert table[("deductions_currency", "AUD")] == ("2", "AUD", "600.00", "", "600.00", "")
    assert table[("deductions_currency", "USD")] == ("1", "USD", "10.00", "1.5", "15.00", "")
    assert table[("deductions_currency", "JPY")] == (
        "1",
        "JPY",
        "1000.00",
        "",
        "",
        "no rate; excluded from the AUD total",
    )
    assert table[("pending", "review rows with an amount")] == (
        "3",
        "AUD",
        "",
        "",
        "14.00",
        "upper bound: includes suspected duplicates and items that may never be deductions"
        "; excluded, no rate: JPY 1",
    )
    assert table[("pending_no_amount", "review rows without an amount")] == (
        "1",
        "",
        "",
        "",
        "",
        "",
    )


def test_negative_pending_amounts_are_included_in_the_pending_total() -> None:
    estimate = _estimate([], [_row(1, "-30.00"), _row(2, "10.00")])
    assert estimate.pending_aud == Decimal("-20.00")


def test_empty_run_has_zero_totals_and_the_fixed_rows() -> None:
    estimate = _estimate([])
    assert [(row.section, row.label) for row in estimate.rows] == [
        ("estimate", "ESTIMATE only; not tax advice"),
        ("deductions", "total"),
        ("pending", "review rows with an amount"),
        ("pending_no_amount", "review rows without an amount"),
    ]
    assert estimate.deductions_aud == Decimal(0)


# --- grouping ----------------------------------------------------------------


def test_groups_by_type_and_month_with_blank_type_and_fy_boundaries() -> None:
    accepted = [
        _row(1, "10.00", date="2025-07-01", document_type="Receipt"),
        _row(2, "20.00", date="2025-07-31", document_type=""),
        _row(3, "30.00", date="2026-06-30", document_type="Invoice"),
        _row(4, "5.00", date="2026-06-01", document_type="Invoice"),
        _row(5, "2.00", currency="USD", date="2025-07-15", document_type="Receipt"),
        _row(6, "9.00", currency="JPY", date="2025-07-16", document_type="Receipt"),
    ]
    table = _by_key(_estimate(accepted))
    types = {label: v for (section, label), v in table.items() if section == "deductions_by_type"}
    months = {label: v for (section, label), v in table.items() if section == "deductions_by_month"}
    assert types == {
        "(none)": ("1", "AUD", "", "", "20.00", ""),
        "Invoice": ("2", "AUD", "", "", "35.00", ""),
        "Receipt": ("3", "AUD", "", "", "13.00", "excluded, no rate: JPY 1"),
    }
    assert months == {
        "2025-07": ("4", "AUD", "", "", "33.00", "excluded, no rate: JPY 1"),
        "2026-06": ("2", "AUD", "", "", "35.00", ""),
    }


def test_row_order_is_fixed_and_independent_of_input_order() -> None:
    accepted = [
        _row(
            i, f"{i}.00", currency=code, date=f"2025-{7 + i % 6:02d}-0{1 + i % 9}", document_type=t
        )
        for i, (code, t) in enumerate(
            [("AUD", "B"), ("USD", "A"), ("JPY", ""), ("EUR", "C"), ("AUD", "A"), ("USD", "B")],
            start=1,
        )
    ]
    shuffled = accepted[:]
    random.Random(7).shuffle(shuffled)
    first = render_estimate_bytes(_estimate(accepted, marginal_rate=Decimal("0.3")))
    second = render_estimate_bytes(_estimate(shuffled, marginal_rate=Decimal("0.3")))
    assert first == second
    sections = [row[0] for row in _parsed(first)[1:]]
    order = [
        "estimate",
        "deductions",
        "deductions_currency",
        "deductions_by_type",
        "deductions_by_month",
        "pending",
        "pending_no_amount",
        "tax_saving",
    ]
    assert sorted(set(sections), key=sections.index) == order
    currencies = [row[1] for row in _parsed(first)[1:] if row[0] == "deductions_currency"]
    assert currencies == sorted(currencies)


# --- arithmetic --------------------------------------------------------------


def test_half_a_cent_rounds_half_up_not_half_even() -> None:
    estimate = _estimate([_row(1, "0.01", currency="USD")], rates={"USD": Decimal("0.5")})
    assert estimate.deductions_aud == Decimal("0.01")  # ROUND_HALF_EVEN would give 0.00


def test_negative_half_cent_rounds_away_from_zero() -> None:
    estimate = _estimate([], [_row(1, "-0.01", currency="USD")], rates={"USD": Decimal("0.5")})
    assert estimate.pending_aud == Decimal("-0.01")


def test_each_row_is_rounded_to_cents_before_summing() -> None:
    rows = [_row(1, "0.01", currency="USD"), _row(2, "0.01", currency="USD")]
    estimate = _estimate(rows, rates={"USD": Decimal("0.5")})
    # per-row rounding: 0.01 + 0.01 = 0.02; rounding the sum (0.01 exact) would give 0.01
    assert estimate.deductions_aud == Decimal("0.02")
    assert _aud(estimate, "deductions_currency", "USD") == "0.02"


def test_tax_saving_is_total_times_rate_rounded_half_up() -> None:
    estimate = _estimate([_row(1, "0.05")], marginal_rate=Decimal("0.5"))
    assert estimate.tax_saving == Decimal("0.03")  # 0.025 rounds up


def test_tax_saving_row_exists_only_with_a_marginal_rate() -> None:
    without = _by_key(_estimate([_row(1, "100.00")]))
    assert not [key for key in without if key[0] == "tax_saving"]
    with_rate = _by_key(_estimate([_row(1, "100.00")], marginal_rate=Decimal("0.32")))
    assert with_rate[("tax_saving", "estimated saving at marginal rate 0.32")] == (
        "",
        "AUD",
        "",
        "",
        "32.00",
        "rough: marginal rate only, ignores Medicare levy, offsets and the rest of your return",
    )


@pytest.mark.parametrize(("rate", "saving"), [("0", "0.00"), ("1", "100.00")])
def test_marginal_rate_bounds_give_a_row(rate: str, saving: str) -> None:
    estimate = _estimate([_row(1, "100.00")], marginal_rate=Decimal(rate))
    assert estimate.tax_saving == Decimal(saving)


def test_unconverted_rows_are_excluded_from_the_tax_saving_base() -> None:
    estimate = _estimate(
        [_row(1, "100.00"), _row(2, "900.00", currency="JPY")], marginal_rate=Decimal("0.5")
    )
    assert estimate.tax_saving == Decimal("50.00")


# --- rendering ---------------------------------------------------------------


def test_first_data_row_states_it_is_an_estimate_and_columns_are_fixed() -> None:
    parsed = _parsed(render_estimate_bytes(_estimate([_row(1, "1.00")])))
    assert tuple(parsed[0]) == ESTIMATE_COLUMNS
    assert ESTIMATE_COLUMNS == (
        "section",
        "label",
        "count",
        "currency",
        "amount",
        "rate_to_aud",
        "amount_aud",
        "notes",
    )
    assert parsed[1][:2] == ["estimate", "ESTIMATE only; not tax advice"]
    assert "ignores the rest of your return" in parsed[1][7]


def test_bytes_follow_the_csv_contract() -> None:
    content = render_estimate_bytes(_estimate([_row(1, "-2.50")], [_row(2, "-1.00")]))
    assert content.startswith(BOM)
    body = content[len(BOM) :]
    assert body.endswith(b"\r\n")
    assert b"\n" not in body.replace(b"\r\n", b"")
    assert b"-2.50" in body  # amounts are never formula-guarded
    assert b"'-2.50" not in body


def test_text_cells_are_formula_guarded() -> None:
    accepted = [_row(1, "1.00", document_type="=SUM(1)"), _row(2, "1.00", document_type="@x")]
    labels = [row[1] for row in _parsed(render_estimate_bytes(_estimate(accepted)))]
    assert "'=SUM(1)" in labels
    assert "'@x" in labels
    assert "=SUM(1)" not in labels


def test_hostile_document_type_is_guarded_and_kept_in_one_cell() -> None:
    hostile = '+cmd|" /C calc"!A1,\r\nx'
    parsed = _parsed(render_estimate_bytes(_estimate([_row(1, "1.00", document_type=hostile)])))
    labels = [row[1] for row in parsed if row[0] == "deductions_by_type"]
    assert labels == ["'" + hostile]
    assert all(len(row) == len(ESTIMATE_COLUMNS) for row in parsed)


# --- rates file --------------------------------------------------------------


def test_valid_rates_file_parses_exact_decimals() -> None:
    assert parse_rates(RATES_CSV.encode(), "r.csv") == {
        "USD": Decimal("1.5"),
        "EUR": Decimal("1.6"),
    }


def test_rates_file_accepts_a_bom_and_lf_endings() -> None:
    assert parse_rates(BOM + b"currency,rate_to_aud\nUSD,0.65\n", "r.csv") == {
        "USD": Decimal("0.65")
    }


def test_header_only_rates_file_is_empty_not_an_error() -> None:
    assert parse_rates(b"currency,rate_to_aud\r\n", "r.csv") == {}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"currency,rate_to_aud\nUSD,1.5\nUSD,1.6\n", "line 3: duplicate currency"),
        (b"currency,rate_to_aud\nAUD,1\n", "line 2: currency must not be AUD"),
        (b"currency,rate_to_aud\nusd,1.5\n", "line 2: currency must be three uppercase letters"),
        (b"currency,rate_to_aud\nUS,1.5\n", "line 2: currency must be three uppercase letters"),
        (b"currency,rate_to_aud\nUSD,0\n", "line 2: rate_to_aud must be greater than zero"),
        (b"currency,rate_to_aud\nUSD,0.00\n", "line 2: rate_to_aud must be greater than zero"),
        (b"currency,rate_to_aud\nUSD,-1.5\n", "line 2: rate_to_aud must be a positive decimal"),
        (b"currency,rate_to_aud\nUSD,1e0\n", "line 2: rate_to_aud must be a positive decimal"),
        (b"currency,rate_to_aud\nUSD,1,500.5\n", "line 2: expected 2 columns"),
        (b'currency,rate_to_aud\nUSD,"1,500.5"\n', "line 2: rate_to_aud must be a positive"),
        (b"currency,rate_to_aud\nUSD,NaN\n", "line 2: rate_to_aud must be a positive decimal"),
        (b"currency,rate_to_aud\nUSD,\n", "line 2: rate_to_aud must be a positive decimal"),
        (b"USD,1.5\n", "line 1: header must be exactly currency,rate_to_aud"),
        (b"", "line 1: header must be exactly currency,rate_to_aud"),
        (b"currency,rate_to_aud,extra\nUSD,1.5,x\n", "line 1: header must be exactly"),
        (b"currency\nUSD\n", "line 1: header must be exactly"),
        (b"currency,rate_to_aud\n\xff\xfe\n", "is not valid UTF-8"),
    ],
)
def test_bad_rates_file_is_a_settings_error_naming_the_file_and_line(
    content: bytes, message: str
) -> None:
    with pytest.raises(SettingsError) as raised:
        parse_rates(content, "rates.csv")
    assert message in str(raised.value)
    assert "rates file 'rates.csv'" in str(raised.value)
    assert raised.value.exit_code == 2


def test_rate_errors_never_echo_the_offending_value() -> None:
    with pytest.raises(SettingsError) as raised:
        parse_rates(b"currency,rate_to_aud\nUSD,secret-9.99x\n", "rates.csv")
    assert "secret" not in str(raised.value)
    assert "9.99" not in str(raised.value)


def test_load_rates_hashes_the_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "rates.csv"
    path.write_bytes(RATES_CSV.encode())
    rates = load_rates(path)
    assert rates.sha256 == hashlib.sha256(RATES_CSV.encode()).hexdigest()
    assert rates.by_currency["USD"] == Decimal("1.5")


def test_unreadable_rates_file_is_a_settings_error_naming_the_path(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot read rates file") as raised:
        load_rates(tmp_path / "missing.csv")
    assert "missing.csv" in str(raised.value)
    with pytest.raises(SettingsError, match="cannot read rates file"):
        load_rates(tmp_path)  # a directory


# --- marginal rate -----------------------------------------------------------


def _sources(environ: dict[str, str], dotenv: dict[str, str] | None = None) -> EnvSources:
    return EnvSources(environ=environ, dotenv=dotenv or {}, env_file=Path("paperless.env"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.32", Decimal("0.32")),
        ("0", Decimal("0")),
        ("1", Decimal("1")),
        ("1.0", Decimal("1.0")),
        (" 0.45 ", Decimal("0.45")),
    ],
)
def test_valid_marginal_rates(text: str, expected: Decimal) -> None:
    assert resolve_marginal_rate(_sources({"TAX_MARGINAL_RATE": text})) == expected


@pytest.mark.parametrize(
    "text",
    ["abc", "-0.1", "1.5", "9.75", "", "  ", "1e-1", "NaN", "32%", "00.5", "007", "0." + "1" * 13],
)
def test_invalid_marginal_rate_names_the_variable_and_source_but_not_the_value(
    text: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        resolve_marginal_rate(_sources({"TAX_MARGINAL_RATE": text}))
    message = str(raised.value)
    assert "environment variable TAX_MARGINAL_RATE" in message
    assert raised.value.exit_code == 2
    if text.strip():
        assert text.strip() not in message


def test_marginal_rate_precedence_is_environment_then_dotenv_then_unset() -> None:
    both = _sources({"TAX_MARGINAL_RATE": "0.1"}, {"TAX_MARGINAL_RATE": "0.2"})
    assert resolve_marginal_rate(both) == Decimal("0.1")
    assert resolve_marginal_rate(_sources({}, {"TAX_MARGINAL_RATE": "0.2"})) == Decimal("0.2")
    assert resolve_marginal_rate(_sources({})) is None


def test_invalid_dotenv_marginal_rate_names_the_env_file() -> None:
    with pytest.raises(SettingsError, match="TAX_MARGINAL_RATE in env file 'paperless.env'"):
        resolve_marginal_rate(_sources({}, {"TAX_MARGINAL_RATE": "lots"}))


@pytest.mark.parametrize("value", ["-0.1", "1.5", "NaN", "Infinity"])
def test_settings_reject_a_marginal_rate_outside_zero_to_one(value: str) -> None:
    with pytest.raises(SettingsError, match="marginal_rate"):
        Settings(marginal_rate=Decimal(value))


def test_settings_accept_the_boundary_rates() -> None:
    assert Settings(marginal_rate=Decimal("0")).marginal_rate == Decimal("0")
    assert Settings(marginal_rate=Decimal("1")).marginal_rate == Decimal("1")


# --- run wiring --------------------------------------------------------------


def _documents() -> list[dict[str, object]]:
    def doc(doc_id: int, value: str | None, document_type: int | None = None) -> dict[str, object]:
        fields: list[dict[str, object]] = [] if value is None else [{"field": 10, "value": value}]
        return document_payload(
            doc_id,
            title=f"Doc {doc_id}",
            correspondent=5,
            document_type=document_type,
            custom_fields=fields,
        )

    return [
        doc(1, "100.00", document_type=6),
        doc(2, "USD10.00"),
        doc(3, "JPY1000"),
        doc(4, "-5.00"),
        doc(5, None),
    ]


def _write_rates(tmp_path: Path, content: str = RATES_CSV) -> Path:
    path = tmp_path / "rates.csv"
    path.write_bytes(content.encode())
    return path


def _estimate_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("estimate:"))


def test_run_writes_estimate_csv_with_rates_and_records_it_in_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    rates = _write_rates(tmp_path)
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--rates", str(rates))) == 0
    captured = capsys.readouterr()

    assert _estimate_line(captured.out) == "estimate: deductions AUD 115.00, pending AUD -5.00"
    assert "no exchange rate for JPY" in captured.err
    assert "no exchange rate for JPY;" in captured.err.splitlines()[0]
    content = (output / "estimate.csv").read_bytes()
    table = {(r[0], r[1]): r[2:] for r in _parsed(content)[1:]}
    assert table[("deductions", "total")][:1] == ["3"]
    assert table[("deductions_by_type", "(none)")][4] == "15.00"
    assert table[("deductions_by_type", "Statement")][4] == "100.00"
    assert table[("pending_no_amount", "review rows without an amount")][0] == "1"

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["outputs"]["estimate.csv"]
    assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    assert entry["rows"] == len(_parsed(content)) - 1
    assert (
        manifest["parameters"]["rates_file_sha256"]
        == hashlib.sha256(RATES_CSV.encode()).hexdigest()
    )
    assert manifest["parameters"]["marginal_rate"] is None
    assert "1.5" not in json.dumps(manifest["parameters"])  # the rates are not copied


def test_run_without_rates_leaves_foreign_amounts_out_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 0
    captured = capsys.readouterr()
    assert _estimate_line(captured.out) == "estimate: deductions AUD 100.00, pending AUD -5.00"
    warnings = [line for line in captured.err.splitlines() if "no exchange rate" in line]
    assert len(warnings) == 1
    assert "JPY, USD" in warnings[0]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parameters"]["rates_file_sha256"] is None


def test_marginal_rate_from_the_environment_adds_the_saving_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    monkeypatch.setenv("TAX_MARGINAL_RATE", "0.32")
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--rates", str(_write_rates(tmp_path)))) == 0
    assert _estimate_line(capsys.readouterr().out) == (
        "estimate: deductions AUD 115.00, pending AUD -5.00, estimated saving AUD 36.80"
    )
    rows = _parsed((output / "estimate.csv").read_bytes())
    saving = [row for row in rows if row[0] == "tax_saving"]
    assert [row[1] for row in saving] == ["estimated saving at marginal rate 0.32"]
    assert saving[0][6] == "36.80"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parameters"]["marginal_rate"] == "0.32"


def test_marginal_rate_from_the_dotenv_file_and_the_real_environment_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    env_file = tmp_path / "paperless.env"
    env_file.write_text("TAX_MARGINAL_RATE=0.10\n", encoding="utf-8")
    assert main(_run_cli(tmp_path, tmp_path / "a", "--env-file", str(env_file))) == 0
    assert "estimated saving AUD 10.00" in _estimate_line(capsys.readouterr().out)
    monkeypatch.setenv("TAX_MARGINAL_RATE", "0.50")
    assert main(_run_cli(tmp_path, tmp_path / "b", "--env-file", str(env_file))) == 0
    assert "estimated saving AUD 50.00" in _estimate_line(capsys.readouterr().out)


def test_there_is_no_marginal_rate_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(_run_cli(tmp_path, tmp_path / "out", "--marginal-rate", "0.3"))
    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_bad_marginal_rate_exits_2_before_any_network_or_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    monkeypatch.setenv("TAX_MARGINAL_RATE", "lots")
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 2
    err = capsys.readouterr().err
    assert "TAX_MARGINAL_RATE" in err
    assert "lots" not in err
    assert not output.exists()


@pytest.mark.parametrize(
    "content", ["currency,rate_to_aud\nUSD,abc\n", "USD,1.5\n", "currency,rate_to_aud\nAUD,1\n"]
)
def test_bad_rates_file_exits_2_names_the_file_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    rates = _write_rates(tmp_path, content)
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--rates", str(rates))) == 2
    err = capsys.readouterr().err
    assert "rates.csv" in err
    assert "abc" not in err
    assert not output.exists()


def test_missing_rates_file_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    assert main(_run_cli(tmp_path, tmp_path / "out", "--rates", str(tmp_path / "nope.csv"))) == 2
    assert "cannot read rates file" in capsys.readouterr().err


def test_dry_run_plans_the_estimate_writes_nothing_and_prints_the_same_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    monkeypatch.setenv("TAX_MARGINAL_RATE", "0.32")
    rates = str(_write_rates(tmp_path))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--rates", rates, "--dry-run")) == 0
    dry = capsys.readouterr()
    assert not output.exists()
    assert "estimate.csv would write" in dry.out
    assert main(_run_cli(tmp_path, output, "--rates", rates)) == 0
    real = capsys.readouterr()
    assert _estimate_line(dry.out) == _estimate_line(real.out)
    assert dry.err == real.err


def test_two_runs_give_byte_identical_estimate_and_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    monkeypatch.setenv("TAX_MARGINAL_RATE", "0.32")
    rates = str(_write_rates(tmp_path))
    first, second = tmp_path / "first", tmp_path / "second"
    assert main(_run_cli(tmp_path, first, "--rates", rates)) == 0
    assert main(_run_cli(tmp_path, second, "--rates", rates)) == 0
    for name in ("estimate.csv", "manifest.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_a_guard_failure_writes_no_estimate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--expected-count", "99")) == 4
    assert not output.exists()


def test_run_output_verifies_and_a_changed_estimate_fails_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--rates", str(_write_rates(tmp_path)))) == 0
    capsys.readouterr()
    assert main(["verify", "--output", str(output)]) == 0
    assert "OK estimate.csv" in capsys.readouterr().out
    path = output / "estimate.csv"
    path.write_bytes(path.read_bytes().replace(b"115.00", b"915.00"))
    assert main(["verify", "--output", str(output)]) == 4
    assert "MISMATCH estimate.csv" in capsys.readouterr().out


# --- bounds and review follow-ups ----------------------------------------------

HEADER_LINE = "currency,rate_to_aud\n"


@pytest.mark.parametrize(
    "rate",
    ["9" * 40, "1234567", "0.1234567890123", "00.5", "01.5", "9" * 40 + "." + "9" * 40],
)
def test_out_of_range_rate_digits_are_a_settings_error_without_the_value(rate: str) -> None:
    with pytest.raises(SettingsError) as raised:
        parse_rates((HEADER_LINE + f"USD,{rate}\n").encode(), "rates.csv")
    message = str(raised.value)
    assert "rates file 'rates.csv' line 2" in message
    assert "at most 6 integer and 12 fraction digits" in message
    assert rate not in message
    assert raised.value.exit_code == 2


def test_rate_at_the_digit_limits_is_accepted() -> None:
    rate = "999999.999999999999"
    assert parse_rates((HEADER_LINE + f"USD,{rate}\n").encode(), "r.csv") == {"USD": Decimal(rate)}


def test_marginal_rate_zero_forms_keep_the_manifest_value_as_typed() -> None:
    assert resolve_marginal_rate(_sources({"TAX_MARGINAL_RATE": "0.5"})) == Decimal("0.5")
    with pytest.raises(SettingsError):
        resolve_marginal_rate(_sources({"TAX_MARGINAL_RATE": "00.5"}))


def test_amount_too_large_for_the_arithmetic_is_an_estimate_error_without_the_value() -> None:
    huge = str(Decimal(10) ** 30)
    for accepted, review in (([_row(1, huge)], []), ([], [_row(1, huge)])):
        with pytest.raises(EstimateError) as raised:
            _estimate(accepted, review)
        assert raised.value.exit_code == 4
        assert "1000000" not in str(raised.value)


def test_huge_foreign_amount_with_a_rate_is_an_estimate_error() -> None:
    row = _row(1, "9" * 25, currency="USD")
    with pytest.raises(EstimateError):
        _estimate([row], rates={"USD": Decimal("999999.999999999999")})


def test_huge_amount_in_a_document_exits_4_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    value = "1" + "0" * 30
    documents = [
        document_payload(1, correspondent=5, custom_fields=[{"field": 10, "value": value}])
    ]
    _install(monkeypatch, make_handler(documents))
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output)) == 4
    err = capsys.readouterr().err
    assert "too large" in err
    assert "Traceback" not in err
    assert "0" * 20 not in err
    assert not output.exists()


def test_rates_file_with_only_absent_currencies_is_clean_and_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    documents = [
        document_payload(1, correspondent=5, custom_fields=[{"field": 10, "value": "100.00"}])
    ]
    _install(monkeypatch, make_handler(documents))
    content = HEADER_LINE + "GBP,2.00\n"
    rates = _write_rates(tmp_path, content)
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--rates", str(rates))) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert _estimate_line(captured.out) == "estimate: deductions AUD 100.00, pending AUD 0.00"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["parameters"]["rates_file_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    )
    rows = _parsed((output / "estimate.csv").read_bytes())
    assert [row[1] for row in rows if row[0] == "deductions_currency"] == ["AUD"]


def test_empty_dotenv_marginal_rate_exits_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    env_file = tmp_path / "paperless.env"
    env_file.write_text("TAX_MARGINAL_RATE=\n", encoding="utf-8")
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--env-file", str(env_file))) == 2
    err = capsys.readouterr().err
    assert "TAX_MARGINAL_RATE in env file" in err
    assert not output.exists()


def test_crlf_rates_file_with_a_lowercase_code_exits_2_with_the_line_and_no_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, make_handler(_documents()))
    crlf = "\r\n"
    rates = _write_rates(
        tmp_path, "currency,rate_to_aud" + crlf + "USD,1.5" + crlf + "zzq,7.77" + crlf
    )
    output = tmp_path / "out"
    assert main(_run_cli(tmp_path, output, "--rates", str(rates))) == 2
    err = capsys.readouterr().err
    assert "line 3" in err
    assert "currency must be three uppercase letters" in err
    assert "zzq" not in err
    assert "7.77" not in err
    assert not output.exists()
