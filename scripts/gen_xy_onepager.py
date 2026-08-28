"""Generate the one-page XY integration spec PDF (fpdf2, pure Python).

Run:  python scripts/gen_xy_onepager.py
Output: docs/Samsoftpay_XY_Integration_OnePager.pdf

A single-page, translate-ready brief for the XY vending supplier's engineers:
what the payment method is, the business process / consumer flow (with a small
diagram), their three questions answered, and what each side must provide to
finish. No external services — fpdf2 core fonts, so it runs anywhere.
"""
import os

from fpdf import FPDF

GREEN = (11, 122, 75)
DARK = (31, 41, 51)
GREY = (90, 100, 110)
LIGHT = (236, 244, 240)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "Samsoftpay_XY_Integration_OnePager.pdf")


def a(s: str) -> str:
    """Sanitize to latin-1 (Helvetica core font has no Unicode)."""
    repl = {"—": "-", "–": "-", "→": "->", "•": "-",
            "‘": "'", "’": "'", "“": '"', "”": '"',
            "…": "...", "×": "x"}
    for k, v in repl.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def build() -> str:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_margins(14, 12, 14)
    W = 210 - 28  # usable width

    # ---- Title bar ----
    pdf.set_fill_color(*GREEN)
    pdf.rect(14, 12, W, 15, "F")
    pdf.set_xy(14, 14)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(W, 6, a("Samsoftpay x XY Vending - Integration Spec"), align="C")
    pdf.set_xy(14, 20.5)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(W, 5, a("Scan-to-pay (MTN Mobile Money) -> dispense. The machine shows a QR; it never touches money or cards."), align="C")
    pdf.set_text_color(*DARK)

    state = {"y": 31}

    def h(title):
        pdf.set_xy(14, state["y"])
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*GREEN)
        pdf.cell(W, 6, a(title))
        pdf.set_text_color(*DARK)
        state["y"] += 7

    def p(text, size=9.5, gap=5.0):
        pdf.set_xy(14, state["y"])
        pdf.set_font("Helvetica", "", size)
        pdf.multi_cell(W, gap, a(text))
        state["y"] = pdf.get_y() + 1.0

    # ---- What it is ----
    h("What the payment method is")
    p("Samsoftpay is a payment gateway in Uganda using MTN Mobile Money (MoMo). The customer scans a QR "
      "shown on the machine and approves the payment on their own phone. No card, no cash, no bank data on "
      "the machine - the QR opens Samsoftpay's secure checkout page.")

    # ---- Flow diagram ----
    h("Business process / how the consumer proceeds")
    steps = ["1. Select\nproduct", "2. Machine\nshows QR", "3. Scan &\npay (MoMo)",
             "4. Samsoftpay\nconfirms", "5. Machine\ndispenses"]
    n = len(steps)
    gap = 4
    bw = (W - gap * (n - 1)) / n
    bh = 16
    bx = 14
    by = state["y"] + 1
    pdf.set_font("Helvetica", "B", 8.5)
    for i, s in enumerate(steps):
        x = bx + i * (bw + gap)
        pdf.set_fill_color(*LIGHT)
        pdf.set_draw_color(*GREEN)
        pdf.rect(x, by, bw, bh, "DF")
        pdf.set_xy(x, by + 3)
        pdf.multi_cell(bw, 4, a(s), align="C")
        if i < n - 1:  # arrow to next box
            ax = x + bw
            ay = by + bh / 2
            pdf.set_draw_color(*GREY)
            pdf.line(ax + 0.5, ay, ax + gap - 0.5, ay)
            pdf.line(ax + gap - 0.5, ay, ax + gap - 2.0, ay - 1.2)
            pdf.line(ax + gap - 0.5, ay, ax + gap - 2.0, ay + 1.2)
    state["y"] = by + bh + 3

    # ---- Three questions ----
    h("XY engineering's three questions, answered")
    for q, txt in [
        ("Payment method?", "MTN Mobile Money via a Samsoftpay QR. The machine only shows a QR and dispenses on success."),
        ("Business process?", "Select -> QR -> customer pays on phone -> Samsoftpay confirms -> machine dispenses."),
        ("How does the consumer proceed?", "Scan QR with phone camera / MoMo app -> confirm number -> approve with MoMo PIN -> dispense. No app install."),
    ]:
        pdf.set_xy(16, state["y"])
        pdf.set_font("Helvetica", "B", 9.3)
        pdf.cell(48, 5, a("- " + q))
        pdf.set_xy(64, state["y"])
        pdf.set_font("Helvetica", "", 9.3)
        pdf.multi_cell(W - 50, 5, a(txt))
        state["y"] = pdf.get_y() + 0.5
    state["y"] += 1

    # ---- What's needed ----
    h("What is needed to finish (Samsoftpay side is already built)")
    p("Option 1 (recommended): Samsoftpay software runs on the machine control board (VMC). It creates the "
      "order, shows the QR, and after payment sends the dispense command. From XY we need only the VMC "
      "hardware + the serial command/protocol document (bytes to dispense a chosen slot and read status). "
      "XY builds NO payment or QR logic.", size=9.3, gap=4.6)
    p("Option 2 (XY cloud dispenses): after payment, Samsoftpay's backend calls your ApplyExportGoods API, "
      "and the machine POSTs the dispense result to our callback. Already built on our side. Needs: machine "
      "registered on your cloud + XY key/secret/merchant-number. (Even here a small on-board program starts "
      "the order, since your cloud has no machine-initiated 'start order' interface.)", size=9.3, gap=4.6)

    # ---- Footer ----
    state["y"] = 285
    pdf.set_draw_color(*GREEN)
    pdf.line(14, state["y"], 14 + W, state["y"])
    pdf.set_xy(14, state["y"] + 1)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(W, 3.6, a(
        "Dispense-result callback: https://api.samsoftpay.com/inbound/xy/dispense-result   |   "
        "API docs: https://api.samsoftpay.com/docs   |   OpenAPI: https://api.samsoftpay.com/openapi.json\n"
        "Tip: this document can be translated by any AI tool - paste it in and ask for Chinese."))

    pdf.output(OUT)
    return OUT


if __name__ == "__main__":
    print("wrote", build())
