"""Scheduled Payouts / Payroll — money-OUT on autopilot must be exactly-once and
balance-safe. This is the highest-risk surface in the system: a per-cycle
idempotency hole or a missing balance check pays real salaries twice or overdrafts
a merchant.

Script-style (run directly, NOT pytest), mirroring tests/test_settlement_sweep.py
and tests/test_payout_guards.py:
  - MOMO_USE_REAL=0            → in-process mock disbursement rail (no MTN, no Redis)
  - temp FILE sqlite           → the mock rail completes on a Timer thread; :memory:
                                 is per-connection and the completion would land in a
                                 different empty DB (CLAUDE.md LOCAL DEV note)
  - RAIL_SUCCESS_PROBABILITY=1 → deterministic sandbox (guardrail 16)

Invariants asserted (each is a money-safety property, not a UI nicety):
  [1] a cycle run twice (beat fires twice / same period) pays EXACTLY once
  [2] a schedule whose merchant balance can't cover it PAUSES and writes ZERO payouts
  [3] a per-recipient cap breach is rejected with ZERO writes (no schedule row)
  [4] the payout kill switch stops ALL scheduled payouts (zero writes, schedule left due)
  [5] a collections-only key is refused (403) at create (guardrail 24, money-OUT)
  [6] next_run_at advances by EXACTLY the interval on a successful run
The ledger sums to zero throughout (guardrail 6).

--------------------------------------------------------------------------------
CONTRACT this test pins for the sibling build components (model / service / route):

app/models/__init__.py — new model `ScheduledPayout` (mode-split per guardrail 12):
    id, public_id ("spo_..."), merchant_id, amount (BigInteger, per-recipient/cycle),
    currency, channel (SAEnum Channel), interval (String: "daily"|"weekly"|"monthly"),
    recipients (JSON/Text: list of {"phone","name"}), max_per_recipient (BigInteger,
    nullable — the per-recipient cap), status (String: "active"|"paused"|"cancelled"),
    is_test (Boolean, default False), next_run_at (DateTime, index), last_run_at
    (DateTime, nullable), failure_reason (String, nullable), created_at.

app/services/scheduled_payouts_service.py:
    _INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(weeks=1),
                  "monthly": timedelta(days=30)}
    class ScheduledPayoutError(Exception): ...
    create_scheduled_payout(*, merchant, amount, currency="UGX",
        channel=Channel.MTN_MOMO, interval="monthly", recipients, name=None,
        max_per_recipient=None, is_test=False) -> ScheduledPayout
        - validates interval/amount>0/recipients non-empty; caps amount>max_per_recipient
          → raise ScheduledPayoutError with ZERO writes (guardrail 13 shape); next_run_at=now.
    run_due(app=None) -> dict   # mirror of subscriptions_service.bill_due
        For each ACTIVE schedule with next_run_at <= now:
          a. kill switch: live schedule + payouts_frozen() → SKIP without claiming
             (leave it due; guardrail 24 kill switch).
          b. pre-check balance UNDER A ROW LOCK (guardrail 5): total =
             len(recipients) * (amount + calculate_payout_fee(...)); if the merchant's
             available balance can't cover the whole run → status="paused", commit,
             create ZERO payouts, continue.
          c. ATOMIC claim (mirror bill_due:185): UPDATE ... SET last_run_at=now,
             next_run_at=_next_run(now, interval) WHERE id=? AND status='active'
             AND next_run_at<=now. If 0 rows → a concurrent run won, continue. THIS
             is the exactly-once guard for a double beat fire.
          d. set g.api_mode = "test" if schedule.is_test else "live", then call
             payouts.create_payout(...) once per recipient (resolves rail BEFORE money
             moves, row-locks, earmarks — guardrails 13/21). g.api_mode MUST match
             schedule.is_test so the payout draws the SAME ledger the pre-check locked.

app/routes/api.py — POST /v1/scheduled-payouts:
    _check_timestamp(); m=_auth(); _require_full_scope()  # money-OUT: 403 collections keys
    Idempotency-Key required. Returns 201 {id, status, interval, next_run_at, ...}.
"""
import atexit
import os
import sys
import tempfile
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Deterministic mock rail, no Redis, temp FILE db (see module docstring).
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="scheduled_payouts_")
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
    Account, AccountType, Channel, JournalEntry, Merchant, Payout, ScheduledPayout,
)
from app.services import ledger
from app.services.fees import calculate_payout_fee
from app.services.platform_flags import FREEZE_PAYOUTS, set_flag
from app.services.scheduled_payouts_service import (
    ScheduledPayoutError, create_scheduled_payout, run_due,
)

FEE = calculate_payout_fee(amount=10_000, currency="UGX")   # 1.5% of 10,000, min 200 = 200
CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


# ── helpers ───────────────────────────────────────────────────────────────────

def make_merchant(email, *, start_balance=0, is_test=False):
    """Create a verified merchant and (optionally) fund its available ledger."""
    m = Merchant(
        name=email.split("@")[0], email=email,
        public_key=f"pk_{email}", secret_key=f"sk_{email}",
        test_secret_key=f"sk_test_{email}", kyc_status="verified",
    )
    db.session.add(m)
    db.session.commit()
    if start_balance:
        avail = ledger.get_or_create_account(
            type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id,
            currency="UGX", is_test=is_test)
        rail = ledger.get_or_create_account(
            type=AccountType.RAIL_CLEARING, merchant_id=None,
            currency="UGX", is_test=is_test)
        ledger.post([(rail, +start_balance), (avail, -start_balance)],
                    currency="UGX", memo="funding")
        db.session.commit()
    return m


def available(mid, is_test=False):
    a = Account.query.filter_by(
        merchant_id=mid, type=AccountType.MERCHANT_AVAILABLE, is_test=is_test).first()
    return -a.cached_balance if a else 0


def journal_total():
    return sum(e.amount for e in JournalEntry.query.all())


def recipients(*phones):
    return [{"phone": p, "name": f"Staff {p[-4:]}"} for p in phones]


# ── the tests ──────────────────────────────────────────────────────────────────

def test_exactly_once_and_interval(app):
    """[1] + [6]: two run_due() calls (a double beat fire) pay each recipient
    exactly once, debit exactly amount+fee per recipient, and advance next_run_at
    by EXACTLY the interval."""
    with app.app_context():
        m = make_merchant("payroll1@x.com", start_balance=100_000)
        sp = create_scheduled_payout(
            merchant=m, amount=10_000, currency="UGX", channel=Channel.MTN_MOMO,
            interval="weekly",
            recipients=recipients("256780000010", "256780000011", "256780000012"),
        )
        sp_id, mid = sp.id, m.id
        n = 3

        run_due()                       # beat tick #1 — claims + pays
        run_due()                       # beat tick #2 (immediate re-fire) — must no-op

        paid = Payout.query.filter_by(merchant_id=mid).all()
        check("[1] double run_due pays each recipient EXACTLY once (3 payouts)",
              len(paid) == n)
        check("[1] every scheduled payout is for the schedule amount",
              all(p.amount == 10_000 for p in paid))
        check("[1] merchant debited exactly n*(amount+fee), no double-spend",
              available(mid) == 100_000 - n * (10_000 + FEE))

        sp = db.session.get(ScheduledPayout, sp_id)
        db.session.refresh(sp)
        check("[6] schedule is still active after a successful run",
              sp.status == "active")
        check("[6] next_run_at advanced by EXACTLY one interval (weekly)",
              sp.last_run_at is not None
              and (sp.next_run_at - sp.last_run_at) == timedelta(weeks=1))
        check("[1] ledger still sums to zero", journal_total() == 0)


def test_insufficient_balance_pauses(app):
    """[2]: a schedule the merchant can't fully cover PAUSES and writes ZERO
    payouts — the whole run is refused atomically, never a partial payroll."""
    with app.app_context():
        m = make_merchant("payroll2@x.com", start_balance=5_000)   # < 10_000 + fee
        sp = create_scheduled_payout(
            merchant=m, amount=10_000, currency="UGX", channel=Channel.MTN_MOMO,
            interval="monthly", recipients=recipients("256780000020"),
        )
        sp_id, mid = sp.id, m.id

        run_due()

        check("[2] no payout was created for an unaffordable schedule",
              Payout.query.filter_by(merchant_id=mid).count() == 0)
        check("[2] merchant balance untouched (no earmark leaked)",
              available(mid) == 5_000)
        sp = db.session.get(ScheduledPayout, sp_id)
        db.session.refresh(sp)
        check("[2] the schedule is PAUSED", sp.status == "paused")
        check("[2] ledger still sums to zero", journal_total() == 0)


def test_cap_breach_zero_writes(app):
    """[3]: a per-recipient cap breach is rejected with ZERO writes — the schedule
    row itself never lands (guardrail 13 zero-write-on-refusal shape)."""
    with app.app_context():
        m = make_merchant("payroll3@x.com")
        before = ScheduledPayout.query.count()
        raised = False
        try:
            create_scheduled_payout(
                merchant=m, amount=20_000, currency="UGX", channel=Channel.MTN_MOMO,
                interval="monthly", recipients=recipients("256780000030"),
                max_per_recipient=10_000,      # 20_000 > cap → reject
            )
        except ScheduledPayoutError:
            raised = True
        db.session.rollback()
        check("[3] a cap-breaching schedule raises ScheduledPayoutError", raised)
        check("[3] and NO schedule row was written",
              ScheduledPayout.query.count() == before)


def test_kill_switch_stops_scheduled_payouts(app):
    """[4]: the payout kill switch halts every scheduled payout with zero writes,
    leaving the schedule DUE so it runs once the freeze is lifted."""
    with app.app_context():
        m = make_merchant("payroll4@x.com", start_balance=100_000)
        sp = create_scheduled_payout(
            merchant=m, amount=10_000, currency="UGX", channel=Channel.MTN_MOMO,
            interval="daily", recipients=recipients("256780000040"),
        )
        sp_id, mid = sp.id, m.id
        due_at = db.session.get(ScheduledPayout, sp_id).next_run_at

        set_flag(FREEZE_PAYOUTS, "on")
        run_due()
        check("[4] frozen: no scheduled payout is created",
              Payout.query.filter_by(merchant_id=mid).count() == 0)
        sp = db.session.get(ScheduledPayout, sp_id)
        db.session.refresh(sp)
        check("[4] frozen: schedule stays active (a platform freeze is not its fault)",
              sp.status == "active")
        check("[4] frozen: schedule is left DUE (next_run_at not advanced)",
              sp.next_run_at == due_at)
        check("[4] frozen: merchant balance untouched", available(mid) == 100_000)

        set_flag(FREEZE_PAYOUTS, "off")
        run_due()
        check("[4] after unfreeze: the schedule now pays",
              Payout.query.filter_by(merchant_id=mid).count() == 1)
        check("[4] ledger still sums to zero", journal_total() == 0)


def test_collections_key_refused_at_create(app):
    """[5]: a collections-only key is 403'd at POST /v1/scheduled-payouts (money-OUT
    on autopilot is even more dangerous than a one-off payout), while a full secret
    key creates the schedule normally. Guardrail 24."""
    with app.app_context():
        m = Merchant(
            name="Kiosk Payroll", email="payroll5@x.com",
            public_key="pk_test_kiosk5", secret_key="sk_live_kiosk5_unused",
            test_secret_key="sk_test_full_sched5",
            test_collections_key="sk_test_col_sched5",
            kyc_status="verified")
        db.session.add(m)
        db.session.commit()

    client = app.test_client()

    def hdr(key, idem):
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "Idempotency-Key": idem, "X-Timestamp": str(int(time.time()))}

    body = dict(amount=10_000, currency="UGX", channel="mtn_momo", interval="monthly",
                recipients=[{"phone": "256780000050", "name": "Staff"}])

    r = client.post("/v1/scheduled-payouts",
                    headers=hdr("sk_test_col_sched5", "sched-col-1"), json=body)
    check("[5] collections key is 403 on POST /v1/scheduled-payouts", r.status_code == 403)
    with app.app_context():
        check("[5] the 403 wrote no schedule row", ScheduledPayout.query.count() == 0)

    r = client.post("/v1/scheduled-payouts",
                    headers=hdr("sk_test_full_sched5", "sched-full-1"), json=body)
    check("[5] full secret key still creates the schedule (201)", r.status_code == 201)
    check("[5] a bogus/unknown key is 401 (auth), not 403 (scope)",
          client.post("/v1/scheduled-payouts",
                      headers=hdr("sk_test_nope_nope", "sched-x-1"),
                      json=body).status_code == 401)


def main():
    app = create_app()
    with app.app_context():
        db.create_all()

    test_exactly_once_and_interval(app)
    test_insufficient_balance_pauses(app)
    test_cap_breach_zero_writes(app)
    test_kill_switch_stops_scheduled_payouts(app)
    test_collections_key_refused_at_create(app)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL SCHEDULED-PAYOUT TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
