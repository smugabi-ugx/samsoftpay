"""Bank settlement (operator-confirmed) must move money exactly like a payout —
earmark on approval, release on confirmed transfer, reverse if it can't be paid.

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_bank_settlement.py

  [1] earmark debits available by amount+fee, parks amount in suspense, fee to revenue
  [2] confirm releases suspense -> psp_float; available stays down; status completed + ref
  [3] a bank transfer that can't be made REVERSES fully back to available
  [4] earmark refuses (zero writes) when available can't cover amount+fee
The ledger sums to zero throughout.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from werkzeug.security import generate_password_hash
from app import create_app
from app.extensions import db
from app.models import (Merchant, WithdrawalRequest, SettlementAccount, AccountType,
                        JournalEntry)
from app.services import ledger
from app.services.bank_settlement import (
    earmark_bank_withdrawal, settle_bank_withdrawal, reverse_bank_withdrawal,
    BankSettlementError)
import uuid


def bal(mtype, mid):
    a = ledger.get_or_create_account(type=mtype, merchant_id=mid, currency="UGX", is_test=False)
    db.session.refresh(a)
    return -int(a.cached_balance)


def journal_sum():
    return sum(int(e.amount) for e in JournalEntry.query.all())


def _wr(m, amount):
    sa = SettlementAccount(public_id=f"sa_{uuid.uuid4().hex[:12]}", merchant_id=m.id,
                           account_type="bank", account_number="011012345678",
                           account_name="Kampala Coffee Ltd", bank_name="Stanbic",
                           is_verified=True)
    db.session.add(sa); db.session.commit()
    wr = WithdrawalRequest(public_id=f"wd_{uuid.uuid4().hex[:12]}", merchant_id=m.id,
                           settlement_account_id=sa.id, amount=amount, currency="UGX",
                           status="pending")
    db.session.add(wr); db.session.commit()
    return wr


def main():
    app = create_app({"WTF_CSRF_ENABLED": False, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        m = Merchant(name="Kampala Coffee Ltd", email="k@x.com", public_key="pk",
                     secret_key="sk_live_k", handle="kc", kyc_status="verified",
                     password_hash=generate_password_hash("x"))
        db.session.add(m); db.session.commit()
        W0 = 500_000
        rail = ledger.get_or_create_account(type=AccountType.RAIL_CLEARING, merchant_id=None, currency="UGX", is_test=False)
        av = ledger.get_or_create_account(type=AccountType.MERCHANT_AVAILABLE, merchant_id=m.id, currency="UGX", is_test=False)
        ledger.post([(rail, +W0), (av, -W0)], currency="UGX", memo="seed"); db.session.commit()

        AMT = 200_000
        from app.services.fees import calculate_payout_fee
        FEE = calculate_payout_fee(amount=AMT, currency="UGX")   # 1.5% cap 5000 -> 3000

        # [1] earmark
        wr = _wr(m, AMT)
        earmark_bank_withdrawal(wr); db.session.commit()
        assert bal(AccountType.MERCHANT_AVAILABLE, m.id) == W0 - AMT - FEE, "[1] available not debited"
        assert bal(AccountType.SUSPENSE, m.id) == AMT, "[1] amount not in suspense"
        assert wr.status == "processing" and wr.fee_amount == FEE, "[1] status/fee"
        assert journal_sum() == 0, "[1] journal zero"
        print(f"[1] PASS — earmarked: available {W0}->{W0-AMT-FEE}, {AMT} in suspense, fee {FEE} to revenue")

        # [2] confirm
        settle_bank_withdrawal(wr, "STANBIC-TT-0099"); db.session.commit()
        assert bal(AccountType.SUSPENSE, m.id) == 0, "[2] suspense not released"
        assert bal(AccountType.MERCHANT_AVAILABLE, m.id) == W0 - AMT - FEE, "[2] available changed on confirm"
        assert bal(AccountType.PSP_FLOAT, None) == AMT, "[2] float not credited"
        assert wr.status == "completed" and wr.bank_reference == "STANBIC-TT-0099", "[2] status/ref"
        assert journal_sum() == 0, "[2] journal zero"
        print("[2] PASS — confirmed: suspense->psp_float, status completed, ref recorded")

        # [3] reverse path (fresh WR)
        wr2 = _wr(m, 50_000)
        earmark_bank_withdrawal(wr2); db.session.commit()
        after_earmark = bal(AccountType.MERCHANT_AVAILABLE, m.id)
        reverse_bank_withdrawal(wr2, "bank rejected the account"); db.session.commit()
        assert bal(AccountType.MERCHANT_AVAILABLE, m.id) == after_earmark + 50_000 + calculate_payout_fee(amount=50_000), "[3] not reversed fully"
        assert wr2.status == "rejected", "[3] status"
        assert journal_sum() == 0, "[3] journal zero"
        print("[3] PASS — unpayable transfer reversed fully back to available")

        # [4] refuse when unaffordable, zero writes
        wr3 = _wr(m, 100_000_000)   # way over balance
        j_before = journal_sum(); n_before = JournalEntry.query.count()
        raised = False
        try:
            earmark_bank_withdrawal(wr3); db.session.commit()
        except BankSettlementError:
            raised = True
            db.session.rollback()
        assert raised, "[4] should refuse unaffordable"
        assert JournalEntry.query.count() == n_before, "[4] zero writes on refusal"
        print("[4] PASS — unaffordable earmark refused with zero writes")

        print("\nALL BANK-SETTLEMENT ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
