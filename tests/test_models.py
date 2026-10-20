"""Tests for the Paperless API response models."""

from __future__ import annotations

from conftest import custom_field_payload, document_payload, page_payload, tag_payload

from paperless_tax_export.models import (
    CustomFieldDef,
    CustomFieldValue,
    Document,
    Page,
    Tag,
)


def test_document_ignores_unknown_json_fields() -> None:
    payload = document_payload(1)
    payload["unexpected"] = {"nested": True}
    payload["another"] = [1, 2, 3]
    doc = Document.model_validate(payload)
    assert doc.id == 1
    assert doc.title == "Statement"


def test_null_custom_field_value_is_preserved() -> None:
    payload = document_payload(1, custom_fields=[{"field": 5, "value": None}])
    doc = Document.model_validate(payload)
    assert doc.has_field(5)
    assert doc.field_value(5) is None


def test_missing_custom_field_entry_is_distinguishable_from_null() -> None:
    doc = Document.model_validate(document_payload(1))
    assert not doc.has_field(5)
    assert doc.field_value(5) is None


def test_field_values_are_keyed_by_field_id_regardless_of_order() -> None:
    payload_a = document_payload(
        1,
        custom_fields=[
            {"field": 5, "value": "A"},
            {"field": 6, "value": 12.5},
            {"field": 7, "value": None},
        ],
    )
    payload_b = document_payload(
        1,
        custom_fields=[
            {"field": 7, "value": None},
            {"field": 6, "value": 12.5},
            {"field": 5, "value": "A"},
        ],
    )
    expected = {5: "A", 6: 12.5, 7: None}
    assert Document.model_validate(payload_a).field_values() == expected
    assert (
        Document.model_validate(payload_a).field_values()
        == Document.model_validate(payload_b).field_values()
    )


def test_document_defaults() -> None:
    doc = Document.model_validate(document_payload(1))
    assert doc.archived_file_name is None
    assert doc.correspondent is None
    assert doc.document_type is None
    assert doc.storage_path is None


def test_custom_field_def_default_currency_from_extra_data() -> None:
    field = CustomFieldDef.model_validate(
        custom_field_payload(1, "Gross Amount", extra_data={"default_currency": "USD"})
    )
    assert field.default_currency() == "USD"


def test_custom_field_def_default_currency_missing_is_none() -> None:
    field = CustomFieldDef.model_validate(custom_field_payload(1, "Gross Amount"))
    assert field.default_currency() is None


def test_custom_field_def_default_currency_non_string_is_none() -> None:
    field = CustomFieldDef.model_validate(
        custom_field_payload(1, "Gross Amount", extra_data={"default_currency": 7})
    )
    assert field.default_currency() is None


def test_select_options_are_read_from_extra_data() -> None:
    options = [{"id": 1, "label": "Home"}, {"id": 2, "label": "Office"}]
    field = CustomFieldDef.model_validate(
        custom_field_payload(1, "Claimable %", extra_data={"select_options": options})
    )
    assert field.select_options() == options


def test_select_options_default_to_empty() -> None:
    field = CustomFieldDef.model_validate(custom_field_payload(1, "Claimable %"))
    assert field.select_options() == []


def test_select_options_ignore_non_list_extra_data() -> None:
    field = CustomFieldDef.model_validate(
        custom_field_payload(1, "Claimable %", extra_data={"select_options": "oops"})
    )
    assert field.select_options() == []


def test_custom_field_value_accepts_dict_values() -> None:
    value = CustomFieldValue.model_validate({"field": 3, "value": {"key": 1}})
    assert value.field == 3
    assert value.value == {"key": 1}


def test_page_is_generic_over_results() -> None:
    payload = page_payload([tag_payload(1, "tax-deductible")], count=1)
    page = Page[Tag].model_validate(payload)
    assert page.count == 1
    assert page.next is None
    assert page.previous is None
    assert len(page.results) == 1
    assert isinstance(page.results[0], Tag)
    assert page.results[0].name == "tax-deductible"


def test_tag_document_count_defaults_to_zero() -> None:
    tag = Tag.model_validate({"id": 2, "name": "income"})
    assert tag.document_count == 0
