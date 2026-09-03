"""Per-merchant Compliance Audit Pack builds a valid formal PDF over real data.

The admin surface for handing a bank / BoU / police a merchant's identity +
ledger. Proves the PDF builds (no fpdf layout crash) over a merchant with
directors, documents, balances and journal entries, and that the routes exist.
"""
import atexit
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="compliance_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import (Merchant, KYCApplication, KYCDirector, KYCDocument,
                        Transaction, TxnStatus, Channel, AccountType)
from app.services import ledger, compliance

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Audit Co", email="a@x.com", public_key="pk_test_a",
                     secret_key="sk_live_a", test_secret_key="sk_test_a",
                     handle="auditco", kyc_status="verified")
        db.session.add(m); db.session.commit()
        kapp = KYCApplication(merchant_id=m.id, status="approved", tin="CM95001234")
        db.session.add(kapp); db.session.commit()
        db.session.add(KYCDirector(application_id=kapp.id, full_name="Mugabi Rogers Samuel",
                       id_type="national_id", id_number="CM95001234XY",
                       contact_phone="256783647260", is_primary=True))
        db.session.add(KYCDocument(application_id=kapp.id, doc_type="director_id",
                       original_filename="sam_id.jpg", stored_filename="abc.jpg"))
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING, merchant_id=None,
                                            currency="UGX", is_test=False)
        av = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id,
                                          currency="UGX", is_test=False)
        for i in range(3):
            ledger.post([(rail, +50000), (av, -50000)], currency="UGX", memo=f"collection {i}")
        db.session.add(Transaction(public_id=f"txn_{uuid.uuid4().hex[:12]}", merchant_id=m.id,
                       amount=50000, fee_amount=750, currency="UGX", channel=Channel.MTN_MOMO,
                       status=TxnStatus.SUCCEEDED, is_test=False))
        db.session.commit()

        pdf = compliance.build_audit_pack(m, is_test=False)
        check("audit pack is a valid PDF", pdf[:5] == b"%PDF-")
        check("audit pack has content (>2KB)", len(pdf) > 2000)

        rules = [r.endpoint for r in app.url_map.iter_rules()]
        check("kyc.admin_view (inline ID preview) route registered", "kyc.admin_view" in rules)
        check("dashboard.merchant_audit_pack route registered", "dashboard.merchant_audit_pack" in rules)

        for t in ["kyc/admin_detail.html", "admin_merchant_console.html"]:
            try:
                app.jinja_env.get_template(t)
                check(f"{t} parses", True)
            except Exception:
                check(f"{t} parses", False)

    failed = [l for l, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS)-len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
