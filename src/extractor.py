"""SchemaExtractor: document ingestion + constrained LLM extraction + validation gate.

Pipeline for one document:

    raw PDF/HTML -> clean text -> Stage 1: schema-constrained decoding
        -> Pydantic validation gate
           pass -> SUCCESS
           fail -> Stage 2: reflection re-prompt (bounded retries + backoff)
                   pass -> REPAIRED_VIA_REFLECTION
                   fail -> Stage 3: json_repair heuristic
                           pass -> REPAIRED_VIA_HEURISTIC
                           fail -> Dead-Letter Queue -> DLQ_FAILED
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError
from pypdf import PdfReader

from src.fallback import (
    DEFAULT_DLQ_PATH,
    build_reflection_prompt,
    format_errors,
    heuristic_repair,
    write_to_dlq,
)
from src.schemas import InvoiceExtraction, reconciliation_report

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_DOCUMENT_CHARS = 12_000  # hard cap on what we ever send for one document

SYSTEM_PROMPT = (
    "You are a precise invoice data extraction engine. Extract exactly one invoice from the "
    "document into the given JSON schema. Rules: money fields are plain numbers (no currency "
    "symbols, no thousands separators); dates are ISO-8601 YYYY-MM-DD; currency is USD, EUR or "
    "GBP; discount_amount is a positive number (0 if none); billing_address is one line. "
    "Only use values that appear in the document. Never invent line items or discounts."
)

ExecutionStatus = Literal["SUCCESS", "REPAIRED_VIA_REFLECTION", "REPAIRED_VIA_HEURISTIC", "DLQ_FAILED"]


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class AttemptLog(BaseModel):
    attempt: int
    stage: Literal["constrained_decoding", "reflection", "heuristic_repair"]
    prompt_sent: str = ""
    prompt_chars: int = 0
    prompt_tokens: Optional[int] = None
    raw_response: str = ""
    errors: list[dict[str, str]] = Field(default_factory=list)
    passed: bool = False


class ExtractionResult(BaseModel):
    validated_data: Optional[InvoiceExtraction] = None
    attempts_used: int = 0
    error_log: list[str] = Field(default_factory=list)
    execution_status: ExecutionStatus
    audit_trail: list[AttemptLog] = Field(default_factory=list)
    reconciliation: dict[str, Any] = Field(default_factory=dict)
    dlq_record: Optional[dict[str, Any]] = None


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
def clean_text(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def text_from_pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return clean_text("\n".join(page.extract_text() or "" for page in reader.pages))


def text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        tag.decompose()
    for row in soup.find_all("tr"):  # keep table rows on one line: "Widget | 2 | $5.00"
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        row.replace_with(soup.new_string(" | ".join(cells) + "\n"))
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "table", "section"]):
        block.append("\n")  # block elements end a line; inline tags (<b>, <span>) don't
    return clean_text(soup.get_text())


def load_document(name: str, data: bytes) -> str:
    lower = name.lower()
    if lower.endswith(".pdf"):
        return text_from_pdf(data)
    decoded = data.decode("utf-8", errors="replace")
    if lower.endswith((".html", ".htm")) or "<html" in decoded[:500].lower():
        return text_from_html(decoded)
    return clean_text(decoded)


# --------------------------------------------------------------------------- #
# LLM backends (swappable so tests and demos never need an API key)
# --------------------------------------------------------------------------- #
class LLMResponse(BaseModel):
    content: str
    prompt_tokens: Optional[int] = None


class LLMBackend(Protocol):
    def __call__(self, messages: list[dict[str, str]]) -> LLMResponse: ...


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic schema into an OpenAI Structured-Outputs 'strict' schema.

    Strict mode needs every property listed in `required` and
    additionalProperties=false. Keywords the decoder does not need (defaults,
    regex, numeric bounds) are dropped here: Pydantic enforces them afterwards.
    """
    keep = {"type", "properties", "required", "items", "anyOf", "$ref", "$defs", "enum", "description", "additionalProperties"}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items() if k in keep or k in ("properties", "$defs")}
            if "properties" in node:
                out["properties"] = {k: walk(v) for k, v in node["properties"].items()}
                out["required"] = list(node["properties"].keys())
                out["additionalProperties"] = False
            if "$defs" in node:
                out["$defs"] = {k: walk(v) for k, v in node["$defs"].items()}
            if "const" in node:
                out["enum"] = [node["const"]]
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(model.model_json_schema())


class OpenAIBackend:
    """Stage 1 constrained decoding with OpenAI Structured Outputs (json_schema, strict)."""

    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None):
        from openai import OpenAI  # imported lazily so tests don't need the package configured

        self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self.model = model
        self.response_format = {
            "type": "json_schema",
            "json_schema": {"name": "InvoiceExtraction", "strict": True, "schema": strict_json_schema(InvoiceExtraction)},
        }
        self.mode = "json_schema_strict"

    def __call__(self, messages: list[dict[str, str]]) -> LLMResponse:
        from openai import BadRequestError

        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, response_format=self.response_format, temperature=0
            )
        except BadRequestError as exc:
            if self.mode != "json_schema_strict" or "schema" not in str(exc).lower():
                raise
            # Older models don't support strict schemas: degrade to JSON mode and let
            # Pydantic + the reflection loop do the enforcement instead.
            self.mode, self.response_format = "json_object", {"type": "json_object"}
            schema_hint = json.dumps(strict_json_schema(InvoiceExtraction))
            messages = [{**messages[0], "content": messages[0]["content"] + "\nJSON schema: " + schema_hint}, *messages[1:]]
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, response_format=self.response_format, temperature=0
            )
        usage = resp.usage.prompt_tokens if resp.usage else None
        return LLMResponse(content=resp.choices[0].message.content or "", prompt_tokens=usage)


class ScriptedBackend:
    """Returns pre-written responses in order. Used by tests and the failure simulator."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> LLMResponse:
        self.calls.append(messages)
        content = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return LLMResponse(content=content, prompt_tokens=None)


class CorruptFirstResponse:
    """Wraps a real backend and breaks its FIRST answer, to demo the reflection loop live."""

    def __init__(self, inner: LLMBackend):
        self.inner = inner
        self.calls = 0

    def __call__(self, messages: list[dict[str, str]]) -> LLMResponse:
        self.calls += 1
        resp = self.inner(messages)
        if self.calls == 1:
            try:
                data = json.loads(resp.content)
                data["total_amount"] = round(float(data["total_amount"]) + 100, 2)  # break the math
                data["invoice_date"] = "sometime in October"  # break the date
                resp = LLMResponse(content=json.dumps(data), prompt_tokens=resp.prompt_tokens)
            except Exception:  # noqa: BLE001
                pass
        return resp


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class SchemaExtractor:
    def __init__(
        self,
        backend: LLMBackend,
        dlq_path: Path | str = DEFAULT_DLQ_PATH,
        backoff_base: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.backend = backend
        self.dlq_path = dlq_path
        self.backoff_base = backoff_base
        self.sleep = sleep

    def _validate(self, raw: str) -> InvoiceExtraction:
        return InvoiceExtraction.model_validate(json.loads(raw))

    def extract_with_retry(
        self,
        raw_text: str,
        max_retries: int = 3,
        enable_reflection: bool = True,
        on_attempt: Optional[Callable[[AttemptLog], None]] = None,
    ) -> ExtractionResult:
        document = raw_text[:MAX_DOCUMENT_CHARS]
        trail: list[AttemptLog] = []
        error_log: list[str] = []
        last_output, last_errors = "", []
        max_attempts = max_retries if enable_reflection else 1

        def record(log: AttemptLog) -> None:
            trail.append(log)
            for e in log.errors:
                error_log.append(f"attempt {log.attempt} [{log.stage}] {e['loc']}: {e['msg']}")
            if on_attempt:
                on_attempt(log)

        for attempt in range(1, max_attempts + 1):
            if attempt == 1:
                stage = "constrained_decoding"
                user_prompt = f"Extract the invoice from this document:\n\n{document}"
            else:
                stage = "reflection"
                self.sleep(self.backoff_base * 2 ** (attempt - 2))  # 0.5s, 1s, 2s ...
                if last_errors and last_errors[0]["loc"] != "<api>":
                    user_prompt = build_reflection_prompt(document, last_output, last_errors)
                # else: the API call itself failed (timeout, rate limit) -> retry the same prompt

            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
            log = AttemptLog(attempt=attempt, stage=stage, prompt_sent=user_prompt, prompt_chars=len(user_prompt))
            try:
                resp = self.backend(messages)
                log.raw_response, log.prompt_tokens = resp.content, resp.prompt_tokens
                last_output = resp.content
                data = self._validate(resp.content)
                log.passed = True
                record(log)
                return ExtractionResult(
                    validated_data=data,
                    attempts_used=attempt,
                    error_log=error_log,
                    execution_status="SUCCESS" if attempt == 1 else "REPAIRED_VIA_REFLECTION",
                    audit_trail=trail,
                    reconciliation=reconciliation_report(data.model_dump(mode="json")),
                )
            except (ValidationError, json.JSONDecodeError) as exc:
                last_errors = format_errors(exc)
                log.errors = last_errors
            except Exception as exc:  # noqa: BLE001 - API/network errors must not crash the worker
                last_errors = [{"loc": "<api>", "msg": f"{type(exc).__name__}: {exc}", "input": ""}]
                log.errors = last_errors
            record(log)

        # ---- Stage 3: heuristic repair ------------------------------------ #
        repair_log = AttemptLog(attempt=len(trail) + 1, stage="heuristic_repair", prompt_sent="(no LLM call: json_repair)")
        payload: Any = None
        try:
            payload = heuristic_repair(last_output)
            repair_log.raw_response = json.dumps(payload, default=str)[:5000]
            data = InvoiceExtraction.model_validate(payload)
            repair_log.passed = True
            record(repair_log)
            return ExtractionResult(
                validated_data=data,
                attempts_used=len(trail) - 1,
                error_log=error_log,
                execution_status="REPAIRED_VIA_HEURISTIC",
                audit_trail=trail,
                reconciliation=reconciliation_report(data.model_dump(mode="json")),
            )
        except Exception as exc:  # noqa: BLE001
            repair_log.errors = format_errors(exc)
            record(repair_log)

        # ---- Dead-letter queue: never return a half-empty object ---------- #
        dlq = write_to_dlq(document, last_output, error_log, self.dlq_path)
        return ExtractionResult(
            validated_data=None,
            attempts_used=len(trail) - 1,
            error_log=error_log,
            execution_status="DLQ_FAILED",
            audit_trail=trail,
            reconciliation=reconciliation_report(payload if isinstance(payload, dict) else None),
            dlq_record=dlq,
        )


def extract_with_retry(raw_text: str, max_retries: int = 3, backend: Optional[LLMBackend] = None, **kwargs: Any) -> ExtractionResult:
    """Functional wrapper matching the assignment's signature."""
    return SchemaExtractor(backend or OpenAIBackend(), **kwargs).extract_with_retry(raw_text, max_retries=max_retries)
