"""Generate the sample documents in samples/ (run: uv run python scripts/make_samples.py)."""

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
OUT.mkdir(exist_ok=True)


def write_pdf(path: Path, lines: list[tuple[int, int, str, str, int]]) -> None:
    """lines = (x, y, text, font, size)."""
    c = canvas.Canvas(str(path), pagesize=letter)
    for x, y, text, font, size in lines:
        c.setFont(font, size)
        c.drawString(x, y, text)
    c.save()


# ---- Messy invoice: symbols, commas, ordinal dates, discount, multi-line address
messy = [
    (60, 740, "NORTHWIND OFFICE SUPPLIES LTD.", "Helvetica-Bold", 16),
    (60, 722, "88 Commerce Way, Unit 4  ·  Portland, OR 97209  ·  billing@northwind.example", "Helvetica", 8),
    (400, 740, "INVOICE", "Helvetica-Bold", 20),
    (400, 718, "Inv No:  inv 2024-0817", "Helvetica", 10),
    (400, 704, "Issued: 12th of Oct, 2024", "Helvetica", 10),
    (400, 690, "Payment due: Nov 11th, 2024", "Helvetica", 10),
    (60, 680, "Bill To:", "Helvetica-Bold", 10),
    (60, 666, "Acme Robotics Inc.", "Helvetica", 10),
    (60, 653, "Attn: Accounts Payable", "Helvetica", 10),
    (60, 640, "4410 Harbor View Rd, Suite 12", "Helvetica", 10),
    (60, 627, "Oakland, CA 94607", "Helvetica", 10),
    (60, 590, "Description", "Helvetica-Bold", 10),
    (300, 590, "Qty", "Helvetica-Bold", 10),
    (360, 590, "Unit Price", "Helvetica-Bold", 10),
    (460, 590, "Amount", "Helvetica-Bold", 10),
    (60, 572, "Ergonomic Office Chair (mesh, black)", "Helvetica", 10),
    (300, 572, "4", "Helvetica", 10), (360, 572, "$249.50", "Helvetica", 10), (460, 572, "$998.00", "Helvetica", 10),
    (60, 556, "Standing Desk Frame - dual motor", "Helvetica", 10),
    (300, 556, "2", "Helvetica", 10), (360, 556, "$412.75", "Helvetica", 10), (460, 556, "$825.50", "Helvetica", 10),
    (60, 540, "USB-C Docking Station", "Helvetica", 10),
    (300, 540, "6", "Helvetica", 10), (360, 540, "$89.99", "Helvetica", 10), (460, 540, "$539.94", "Helvetica", 10),
    (60, 524, "Cable management kit", "Helvetica", 10),
    (300, 524, "10 pcs", "Helvetica", 10), (360, 524, "USD 12.40", "Helvetica", 10), (460, 524, "$124.00", "Helvetica", 10),
    (360, 490, "Subtotal", "Helvetica", 10), (460, 490, "$2,487.44", "Helvetica", 10),
    (360, 476, "Loyalty discount (5%)", "Helvetica", 10), (460, 476, "-$124.37", "Helvetica", 10),
    (360, 462, "Sales tax 8.25%", "Helvetica", 10), (460, 462, "$194.95", "Helvetica", 10),
    (360, 444, "TOTAL DUE", "Helvetica-Bold", 11), (460, 444, "$2,558.02", "Helvetica-Bold", 11),
    (60, 400, "Thank you for your business! Late payments subject to 1.5% monthly fee.", "Helvetica-Oblique", 8),
    (60, 388, "Ref PO# 55-1029  ·  Terms: Net 30", "Helvetica-Oblique", 8),
]
write_pdf(OUT / "invoice_messy.pdf", messy)

# ---- Clean "golden" invoice for the attempt-1 test
clean = [
    (60, 740, "Bluebird Analytics LLC", "Helvetica-Bold", 16),
    (60, 715, "Invoice Number: INV-2024-0001", "Helvetica", 11),
    (60, 700, "Invoice Date: 2024-09-15", "Helvetica", 11),
    (60, 685, "Currency: USD", "Helvetica", 11),
    (60, 650, "Description | Qty | Unit Price | Line Total", "Helvetica-Bold", 11),
    (60, 634, "Data pipeline consulting (hours) | 10 | 150.00 | 1500.00", "Helvetica", 11),
    (60, 618, "Dashboard license | 1 | 299.00 | 299.00", "Helvetica", 11),
    (60, 590, "Subtotal: 1799.00", "Helvetica", 11),
    (60, 575, "Tax (10%): 179.90", "Helvetica", 11),
    (60, 560, "Total: 1978.90", "Helvetica-Bold", 11),
]
write_pdf(OUT / "invoice_clean.pdf", clean)

print("Wrote", *(p.name for p in OUT.glob("*.pdf")))
