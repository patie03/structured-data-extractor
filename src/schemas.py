"""Strictly typed Pydantic v2 data contract for invoice extraction.

Everything the LLM returns must pass through these models before it is allowed
downstream. "Before" validators clean up messy real-world formats (currency
symbols, thousands separators, "12th of Oct, 2024" dates) so that the strict
type checks that follow can stay strict.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Tolerance (in currency units) for all money reconciliation checks.
TOLERANCE = 0.02

Currency = Literal["USD", "EUR", "GBP"]

# Common ways each currency is written in messy documents.
_CURRENCY_ALIASES = {
    "$": "USD", "US$": "USD", "USD": "USD", "US DOLLAR": "USD", "US DOLLARS": "USD", "DOLLARS": "USD",
    "€": "EUR", "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "£": "GBP", "GBP": "GBP", "POUND": "GBP", "POUNDS": "GBP", "POUNDS STERLING": "GBP",
}

_DATE_FORMATS = (
    "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y",
    "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%b-%Y", "%d-%B-%Y",
)


# --------------------------------------------------------------------------- #
# Pre-parsers (run BEFORE Pydantic's strict type checks)
# --------------------------------------------------------------------------- #
def parse_money(value: Any) -> Any:
    """Turn "$1,249.50", "1 249,50 €"-style noise, or "(12.00)" into a float.

    Non-string values are returned unchanged so Pydantic can still reject them.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[^\d.,\-]", "", text)  # drop symbols, letters, spaces
    if re.search(r",\d{1,2}$", text) and ("." not in text or text.rfind(".") < text.rfind(",")):
        text = text.replace(".", "").replace(",", ".")  # European: "1.150,00" -> 1150.00
    text = text.replace(",", "")
    if text in ("", "-", "."):
        return value  # let Pydantic raise a clear "not a number" error
    number = float(text)
    return -abs(number) if negative else number


def parse_date(value: Any) -> Any:
    """Normalise non-standard dates ("12th of Oct, 2024") to datetime.date."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bof\b", " ", text, flags=re.IGNORECASE)
    text = text.replace(",", " ").replace("Sept", "Sep")
    text = re.sub(r"\s+", " ", text).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return value  # unchanged -> Pydantic raises a precise date error


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class LineItem(BaseModel):
    """One billed row on the invoice."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    description: str = Field(min_length=1, description="What was sold")
    quantity: int = Field(gt=0, description="Units billed; must be a positive whole number")
    unit_price: float = Field(gt=0, description="Price per unit, number only (no symbols)")
    line_total: float = Field(ge=0, description="quantity * unit_price")

    @field_validator("unit_price", "line_total", mode="before")
    @classmethod
    def _clean_money(cls, v: Any) -> Any:
        return parse_money(v)

    @model_validator(mode="after")
    def _line_math(self) -> "LineItem":
        expected = round(self.quantity * self.unit_price, 2)
        if abs(expected - self.line_total) > TOLERANCE:
            raise ValueError(
                f"line_total {self.line_total} != quantity {self.quantity} x unit_price "
                f"{self.unit_price} = {expected}"
            )
        return self


class InvoiceExtraction(BaseModel):
    """The validated invoice record that downstream systems may consume."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    invoice_number: str = Field(
        pattern=r"^[A-Z0-9][A-Z0-9\-/]{2,31}$",
        description="Invoice ID, uppercase letters/digits/dashes, e.g. INV-2024-0042",
    )
    vendor_name: str = Field(min_length=2)
    invoice_date: date = Field(description="ISO-8601 date, YYYY-MM-DD")
    due_date: Optional[date] = Field(default=None, description="ISO-8601 date or null")
    billing_address: Optional[str] = Field(default=None, description="Full address, one line")
    line_items: list[LineItem] = Field(min_length=1)
    subtotal: float = Field(ge=0, description="Sum of line_total values")
    discount_amount: float = Field(default=0.0, ge=0, description="Discount as a positive number, 0 if none")
    tax_rate_percent: Optional[float] = Field(default=None, ge=0, le=100, description="e.g. 8.25 for 8.25%")
    tax_amount: float = Field(ge=0)
    total_amount: float = Field(gt=0)
    currency: Currency

    # ---- field-level cleaning / assertions -------------------------------- #
    @field_validator("invoice_number", mode="before")
    @classmethod
    def _clean_invoice_number(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = re.sub(r"^(invoice|inv)\s*(no\.?|number|#)\s*:?\s*", "", v.strip(), flags=re.IGNORECASE)
            v = v.lstrip("#").strip().upper().replace(" ", "-")
        return v

    @field_validator("invoice_date", "due_date", mode="before")
    @classmethod
    def _clean_dates(cls, v: Any) -> Any:
        return parse_date(v)

    @field_validator("subtotal", "tax_amount", "total_amount", mode="before")
    @classmethod
    def _clean_amounts(cls, v: Any) -> Any:
        return parse_money(v)

    @field_validator("discount_amount", mode="before")
    @classmethod
    def _discount_positive(cls, v: Any) -> Any:
        # Documents often print discounts as "-$50.00"; we store the magnitude.
        if v is None:
            return 0.0
        v = parse_money(v)
        return abs(v) if isinstance(v, (int, float)) else v

    @field_validator("tax_rate_percent", mode="before")
    @classmethod
    def _clean_tax_rate(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.replace("%", "").strip()
        return v

    @field_validator("currency", mode="before")
    @classmethod
    def _clean_currency(cls, v: Any) -> Any:
        if isinstance(v, str):
            return _CURRENCY_ALIASES.get(v.strip().upper(), v.strip().upper())
        return v

    # ---- cross-field consistency ------------------------------------------ #
    @model_validator(mode="after")
    def _reconcile(self) -> "InvoiceExtraction":
        items_sum = round(sum(item.line_total for item in self.line_items), 2)
        if abs(items_sum - self.subtotal) > TOLERANCE:
            raise ValueError(
                f"Reconciliation failed: line items sum to {items_sum} but subtotal is {self.subtotal}"
            )
        expected_total = round(self.subtotal - self.discount_amount + self.tax_amount, 2)
        if abs(expected_total - self.total_amount) > TOLERANCE:
            raise ValueError(
                f"Reconciliation failed: subtotal {self.subtotal} - discount {self.discount_amount} "
                f"+ tax {self.tax_amount} = {expected_total}, but total_amount is {self.total_amount}"
            )
        if self.due_date and self.due_date < self.invoice_date:
            raise ValueError(f"due_date {self.due_date} is before invoice_date {self.invoice_date}")
        return self


def reconciliation_report(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort math check on ANY payload (valid or not) for the UI badge."""
    try:
        items = payload["line_items"]  # type: ignore[index]
        items_sum = round(sum(float(parse_money(i["line_total"])) for i in items), 2)
        subtotal = float(parse_money(payload["subtotal"]))
        discount = abs(float(parse_money(payload.get("discount_amount") or 0)))
        tax = float(parse_money(payload["tax_amount"]))
        total = float(parse_money(payload["total_amount"]))
    except Exception:  # noqa: BLE001 - payload may be garbage; that's the point
        return {"status": "UNKNOWN", "detail": "Not enough numeric fields to reconcile."}
    expected = round(subtotal - discount + tax, 2)
    ok = abs(items_sum - subtotal) <= TOLERANCE and abs(expected - total) <= TOLERANCE
    return {
        "status": "MATCH" if ok else "MISMATCH",
        "line_items_sum": items_sum,
        "subtotal": subtotal,
        "discount": discount,
        "tax": tax,
        "expected_total": expected,
        "stated_total": total,
        "difference": round(total - expected, 2),
    }
