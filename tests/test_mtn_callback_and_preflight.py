"""MTN production readiness: the native callback (hint-verify) + `flask preflight`.

The callback design rule: MTN does not sign callbacks, so /inbound/mtn/callback
NEVER completes a charge from the payload — it re-queries MTN's status API and
completes from THAT answer only. A forged callback can trigger nothing but a
status check.

What this proves:
  1. Callback + MTN-says-SUCCESSFUL -> charge SUCCEEDED and merchant credited.
  2. FORGED callback claiming success while MTN says PENDING -> charge UNCHANGED.
  3. Callback + MTN-says-FAILED -> charge FAILED.
  4. Unknown reference -> 200, nothing happens (no information leak).
  5. Mock mode (MOMO_USE_REAL off) -> no-op (timers own completion).
  6. Resolution works by public_id (externalId) as well as rail_reference.
  7. `flask preflight` FAILS (exit 1) on weak dev secrets.
  8. `flask preflight --skip-network` passes on a strong config with a stamped DB.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="mtncb_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")
import atexit
@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass

from app import create_app
from app.extensions import db
from app.models import Account, AccountType, Channel, Merchant, Transaction, TxnStatus
from app.services import sweep as sweep_svc

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


MTN_ANSWER = {"value": None}


def fake_status(ref):
    return MTN_ANSWER["value"]


def merchant_pending(mid):
    a = Account.query.filter_by(merchant_id=mid, type=AccountType.MERCHANT_PENDING,
                                is_test=False).first()
    return -a.cached_balance if a else 0


def main():
    app = create_app({"MOMO_USE_REAL": True})
    sweep_svc._query_mtn_status = fake_status   # no network

    with app.app_context():
        db.create_all()
        m = Merchant(name="CB Co", email="cb@x.com", public_key="pk", secret_key="sk_live",
                     kyc_status="verified", handle="cb")
        db.session.add(m)
        db.session.commit()
        mid = m.id

        def make_txn(pub, ref):
            t = Transaction(public_id=pub, merchant_id=mid, amount=10000, fee_amount=200,
                            currency="UGX", channel=Channel.MTN_MOMO,
                            status=TxnStatus.AUTHORIZED, is_test=False,
                            customer_phone="256780000001", rail_reference=ref)
            db.session.add(t)
            db.session.commit()
            return t.id

        c = app.test_client()

        # 1. MTN confirms SUCCESSFUL -> completed + credited.
        t1 = make_txn("txn_cb1", "ref-cb1")
        MTN_ANSWER["value"] = "SUCCESSFUL"
        r = c.post("/inbound/mtn/callback", json={"referenceId": "ref-cb1"})
        check("callback returns 200", r.status_code == 200)
        txn = db.session.get(Transaction, t1)
        check("MTN-confirmed callback -> SUCCEEDED", txn.status == TxnStatus.SUCCEEDED)
        check("merchant credited net (9800)", merchant_pending(mid) == 9800)

        # 2. FORGED success while MTN says PENDING -> unchanged.
        t2 = make_txn("txn_cb2", "ref-cb2")
        MTN_ANSWER["value"] = "PENDING"
        c.post("/inbound/mtn/callback",
               json={"referenceId": "ref-cb2", "status": "SUCCESSFUL"})
        txn = db.session.get(Transaction, t2)
        check("forged callback does NOT complete (MTN says PENDING)",
              txn.status == TxnStatus.AUTHORIZED)
        check("no extra credit from the forgery", merchant_pending(mid) == 9800)

        # 3. MTN says FAILED -> failed, no credit.
        MTN_ANSWER["value"] = "FAILED"
        c.post("/inbound/mtn/callback", json={"referenceId": "ref-cb2"})
        txn = db.session.get(Transaction, t2)
        check("MTN-failed callback -> FAILED", txn.status == TxnStatus.FAILED)
        check("failed charge credited nothing", merchant_pending(mid) == 9800)

        # 4. Unknown reference -> 200, nothing.
        r = c.post("/inbound/mtn/callback", json={"referenceId": "ref-nope"})
        check("unknown reference -> 200 (no leak)", r.status_code == 200)

        # 6. externalId (public_id) resolution.
        t3 = make_txn("txn_cb3", "ref-cb3")
        MTN_ANSWER["value"] = "SUCCESSFUL"
        c.post("/inbound/mtn/callback", json={"externalId": "txn_cb3"})
        txn = db.session.get(Transaction, t3)
        check("externalId resolution completes the charge", txn.status == TxnStatus.SUCCEEDED)

    # 5. Mock mode -> no-op.
    app2 = create_app({"MOMO_USE_REAL": False})
    with app2.app_context():
        t4 = Transaction.query.filter_by(public_id="txn_cb2").first()
        before = t4.status
        MTN_ANSWER["value"] = "SUCCESSFUL"
        app2.test_client().post("/inbound/mtn/callback", json={"referenceId": "ref-cb2"})
        t4 = Transaction.query.filter_by(public_id="txn_cb2").first()
        check("mock mode: callback is a no-op", t4.status == before)

    # 7. preflight FAILS on weak dev secrets.
    r = app2.test_cli_runner().invoke(args=["preflight", "--skip-network"])
    check("preflight exits 1 on weak dev secrets", r.exit_code == 1)
    check("preflight names the failing secret", "SECRET_KEY strong" in r.output)

    # 8. preflight passes on a strong config with a stamped DB.
    strong = create_app({
        "SECRET_KEY": "s" * 40,
        "WEBHOOK_SIGNING_SECRET": "whsec_" + "x" * 40,
        "MOMO_USE_REAL": False,
    })
    with strong.app_context():
        from sqlalchemy import text
        from alembic.config import Config as ACfg
        from alembic.script import ScriptDirectory
        acfg = ACfg()
        acfg.set_main_option("script_location", "migrations")
        head = ScriptDirectory.from_config(acfg).get_heads()[0]
        try:
            db.session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        except Exception:
            db.session.rollback()
            db.session.execute(text("DELETE FROM alembic_version"))
        db.session.execute(text(f"INSERT INTO alembic_version VALUES ('{head}')"))
        db.session.commit()
    r = strong.test_cli_runner().invoke(args=["preflight", "--skip-network"])
    check("preflight READY on a strong config (exit 0)", r.exit_code == 0)
    check("preflight reports READY", "READY" in r.output)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL MTN-CALLBACK + PREFLIGHT TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
