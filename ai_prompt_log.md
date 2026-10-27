# AI Prompt Log & Code Audit

**Student:** Patience Kalanga
**AI assistant used:** Claude (Anthropic), in a chat-based coding session
**Date:** October 25, 2026

This log records the prompt I used, what the AI produced, and the problems I found when I audited it against the four checklist items in the assignment (Pydantic v1/v2, context stuffing, silent failures, date/type coercion).

---

## 1. Prompt used

I started from the course's sample prompt and adapted it:

```text
You are a Staff AI Systems Engineer specializing in reliable programmatic LLM integration.

I need an automated, fault-tolerant unstructured data extractor in Python 3.11+ that parses raw
PDF text or HTML documents into strictly validated Pydantic v2 models.

Data Model (src/schemas.py):
 - LineItem: description (str), quantity (int > 0), unit_price (float > 0), line_total (float).
 - InvoiceExtraction: invoice_number (str), vendor_name (str), invoice_date (date),
   line_items (List[LineItem]), subtotal (float), tax_amount (float), total_amount (float),
   currency (Literal["USD", "EUR", "GBP"]).
 - A @model_validator(mode="after") verifying that
   sum(item.line_total for item in line_items) + tax_amount == total_amount (within 0.02).

Extraction Engine (src/extractor.py):
 - Ingest raw text extracted via pypdf or beautifulsoup4.
 - Use instructor or the native OpenAI client with response_format={"type": "json_object"}
   or structured output schemas.
 - Implement extract_with_retry(raw_text: str, max_retries: int = 3) -> ExtractionResult:
   Stage 1: native constrained structured decoding.
   Stage 2 (Reflection Repair): on ValidationError, re-prompt with the original context, the
   malformed JSON, and the exact Pydantic errors (err['loc'], err['msg']).
   Stage 3 (Heuristic Fallback): after max retries, json_repair, attempt partial extraction,
   or dump to a Dead-Letter Queue file with a failure flag.

Result: ExtractionResult(validated_data, attempts_used, error_log, execution_status in
{"SUCCESS", "REPAIRED_VIA_REFLECTION", "DLQ_FAILED"}).

My additions:
 - My test invoices have discounts, "$1,249.50"-style money, and dates like "12th of Oct, 2024".
 - Also build a Streamlit UI and pytest tests that run WITHOUT an API key (mock the LLM).
```

## 2. What the AI produced

Four modules (`schemas.py`, `extractor.py`, `fallback.py`, `app.py`), sample documents, and a pytest suite that uses a scripted fake LLM so no API key or money is needed for tests.

## 3. Audit findings & fixes

Following the prompt literally produces the problems below. I checked each one against the code and the tests.

### Bug 1: The reconciliation rule in the prompt is mathematically wrong (cross-field logic)
- **Problem:** `sum(line_total) + tax_amount == total_amount` ignores **discounts**. My messy sample invoice has a 5% loyalty discount (−$124.37), so a correct extraction would be **rejected** and sent to the DLQ. That's a false failure, which is worse than useless in production.
- **Fix (`schemas.py`):** two checks with a 0.02 tolerance:
  1. `sum(line_total) ≈ subtotal`
  2. `subtotal − discount_amount + tax_amount ≈ total_amount`

  Plus a per-line check that `quantity × unit_price ≈ line_total`.
- **Proof:** `test_cross_field_reconciliation_fails_when_items_do_not_sum` and `test_reconciliation_allows_two_cent_tolerance`.

### Bug 2: `json_object` mode is not constrained decoding
- **Problem:** `response_format={"type": "json_object"}` only guarantees *some* valid JSON. It does not force the schema's keys, types or enum values, so the model can still drop `currency` or invent keys.
- **Fix (`extractor.py`):** use OpenAI **Structured Outputs** (`"type": "json_schema", "strict": true`). Strict mode needs every field listed in `required` and `additionalProperties: false`, so `strict_json_schema()` converts the Pydantic schema to that form. Pydantic still validates everything afterwards (regex, bounds, math), because the decoder can't check business rules.
- **Proof:** `test_strict_schema_is_closed_for_constrained_decoding`.

### Bug 3: Context stuffing and token bloat in the retry loop
- **Problem:** "re-prompt with the original context" naturally becomes "append the whole document plus the whole chat history on every retry". With 3 retries, a 10-page invoice is paid for 3 or more times, and the prompt grows each time.
- **Fix (`fallback.py`):**
  - Each reflection call is a **fresh, short prompt**, not a growing chat.
  - It contains only the error list, the previous JSON, and `relevant_context()`: the document lines that relate to the failing fields, capped at 2,500 characters.
  - The whole document is also capped at 12,000 characters.
- **Proof:** `test_reflection_loop_self_heals_on_attempt_2` asserts that the reflection excerpt leaves out unrelated lines (the PO reference footer) and is shorter than the document. The UI audit log shows prompt size per attempt.

### Bug 4: Silent failure through "partial extraction"
- **Problem:** "attempt partial extraction" in Stage 3 typically means `model_construct()` or returning an object full of `None` values. That quietly sends half-empty records to the accounting database with no warning, which is exactly the "loss of deterministic schema enforcement" anti-pattern.
- **Fix:** no partial objects are ever returned. If `json_repair` output still fails validation, `validated_data` is `None`, `execution_status="DLQ_FAILED"`, and the document plus the last output plus all errors go to `dlq/dead_letter.jsonl` with `needs_human_review: true`.
- **Proof:** `test_irrecoverable_payload_goes_to_dlq_without_crashing`.

### Bug 5: Date and money type coercion fragility
- **Problem:** a plain `invoice_date: date` field rejects `"12th of Oct, 2024"`. A plain `float` rejects `"$2,487.44"` and misreads European `"1.150,00 €"`.
- **Fix:** `mode="before"` field validators run custom pre-parsers (`parse_date`, `parse_money`) before Pydantic's strict checks. They:
  - strip ordinals, symbols and thousands separators
  - handle European decimal commas
  - map `$`, `€` and `£` to currency codes

  Anything still unparseable is passed through unchanged, so Pydantic raises a precise error instead of us guessing.
- **Real bug I hit while testing:** my first `parse_money` turned `"1.150,00"` into `1.15`. I fixed it to detect the European format; the test case is in `test_parse_money`.

### Finding 6 (seen in a live run): errors show up one at a time
- **What happened:** with "Inject a bad first answer" on, the first answer had TWO problems: a bad date ("sometime in October") and a wrong total (2658.02 instead of 2558.02). Pydantic only reported the date. Its `@model_validator(mode="after")` (the math check) does not run while a field is still invalid. So attempt 2 fixed the date, then the math check failed, and attempt 3 fixed the total.
- **Why it matters:** each hidden error can cost one extra LLM call. That is why `max_retries` is 3 and not 1.
- **Possible improvement:** run the math check on the raw numbers too (the UI's `reconciliation_report()` already does this), and add those issues to the first reflection prompt.

### Finding 7 (found while deploying): bugs that only showed up in the cloud
1. **The old API key stayed in memory.** The app copied the key from Streamlit Secrets only `if not os.getenv(name)`. After I fixed the key in Secrets, the old wrong key was still loaded, so I kept getting `401 invalid_api_key`. **Fix:** Secrets always win.
2. **A hidden space at the end of the key.** My `.env` had a space after the key. Locally `python-dotenv` strips it; in Streamlit Secrets it stayed inside the quotes and broke the key. **Fix:** `.strip()` the key before use.
3. **Reflection on API errors makes no sense.** With a bad key there is no JSON to fix, but the app still sent a "fix your JSON" prompt. **Fix:** on `<api>` errors, retry the same prompt with backoff instead of reflecting.

### Check: Pydantic v1 vs v2 syntax
- I searched the code for `@validator`, `@root_validator`, `.dict()`, `.parse_obj()` and `class Config`. There are none. Everything uses v2: `@field_validator`, `@model_validator(mode="after")`, `model_validate`, `model_dump(mode="json")` and `ConfigDict`.

### Other hardening
- **API and network errors** are caught and logged as an attempt, so they never crash the worker (`test_api_errors_are_caught_not_raised`).
- **Exponential backoff** of 0.5 s, then 1 s, then 2 s between reflection attempts (`test_backoff_doubles_between_retries`).
- **Bounded retries:** `max_retries` is a hard limit, and turning reflection off means exactly one LLM call (`test_reflection_disabled_makes_one_call_then_dlq`).
- **Status values:** I added a 4th status, `REPAIRED_VIA_HEURISTIC`, so a `json_repair` success is not mislabelled as `SUCCESS`.

## 4. My verification (tick each box after I have done it myself)

- [ ] Ran `uv run pytest -v`: all tests pass.
- [ ] Ran the Streamlit app on `invoice_messy.pdf` and `vendor_contract.html` and checked the reconciliation badge by hand. For example, 2,487.44 − 124.37 + 194.95 = 2,558.02.
- [ ] Turned on "Inject a bad first answer" and watched attempt 1 fail and attempt 2 self-heal.
- [ ] Clicked "Simulate Edge Case Failure" and confirmed a new row in the dead-letter queue.
- [ ] Confirmed `.env` is in `.gitignore`, so the API key is never committed.
