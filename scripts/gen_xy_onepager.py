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
    pdf.cell(W, 5, a("Scan-to-pay -> dispense. The machine shows a Samsoftpay QR; it never touches money or cards."), align="C")
    pdf.set_text_color(*DARK)

    state = {"y": 31}

    def h(title):
        state["y"] += 1.8   # breathing room above each section heading
        pdf.set_xy(14, state["y"])
        pdf.set_font("Helvetica", "B", 11.5)
        pdf.set_text_color(*GREEN)
        pdf.cell(W, 6, a(title))
        pdf.set_text_color(*DARK)
        state["y"] += 7

    def p(text, size=10, gap=5.6):
        pdf.set_xy(14, state["y"])
        pdf.set_font("Helvetica", "", size)
        pdf.multi_cell(W, gap, a(text))
        state["y"] = pdf.get_y() + 2.2

    # ---- What it is ----
    h("What the payment method is")
    p("Samsoftpay is a payment gateway in Uganda. The customer scans a Samsoftpay QR shown on the machine and "
      "completes payment on their own phone, choosing their method on the Samsoftpay page (Mobile Money today; "
      "more methods such as Airtel and cards are added centrally, with NO change to the machine). No card, no "
      "cash, no bank data on the machine - the QR opens Samsoftpay's secure checkout page.")

    # ---- Flow diagram ----
    h("Business process / how the consumer proceeds")
    steps = ["1. Select\nproduct", "2. Machine\nshows QR", "3. Scan &\npay on phone",
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

    # ---- Three questions (stacked: bold Q line, then answer line — no overlap) ----
    h("XY engineering's three questions, answered")

    def qa(q, ans):
        pdf.set_xy(16, state["y"])
        pdf.set_font("Helvetica", "B", 10)
        pdf.multi_cell(W - 4, 5.0, a(q))
        pdf.set_xy(16, pdf.get_y())
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(W - 4, 5.0, a(ans))
        state["y"] = pdf.get_y() + 2.4

    qa("1. What is the payment method?",
       "A Samsoftpay QR. The customer pays on their phone (Mobile Money today; Airtel and cards added centrally "
       "later, with no machine change). The machine only displays a QR and dispenses on success.")
    qa("2. What is the business process?",
       "Select -> QR -> customer pays on their phone -> Samsoftpay confirms -> machine dispenses (the 5 steps above).")
    qa("3. How does the consumer proceed?",
       "Scan the QR with the phone camera or a payment app -> choose the method and confirm -> approve on the "
       "phone -> the machine dispenses. No app install, no card or cash at the machine.")

    # ---- How to connect to the API ----
    h("How to connect to the Samsoftpay API")
    p("Base URL  https://api.samsoftpay.com   |   Auth: header  Authorization: Bearer <key>   "
      "(use the LIVE secret key sk_live_... in production; sk_test_... for the sandbox).", size=9.3, gap=4.5)
    p("1) Create the order + QR:  POST /v1/vending/orders  (or POST /v1/payment-links) -> returns a QR image "
      "URL to show on the machine.   2) Know it is paid: set your Webhook URL + copy the signing secret "
      "(whsec_...) in the dashboard; we POST a signed charge.succeeded event to you (or poll GET /v1/charges"
      "?reference=...).   3) Dispense-result (XY -> us): POST /inbound/xy/dispense-result.", size=9.3, gap=4.5)

    # ---- Sandbox vs live ----
    h("Sandbox vs live - simulate or real (read before testing)")
    p("The API KEY PREFIX decides the mode. sk_test_ = SANDBOX: charges auto-complete instantly with NO "
      "real PIN prompt and NO real money (safe testing of the scan-to-dispense loop). sk_live_ = LIVE: the "
      "customer gets a real PIN prompt and real money moves before we confirm. Every charge/webhook carries "
      "a mode field ('test'/'live') - in production, only dispense when mode is 'live'.", size=9.3, gap=4.5)

    # ---- What each side provides ----
    h("What is needed to finish (the Samsoftpay side is already built)")
    p("Option 1 (recommended): our software on the machine board (VMC) creates the order, shows the QR, and "
      "sends the dispense command after payment. From XY we need only the VMC + its serial command/protocol "
      "document. XY builds NO payment or QR logic.", size=9.3, gap=4.4)
    p("Option 2 (XY cloud dispenses): after payment our backend calls your ApplyExportGoods API and the machine "
      "POSTs the result to our callback. Already built our side; needs the machine registered on your cloud + "
      "XY key/secret/merchant-number.", size=9.3, gap=4.4)

    # ---- Footer ---- anchored just below the content (never off-page)
    import os as _os
    if _os.environ.get("XY_DEBUG"):
        print("CONTENT_END_Y", round(state["y"], 1))
    state["y"] = min(max(state["y"] + 6, 250), 286)
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
