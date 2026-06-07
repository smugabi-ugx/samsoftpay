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
from flask_login import UserMixin
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
    VISA = "visa"
    CRYPTO = "crypto"


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

class Merchant(UserMixin, db.Model):
    __tablename__ = "merchants"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), nullable=False, unique=True)
    password_hash = Column(String(256), nullable=True)
    role = Column(String(20), default="merchant", nullable=False, index=True)  # merchant | admin
    email_verified = Column(Boolean, default=False, nullable=False)
    two_fa_enabled = Column(Boolean, default=True, nullable=False)
    otp_code = Column(String(6), nullable=True)
    otp_expires_at = Column(DateTime, nullable=True)
    otp_attempts = Column(Integer, default=0, nullable=False)   # wrong OTP counter
    login_attempts = Column(Integer, default=0, nullable=False) # wrong password counter
    locked_until = Column(DateTime, nullable=True)              # account lock expiry
    last_login_ip = Column(String(45), nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    public_key = Column(String(80), nullable=False, unique=True, index=True)
    secret_key = Column(String(80), nullable=False, unique=True, index=True)
    test_public_key = Column(String(80), nullable=True, unique=True, index=True)
    test_secret_key = Column(String(80), nullable=True, unique=True, index=True)
    handle = Column(String(40), nullable=True, unique=True, index=True)
    logo_filename = Column(String(255), nullable=True)   # uploaded business logo
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
    is_test = Column(Boolean, default=False, nullable=False)
    merchant_reference = Column(String(120), nullable=True)
    customer_phone = Column(String(20), nullable=True)
    customer_email = Column(String(200), nullable=True)
    rail_reference = Column(String(120), nullable=True, index=True)
    failure_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)
    refunded_at = Column(DateTime, nullable=True)
    refund_payout_id = Column(Integer, ForeignKey("payouts.id"), nullable=True)

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
    fee_amount = Column(BigInteger, nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="UGX")
    channel = Column(SAEnum(Channel), nullable=False, default=Channel.MTN_MOMO)
    status = Column(SAEnum(PayoutStatus), nullable=False, default=PayoutStatus.PENDING, index=True)
    is_test = Column(Boolean, default=False, nullable=False)
    recipient_phone = Column(String(20), nullable=False)
    recipient_name = Column(String(200), nullable=True)
    rail_reference = Column(String(120), nullable=True, index=True)
    failure_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    completed_at = Column(DateTime, nullable=True)
    batch_id = Column(Integer, ForeignKey("payout_batches.id"), nullable=True, index=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payout_amount_positive"),
    )


class PaymentLink(db.Model):
    """A shareable payment link.

    The merchant creates one with an amount + description. They get back a URL
    like /pay/lnk_xxx that they send to a customer (via WhatsApp, SMS, email).
    The customer opens it, picks a channel, enters their phone, pays. Behind
    the scenes the normal charge flow runs.

    A link is one-shot by default — once it's been paid successfully it can't be
    reused. allow_multiple_uses lets a merchant reuse the same link for a
    recurring product page (e.g. "donate here").

    success_url / cancel_url: after payment completes, the customer is offered
    a "Return to <merchant>" button that navigates them back to the merchant's
    site. This is how Stripe/Flutterwave/Samsoftpay close the loop.
    """
    __tablename__ = "payment_links"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="UGX")
    description = Column(String(255), nullable=True)
    reference = Column(String(120), nullable=True)
    success_url = Column(String(500), nullable=True)
    cancel_url = Column(String(500), nullable=True)
    allow_multiple_uses = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    # FK to a transaction once it's been paid — null until then.
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_link_amount_positive"),
    )


class AuditLog(db.Model):
    """Append-only record of every sensitive API action.

    Never update or delete rows — corrections are new entries.
    """
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=True)
    event = Column(String(80), nullable=False, index=True)  # e.g. charge.created, auth.failed
    actor_ip = Column(String(45), nullable=True)
    resource_id = Column(String(40), nullable=True)         # txn/payout public_id
    detail = Column(Text, nullable=True)                    # JSON extra context
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class PayoutBatch(db.Model):
    """A bulk payout job from a CSV upload.

    The merchant uploads a CSV with rows like (name, phone, amount). We
    create one Payout per row. The batch tracks overall progress and
    total amount.
    """
    __tablename__ = "payout_batches"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    currency = Column(String(3), nullable=False, default="UGX")
    total_amount = Column(BigInteger, nullable=False, default=0)
    total_count = Column(Integer, nullable=False, default=0)
    succeeded_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    status = Column(String(20), default="pending", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)


# ──────────────────────────────────────────────────────────────
# Settlement Accounts & Withdrawals
# ──────────────────────────────────────────────────────────────

class SettlementAccount(db.Model):
    """A verified account the merchant withdraws their balance to.

    Must be verified by admin before withdrawals are permitted.
    Mirrors what real PSPs require: a named MoMo or bank account
    that matches the KYC-verified business name.
    """
    __tablename__ = "settlement_accounts"
    id = Column(Integer, primary_key=True)
    public_id   = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    # account_type: momo_mtn | momo_airtel | bank | crypto
    account_type   = Column(String(20), nullable=False)
    account_number = Column(String(100), nullable=False)  # phone no or bank acct no
    account_name   = Column(String(200), nullable=False)
    bank_name      = Column(String(100), nullable=True)   # for bank accounts
    is_verified    = Column(Boolean, default=False, nullable=False)
    is_primary     = Column(Boolean, default=False, nullable=False)
    verified_at    = Column(DateTime, nullable=True)
    verified_by    = Column(Integer, ForeignKey("merchants.id"), nullable=True)
    created_at     = Column(DateTime, default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_settlement_merchant", "merchant_id"),
    )


class WithdrawalRequest(db.Model):
    """A merchant request to withdraw their available balance to a settlement account.

    Flow: merchant requests → admin approves → payout created via MoMo/bank rail.
    """
    __tablename__ = "withdrawal_requests"
    id = Column(Integer, primary_key=True)
    public_id             = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id           = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    settlement_account_id = Column(Integer, ForeignKey("settlement_accounts.id"), nullable=False)
    amount     = Column(BigInteger, nullable=False)
    fee_amount = Column(BigInteger, nullable=False, default=0)
    currency   = Column(String(3), nullable=False, default="UGX")
    status     = Column(String(20), default="pending", nullable=False, index=True)
    # pending | approved | processing | completed | rejected | cancelled
    payout_id  = Column(Integer, ForeignKey("payouts.id"), nullable=True)
    notes      = Column(Text, nullable=True)
    admin_notes= Column(Text, nullable=True)
    processed_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=utcnow, nullable=False, index=True)

    settlement_account = relationship("SettlementAccount")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_withdrawal_amount_positive"),
    )


# ──────────────────────────────────────────────────────────────
# Wallet Top-Up Requests
# ──────────────────────────────────────────────────────────────

class TopUpRequest(db.Model):
    """A merchant request to add funds to their available balance.

    MoMo:  creates a PaymentLink → merchant scans QR → on success, ledger credited.
    Bank:  merchant provides bank reference → admin verifies → ledger credited.
    """
    __tablename__ = "topup_requests"
    id = Column(Integer, primary_key=True)
    public_id   = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    method      = Column(String(20), nullable=False)  # momo | bank
    amount      = Column(BigInteger, nullable=False)
    currency    = Column(String(3), nullable=False, default="UGX")
    status      = Column(String(20), default="pending", nullable=False, index=True)
    # pending | completed | rejected | expired
    # MoMo-specific
    payment_link_id = Column(Integer, ForeignKey("payment_links.id"), nullable=True)
    transaction_id  = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    # Bank-specific
    bank_name   = Column(String(100), nullable=True)
    reference   = Column(String(100), nullable=True)
    # Review
    admin_notes = Column(Text, nullable=True)
    processed_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=utcnow, nullable=False, index=True)


# ──────────────────────────────────────────────────────────────
# Bills & Tax
# ──────────────────────────────────────────────────────────────

class BillCategory(str, Enum):
    SCHOOL_FEES   = "school_fees"
    UTILITY       = "utility"
    GOVERNMENT    = "government"
    HOSPITAL      = "hospital"
    MEMBERSHIP    = "membership"
    RENT          = "rent"
    OTHER         = "other"


class Bill(db.Model):
    """A payable bill issued by a merchant to a specific customer or open."""
    __tablename__ = "bills"
    id = Column(Integer, primary_key=True)
    public_id   = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)

    # Bill details
    category       = Column(SAEnum(BillCategory), nullable=False, default=BillCategory.OTHER)
    title          = Column(String(255), nullable=False)
    description    = Column(Text, nullable=True)
    account_ref    = Column(String(120), nullable=True, index=True)  # student ID, meter no, etc.
    customer_name  = Column(String(200), nullable=True)
    customer_phone = Column(String(30), nullable=True)

    # Amount & tax
    amount          = Column(BigInteger, nullable=False, default=0)  # 0 = customer enters amount
    is_variable     = Column(Boolean, default=False, nullable=False)
    currency        = Column(String(3), default="UGX", nullable=False)
    tax_rate_bps    = Column(Integer, default=0, nullable=False)  # basis points, e.g. 1800 = 18%
    tax_inclusive   = Column(Boolean, default=False, nullable=False)

    # Status
    status         = Column(String(20), default="active", nullable=False, index=True)
    # active | paid | overdue | cancelled
    due_date       = Column(DateTime, nullable=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_bill_amount_nonneg"),
    )


class TaxConfiguration(db.Model):
    """Per-merchant tax settings."""
    __tablename__ = "tax_configurations"
    id          = Column(Integer, primary_key=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, unique=True)
    vat_enabled     = Column(Boolean, default=False, nullable=False)
    vat_rate_bps    = Column(Integer, default=1800, nullable=False)  # 1800 = 18%
    vat_number      = Column(String(50), nullable=True)   # TIN / VAT reg number
    tax_inclusive   = Column(Boolean, default=False, nullable=False)
    # levy: Mobile Money Levy 0.5% shown separately on receipts
    show_levy       = Column(Boolean, default=True, nullable=False)
    levy_rate_bps   = Column(Integer, default=50, nullable=False)    # 50 = 0.5%
    business_name   = Column(String(200), nullable=True)
    business_address= Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=False)


# ──────────────────────────────────────────────────────────────
# KYC — Merchant Verification
# ──────────────────────────────────────────────────────────────

class KYCApplication(db.Model):
    """Merchant KYC/verification application. Mirrors MTN Uganda requirements."""
    __tablename__ = "kyc_applications"
    id = Column(Integer, primary_key=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, unique=True)
    status = Column(String(20), default="draft", nullable=False, index=True)
    # draft | submitted | under_review | approved | rejected

    # Step 1 — Business info
    company_name = Column(String(200), nullable=True)
    tin = Column(String(50), nullable=True)
    registration_number = Column(String(100), nullable=True)
    date_of_incorporation = Column(String(20), nullable=True)
    physical_address = Column(Text, nullable=True)
    contact_phone = Column(String(30), nullable=True)
    service_type = Column(String(50), nullable=True)  # collections|disbursements|both

    # Step 4 — Settlement / bank details
    bank_name = Column(String(100), nullable=True)
    bank_branch = Column(String(100), nullable=True)
    account_number = Column(String(50), nullable=True)
    account_name = Column(String(200), nullable=True)

    # Step 5 — AML/CFT
    ownership_structure = Column(String(20), nullable=True)   # private | public
    is_listed = Column(Boolean, default=False, nullable=False)
    fatf_country_exposure = Column(Boolean, default=False, nullable=False)
    prior_investigations = Column(Boolean, default=False, nullable=False)
    has_compliance_officer = Column(Boolean, default=False, nullable=False)
    aml_notes = Column(Text, nullable=True)

    # Review
    reviewer_notes = Column(Text, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)

    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=False)

    directors = relationship("KYCDirector", back_populates="application",
                             cascade="all, delete-orphan")
    documents = relationship("KYCDocument", back_populates="application",
                             cascade="all, delete-orphan")


class KYCDirector(db.Model):
    """Director / authorised signatory on a KYC application."""
    __tablename__ = "kyc_directors"
    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("kyc_applications.id"), nullable=False, index=True)
    full_name = Column(String(200), nullable=False)
    date_of_birth = Column(String(20), nullable=True)
    city_of_birth = Column(String(100), nullable=True)
    nationality = Column(String(100), nullable=True)
    id_type = Column(String(30), nullable=True)    # national_id | passport | refugee_id
    id_number = Column(String(100), nullable=True)
    contact_phone = Column(String(30), nullable=True)
    email = Column(String(200), nullable=True)
    is_primary = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    application = relationship("KYCApplication", back_populates="directors")


class KYCDocument(db.Model):
    """Uploaded supporting document for a KYC application."""
    __tablename__ = "kyc_documents"
    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("kyc_applications.id"), nullable=False, index=True)
    doc_type = Column(String(60), nullable=False)
    # certificate | form7_8 | tin | trade_licence | annual_returns
    # director_id | aml_questionnaire | financial_statements | other
    original_filename = Column(String(255), nullable=False)
    stored_filename = Column(String(255), nullable=False)   # UUID-based safe name
    uploaded_at = Column(DateTime, default=utcnow, nullable=False)
    application = relationship("KYCApplication", back_populates="documents")


# ──────────────────────────────────────────────────────────────
# Gift Cards / Vouchers
# ──────────────────────────────────────────────────────────────

class GiftCard(db.Model):
    """A redeemable gift card / voucher issued by a merchant."""
    __tablename__ = "gift_cards"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    code = Column(String(25), nullable=False, unique=True, index=True)  # e.g. SAMF-X4K2-9WQP
    face_value = Column(BigInteger, nullable=False)   # original value
    balance = Column(BigInteger, nullable=False)      # remaining balance (partial redemption)
    currency = Column(String(3), nullable=False, default="UGX")
    notes = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    redeemed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("face_value > 0", name="ck_giftcard_value_positive"),
    )


# ──────────────────────────────────────────────────────────────
# Subscriptions
# ──────────────────────────────────────────────────────────────

class SubscriptionPlan(db.Model):
    """A recurring billing plan defined by a merchant."""
    __tablename__ = "subscription_plans"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="UGX")
    interval = Column(String(20), nullable=False)   # weekly | monthly | yearly
    channel = Column(SAEnum(Channel), nullable=False, default=Channel.MTN_MOMO)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    subscriptions = relationship("Subscription", back_populates="plan")


class Subscription(db.Model):
    """An active (or cancelled) subscription of a customer to a plan."""
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"), nullable=False, index=True)
    customer_phone = Column(String(20), nullable=False)
    customer_email = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    # active | paused | cancelled | failed
    current_period_start = Column(DateTime, nullable=False)
    next_billing_at = Column(DateTime, nullable=False, index=True)
    cancelled_at = Column(DateTime, nullable=True)
    failure_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    plan = relationship("SubscriptionPlan", back_populates="subscriptions")
