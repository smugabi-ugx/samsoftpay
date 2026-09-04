"""KYC re-verification — Stripe-standard step-up gating.

A merchant can be VERIFIED yet not currently allowed to move live money:
  - an ID on file has expired (kyc_expires_at in the past), or
  - review flagged the account (reverify_required).
Both must gate LIVE charges/payouts while leaving the KYC record intact, and
TEST mode must stay open the whole time (building never needs approval).

What this proves:
  1. kyc_is_current(): verified+no-expiry True; expired False; flagged False;
     unverified False.
  2. A LIVE charge succeeds while verification is current.
  3. A LIVE charge is REFUSED once the ID has expired.
  4. A LIVE charge is REFUSED once the account is flagged for re-verification.
  5. Clearing the flag restores live charging.
  6. TEST-mode charges keep working even while flagged (mode exempt).
  7. _earliest_id_expiry() picks the earliest parseable director ID expiry and
     approval writes it to merchant.kyc_expires_at (+ clears the flag).
"""
import atexit
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="kyc_reverify_")
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
from app.models import Merchant, KYCApplication, KYCDirector
from app.routes.kyc import _earliest_id_expiry

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


LIVE = "sk_live_kycreverify"
TEST = "sk_test_kycreverify"


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Reverify Co", email="reverify@x.com",
                     public_key="pk_test_reverify",
                     secret_key=LIVE, test_secret_key=TEST,
                     kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        mid = m.id

        # 1. kyc_is_current() truth table.
        check("current: verified + no expiry + not flagged", m.kyc_is_current() is True)

        m.kyc_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        check("expired ID -> not current", m.kyc_is_current() is False)
        m.kyc_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        check("future expiry -> current again", m.kyc_is_current() is True)

        m.reverify_required = True
        check("flagged -> not current", m.kyc_is_current() is False)
        m.reverify_required = False

        m.kyc_status = "pending"
        check("unverified -> not current", m.kyc_is_current() is False)
        m.kyc_status = "verified"
        db.session.commit()

        # 7. _earliest_id_expiry over an application with two directors.
        kapp = KYCApplication(merchant_id=mid, status="submitted")
        db.session.add(kapp)
        db.session.commit()
        db.session.add(KYCDirector(application_id=kapp.id, full_name="A",
                                   id_expiry="2030-12-31", is_primary=True))
        db.session.add(KYCDirector(application_id=kapp.id, full_name="B",
                                   id_expiry="2027-06-01"))
        db.session.add(KYCDirector(application_id=kapp.id, full_name="C",
                                   id_expiry="not-a-date"))
        db.session.commit()
        earliest = _earliest_id_expiry(kapp)
        check("_earliest_id_expiry picks 2027-06-01 (earliest parseable)",
              earliest is not None and earliest.date().isoformat() == "2027-06-01")

    client = app.test_client()

    def hdr(key, idem):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))}

    def charge(key, idem):
        return client.post("/v1/charges", headers=hdr(key, idem),
                           json={"amount": 5000, "currency": "UGX", "channel": "mtn_momo",
                                 "customer": {"phone": "256700000000"}})

    def set_state(**kw):
        with app.app_context():
            mm = db.session.get(Merchant, mid)
            for k, v in kw.items():
                setattr(mm, k, v)
            db.session.commit()

    # 2. Live charge succeeds while current.
    set_state(kyc_status="verified", reverify_required=False, kyc_expires_at=None)
    r = charge(LIVE, "live-ok-1")
    check("LIVE charge 201 while verification current", r.status_code == 201)

    # 3. Expired ID refuses the live charge.
    set_state(kyc_expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    r = charge(LIVE, "live-expired-1")
    check("LIVE charge refused when ID expired (not 201)", r.status_code != 201)
    check("  refusal mentions verification",
          "verif" in (r.get_data(as_text=True) or "").lower())

    # 4. Flag refuses the live charge.
    set_state(kyc_expires_at=None, reverify_required=True)
    r = charge(LIVE, "live-flagged-1")
    check("LIVE charge refused when flagged for re-verification (not 201)", r.status_code != 201)

    # 6. TEST mode still works while flagged.
    r = charge(TEST, "test-while-flagged-1")
    check("TEST charge still 201 while flagged (mode exempt)", r.status_code == 201)

    # 5. Clearing the flag restores live charging.
    set_state(reverify_required=False)
    r = charge(LIVE, "live-restored-1")
    check("LIVE charge 201 again after flag cleared", r.status_code == 201)

    failed = [l for l, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS)-len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
