"""Environment and dotenv lookup for the Paperless token and base URL.

Precedence, first supplier wins: explicit command-line flag, real environment
variable, dotenv-file entry, built-in default. The dotenv file is only ever
named by ``--env-file`` or ``PAPERLESS_ENV_FILE``; a ``.env`` in the working
directory is never loaded. The file is parsed into a mapping and merged in code:
nothing is written to ``os.environ``.

Error messages name the source (flag, variable, path) and never a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from paperless_tax_export.errors import SettingsError

ENV_TOKEN = "PAPERLESS_TOKEN"
ENV_BASE_URL = "PAPERLESS_BASE_URL"
ENV_ENV_FILE = "PAPERLESS_ENV_FILE"


@dataclass(frozen=True, slots=True)
class EnvSources:
    """The real environment plus the parsed dotenv entries (if a file was named)."""

    environ: Mapping[str, str]
    dotenv: Mapping[str, str]
    env_file: Path | None

    def lookup(self, name: str) -> tuple[str, str] | None:
        """Return ``(value, source description)`` for *name*; the real environment wins."""
        if name in self.environ:
            return self.environ[name], f"environment variable {name}"
        if name in self.dotenv:
            return self.dotenv[name], f"{name} in env file '{self.env_file}'"
        return None


def build_sources(env_file_flag: Path | None, environ: Mapping[str, str]) -> EnvSources:
    """Load the dotenv file named by ``--env-file`` or ``PAPERLESS_ENV_FILE``, if any."""
    env_file = env_file_flag
    if env_file is None and ENV_ENV_FILE in environ:
        raw = environ[ENV_ENV_FILE].strip()
        if not raw:
            raise SettingsError(f"environment variable {ENV_ENV_FILE} is empty")
        env_file = Path(raw)
    if env_file is None:
        return EnvSources(environ=environ, dotenv={}, env_file=None)
    return EnvSources(environ=environ, dotenv=_read_env_file(env_file), env_file=env_file)


def resolve_base_url(flag: str | None, default: str, sources: EnvSources) -> str:
    """Flag, then ``PAPERLESS_BASE_URL`` (environment, then dotenv), then *default*."""
    if flag is not None:
        return flag
    found = sources.lookup(ENV_BASE_URL)
    if found is None:
        return default
    value, source = found
    value = value.strip()
    if not value:
        raise SettingsError(f"base URL from {source} is empty")
    return value


def resolve_token(token_file_flag: Path | None, default_file: Path, sources: EnvSources) -> str:
    """Flag file, then ``PAPERLESS_TOKEN`` (environment, then dotenv), then *default_file*."""
    if token_file_flag is not None:
        return _read_token_file(token_file_flag)
    found = sources.lookup(ENV_TOKEN)
    if found is None:
        return _read_token_file(default_file)
    value, source = found
    token = value.strip()
    if not token:
        raise SettingsError(f"token from {source} is empty")
    return token


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        with path.open(encoding="utf-8") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except OSError as exc:
        raise SettingsError(
            f"cannot read env file '{path}': {exc.strerror or 'I/O error'}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise SettingsError(f"env file '{path}' is not valid UTF-8") from exc
    return {key: value if value is not None else "" for key, value in values.items()}


def _read_token_file(token_file: Path) -> str:
    """Read and strip the token from *token_file*; errors name the path, never the content."""
    try:
        content = token_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"cannot read token file '{token_file}': {exc}") from exc
    token = content.strip()
    if not token:
        raise SettingsError(f"token file '{token_file}' is empty")
    return token
