"""Wallet v1 — on-us transfer: pay another Samsoftpay account from your balance.

The SAFE, pre-licence version of "pay with wallet". It moves money that ALREADY
exists inside Samsoftpay (a merchant's settled MERCHANT_AVAILABLE balance) to
another Samsoftpay account. No new consumer float is held, so it stays inside
what a payment aggregator does — the stored-value CONSUMER wallet (top up + hold
a balance) is v2 and needs the Equity trust account / licence.

Money move (zero-sum, no external rail, no fee):
    (payer MERCHANT_AVAILABLE, +amount)   # reduce payer's available (it's a credit)
    (payee MERCHANT_AVAILABLE, -amount)   # increase payee's available (on-us = settled)

Safety, mirroring payouts:
  - row-lock the payer's available account BEFORE the balance check (no overdraft
    under concurrency)
  - KYC-gate the payer on live money (kyc_is_current)
  - mode-scoped: a live transfer moves live balances, a test transfer sandbox
  - payee must be an ACTIVE, real (non-managed) merchant, and never the payer

The payee gets a Transaction (channel=WALLET, SUCCEEDED, settled_at set so the
sweep skips it) so the payment appears in their balance/dashboard like any other
collection.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..extensions import db
from ..models import (
    AccountType, Channel, Merchant, Transaction, TxnStatus,
)
from . import ledger


class WalletTransferError(Exception):
    pass


def resolve_payee(ref: str) -> Merchant | None:
    """Find a payee by @handle, email, or numeric merchant id. Managed
    subaccounts and inactive accounts are NOT returned."""
    ref = (ref or "").strip().lstrip("@")
    if not ref:
        return None
    q = Merchant.query
    m = q.filter_by(handle=ref).first()
    if m is None and "@" in ref:
        m = q.filter_by(email=ref).first()
    if m is None and ref.isdigit():
        m = db.session.get(Merchant, int(ref))
    if m is None:
        return None
    if getattr(m, "is_managed", False) or not m.is_active:
        return None
    return m


def transfer(*, payer: Merchant, payee: Merchant, amount: int, currency: str = "UGX",
             is_test: bool, reference: str | None = None) -> dict:
    """Move `amount` from payer's available balance to payee, on-us. Returns
    {"ok": True, "transaction": txn} or {"ok": False, "error": "<reason>"}."""
    if amount is None or int(amount) <= 0:
        return {"ok": False, "error": "amount_must_be_positive"}
    amount = int(amount)
    if currency != "UGX":
        return {"ok": False, "error": "only_UGX_supported"}
    if payee is None:
        return {"ok": False, "error": "payee_not_found"}
    if payer.id == payee.id:
        return {"ok": False, "error": "cannot_send_to_yourself"}
    if getattr(payee, "is_managed", False) or not payee.is_active:
        return {"ok": False, "error": "payee_not_available"}
    if not payer.is_active:
        return {"ok": False, "error": "payer_not_active"}
    # KYC gate on LIVE money out (sandbox is open — building never needs approval).
    if not is_test and not payer.kyc_is_current():
        return {"ok": False, "error": "payer_not_verified"}

    # Row-lock the payer's available account BEFORE the balance check, so two
    # concurrent transfers can't both pass and overdraw it (payout discipline).
    payer_avail = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=payer.id,
        currency=currency, is_test=is_test)
    ledger.lock_account_for_update(payer_avail)
    available_positive = -payer_avail.cached_balance   # stored as a credit (negative)
    if available_positive < amount:
        return {"ok": False, "error": "insufficient_balance",
                "available": int(available_positive), "requested": amount}

    payee_avail = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=payee.id,
        currency=currency, is_test=is_test)

    now = datetime.now(timezone.utc)
    txn = Transaction(
        public_id=f"txn_{uuid.uuid4().hex[:16]}",
        merchant_id=payee.id,               # the payee RECEIVES the payment
        amount=amount, fee_amount=0, currency=currency,
        channel=Channel.WALLET, status=TxnStatus.SUCCEEDED,
        is_test=is_test,
        merchant_reference=reference or f"wallet from {payer.handle or payer.email}",
        customer_email=payer.email,         # so the payee can see who paid
        completed_at=now, settled_at=now,   # on-us = already settled; sweep skips it
    )
    db.session.add(txn)
    db.session.flush()   # need txn.id for the ledger transaction_id

    ledger.post(
        [(payer_avail, +amount), (payee_avail, -amount)],
        currency=currency, transaction_id=txn.id,
        memo=f"wallet transfer {txn.public_id}: {payer.email} -> {payee.email}")
    db.session.commit()
    return {"ok": True, "transaction": txn}
