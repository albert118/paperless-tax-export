"""High-level selection over the Paperless client.

These helpers turn a raw client into the concrete objects the export needs:
the single deduction tag, the custom-field definitions, and the documents
selected for a financial year. Document names in error messages never include
any amounts or supplier names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from paperless_tax_export.client import PaperlessClient
from paperless_tax_export.errors import CountGuardError, StartupError
from paperless_tax_export.models import CustomFieldDef, Document, Tag


@dataclass(frozen=True)
class SelectionResult:
    """The outcome of selecting documents for one financial year."""

    total_tagged: int
    documents: tuple[Document, ...]
    out_of_fy_count: int


def resolve_tag(client: PaperlessClient, name: str) -> Tag:
    """Resolve *name* to the single Paperless tag with that exact name."""
    tags = client.list_tags()
    matches = [tag for tag in tags if tag.name == name]
    if len(matches) != 1:
        found = ", ".join(repr(tag.name) for tag in matches) or "no tags"
        raise StartupError(
            f"expected exactly one tag named {name!r}, found {len(matches)} ({found})"
        )
    return matches[0]


def resolve_fields(
    client: PaperlessClient,
    *,
    required: Sequence[str],
    optional: Sequence[str],
) -> dict[str, CustomFieldDef | None]:
    """Resolve custom-field definitions by name.

    Every ``required`` name must exist; all missing names are reported in one
    message. Missing ``optional`` names map to ``None``.
    """
    fields = client.list_custom_fields()
    by_name = {field.name: field for field in fields}
    missing = [name for name in required if name not in by_name]
    if missing:
        named = ", ".join(repr(name) for name in missing)
        raise StartupError(f"required custom fields are missing from Paperless: {named}")
    result: dict[str, CustomFieldDef | None] = {}
    for name in required:
        result[name] = by_name[name]
    for name in optional:
        result[name] = by_name.get(name)
    return result


def select_documents(
    client: PaperlessClient,
    *,
    tag_id: int,
    fy_start: str,
    fy_end: str,
) -> SelectionResult:
    """Select all documents for one inclusive financial-year window.

    The FY window is passed to Paperless as inclusive ``created__gte`` /
    ``created__lte`` bounds. Every tagged document inside the window is
    returned; tagged documents outside it are only counted toward
    ``out_of_fy_count`` (they are never returned and never excluded here).
    """
    documents = client.list_documents(
        tag_id=tag_id,
        created_from=fy_start,
        created_to=fy_end,
    )
    _check_filters_were_honoured(documents, tag_id=tag_id, fy_start=fy_start, fy_end=fy_end)
    total_tagged = client.count_documents(tag_id=tag_id)
    out_of_fy_count = total_tagged - len(documents)
    if out_of_fy_count < 0:
        raise CountGuardError(
            f"{total_tagged} documents carry tag id {tag_id}, fewer than the "
            f"{len(documents)} selected in the financial year; Paperless changed "
            "during the run; re-run"
        )
    return SelectionResult(
        total_tagged=total_tagged,
        documents=tuple(documents),
        out_of_fy_count=out_of_fy_count,
    )


def _check_filters_were_honoured(
    documents: Sequence[Document], *, tag_id: int, fy_start: str, fy_end: str
) -> None:
    """Raise ``StartupError`` when a returned document breaks the tag or date filter.

    Paperless ignores query parameters it does not recognise, so a filter that
    stopped working would return unrelated documents. Only counts are reported.
    """
    untagged = sum(1 for document in documents if tag_id not in document.tags)
    outside = sum(1 for document in documents if not fy_start <= document.created[:10] <= fy_end)
    if untagged or outside:
        raise StartupError(
            f"of {len(documents)} documents listed for tag id {tag_id}, {untagged} do not carry "
            f"the tag and {outside} were created outside {fy_start} to {fy_end}; "
            "the server may have ignored a filter"
        )


def select_ibkr_documents(
    client: PaperlessClient,
    correspondent_name: str,
    fy_start: str,
    fy_end: str,
) -> tuple[bool, tuple[Document, ...]]:
    """Select the documents of one correspondent inside the inclusive FY window.

    Returns ``(found, documents)``. A correspondent that does not exist is not
    an error: the result is ``(False, ())`` and the caller decides how to
    report it. The name is matched exactly; more than one correspondent with
    that name is ambiguous and raises ``StartupError``. Documents are sorted
    by ``(created date, document id)``.
    """
    matches = [item for item in client.list_correspondents() if item.name == correspondent_name]
    if not matches:
        return False, ()
    if len(matches) > 1:
        raise StartupError(
            f"expected at most one correspondent named {correspondent_name!r}, found {len(matches)}"
        )
    documents = client.list_documents(
        correspondent_id=matches[0].id,
        created_from=fy_start,
        created_to=fy_end,
    )
    unexpected = sum(1 for document in documents if document.correspondent != matches[0].id)
    if unexpected:
        raise StartupError(
            f"{unexpected} of {len(documents)} documents listed for correspondent "
            f"{correspondent_name!r} (id {matches[0].id}) belong to another correspondent; "
            "the server may have ignored the correspondent filter"
        )
    ordered = sorted(documents, key=lambda document: (document.created[:10], document.id))
    return True, tuple(ordered)
