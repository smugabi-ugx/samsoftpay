"""All ORM models.

Money is stored in MINOR UNITS (integer cents/centavos/UGX-shillings since UGX
has no minor unit but we still use an integer to avoid floating-point drift).
Never use floats for money.
"""
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from ..extensions import db


def utcnow():
    return datetime.now(timezone.utc)


# ---------- Enums ----------

class TxnStatus(str, Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"   # rail accepted, awaiting completion
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class PayoutStatus(str, Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"   # disbursement accepted by rail
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Channel(str, Enum):
    MTN_MOMO = "mtn_momo"
    AIRTEL_MONEY = "airtel_money"
    CARD = "card"


class AccountType(str, Enum):
    """Ledger account classification.

    PSP (us):
      - rail_clearing: money sitting at the MNO/bank before it settles to us
      - psp_revenue: our fees
      - psp_float: our own funds (e.g. for refunds we cover)
    Merchant:
      - merchant_pending: funds collected, not yet available for payout
      - merchant_available: funds available to settle to merchant bank
    Customer-side is implicit (external).
    """
    RAIL_CLEARING = "rail_clearing"
    PSP_REVENUE = "psp_revenue"
    PSP_FLOAT = "psp_float"
    MERCHANT_PENDING = "merchant_pending"
    MERCHANT_AVAILABLE = "merchant_available"
    SUSPENSE = "suspense"   # for unreconciled items


# ---------- Tenant / merchant ----------

class Merchant(db.Model):
    __tablename__ = "merchants"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), nullable=False, unique=True)
    public_key = Column(String(80), nullable=False, unique=True, index=True)
    secret_key = Column(String(80), nullable=False, unique=True, index=True)
    webhook_url = Column(String(500), nullable=True)
    kyc_status = Column(String(20), default="pending")  # pending|verified|rejected
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    accounts = relationship("Account", back_populates="merchant")
    transactions = relationship("Transaction", back_populates="merchant")


# ---------- Ledger ----------

class Account(db.Model):
    """A ledger account. Balances are NEVER stored here directly — they're
    derived by summing journal entries. We keep a cached balance for fast
    reads but it's recomputable.
    """
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    type = Column(SAEnum(AccountType), nullable=False)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=True)
    currency = Column(String(3), nullable=False, default="UGX")
    # Cached for performance; the journal is source of truth.
    cached_balance = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    merchant = relationship("Merchant", back_populates="accounts")

    __table_args__ = (
        UniqueConstraint("type", "merchant_id", "currency", name="uq_account"),
    )


class JournalEntry(db.Model):
    """One half of a double-entry posting. Always written in pairs (debit+credit)
    within the same `journal_id`. The pair sum MUST equal zero per currency.
    Entries are append-only; corrections are done by writing reversing entries.
    """
    __tablename__ = "journal_entries"
    id = Column(Integer, primary_key=True)
    journal_id = Column(String(36), nullable=False, index=True)  # uuid groups paired entries
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    # signed amount: debit positive, credit negative (or pick a convention and stick)
    # We use: positive = increase the account, negative = decrease.
    # The debit/credit interpretation depends on account type; what matters is
    # that summing all entries for a single journal_id = 0.
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    memo = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    account = relationship("Account")

    __table_args__ = (
        Index("ix_journal_account_created", "account_id", "created_at"),
    )


# ---------- Transactions ----------

class Transaction(db.Model):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)  # what merchants see
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    amount = Column(BigInteger, nullable=False)
    fee_amount = Column(BigInteger, nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="UGX")
    channel = Column(SAEnum(Channel), nullable=False)
    status = Column(SAEnum(TxnStatus), nullable=False, default=TxnStatus.PENDING, index=True)
    merchant_reference = Column(String(120), nullable=True)
    customer_phone = Column(String(20), nullable=True)
    customer_email = Column(String(200), nullable=True)
    rail_reference = Column(String(120), nullable=True, index=True)
    failure_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)

    merchant = relationship("Merchant", back_populates="transactions")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_txn_amount_positive"),
    )


class IdempotencyKey(db.Model):
    """Maps (merchant, key) -> response so retries return the same answer."""
    __tablename__ = "idempotency_keys"
    id = Column(Integer, primary_key=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False)
    key = Column(String(120), nullable=False)
    request_hash = Column(String(64), nullable=False)  # detects body mismatch
    response_status = Column(Integer, nullable=False)
    response_body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("merchant_id", "key", name="uq_idem"),
    )


class WebhookDelivery(db.Model):
    __tablename__ = "webhook_deliveries"
    id = Column(Integer, primary_key=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    url = Column(String(500), nullable=False)
    payload = Column(Text, nullable=False)
    signature = Column(String(128), nullable=False)
    status = Column(String(20), default="pending")  # pending|sent|failed
    attempts = Column(Integer, default=0, nullable=False)
    last_response_code = Column(Integer, nullable=True)
    last_response_body = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class RailEvent(db.Model):
    """Persists every event coming back from a mock rail — used by reconciliation."""
    __tablename__ = "rail_events"
    id = Column(Integer, primary_key=True)
    rail = Column(SAEnum(Channel), nullable=False)
    rail_reference = Column(String(120), nullable=False, index=True)
    event_type = Column(String(40), nullable=False)  # initiated|succeeded|failed
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False)
    raw_payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class Payout(db.Model):
    """A disbursement from the PSP to a merchant (or other recipient).

    In real terms: we're sending the merchant their settled funds via
    MTN Disbursement -> their MoMo wallet (or eventually their bank).
    """
    __tablename__ = "payouts"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="UGX")
    channel = Column(SAEnum(Channel), nullable=False, default=Channel.MTN_MOMO)
    status = Column(SAEnum(PayoutStatus), nullable=False, default=PayoutStatus.PENDING, index=True)
    recipient_phone = Column(String(20), nullable=False)
    recipient_name = Column(String(200), nullable=True)
    rail_reference = Column(String(120), nullable=True, index=True)
    failure_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payout_amount_positive"),
    )
