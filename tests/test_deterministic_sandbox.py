"""The sandbox must behave like Stripe/Pesapal/Flutterwave: deterministic.

A real payment sandbox never makes an integrator's ordinary test transaction
randomly fail — that's what got reported: two purchases with the same phone
number succeeded, a third failed with "insufficient_funds", pure chance
(the mock rolled dice at its old 0.85 default). An integrator cannot build
reliable failure-handling against randomness, and a normal happy-path test
should never flake.

Fixed on two fronts, both proven here by running each case several times:
  1. An ORDINARY phone number now succeeds every time (new default: 1.0, not
     a coin flip).
  2. A MAGIC test number (rails.TEST_PHONE_OUTCOMES) gets EXACTLY the outcome
     it documents, every time — the deliberate-failure lever, same spirit as
     Stripe's 4000 0000 0000 0002.
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"   # parsed as int elsewhere
# Deliberately NOT setting RAIL_SUCCESS_PROBABILITY — proving the CODE default
# (1.0) is what makes an ordinary number reliable, with no env var required.

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="deterministic_test_")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")


@atexit.register
def _cleanup_db():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant, PaymentLink, Transaction

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def buy_and_wait(client, hdr, phone, ref):
    r = client.post("/v1/payment-links", headers={**hdr, "Idempotency-Key": ref},
                    json={"amount": 2500, "description": "Coca-Cola 300ml"})
    link_id = r.json["id"]
    client.post(f"/pay/{link_id}/submit", data={"channel": "mtn_momo", "phone": phone})

    app = client.application
    deadline = time.time() + 6
    while time.time() < deadline:
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=link_id).one()
            if link.transaction_id:
                txn = db.session.get(Transaction, link.transaction_id)
                if txn.status.value in ("succeeded", "failed"):
                    return txn.status.value, txn.failure_reason
        time.sleep(0.1)
    raise AssertionError("charge never settled")


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        db.session.add(Merchant(
            name="Determinism Test", email="det@x.com",
            public_key="pk_det", secret_key="sk_det",
            test_public_key="pk_test_det", test_secret_key="sk_test_det",
            kyc_status="verified",
        ))
        db.session.commit()

    client = app.test_client()
    hdr = {"Authorization": "Bearer sk_test_det", "Content-Type": "application/json",
           "X-Timestamp": str(int(time.time()))}

    # ---- an ordinary number succeeds, every single time ----
    ordinary_results = [buy_and_wait(client, hdr, "256783647260", f"ord{i}")
                        for i in range(6)]
    check("an ordinary phone number succeeded on all 6 attempts (no flakiness)",
          all(status == "succeeded" for status, _ in ordinary_results))

    # ---- the documented "always succeeds" magic number, same guarantee ----
    always_ok = [buy_and_wait(client, hdr, "256700000000", f"ok{i}") for i in range(3)]
    check("magic success number (700000000) succeeded on all 3 attempts",
          all(status == "succeeded" for status, _ in always_ok))

    # ---- the magic failure numbers are deterministic, not just usually right ----
    insuff = [buy_and_wait(client, hdr, "256700000001", f"if{i}") for i in range(4)]
    check("magic insufficient_funds number (700000001) failed all 4 attempts",
          all(status == "failed" for status, _ in insuff))
    check("...with the documented reason every time",
          all(reason == "insufficient_funds" for _, reason in insuff))

    cancelled, _ = buy_and_wait(client, hdr, "0700000002", "uc1")   # local 07... form
    check("magic user_cancelled number works in local 07... format too",
          cancelled == "failed")

    timeout, reason = buy_and_wait(client, hdr, "256 700 000 003", "to1")  # spaced form
    check("magic timeout number tolerates spaces in the input",
          timeout == "failed" and reason == "timeout")

    print()
    failed = [label for label, ok in CHECKS if not ok]
    if failed:
        print(f"{len(failed)} CHECK(S) FAILED:")
        for label in failed:
            print("  - " + label)
        sys.exit(1)
    print(f"All {len(CHECKS)} deterministic-sandbox checks passed.")


if __name__ == "__main__":
    main()
