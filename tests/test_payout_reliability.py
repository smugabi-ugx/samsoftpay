"""Payouts get the same straggler safety net + external MTN reconciliation
that charges already had — the OUTBOUND leg was previously unguarded.

A payout is parked AUTHORIZED whenever its 90s poller times out (MTN can
complete at 91s) or a network failure was ambiguous. Until now nothing
automatically finished it from MTN's own answer — it sat AUTHORIZED until a
human ran `flask stranded-payouts`. For the one direction where "money may have
left but we don't know" is worst, that was the gap.

What this proves:
  1. sweep_stale_payouts completes an AUTHORIZED payout MTN says SUCCESSFUL.
  2. ...and one MTN says FAILED (full refund to merchant).
  3. MTN PENDING / network-unknown is SKIPPED, never refunded (recipient may be paid).
  4. A PENDING payout with NO rail_reference is never touched by the sweep
     (that's a stranded-earmark case for `flask reverse-payout`).
  5. reconcile_against_mtn flags a payout MTN says SUCCESSFUL but we don't (critical).
  6. ...and a payout we SUCCEEDED but MTN says FAILED (critical).
  7. A later agreeing run auto-resolves the payout exception.
  8. Ledger still sums to zero throughout.
"""
import atexit
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="payout_rel_")
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
from app.models import (
    Account, AccountType, Channel, JournalEntry, Merchant, Payout, PayoutStatus,
    ReconException, utcnow,
)
from app.services import ledger, sweep as sweep_svc
from app.services.reconciliation import reconcile_against_mtn

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


# Controllable fake MTN disbursement status keyed by rail_reference.
MTN = {}


def fake_disb_status(ref):
    return MTN.get(ref)


def journal_zero():
    return sum(e.amount for e in JournalEntry.query.all()) == 0


def make_authorized_payout(app, mid, ref, amount=10000, fee=500, minutes_old=120,
                           status=PayoutStatus.AUTHORIZED):
    """Create a payout with a proper earmark (avail -> suspense + revenue),
    matching create_payout, then backdate + park it AUTHORIZED."""
    with app.app_context():
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX")
        suspense = ledger.get_or_create_account(
            type=AccountType.SUSPENSE, merchant_id=mid, currency="UGX")
        revenue = ledger.get_or_create_account(
            type=AccountType.PSP_REVENUE, merchant_id=None, currency="UGX")
        ledger.post([(avail, +(amount + fee)), (suspense, -amount), (revenue, -fee)],
                    currency="UGX", memo="earmark")
        p = Payout(public_id=ref.replace("ref", "pout"), merchant_id=mid, amount=amount,
                   fee_amount=fee, currency="UGX", channel=Channel.MTN_MOMO,
                   status=status, is_test=False, recipient_phone="256780000001",
                   recipient_name="Supplier", rail_reference=ref,
                   created_at=utcnow() - timedelta(minutes=minutes_old))
        db.session.add(p)
        db.session.commit()
        return p.id


def main():
    app = create_app({"MOMO_USE_REAL": True})   # enable the real-rail sweep path
    sweep_svc._query_mtn_disbursement_status = fake_disb_status   # no network

    with app.app_context():
        db.create_all()
        # Fund the merchant generously so every earmark has balance.
        m = Merchant(name="PayoutCo", email="p@x.com", public_key="pk_test_p",
                     secret_key="sk_live_p", kyc_status="verified")
        db.session.add(m)
        db.session.commit()
        mid = m.id
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX")
        rail = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING, merchant_id=None, currency="UGX")
        ledger.post([(rail, +500000), (avail, -500000)], currency="UGX", memo="funding")
        db.session.commit()

    # 1. SUCCESSFUL -> completed as succeeded.
    MTN["ref-succ"] = "SUCCESSFUL"
    pid = make_authorized_payout(app, mid, "ref-succ")
    with app.app_context():
        sweep_svc.sweep_stale_payouts(stale_minutes=60)
    with app.app_context():
        p = db.session.get(Payout, pid)
        check("MTN SUCCESSFUL -> payout SUCCEEDED", p.status == PayoutStatus.SUCCEEDED)

    # 2. FAILED -> refunded to merchant.
    MTN["ref-fail"] = "FAILED"
    pid = make_authorized_payout(app, mid, "ref-fail")
    with app.app_context():
        before = -ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX").cached_balance
    with app.app_context():
        sweep_svc.sweep_stale_payouts(stale_minutes=60)
    with app.app_context():
        p = db.session.get(Payout, pid)
        after = -ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency="UGX").cached_balance
        check("MTN FAILED -> payout FAILED", p.status == PayoutStatus.FAILED)
        check("MTN FAILED -> merchant refunded amount+fee", after == before + 10000 + 500)

    # 3. PENDING and network-unknown are skipped (left AUTHORIZED, no refund).
    MTN["ref-pend"] = "PENDING"
    pid_pend = make_authorized_payout(app, mid, "ref-pend")
    pid_unk = make_authorized_payout(app, mid, "ref-unk")   # not in MTN -> None
    with app.app_context():
        sweep_svc.sweep_stale_payouts(stale_minutes=60)
    with app.app_context():
        check("MTN PENDING -> left AUTHORIZED",
              db.session.get(Payout, pid_pend).status == PayoutStatus.AUTHORIZED)
        check("network-unknown -> left AUTHORIZED (never refunded)",
              db.session.get(Payout, pid_unk).status == PayoutStatus.AUTHORIZED)

    # 4. PENDING payout with NO rail_reference is never touched by the sweep.
    with app.app_context():
        noref = Payout(public_id="pout_noref", merchant_id=mid, amount=3000,
                       fee_amount=200, currency="UGX", channel=Channel.MTN_MOMO,
                       status=PayoutStatus.PENDING, is_test=False,
                       recipient_phone="256780000002", rail_reference=None,
                       created_at=utcnow() - timedelta(minutes=120))
        db.session.add(noref)
        db.session.commit()
        noref_id = noref.id
    with app.app_context():
        sweep_svc.sweep_stale_payouts(stale_minutes=60)
    with app.app_context():
        check("no-reference PENDING payout untouched by sweep",
              db.session.get(Payout, noref_id).status == PayoutStatus.PENDING)
        check("ledger sums to zero after all sweeps", journal_zero())

    # 5/6/7. Reconciliation of payouts against MTN.
    #   - AUTHORIZED payout that MTN says SUCCESSFUL  -> critical exception.
    MTN["ref-recon1"] = "SUCCESSFUL"
    make_authorized_payout(app, mid, "ref-recon1", status=PayoutStatus.AUTHORIZED)
    #   - SUCCEEDED payout that MTN says FAILED       -> critical exception.
    MTN["ref-recon2"] = "FAILED"
    make_authorized_payout(app, mid, "ref-recon2", status=PayoutStatus.SUCCEEDED)
    with app.app_context():
        summary = reconcile_against_mtn(min_age_minutes=1)
        e1 = ReconException.query.filter_by(rail_reference="ref-recon1").first()
        e2 = ReconException.query.filter_by(rail_reference="ref-recon2").first()
        check("recon flags MTN-succeeded-local-not (critical)",
              e1 and e1.kind == "mtn_payout_succeeded_local_not" and e1.severity == "critical")
        check("recon flags local-succeeded-MTN-failed (critical)",
              e2 and e2.kind == "local_payout_succeeded_mtn_failed" and e2.severity == "critical")

    # 7. A later agreeing run auto-resolves. Flip ref-recon2's MTN answer to
    #    SUCCESSFUL so it now agrees with our SUCCEEDED.
    MTN["ref-recon2"] = "SUCCESSFUL"
    with app.app_context():
        reconcile_against_mtn(min_age_minutes=1)
        e2 = ReconException.query.filter_by(rail_reference="ref-recon2").first()
        check("agreeing payout exception auto-resolves",
              e2 and e2.status == "resolved" and e2.resolved_by == "auto")
        check("ledger still sums to zero", journal_zero())

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL PAYOUT-RELIABILITY TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
