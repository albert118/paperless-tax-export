"""Tests for token and base URL resolution from flags, environment and dotenv files."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from test_cli import _audit_handler, _run_handler

from paperless_tax_export.cli import main
from paperless_tax_export.client import PaperlessClient

SECRET = "tok-9f3a7c1d-DISTINCTIVE"
DEFAULT_URL = "http://localhost:8000"
ENV_URL = "http://env.test"
DOTENV_URL = "http://dotenv.test"
FLAG_URL = "http://flag.test"
COMMANDS = ["audit", "run"]


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hide the host's PAPERLESS_* variables and run from an empty directory."""
    for name in list(os.environ):
        if name.startswith("PAPERLESS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


class Seen:
    """The (base_url, token) pairs the CLI built clients with."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @property
    def last(self) -> tuple[str, str]:
        assert self.calls, "no client was built"
        return self.calls[-1]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> Seen:
    seen = Seen()
    transport = httpx.MockTransport(handler)

    def build(
        base_url: str,
        token: str,
        *,
        page_size: int = 100,
        timeout: float = 30.0,
    ) -> PaperlessClient:
        seen.calls.append((base_url, token))
        return PaperlessClient(
            base_url, token, page_size=page_size, timeout=timeout, transport=transport
        )

    monkeypatch.setattr("paperless_tax_export.cli.PaperlessClient", build)
    return seen


def _handler_for(command: str) -> Callable[[httpx.Request], httpx.Response]:
    return _audit_handler() if command == "audit" else _run_handler()


def _argv(command: str, tmp_path: Path, *extra: str) -> list[str]:
    return [command, *extra, "--output", str(tmp_path / "out")]


def _file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _dotenv(tmp_path: Path, token: str | None = None, base_url: str | None = None) -> Path:
    lines = []
    if token is not None:
        lines.append(f"PAPERLESS_TOKEN={token}")
    if base_url is not None:
        lines.append(f"PAPERLESS_BASE_URL={base_url}")
    return _file(tmp_path, "paperless.env", "\n".join(lines) + "\n")


def _default_token_file(tmp_path: Path, content: str) -> None:
    default_file = tmp_path / "secrets" / "paperless-token.txt"
    default_file.parent.mkdir()
    default_file.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("command", COMMANDS)
def test_token_flag_beats_env_and_dotenv(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _handler_for(command))
    token_file = _file(tmp_path, "token.txt", "flag-token\n")
    env_file = _dotenv(tmp_path, token="dotenv-token")
    monkeypatch.setenv("PAPERLESS_TOKEN", "env-token")
    code = main(
        _argv(command, tmp_path, "--token-file", str(token_file), "--env-file", str(env_file))
    )
    assert code == 0
    assert seen.last[1] == "flag-token"


@pytest.mark.parametrize("command", COMMANDS)
def test_token_env_beats_dotenv(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _handler_for(command))
    env_file = _dotenv(tmp_path, token="dotenv-token")
    monkeypatch.setenv("PAPERLESS_TOKEN", "env-token")
    assert main(_argv(command, tmp_path, "--env-file", str(env_file))) == 0
    assert seen.last[1] == "env-token"


@pytest.mark.parametrize("command", COMMANDS)
def test_token_dotenv_beats_default_token_file(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _handler_for(command))
    _default_token_file(tmp_path, "default-token")
    env_file = _dotenv(tmp_path, token="dotenv-token")
    assert main(_argv(command, tmp_path, "--env-file", str(env_file))) == 0
    assert seen.last[1] == "dotenv-token"


@pytest.mark.parametrize("command", COMMANDS)
def test_token_falls_back_to_default_token_file(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _handler_for(command))
    _default_token_file(tmp_path, "default-token\n")
    assert main(_argv(command, tmp_path)) == 0
    assert seen.last[1] == "default-token"


@pytest.mark.parametrize("command", COMMANDS)
def test_env_token_beats_default_token_file(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _handler_for(command))
    _default_token_file(tmp_path, "default-token")
    monkeypatch.setenv("PAPERLESS_TOKEN", "env-token")
    assert main(_argv(command, tmp_path)) == 0
    assert seen.last[1] == "env-token"


@pytest.mark.parametrize("command", COMMANDS)
def test_base_url_precedence_matrix(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _handler_for(command))
    monkeypatch.setenv("PAPERLESS_TOKEN", "env-token")
    env_file = _dotenv(tmp_path, base_url=DOTENV_URL)

    assert main(_argv(command, tmp_path)) == 0
    assert seen.last[0] == DEFAULT_URL

    assert main(_argv(command, tmp_path, "--env-file", str(env_file))) == 0
    assert seen.last[0] == DOTENV_URL

    monkeypatch.setenv("PAPERLESS_BASE_URL", ENV_URL)
    assert main(_argv(command, tmp_path, "--env-file", str(env_file))) == 0
    assert seen.last[0] == ENV_URL

    argv = _argv(command, tmp_path, "--env-file", str(env_file), "--base-url", FLAG_URL)
    assert main(argv) == 0
    assert seen.last[0] == FLAG_URL


def test_paperless_env_file_equals_env_file_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _audit_handler())
    env_file = _dotenv(tmp_path, token="dotenv-token", base_url=DOTENV_URL)
    monkeypatch.setenv("PAPERLESS_ENV_FILE", str(env_file))
    assert main(_argv("audit", tmp_path)) == 0
    assert seen.last == (DOTENV_URL, "dotenv-token")


def test_env_file_flag_beats_paperless_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _audit_handler())
    flag_file = _file(tmp_path, "flag.env", "PAPERLESS_TOKEN=flag-file-token\n")
    other_file = _file(tmp_path, "other.env", "PAPERLESS_TOKEN=other-file-token\n")
    monkeypatch.setenv("PAPERLESS_ENV_FILE", str(other_file))
    assert main(_argv("audit", tmp_path, "--env-file", str(flag_file))) == 0
    assert seen.last[1] == "flag-file-token"


def test_dotenv_cannot_name_another_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _audit_handler())
    nested = _file(tmp_path, "nested.env", "PAPERLESS_TOKEN=nested-token\n")
    outer = _file(
        tmp_path, "outer.env", f"PAPERLESS_ENV_FILE={nested}\nPAPERLESS_TOKEN=outer-token\n"
    )
    assert main(_argv("audit", tmp_path, "--env-file", str(outer))) == 0
    assert seen.last[1] == "outer-token"


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_env_file_is_a_settings_error_naming_the_path(
    command: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "nope.env"
    code = main(_argv(command, tmp_path, "--env-file", str(missing)))
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert str(missing) in captured.err


def test_missing_paperless_env_file_is_a_settings_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "nope.env"
    monkeypatch.setenv("PAPERLESS_ENV_FILE", str(missing))
    code = main(_argv("audit", tmp_path))
    assert code == 2
    assert str(missing) in capsys.readouterr().err


def test_env_file_that_is_a_directory_is_a_settings_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = main(_argv("audit", tmp_path, "--env-file", str(tmp_path)))
    assert code == 2
    assert str(tmp_path) in capsys.readouterr().err


def test_env_file_that_is_not_utf8_is_a_settings_error_without_content(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.env"
    bad.write_bytes(b"PAPERLESS_TOKEN=\xff\xfe" + SECRET.encode() + b"\n")
    code = main(_argv("audit", tmp_path, "--env-file", str(bad)))
    captured = capsys.readouterr()
    assert code == 2
    assert str(bad) in captured.err
    assert SECRET not in captured.err


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("empty", ["", "   ", "\t"])
def test_empty_token_in_environment_is_rejected_naming_the_source(
    command: str,
    empty: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PAPERLESS_TOKEN", empty)
    code = main(_argv(command, tmp_path))
    captured = capsys.readouterr()
    assert code == 2
    assert "PAPERLESS_TOKEN" in captured.err
    assert "environment" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("line", ["PAPERLESS_TOKEN=", 'PAPERLESS_TOKEN="   "', "PAPERLESS_TOKEN"])
def test_empty_token_in_dotenv_is_rejected_naming_the_file(
    line: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env_file = _file(tmp_path, "paperless.env", line + "\n")
    code = main(_argv("audit", tmp_path, "--env-file", str(env_file)))
    captured = capsys.readouterr()
    assert code == 2
    assert "PAPERLESS_TOKEN" in captured.err
    assert str(env_file) in captured.err


@pytest.mark.parametrize("content", ["", "  \n"])
def test_empty_token_file_is_rejected_for_flag_and_default(
    content: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    flag_file = _file(tmp_path, "token.txt", content)
    assert main(_argv("audit", tmp_path, "--token-file", str(flag_file))) == 2
    assert str(flag_file) in capsys.readouterr().err

    _default_token_file(tmp_path, content)
    assert main(_argv("audit", tmp_path)) == 2
    assert "paperless-token.txt" in capsys.readouterr().err


def test_empty_real_env_token_does_not_fall_through_to_dotenv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env_file = _dotenv(tmp_path, token="dotenv-token")
    monkeypatch.setenv("PAPERLESS_TOKEN", "")
    code = main(_argv("audit", tmp_path, "--env-file", str(env_file)))
    assert code == 2
    assert "environment variable PAPERLESS_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("empty", ["", "  "])
def test_empty_base_url_from_env_is_rejected_naming_the_source(
    empty: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PAPERLESS_TOKEN", "env-token")
    monkeypatch.setenv("PAPERLESS_BASE_URL", empty)
    code = main(_argv("audit", tmp_path))
    assert code == 2
    assert "PAPERLESS_BASE_URL" in capsys.readouterr().err


def test_empty_paperless_env_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("PAPERLESS_ENV_FILE", " ")
    assert main(_argv("audit", tmp_path)) == 2
    assert "PAPERLESS_ENV_FILE" in capsys.readouterr().err


def test_whitespace_is_stripped_from_every_token_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _audit_handler())
    flag_file = _file(tmp_path, "token.txt", "  file-token \r\n")
    assert main(_argv("audit", tmp_path, "--token-file", str(flag_file))) == 0
    assert seen.last[1] == "file-token"

    monkeypatch.setenv("PAPERLESS_TOKEN", "  env-token\t")
    assert main(_argv("audit", tmp_path)) == 0
    assert seen.last[1] == "env-token"

    monkeypatch.delenv("PAPERLESS_TOKEN")
    env_file = _file(tmp_path, "paperless.env", 'PAPERLESS_TOKEN="  dotenv-token  "\n')
    assert main(_argv("audit", tmp_path, "--env-file", str(env_file))) == 0
    assert seen.last[1] == "dotenv-token"


def test_dotenv_in_working_directory_is_never_auto_loaded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _audit_handler())
    (tmp_path / ".env").write_text(
        f"PAPERLESS_TOKEN={SECRET}\nPAPERLESS_BASE_URL={DOTENV_URL}\n", encoding="utf-8"
    )
    code = main(_argv("audit", tmp_path))
    captured = capsys.readouterr()
    assert code == 2
    assert "token file" in captured.err
    assert SECRET not in captured.err
    assert not seen.calls


def test_loading_a_dotenv_file_does_not_mutate_os_environ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _audit_handler())
    env_file = _file(
        tmp_path,
        "paperless.env",
        f"PAPERLESS_TOKEN=dotenv-token\nPAPERLESS_BASE_URL={DOTENV_URL}\nOTHER_KEY=x\n",
    )
    before = dict(os.environ)
    assert main(_argv("audit", tmp_path, "--env-file", str(env_file))) == 0
    assert dict(os.environ) == before
    assert "OTHER_KEY" not in os.environ


def test_dotenv_values_are_not_interpolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _install(monkeypatch, _audit_handler())
    monkeypatch.setenv("HOME_SECRET", "leaked")
    env_file = _file(tmp_path, "paperless.env", "PAPERLESS_TOKEN=abc${HOME_SECRET}\n")
    assert main(_argv("audit", tmp_path, "--env-file", str(env_file))) == 0
    assert seen.last[1] == "abc${HOME_SECRET}"


@pytest.mark.parametrize("command", COMMANDS)
def test_token_never_reaches_output_files_or_streams(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _install(monkeypatch, _handler_for(command))
    env_file = _dotenv(tmp_path, token=SECRET)
    code = main(_argv(command, tmp_path, "--env-file", str(env_file)))
    captured = capsys.readouterr()
    assert code == 0
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    written = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert written
    for path in written:
        assert SECRET.encode() not in path.read_bytes(), path.name


@pytest.mark.parametrize("source", ["env", "dotenv", "file"])
def test_token_is_absent_from_error_output_and_logs(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    def unauthorised(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    _install(monkeypatch, unauthorised)
    argv = _argv("audit", tmp_path)
    if source == "env":
        monkeypatch.setenv("PAPERLESS_TOKEN", SECRET)
    elif source == "dotenv":
        argv += ["--env-file", str(_dotenv(tmp_path, token=SECRET))]
    else:
        argv += ["--token-file", str(_file(tmp_path, "token.txt", SECRET))]
    code = main(argv)
    captured = capsys.readouterr()
    assert code == 3
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert SECRET not in caplog.text
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()


def test_bad_dotenv_base_url_error_does_not_leak_the_token(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    env_file = _dotenv(tmp_path, token=SECRET, base_url="not-a-url")
    code = main(_argv("audit", tmp_path, "--env-file", str(env_file)))
    captured = capsys.readouterr()
    assert code == 2
    assert SECRET not in captured.err
    assert SECRET not in captured.out


def test_help_documents_the_env_options_and_default_token_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    out = "".join(capsys.readouterr().out.split())
    assert "--env-file" in out
    assert "PAPERLESS_TOKEN" in out
    assert "".join(str(Path("secrets/paperless-token.txt")).split()) in out
    assert "thereisno--tokenflagorenvironmentvariable" not in out
