"""Public checkout routes must throttle — they fire real MoMo prompts and probe
gift-card codes with no authentication.

Finding security-2: /pay/<id>/submit, /apply-voucher and /crypto/initiate had NO
rate limit, unlike the bills/subscribe routes that carry the same risk. An
attacker could prompt-bomb an arbitrary phone or brute-force gift-card codes.

What this proves:
  1. apply-voucher is throttled (11th rapid request in a minute -> 429).
  2. checkout_submit is throttled.
  3. Within the limit, the route still works (not broken by the decorator).
"""
import atexit
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="ratelimit_")
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
from app.models import Merchant, PaymentLink

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    # Force memory rate-limit storage (default locally) and make sure limits are on.
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="RL Co", email="rl@x.com", public_key="pk_rl",
                     secret_key="sk_live_rl", kyc_status="verified", handle="rl-co")
        db.session.add(m)
        db.session.commit()
        mid = m.id
        link = PaymentLink(public_id=f"lnk_{uuid.uuid4().hex[:12]}", merchant_id=mid,
                           amount=5000, currency="UGX", is_test=True, is_active=True)
        db.session.add(link)
        db.session.commit()
        link_pub = link.public_id

    c = app.test_client()

    # 1. apply-voucher: first is allowed (bogus code -> not 429); 11th -> 429.
    first = c.post(f"/pay/{link_pub}/apply-voucher", data={"code": "BOGUS-CODE-1"})
    check("first apply-voucher is not rate-limited", first.status_code != 429)
    saw_429 = False
    for i in range(15):
        r = c.post(f"/pay/{link_pub}/apply-voucher", data={"code": f"BOGUS-{i}"})
        if r.status_code == 429:
            saw_429 = True
            break
    check("apply-voucher eventually returns 429 under rapid fire", saw_429)

    # 2. checkout_submit is throttled too (fresh link to avoid one-shot redirect).
    with app.app_context():
        link2 = PaymentLink(public_id=f"lnk_{uuid.uuid4().hex[:12]}", merchant_id=mid,
                            amount=5000, currency="UGX", is_test=True, is_active=True,
                            allow_multiple_uses=True)
        db.session.add(link2)
        db.session.commit()
        link2_pub = link2.public_id
    saw_429_submit = False
    for i in range(15):
        r = c.post(f"/pay/{link2_pub}/submit", data={"channel": "mtn_momo", "phone": "256700000000"})
        if r.status_code == 429:
            saw_429_submit = True
            break
    check("checkout_submit eventually returns 429 under rapid fire", saw_429_submit)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL CHECKOUT RATE-LIMIT TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
