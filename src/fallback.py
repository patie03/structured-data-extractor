"""Stage 2 and Stage 3 of the self-healing pipeline.

Stage 2 - Reflection: build a SHORT corrective prompt from the exact Pydantic
          errors, the previous (bad) JSON, and only the parts of the document
          that relate to the failing fields (no context stuffing).
Stage 3 - Heuristic repair with json_repair, then Dead-Letter Queue (DLQ)
          routing for human review. We never return a half-empty object.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json_repair
from pydantic import ValidationError

DEFAULT_DLQ_PATH = Path("dlq/dead_letter.jsonl")

# Max characters of source document allowed in a reflection prompt.
REFLECTION_CONTEXT_BUDGET = 2500

# Words to search for in the document for each failing field.
_FIELD_KEYWORDS: dict[str, list[str]] = {
    "invoice_number": ["invoice", "inv", "#", "no."],
    "vendor_name": ["from", "vendor", "ltd", "inc", "llc", "gmbh"],
    "invoice_date": ["date", "issued", "dated"],
    "due_date": ["due", "payable"],
    "billing_address": ["bill to", "address", "street", "st.", "road"],
    "line_items": ["qty", "quantity", "unit", "price", "description", "item", "x "],
    "subtotal": ["subtotal", "sub-total", "sub total"],
    "discount_amount": ["discount", "less"],
    "tax_rate_percent": ["tax", "vat", "%"],
    "tax_amount": ["tax", "vat"],
    "total_amount": ["total", "amount due", "balance due", "grand"],
    "currency": ["$", "€", "£", "usd", "eur", "gbp", "currency"],
}
# Cross-field (model-level) errors have an empty loc; they concern the money fields.
_RECONCILIATION_FIELDS = ["line_items", "subtotal", "discount_amount", "tax_amount", "total_amount"]


# --------------------------------------------------------------------------- #
# Error formatting
# --------------------------------------------------------------------------- #
def format_errors(error: Exception) -> list[dict[str, str]]:
    """Turn a ValidationError / JSONDecodeError into [{'loc':..., 'msg':..., 'input':...}]."""
    if isinstance(error, ValidationError):
        out = []
        for err in error.errors(include_url=False):
            loc = ".".join(str(p) for p in err["loc"]) or "<root: cross-field check>"
            given = "(missing)" if err["type"] == "missing" else _short(err.get("input"))
            out.append({"loc": loc, "msg": err["msg"], "input": given})
        return out
    return [{"loc": "<json>", "msg": f"{type(error).__name__}: {error}", "input": ""}]


def _short(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------- #
# Stage 2: targeted context + reflection prompt
# --------------------------------------------------------------------------- #
def relevant_context(raw_text: str, errors: list[dict[str, str]], budget: int = REFLECTION_CONTEXT_BUDGET) -> str:
    """Return only the document lines related to the failing fields.

    This is the fix for "context stuffing": instead of re-sending a whole
    multi-page document on every retry, we send at most `budget` characters.
    """
    fields: set[str] = set()
    for err in errors:
        top = err["loc"].split(".")[0]
        if top.startswith("<"):  # cross-field / JSON-level error
            fields.update(_RECONCILIATION_FIELDS)
        else:
            fields.add(top)

    keywords = [k for f in fields for k in _FIELD_KEYWORDS.get(f, [f.replace("_", " ")])]
    lines = raw_text.splitlines()
    keep: set[int] = set()
    for i, line in enumerate(lines):
        low = line.lower()
        if any(k in low for k in keywords) or (fields & {"line_items"} and re.search(r"\d+\.\d{2}", line)):
            keep.update({i - 1, i, i + 1})  # include one neighbour line each side

    picked = [lines[i] for i in sorted(keep) if 0 <= i < len(lines) and lines[i].strip()]
    context = "\n".join(picked) if picked else raw_text
    if len(context) > budget:
        context = context[:budget] + "\n...[truncated]"
    return context


def build_reflection_prompt(raw_text: str, previous_output: str, errors: list[dict[str, str]]) -> str:
    """The corrective re-prompt sent on attempts 2..N."""
    error_lines = "\n".join(f"- field `{e['loc']}`: {e['msg']} (you gave: {e['input']})" for e in errors)
    return (
        "Your previous JSON extraction FAILED schema validation.\n\n"
        f"## Validation errors (fix exactly these)\n{error_lines}\n\n"
        f"## Your previous JSON\n{previous_output[:4000]}\n\n"
        f"## Relevant excerpt from the source document\n{relevant_context(raw_text, errors)}\n\n"
        "Return the COMPLETE corrected JSON object. Keep every field that was already correct. "
        "Use plain numbers for money (no symbols or commas), ISO dates (YYYY-MM-DD), and a "
        "currency code of USD, EUR or GBP. Make line items add up to subtotal, and "
        "subtotal - discount_amount + tax_amount = total_amount. Do not invent values that are "
        "not in the document."
    )


# --------------------------------------------------------------------------- #
# Stage 3: heuristic repair + DLQ
# --------------------------------------------------------------------------- #
def heuristic_repair(raw_output: str) -> Any:
    """Use json_repair to fix broken JSON (missing quotes, trailing commas, prose around it)."""
    return json_repair.loads(raw_output) if raw_output else None


def write_to_dlq(
    raw_text: str,
    last_output: str,
    error_log: list[str],
    dlq_path: Path | str = DEFAULT_DLQ_PATH,
) -> dict[str, Any]:
    """Append a failure record (one JSON object per line) for human review."""
    record = {
        "dlq_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "DLQ_FAILED",
        "needs_human_review": True,
        "document_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
        "document_preview": raw_text[:500],
        "last_llm_output": last_output[:5000],
        "error_log": error_log,
    }
    path = Path(dlq_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def read_dlq(dlq_path: Path | str = DEFAULT_DLQ_PATH, limit: int = 20) -> list[dict[str, Any]]:
    path = Path(dlq_path)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-limit:][::-1]
