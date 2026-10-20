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
from paperless_tax_export.errors import StartupError
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
    total_tagged = client.count_documents(tag_id=tag_id)
    return SelectionResult(
        total_tagged=total_tagged,
        documents=tuple(documents),
        out_of_fy_count=total_tagged - len(documents),
    )
