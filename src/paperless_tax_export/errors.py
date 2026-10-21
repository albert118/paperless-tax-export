"""Exit codes and typed exceptions for paperless-tax-export.

Every failure that can reach the CLI maps to a distinct exit code so callers
can distinguish usage errors (2), network/server problems (3) and internal
guard failures (4).
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NETWORK = 3
EXIT_GUARD = 4


class TaxExportError(Exception):
    """Base class for all tool errors; carries the process exit code."""

    exit_code: int


class SettingsError(TaxExportError):
    """Invalid or contradictory settings."""

    exit_code = EXIT_USAGE


class TaxExportAuthError(TaxExportError):
    """Paperless rejected the API token (HTTP 401/403)."""

    exit_code = EXIT_NETWORK


class HttpStatusError(TaxExportError):
    """Paperless returned an unexpected non-2xx status."""

    exit_code = EXIT_NETWORK


class TransportError(TaxExportError):
    """The Paperless server could not be reached (network or timeout)."""

    exit_code = EXIT_NETWORK


class ApiVersionError(TaxExportError):
    """Paperless does not serve the API version this client speaks (406)."""

    exit_code = EXIT_NETWORK


class StartupError(TaxExportError):
    """Tag or custom-field configuration cannot be resolved at startup."""

    exit_code = EXIT_GUARD


class PaginationIntegrityError(TaxExportError):
    """Paperless paging violated an integrity invariant (count, duplicates)."""

    exit_code = EXIT_GUARD


class CountGuardError(TaxExportError):
    """A count guard failed: classifier totals or the expected count disagreed."""

    exit_code = EXIT_GUARD
