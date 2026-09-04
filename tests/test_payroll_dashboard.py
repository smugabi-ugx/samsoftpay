"""Recurring-payout dashboard UI (the merchant-facing feature over the scheduled
payout engine).

What this proves:
  1. The payroll page renders for the owner (200).
  2. Creating a schedule from the form persists a live (is_test=False) ScheduledPayout
     with the parsed recipients; a single line and many lines both work.
  3. A bad config (amount 0) re-renders with an error and writes NO row.
  4. Pause / resume / cancel buttons move the schedule between states.
  5. Owner-or-admin gated: another logged-in merchant is 403.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="payroll_dash_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant, ScheduledPayout

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="Boss", email="boss@x.com", public_key="pk_b",
                     secret_key="sk_b", kyc_status="verified", handle="boss",
                     password_hash=generate_password_hash("x"))
        other = Merchant(name="Other", email="ot@x.com", public_key="pk_ot",
                         secret_key="sk_ot", kyc_status="verified", handle="other",
                         password_hash=generate_password_hash("x"))
        db.session.add_all([m, other])
        db.session.commit()
        mid, oid = m.id, other.id

    c = app.test_client()

    def login(uid):
        with c.session_transaction() as sess:
            sess["_user_id"] = str(uid)
            sess["_fresh"] = True

    # 1. page renders for the owner
    login(mid)
    r = c.get(f"/dashboard/{mid}/payroll")
    check("payroll page renders for the owner (200)", r.status_code == 200)
    check("page mentions recurring payouts", b"Recurring payouts" in r.data)

    # 2. create a schedule with two recipients (one with a name, one bare)
    r = c.post(f"/dashboard/{mid}/payroll/new", data={
        "name": "Staff salaries", "amount": "300,000", "interval": "monthly",
        "recipients": "256780000001,Jane Doe\n256770111222",
    })
    check("create redirects on success (302)", r.status_code in (302, 303))
    with app.app_context():
        sps = ScheduledPayout.query.filter_by(merchant_id=mid).all()
        check("exactly one schedule was created", len(sps) == 1)
        sp = sps[0]
        import json
        recips = json.loads(sp.recipients)
        check("...it is LIVE (is_test=False), active, monthly, UGX 300,000",
              sp.is_test is False and sp.status == "active"
              and sp.interval == "monthly" and sp.amount == 300000)
        check("...with both recipients parsed (name kept where given)",
              len(recips) == 2 and recips[0]["phone"] == "256780000001"
              and recips[0]["name"] == "Jane Doe" and recips[1]["phone"] == "256770111222")
        sp_public = sp.public_id

    # 3. a bad config (amount 0) re-renders with an error, writes no row
    r = c.post(f"/dashboard/{mid}/payroll/new", data={
        "amount": "0", "interval": "monthly", "recipients": "256780000009"})
    check("bad config (amount 0) re-renders with 200 + error", r.status_code == 200)
    check("...and shows the validation message", b"amount must be positive" in r.data)
    with app.app_context():
        check("...and writes NO new schedule row",
              ScheduledPayout.query.filter_by(merchant_id=mid).count() == 1)

    # 4. pause / resume / cancel
    c.post(f"/dashboard/{mid}/payroll/{sp_public}/pause")
    with app.app_context():
        check("pause -> status paused",
              ScheduledPayout.query.filter_by(public_id=sp_public).one().status == "paused")
    c.post(f"/dashboard/{mid}/payroll/{sp_public}/resume")
    with app.app_context():
        check("resume -> status active",
              ScheduledPayout.query.filter_by(public_id=sp_public).one().status == "active")
    c.post(f"/dashboard/{mid}/payroll/{sp_public}/cancel")
    with app.app_context():
        check("cancel -> status cancelled",
              ScheduledPayout.query.filter_by(public_id=sp_public).one().status == "cancelled")

    # 5. another merchant cannot view or act on this one
    login(oid)
    check("a different logged-in merchant is 403 on the page",
          c.get(f"/dashboard/{mid}/payroll").status_code == 403)
    check("a different logged-in merchant is 403 on an action",
          c.post(f"/dashboard/{mid}/payroll/{sp_public}/pause").status_code == 403)

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL PAYROLL-DASHBOARD TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
