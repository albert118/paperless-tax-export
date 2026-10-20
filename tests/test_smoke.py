"""Smoke tests for the bootstrap skeleton."""

import pytest

from paperless_tax_export import __version__
from paperless_tax_export.cli import main


def test_version_is_a_non_empty_string() -> None:
    assert isinstance(__version__, str)
    assert __version__ != ""


def test_version_flag_exits_zero_and_prints_the_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out
