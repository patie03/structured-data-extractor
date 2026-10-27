"""Unit tests for ingestion, parsing and schema enforcement (no API key needed)."""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from src.extractor import ScriptedBackend, SchemaExtractor, load_document, strict_json_schema
from src.schemas import InvoiceExtraction, LineItem, parse_date, parse_money
from tests.conftest import SAMPLES


# Test 1 ------------------------------------------------------------------- #
def test_golden_document_parses_on_attempt_1(golden, tmp_path, no_sleep):
    """A clean sample PDF is ingested and validates on the first attempt with zero errors."""
    text = load_document("invoice_clean.pdf", (SAMPLES / "invoice_clean.pdf").read_bytes())
    assert "INV-2024-0001" in text and "1978.90" in text  # pypdf ingestion worked

    backend = ScriptedBackend([json.dumps(golden)])
    result = SchemaExtractor(backend, dlq_path=tmp_path / "dlq.jsonl", sleep=no_sleep[1]).extract_with_retry(text)

    assert result.execution_status == "SUCCESS"
    assert result.attempts_used == 1
    assert result.error_log == []
    assert result.validated_data.total_amount == pytest.approx(1978.90)
    assert result.reconciliation["status"] == "MATCH"
    assert "INV-2024-0001" in backend.calls[0][1]["content"]  # the document reached the model


# Test 2 ------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mutate, bad_field",
    [
        (lambda d: d["line_items"][0].update(quantity=0), "quantity"),
        (lambda d: d["line_items"][0].update(quantity=-3), "quantity"),
        (lambda d: d.update(total_amount=-1978.9), "total_amount"),
        (lambda d: d["line_items"][1].update(unit_price=-299.0), "unit_price"),
        (lambda d: d.update(currency="JPY"), "currency"),
        (lambda d: d.update(invoice_number="??"), "invoice_number"),
    ],
)
def test_schema_constraints_reject_bad_values(golden, mutate, bad_field):
    mutate(golden)
    with pytest.raises(ValidationError) as exc:
        InvoiceExtraction.model_validate(golden)
    assert bad_field in str(exc.value)


# Test 3 ------------------------------------------------------------------- #
def test_cross_field_reconciliation_fails_when_items_do_not_sum(golden):
    golden["total_amount"] = 2078.90  # $100 more than subtotal + tax
    with pytest.raises(ValidationError, match="Reconciliation failed"):
        InvoiceExtraction.model_validate(golden)


def test_reconciliation_allows_two_cent_tolerance(golden):
    golden["total_amount"] = 1978.92  # rounding noise within 0.02
    assert InvoiceExtraction.model_validate(golden).total_amount == pytest.approx(1978.92)


def test_line_item_math_is_checked():
    with pytest.raises(ValidationError, match="line_total"):
        LineItem(description="Chair", quantity=4, unit_price=249.50, line_total=900.00)


# Messy-format pre-parsers ------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, expected",
    [("$1,249.50", 1249.50), ("USD 12.40", 12.40), ("-$124.37", -124.37), ("1.150,00 €", 1150.00), ("(12.00)", -12.0), (99, 99)],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw, expected",
    [("12th of Oct, 2024", date(2024, 10, 12)), ("Nov 11th, 2024", date(2024, 11, 11)), ("2024-09-15", date(2024, 9, 15)), ("03.10.2024", date(2024, 10, 3))],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_messy_strings_are_cleaned_before_strict_validation(golden):
    golden.update(invoice_date="15th of Sept, 2024", subtotal="$1,799.00", total_amount="$1,978.90", currency="$", invoice_number="inv #inv-2024-0001")
    inv = InvoiceExtraction.model_validate(golden)
    assert inv.invoice_date == date(2024, 9, 15) and inv.currency == "USD" and inv.subtotal == 1799.0
    assert inv.model_dump(mode="json")["invoice_date"] == "2024-09-15"  # Pydantic v2 API


def test_html_ingestion_drops_scripts_and_keeps_table_rows():
    text = load_document("vendor_contract.html", (SAMPLES / "vendor_contract.html").read_bytes())
    assert "dataLayer" not in text
    assert "Rack space, 42U cabinet (monthly) | 3 | 1.150,00 € | 3.450,00 €" in text


def test_strict_schema_is_closed_for_constrained_decoding():
    schema = strict_json_schema(InvoiceExtraction)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(InvoiceExtraction.model_fields)
    assert schema["properties"]["currency"]["enum"] == ["USD", "EUR", "GBP"]
