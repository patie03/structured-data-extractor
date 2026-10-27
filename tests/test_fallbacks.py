"""Tests that simulate bad LLM output and check the self-healing pipeline."""

import json
import os

import pytest

from src.extractor import OpenAIBackend, ScriptedBackend, SchemaExtractor, load_document
from tests.conftest import SAMPLES


def _messy_text() -> str:
    return load_document("invoice_messy.pdf", (SAMPLES / "invoice_messy.pdf").read_bytes())


# Test 4 ------------------------------------------------------------------- #
def test_reflection_loop_self_heals_on_attempt_2(golden, tmp_path, no_sleep):
    bad = dict(golden)
    del bad["currency"]  # missing required key
    bad["invoice_date"] = "sometime in September"  # unparseable date
    backend = ScriptedBackend([json.dumps(bad), json.dumps(golden)])

    result = SchemaExtractor(backend, dlq_path=tmp_path / "dlq.jsonl", sleep=no_sleep[1]).extract_with_retry(_messy_text())

    assert result.execution_status == "REPAIRED_VIA_REFLECTION"
    assert result.attempts_used == 2
    assert result.validated_data.currency == "USD"
    # The corrective prompt names the exact failing fields from the ValidationError ...
    reflection_prompt = backend.calls[1][1]["content"]
    assert "`currency`" in reflection_prompt and "`invoice_date`" in reflection_prompt
    assert "sometime in September" in reflection_prompt  # ... includes the previous bad JSON ...
    # ... and is NOT the whole document again (no context stuffing).
    excerpt = reflection_prompt.split("## Relevant excerpt from the source document")[1].split("Return the COMPLETE")[0]
    assert "Ref PO# 55-1029" not in excerpt
    assert len(excerpt) < len(_messy_text())
    assert not (tmp_path / "dlq.jsonl").exists()


# Test 5 ------------------------------------------------------------------- #
def test_irrecoverable_payload_goes_to_dlq_without_crashing(tmp_path, no_sleep):
    dlq = tmp_path / "dlq.jsonl"
    backend = ScriptedBackend(["Sorry, I could not find an invoice in this text."])
    text = (SAMPLES / "irrecoverable.txt").read_text()

    result = SchemaExtractor(backend, dlq_path=dlq, sleep=no_sleep[1]).extract_with_retry(text, max_retries=3)

    assert result.execution_status == "DLQ_FAILED"
    assert result.validated_data is None  # never a half-empty object
    assert result.attempts_used == 3 and len(backend.calls) == 3
    records = [json.loads(line) for line in dlq.read_text().splitlines()]
    assert len(records) == 1 and records[0]["needs_human_review"] is True
    assert records[0]["last_llm_output"].startswith("Sorry")
    assert result.audit_trail[-1].stage == "heuristic_repair"


# Extra resilience tests --------------------------------------------------- #
def test_backoff_doubles_between_retries(tmp_path, no_sleep):
    sleeps, sleep = no_sleep
    SchemaExtractor(ScriptedBackend(["nope"]), dlq_path=tmp_path / "d.jsonl", sleep=sleep).extract_with_retry("x", max_retries=3)
    assert sleeps == [0.5, 1.0]


def test_json_repair_heuristic_fixes_broken_json(golden, tmp_path, no_sleep):
    broken = json.dumps(golden)[:-1] + ",}"  # trailing comma -> invalid JSON
    broken = "Here is the invoice:\n" + broken  # plus chatty prose
    result = SchemaExtractor(ScriptedBackend([broken]), dlq_path=tmp_path / "d.jsonl", sleep=no_sleep[1]).extract_with_retry("doc")
    assert result.execution_status == "REPAIRED_VIA_HEURISTIC"
    assert result.validated_data.invoice_number == "INV-2024-0001"


def test_reflection_disabled_makes_one_call_then_dlq(golden, tmp_path, no_sleep):
    golden["total_amount"] = 5000
    backend = ScriptedBackend([json.dumps(golden)])
    result = SchemaExtractor(backend, dlq_path=tmp_path / "d.jsonl", sleep=no_sleep[1]).extract_with_retry("doc", enable_reflection=False)
    assert len(backend.calls) == 1
    assert result.execution_status == "DLQ_FAILED"
    assert result.reconciliation["status"] == "MISMATCH"


def test_api_errors_are_caught_not_raised(tmp_path, no_sleep):
    def exploding_backend(messages):
        raise ConnectionError("network down")

    result = SchemaExtractor(exploding_backend, dlq_path=tmp_path / "d.jsonl", sleep=no_sleep[1]).extract_with_retry("doc", max_retries=2)
    assert result.execution_status == "DLQ_FAILED"
    assert any("network down" in e for e in result.error_log)


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
def test_live_messy_invoice_end_to_end(tmp_path):
    """Run with: uv run pytest -m live -v"""
    result = SchemaExtractor(OpenAIBackend(), dlq_path=tmp_path / "d.jsonl").extract_with_retry(_messy_text())
    assert result.execution_status in ("SUCCESS", "REPAIRED_VIA_REFLECTION")
    assert result.validated_data.total_amount == pytest.approx(2558.02)
