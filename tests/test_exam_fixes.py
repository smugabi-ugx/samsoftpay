"""Fixes for the onboarding-readiness exam findings (all originally EXECUTED bugs).

What this proves:
  1. BLOCKER: refunding a TEST charge moves ONLY test money — the live ledger is
     untouched (it used to debit real UGX for sandbox activity), and the refund
     payout itself is is_test=True.
  2. An API caller whose key mode mismatches the charge mode is refused with
     zero writes (a test key can no longer mark a LIVE charge refunded).
  3. Rejecting a withdrawal that is no longer pending is refused (it used to
     flip a DELIVERED payout's request to 'rejected').
  4. Rejecting a completed top-up is refused (credit used to stay with a lying record).
  5. WithdrawalRequest reaches a terminal state when its payout completes
     (used to stay 'processing' forever) — and payout.succeeded webhook enqueued.
  6. Bulk payout: reusing a reference with a DIFFERENT body errors instead of
     silently returning the old payout as ok:true.
  7. KYC gate: a pending-KYC merchant's live key cannot create charges; the
     test key still can.
  8. KYC step3 document delete no longer 500s.
"""
import atexit
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="examfix_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try: os.unlink(_P)
    except OSError: pass


from app import create_app
from app.extensions import db
from app.models import (
    Account, AccountType, Channel, Merchant, Payout, PayoutStatus,
    SettlementAccount, Transaction, TxnStatus, TopUpRequest, WebhookDelivery,
    WithdrawalRequest, utcnow,
)
from app.services import ledger

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def bal(mid, t, is_test):
    a = Account.query.filter_by(merchant_id=mid, type=t, is_test=is_test).first()
    return -a.cached_balance if a else 0


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    import app.tasks.webhooks_task as _wt
    _wt.deliver_webhook.delay = lambda *a, **k: None   # no broker offline

    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        m = Merchant(name="Exam Co", email="exam@x.com", public_key="pk_e",
                     secret_key="sk_live_e", test_secret_key="sk_test_e",
                     kyc_status="verified", handle="exam",
                     webhook_url="https://merchant.example/hooks",
                     password_hash=generate_password_hash("x"))
        admin = Merchant(name="Adm", email="adm@x.com", public_key="pk_a",
                         secret_key="sk_a", role="admin", kyc_status="verified",
                         handle="adm", password_hash=generate_password_hash("x"))
        pending = Merchant(name="Pending Co", email="pend@x.com", public_key="pk_p",
                           secret_key="sk_live_p", test_secret_key="sk_test_p",
                           kyc_status="pending", handle="pend",
                           password_hash=generate_password_hash("x"))
        db.session.add_all([m, admin, pending])
        db.session.commit()
        mid, aid = m.id, admin.id
        # Fund BOTH ledgers for the refund test.
        for is_test in (False, True):
            avail = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE,
                                                 merchant_id=mid, currency="UGX", is_test=is_test)
            rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING,
                                                merchant_id=None, currency="UGX", is_test=is_test)
            ledger.post([(rail, +100000), (avail, -100000)], currency="UGX", memo="fund")
        db.session.commit()

    c = app.test_client()

    # ── 1. Dashboard refund of a TEST charge must not touch live money ──
    with app.app_context():
        t = Transaction(public_id="txn_t1", merchant_id=mid, amount=10000, fee_amount=200,
                        currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                        is_test=True, customer_phone="256780000001", completed_at=utcnow())
        db.session.add(t); db.session.commit()
        live_before = bal(mid, AccountType.MERCHANT_AVAILABLE, False)
    with c.session_transaction() as s:
        s["_user_id"] = str(mid); s["_fresh"] = True
    c.post(f"/dashboard/{mid}/charge/txn_t1/refund")
    with app.app_context():
        check("live available UNTOUCHED by a test-charge refund",
              bal(mid, AccountType.MERCHANT_AVAILABLE, False) == live_before)
        t = Transaction.query.filter_by(public_id="txn_t1").first()
        p = db.session.get(Payout, t.refund_payout_id) if t.refund_payout_id else None
        check("test refund executed on the TEST ledger (payout is_test=True)",
              t.status == TxnStatus.REFUNDED and p is not None and p.is_test is True)

    # ── 2. API mode mismatch refused with zero writes ──
    with app.app_context():
        t2 = Transaction(public_id="txn_live1", merchant_id=mid, amount=5000, fee_amount=200,
                         currency="UGX", channel=Channel.MTN_MOMO, status=TxnStatus.SUCCEEDED,
                         is_test=False, customer_phone="256780000001", completed_at=utcnow())
        db.session.add(t2); db.session.commit()
    r = c.post("/v1/charges/txn_live1/refund", headers={
        "Authorization": "Bearer sk_test_e", "Content-Type": "application/json",
        "Idempotency-Key": "mm1", "X-Timestamp": str(int(time.time()))})
    with app.app_context():
        t2 = Transaction.query.filter_by(public_id="txn_live1").first()
        check("test key cannot refund a LIVE charge (mode_mismatch, charge intact)",
              t2.status == TxnStatus.SUCCEEDED and r.status_code == 400
              and "mode_mismatch" in (r.get_json() or {}).get("error", ""))

    # ── 3/4. Reject-after-terminal guards ──
    with c.session_transaction() as s:
        s["_user_id"] = str(aid); s["_fresh"] = True
    with app.app_context():
        sa = SettlementAccount(public_id="sacc_1", merchant_id=mid, account_type="momo", account_number="256780000001", account_name="Exam Co")
        db.session.add(sa); db.session.commit()
        wr = WithdrawalRequest(public_id="wd_1", merchant_id=mid,
                               settlement_account_id=sa.id, amount=5000,
                               currency="UGX", status="processing")
        tp = TopUpRequest(public_id="tpr_1", merchant_id=mid, method="momo",
                          amount=15000, status="completed")
        db.session.add(wr)
        if tp is not None: db.session.add(tp)
        db.session.commit()
        wr_id = wr.id
        tp_id = tp.id if tp is not None else None
    c.post(f"/admin/withdrawals/{wr_id}/reject", data={"reason": "nope"})
    with app.app_context():
        wr = db.session.get(WithdrawalRequest, wr_id)
        check("processing withdrawal CANNOT be rejected", wr.status == "processing")
    if tp_id is not None:
        c.post(f"/admin/topups/{tp_id}/reject", data={"reason": "nope"})
        with app.app_context():
            tp = db.session.get(TopUpRequest, tp_id)
            check("completed top-up CANNOT be rejected", tp.status == "completed")

    # ── 5. Withdrawal terminal state + payout webhook on completion ──
    with app.app_context():
        from flask import g as _g
        with app.test_request_context():
            _g.api_mode = "live"
            from app.services.payouts import create_payout, complete_payout
            po = create_payout(merchant=db.session.get(Merchant, mid), amount=3000,
                               currency="UGX", recipient_phone="256780000001")
            wr2 = WithdrawalRequest(public_id="wd_2", merchant_id=mid,
                                    settlement_account_id=1, amount=3000,
                                    currency="UGX", status="processing", payout_id=po.id)
            db.session.add(wr2); db.session.commit()
            wh_before = WebhookDelivery.query.count()
            complete_payout(po.id, success=True, rail_reference=po.rail_reference or "r")
            wr2 = WithdrawalRequest.query.filter_by(public_id="wd_2").first()
            check("withdrawal reaches terminal 'completed'", wr2.status == "completed")
            deliveries = WebhookDelivery.query.order_by(WebhookDelivery.id.desc()).all()
            newest = deliveries[0] if len(deliveries) > wh_before else None
            check("payout.succeeded webhook enqueued",
                  newest is not None and '"payout.succeeded"' in newest.payload)

    # ── 6. Bulk reference reuse with different body errors ──
    def bulk(items, idem):
        return c.post("/v1/payouts/bulk", headers={
            "Authorization": "Bearer sk_live_e", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))},
            json={"payouts": items})
    r1 = bulk([{"amount": 2000, "phone": "256780000001", "reference": "dup-x"}], "b1")
    check("bulk first pay ok", r1.status_code == 200 and r1.json["accepted"] == 1)
    r2 = bulk([{"amount": 9999, "phone": "256780000001", "reference": "dup-x"}], "b2")
    item = r2.json["results"][0]
    check("bulk reference reuse w/ different body ERRORS (no silent ok)",
          item["ok"] is False and "reused" in item["error"])

    # ── 7. KYC gate on live keys ──
    def charge(key, idem):
        return c.post("/v1/charges", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))},
            json={"amount": 5000, "channel": "mtn_momo",
                  "customer": {"phone": "256700000000"}})
    r = charge("sk_live_p", "kg1")
    check("pending-KYC live key refused (400, verification message)",
          r.status_code == 400 and "verif" in (r.get_json() or {}).get("error", ""))
    r = charge("sk_test_p", "kg2")
    check("pending-KYC TEST key still works (201)", r.status_code == 201)

    # ── 8. KYC step3 delete no longer 500s ──
    with app.app_context():
        from app.models import KYCApplication, KYCDocument
        pend = Merchant.query.filter_by(email="pend@x.com").first()
        ka = KYCApplication(merchant_id=pend.id, company_name="P")
        db.session.add(ka); db.session.commit()
        doc = KYCDocument(application_id=ka.id, doc_type="certificate",
                          original_filename="c.pdf", stored_filename="nofile.pdf")
        db.session.add(doc); db.session.commit()
        doc_id = doc.id
        pend_id = pend.id
    with c.session_transaction() as s:
        s["_user_id"] = str(pend_id); s["_fresh"] = True
    r = c.post(f"/kyc/step/3/delete/{doc_id}", follow_redirects=False)
    check("KYC doc delete redirects (no 500)", r.status_code in (301, 302, 303))

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL EXAM-FIX TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
