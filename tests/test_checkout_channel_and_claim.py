"""Money-bug fixes on the public QR/hosted checkout (from the Aug-29 audit).

Proves:
  CRITICAL: POST /pay/<id>/submit with channel=crypto/visa is REJECTED before any
            charge — the passthrough-adapter free-money / free-dispense exploit is
            closed (a legitimate crypto pick redirects to /pay/<id>/crypto, never here).
  HIGH:     a single-use link is CLAIMED before create_charge, so a concurrent /
            double-tap submit cannot book a second charge; a legit first submit still
            works; a failed-then-retry flow is not blocked by a stale claim.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="chk_claim_")
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
from app.models import Merchant, PaymentLink, Transaction, utcnow

# Don't let the mock rail's completion Timer block on a broker.
import app.tasks.webhooks_task as _wt
_wt.deliver_webhook.delay = lambda *a, **k: None


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Chk Co", email="c@x.com", public_key="pk_live_c",
                     test_public_key="pk_test_c", secret_key="sk_live_c",
                     test_secret_key="sk_test_c", handle="chkco",
                     kyc_status="verified", is_active=True)
        db.session.add(m); db.session.commit()
        mid = m.id

        def new_link(pid, multi=False):
            lk = PaymentLink(public_id=pid, merchant_id=mid, amount=5000,
                             currency="UGX", is_test=True,
                             allow_multiple_uses=multi)
            db.session.add(lk); db.session.commit()
            return pid

        new_link("lnk_crypto")
        new_link("lnk_claim")
        new_link("lnk_legit")

    client = app.test_client()

    def txn_count():
        with app.app_context():
            return Transaction.query.count()

    # ---- CRITICAL: channel substitution rejected, no charge minted ----
    for bad in ("crypto", "visa"):
        before = txn_count()
        r = client.post("/pay/lnk_crypto/submit", data={"channel": bad})
        assert r.status_code == 200, (bad, r.status_code)          # re-render, not a charge
        assert "available here" in r.get_data(as_text=True).lower(), bad
        assert txn_count() == before, f"{bad} minted a charge!"
        with app.app_context():
            lk = PaymentLink.query.filter_by(public_id="lnk_crypto").one()
            assert lk.transaction_id is None, f"{bad} attached a txn"
    print("[CRIT] channel=crypto/visa on /submit rejected before any charge; no txn minted")

    # ---- HIGH: an already-claimed single-use link blocks a second submit ----
    with app.app_context():
        lk = PaymentLink.query.filter_by(public_id="lnk_claim").one()
        lk.checkout_claimed_at = utcnow()          # a fresh claim = a submit in flight
        db.session.commit()
    before = txn_count()
    r = client.post("/pay/lnk_claim/submit",
                    data={"channel": "mtn_momo", "phone": "256700000000"})
    assert r.status_code == 302, r.status_code                     # redirected to status
    assert "/status" in r.headers.get("Location", ""), r.headers.get("Location")
    assert txn_count() == before, "a claimed link still fired a second charge!"
    print("[HIGH] a freshly-claimed single-use link blocks a concurrent submit (no 2nd charge)")

    # ---- HIGH: a stale claim is reclaimable (doesn't wedge the link) ----
    from datetime import timedelta
    with app.app_context():
        lk = PaymentLink.query.filter_by(public_id="lnk_claim").one()
        lk.checkout_claimed_at = utcnow() - timedelta(minutes=10)   # stale
        db.session.commit()
    r = client.post("/pay/lnk_claim/submit",
                    data={"channel": "mtn_momo", "phone": "256700000000"})
    assert r.status_code == 302, r.status_code
    with app.app_context():
        lk = PaymentLink.query.filter_by(public_id="lnk_claim").one()
        assert lk.transaction_id is not None, "stale claim wedged the link"
    print("[HIGH] a stale claim is reclaimable — a legit submit proceeds and charges once")

    # ---- legit first submit works and claims + attaches exactly one charge ----
    before = txn_count()
    r = client.post("/pay/lnk_legit/submit",
                    data={"channel": "mtn_momo", "phone": "256700000000"})
    assert r.status_code == 302 and "/status" in r.headers.get("Location", ""), r.status_code
    assert txn_count() == before + 1, "legit submit did not create exactly one charge"
    with app.app_context():
        lk = PaymentLink.query.filter_by(public_id="lnk_legit").one()
        assert lk.transaction_id is not None and lk.checkout_claimed_at is not None
    # a SECOND submit on the now-attached single-use link is blocked by the guard
    r2 = client.post("/pay/lnk_legit/submit",
                     data={"channel": "mtn_momo", "phone": "256700000000"})
    assert r2.status_code == 302, r2.status_code
    assert txn_count() == before + 1, "second submit on a paid link created another charge"
    print("[HIGH] legit submit charges exactly once; a repeat on the same single-use link is blocked")

    print("\nAll checkout channel-allowlist + double-submit-claim checks passed.")


if __name__ == "__main__":
    main()
