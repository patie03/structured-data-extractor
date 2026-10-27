"""Streamlit UI for the resilient invoice extractor.

Run locally:  uv run streamlit run src/app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractor import (  # noqa: E402
    DEFAULT_MODEL,
    AttemptLog,
    CorruptFirstResponse,
    ExtractionResult,
    OpenAIBackend,
    ScriptedBackend,
    SchemaExtractor,
    load_document,
)
from src.fallback import DEFAULT_DLQ_PATH, read_dlq  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
load_dotenv(ROOT / ".env")


def _load_cloud_secrets() -> None:
    """On Streamlit Cloud, keys live in the secret manager, not in a .env file."""
    try:
        for name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
            if name in st.secrets:  # secrets win, so an updated key takes effect
                os.environ[name] = str(st.secrets[name]).strip()  # stray spaces break the key
    except Exception:  # no secrets.toml locally: .env is used instead
        pass


_load_cloud_secrets()
st.set_page_config(page_title="Resilient Invoice Extractor", page_icon="🧾", layout="wide")

STATUS_STYLE = {
    "SUCCESS": ("✅", "green"),
    "REPAIRED_VIA_REFLECTION": ("🔁", "blue"),
    "REPAIRED_VIA_HEURISTIC": ("🩹", "orange"),
    "DLQ_FAILED": ("🧨", "red"),
}

# --------------------------------------------------------------------------- #
# Sidebar: pipeline settings
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("⚙️ Pipeline settings")
    has_key = bool(os.getenv("OPENAI_API_KEY"))
    st.caption("OpenAI key: " + ("✅ loaded" if has_key else "❌ missing (add it to .env or Streamlit secrets)"))
    model = st.text_input("Model", value=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    enable_reflection = st.toggle("Reflection self-correction loop", value=True)
    max_retries = st.slider("Max LLM attempts", 1, 5, 3, disabled=not enable_reflection)
    corrupt_first = st.checkbox(
        "Inject a bad first answer (demo the reflection loop)",
        help="Breaks the model's first JSON (wrong total + bad date) so you can watch it self-heal.",
    )
    st.divider()
    st.markdown(
        "**Stages**\n1. Constrained decoding (strict JSON schema)\n2. Reflection re-prompt with exact "
        "Pydantic errors\n3. `json_repair` heuristic → Dead-Letter Queue"
    )

st.title("🧾 Resilient Invoice Extractor")
st.caption("Messy PDF / HTML → strict Pydantic v2 record, with self-correction and a dead-letter queue.")

# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
tab_upload, tab_sample, tab_paste = st.tabs(["📤 Upload file", "📁 Sample documents", "📋 Paste text / HTML"])
doc_name, doc_bytes = None, None
with tab_upload:
    up = st.file_uploader("PDF, HTML or TXT", type=["pdf", "html", "htm", "txt"])
    if up:
        doc_name, doc_bytes = up.name, up.getvalue()
with tab_sample:
    samples = sorted(p.name for p in SAMPLES.iterdir() if p.suffix in (".pdf", ".html", ".txt"))
    choice = st.selectbox("Choose a sample", samples, index=samples.index("invoice_messy.pdf") if "invoice_messy.pdf" in samples else 0)
    use_sample = st.checkbox("Use this sample", value=up is None)
    if use_sample and not doc_bytes:
        doc_name, doc_bytes = choice, (SAMPLES / choice).read_bytes()
with tab_paste:
    pasted = st.text_area("Paste raw text or HTML", height=150)
    if pasted.strip():
        doc_name, doc_bytes = "pasted.html" if "<" in pasted else "pasted.txt", pasted.encode()

col_run, col_sim = st.columns([1, 1])
run = col_run.button("▶️ Run extraction", type="primary", width="stretch", disabled=doc_bytes is None)
simulate = col_sim.button("🧨 Simulate Edge Case Failure (DLQ)", width="stretch")


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def run_pipeline(raw_text: str, backend, retries: int, reflection: bool) -> ExtractionResult:
    with st.status("Running extraction pipeline…", expanded=True) as status:

        def on_attempt(log: AttemptLog) -> None:  # live progress, one line per attempt
            icon = "✅" if log.passed else "❌"
            st.write(f"{icon} Attempt {log.attempt} · `{log.stage}` · {len(log.errors)} validation error(s)")

        extractor = SchemaExtractor(backend, dlq_path=ROOT / DEFAULT_DLQ_PATH)
        result = extractor.extract_with_retry(raw_text, max_retries=retries, enable_reflection=reflection, on_attempt=on_attempt)
        status.update(label=f"Finished: {result.execution_status}", state="error" if result.execution_status == "DLQ_FAILED" else "complete")
    return result


if simulate:
    raw = (SAMPLES / "irrecoverable.txt").read_text()
    nonsense = ["I'm sorry, there is no invoice here.", '{"invoice_number": ???, "total": "lots"', "¯\\_(ツ)_/¯"]
    st.session_state["doc"] = ("irrecoverable.txt (simulated)", raw)
    st.session_state["result"] = run_pipeline(raw, ScriptedBackend(nonsense), max_retries, enable_reflection)
elif run and doc_bytes is not None:
    if not has_key:
        st.error("No OPENAI_API_KEY found. Add it to `.env` (local) or Streamlit secrets (cloud).")
        st.stop()
    raw = load_document(doc_name, doc_bytes)
    backend = OpenAIBackend(model=model)
    if corrupt_first:
        backend = CorruptFirstResponse(backend)
    st.session_state["doc"] = (doc_name, raw)
    st.session_state["result"] = run_pipeline(raw, backend, max_retries, enable_reflection)

# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
result: ExtractionResult | None = st.session_state.get("result")
if result:
    name, raw = st.session_state["doc"]
    icon, color = STATUS_STYLE[result.execution_status]
    st.markdown(f"### {icon} :{color}[{result.execution_status}]")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("LLM attempts used", result.attempts_used)
    m2.metric("Validation errors seen", len(result.error_log))
    tokens = sum(a.prompt_tokens or 0 for a in result.audit_trail)
    m3.metric("Prompt tokens", tokens if tokens else "n/a")
    rec = result.reconciliation
    rec_status = rec.get("status", "UNKNOWN")
    m4.metric("Reconciliation", rec_status)

    # Reconciliation badge
    if rec_status == "MATCH":
        st.success(
            f"🟢 Line items {rec['line_items_sum']:,.2f} = subtotal {rec['subtotal']:,.2f} · "
            f"{rec['subtotal']:,.2f} − discount {rec['discount']:,.2f} + tax {rec['tax']:,.2f} = "
            f"total {rec['stated_total']:,.2f}"
        )
    elif rec_status == "MISMATCH":
        st.error(
            f"🔴 Expected total {rec['expected_total']:,.2f} but document/model says {rec['stated_total']:,.2f} "
            f"(off by {rec['difference']:,.2f}); line items sum to {rec['line_items_sum']:,.2f} vs subtotal {rec['subtotal']:,.2f}"
        )
    else:
        st.warning("⚪ Could not reconcile: no usable numbers in the output.")

    # Side-by-side view
    left, right = st.columns(2)
    with left:
        st.subheader(f"📄 Raw document · {name}")
        st.code(raw, language=None, wrap_lines=True, height=480)
    with right:
        st.subheader("🧩 Validated JSON payload")
        if result.validated_data:
            data = result.validated_data.model_dump(mode="json")
            st.json(data, expanded=True)
            st.dataframe(data["line_items"], width="stretch")
        else:
            st.error("No record passed validation, so nothing was sent downstream. Routed to the dead-letter queue.")
            if result.dlq_record:
                st.json(result.dlq_record, expanded=False)

    # Audit log
    st.subheader("🔍 Audit log")
    for log in result.audit_trail:
        mark = "✅ passed" if log.passed else f"❌ {len(log.errors)} error(s)"
        size = f" · {log.prompt_chars:,} prompt chars" if log.prompt_chars else ""
        with st.expander(f"Attempt {log.attempt} · {log.stage} · {mark}{size}", expanded=not log.passed):
            if log.errors:
                st.markdown("**Pydantic validation errors**")
                st.table(log.errors)
            st.markdown("**Prompt dispatched to the model**")
            st.code(log.prompt_sent, language="markdown", wrap_lines=True, height=220)
            st.markdown("**Raw model response**")
            st.code(log.raw_response or "(empty)", language="json", wrap_lines=True, height=180)

with st.expander("📥 Dead-letter queue (latest records for human review)"):
    rows = read_dlq(ROOT / DEFAULT_DLQ_PATH)
    if rows:
        st.dataframe(
            [{"time": r["timestamp"], "id": r["dlq_id"][:8], "errors": len(r["error_log"]), "preview": r["document_preview"][:80]} for r in rows],
            width="stretch",
        )
        st.download_button("Download dead_letter.jsonl", (ROOT / DEFAULT_DLQ_PATH).read_bytes(), "dead_letter.jsonl")
    else:
        st.caption("Empty. Nice.")
