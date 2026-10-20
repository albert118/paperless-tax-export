"""Pydantic models for the Paperless-ngx API responses this tool consumes."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

# The union of value shapes Paperless custom fields can carry. The declared
# union intentionally mirrors the JSON types so a value is never silently
# coerced by pydantic before we inspect it.
CustomFieldValueType = str | int | float | dict[str, Any] | None


class CustomFieldValue(BaseModel):
    """A single filled-in value of one custom field on one document."""

    model_config = ConfigDict(extra="ignore")

    field: int
    value: CustomFieldValueType


class CustomFieldDef(BaseModel):
    """The definition of a custom field."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    data_type: str
    extra_data: dict[str, Any] = Field(default_factory=dict)
    document_count: int = 0

    def default_currency(self) -> str | None:
        """The currency Paperless suggests for monetary fields, if any."""
        value: Any = self.extra_data.get("default_currency")
        # extra_data is an untyped JSON blob; narrow the Any to str explicitly.
        return value if isinstance(value, str) else None

    def select_options(self) -> list[dict[str, Any]]:
        """The select-options declared for this field, or an empty list."""
        value: Any = self.extra_data.get("select_options")
        if isinstance(value, list) and all(isinstance(option, dict) for option in value):
            # extra_data is an untyped JSON blob; cast the narrowed Any list to
            # the declared option shape rather than widening the return type.
            return cast(list[dict[str, Any]], value)
        return []


class Document(BaseModel):
    """A Paperless document with only the fields this tool needs."""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    created: str
    modified: str
    tags: list[int]
    correspondent: int | None
    document_type: int | None
    storage_path: int | None
    custom_fields: list[CustomFieldValue]
    archived_file_name: str | None

    def has_field(self, field_id: int) -> bool:
        """True when an entry for *field_id* exists, even if its value is null."""
        return any(custom_field.field == field_id for custom_field in self.custom_fields)

    def field_value(self, field_id: int) -> CustomFieldValueType:
        """The stored value for *field_id*, or ``None`` when no entry exists."""
        for custom_field in self.custom_fields:
            if custom_field.field == field_id:
                return custom_field.value
        return None

    def field_values(self) -> dict[int, CustomFieldValueType]:
        """All custom-field values keyed by field id, order-independent."""
        return {custom_field.field: custom_field.value for custom_field in self.custom_fields}


class Tag(BaseModel):
    """A Paperless tag."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    document_count: int = 0


class Correspondent(BaseModel):
    """A Paperless correspondent."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    document_count: int = 0


class DocumentType(BaseModel):
    """A Paperless document type."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    document_count: int = 0


class StoragePath(BaseModel):
    """A Paperless storage path."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    path: str
    document_count: int = 0


class Page[T](BaseModel):
    """One page of a paginated Paperless list endpoint."""

    model_config = ConfigDict(extra="ignore")

    count: int
    next: str | None
    previous: str | None
    results: list[T]
