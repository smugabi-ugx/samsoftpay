"""Bank settlement — pay a merchant's balance to a BANK account.

Uganda has no automated bank-disbursement rail wired yet (MTN MoMo is the only
one), so a bank withdrawal is operator-confirmed: on approval we EARMARK the
money out of the merchant's available balance (exactly like a payout earmark), and
when the operator has actually made the bank transfer they CONFIRM it with a bank
reference, which releases the earmark to psp_float (the money has left us). If the
bank transfer can't be made, the earmark is REVERSED back to available.

The ledger postings mirror payouts.create_payout / complete_payout precisely
(guardrail 6 zero-sum), so bank settlement moves money through the same
merchant_available → suspense → psp_float path — just with a human, not a rail,
completing the middle step. LIVE ledger only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..extensions import db
from ..models import AccountType
from . import ledger
from .fees import calculate_payout_fee


class BankSettlementError(Exception):
    pass


def _accts(merchant_id, currency):
    avail = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant_id,
        currency=currency, is_test=False)
    suspense = ledger.get_or_create_account(
        type=AccountType.SUSPENSE, merchant_id=merchant_id,
        currency=currency, is_test=False)
    revenue = ledger.get_or_create_account(
        type=AccountType.PSP_REVENUE, merchant_id=None,
        currency=currency, is_test=False)
    return avail, suspense, revenue


def earmark_bank_withdrawal(wr) -> None:
    """Approve a BANK withdrawal: debit available (amount+fee), park amount in
    suspense, take the fee to revenue. Row-locks the available balance and refuses
    with ZERO writes if it can't cover amount+fee (guardrails 5/13). Caller commits."""
    fee = calculate_payout_fee(amount=wr.amount, currency=wr.currency)
    avail, suspense, revenue = _accts(wr.merchant_id, wr.currency)
    ledger.lock_account_for_update(avail)
    available = -int(avail.cached_balance)
    if available < wr.amount + fee:
        raise BankSettlementError(
            f"insufficient available balance: have {available}, need {wr.amount + fee}")
    ledger.post(
        [(avail, +(wr.amount + fee)), (suspense, -wr.amount), (revenue, -fee)],
        currency=wr.currency,
        memo=f"bank settlement {wr.public_id} earmarked (fee {fee})")
    wr.fee_amount = fee
    wr.status = "processing"
    wr.processed_at = datetime.now(timezone.utc)


def settle_bank_withdrawal(wr, bank_reference: str) -> None:
    """Confirm the operator made the bank transfer: release the earmark from
    suspense to psp_float (money has left us). Caller commits."""
    _, suspense, _ = _accts(wr.merchant_id, wr.currency)
    psp_float = ledger.get_or_create_account(
        type=AccountType.PSP_FLOAT, merchant_id=None, currency=wr.currency, is_test=False)
    ledger.post(
        [(suspense, +wr.amount), (psp_float, -wr.amount)],
        currency=wr.currency,
        memo=f"bank settlement {wr.public_id} paid (ref {bank_reference})")
    wr.status = "completed"
    wr.bank_reference = (bank_reference or "")[:120]
    wr.processed_at = datetime.now(timezone.utc)


def reverse_bank_withdrawal(wr, reason: str = "") -> None:
    """The bank transfer could not be made: reverse the earmark back to the
    merchant's available balance. Caller commits."""
    fee = int(wr.fee_amount or 0)
    avail, suspense, revenue = _accts(wr.merchant_id, wr.currency)
    ledger.post(
        [(suspense, +wr.amount), (revenue, +fee), (avail, -(wr.amount + fee))],
        currency=wr.currency,
        memo=f"bank settlement {wr.public_id} reversed (unpaid)")
    wr.status = "rejected"
    wr.admin_notes = (reason or "bank transfer could not be completed")[:255]
    wr.processed_at = datetime.now(timezone.utc)
