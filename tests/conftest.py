import json
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

GOLDEN = {
    "invoice_number": "INV-2024-0001",
    "vendor_name": "Bluebird Analytics LLC",
    "invoice_date": "2024-09-15",
    "due_date": None,
    "billing_address": None,
    "line_items": [
        {"description": "Data pipeline consulting (hours)", "quantity": 10, "unit_price": 150.0, "line_total": 1500.0},
        {"description": "Dashboard license", "quantity": 1, "unit_price": 299.0, "line_total": 299.0},
    ],
    "subtotal": 1799.0,
    "discount_amount": 0,
    "tax_rate_percent": 10,
    "tax_amount": 179.9,
    "total_amount": 1978.9,
    "currency": "USD",
}


@pytest.fixture
def golden() -> dict:
    return json.loads(json.dumps(GOLDEN))  # deep copy


@pytest.fixture
def no_sleep():
    calls: list[float] = []
    return calls, calls.append
