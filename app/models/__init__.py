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
from sqlalchemy import event as _sa_event
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
    # SHA-256 of the secret keys, for lookup-by-hash so a DB leak doesn't expose
    # usable keys. Auto-populated from the plaintext keys by the event listener
    # below. Plaintext columns are retained for one-time display + transition.
    secret_key_hash = Column(String(64), nullable=True, unique=True, index=True)
    test_secret_key_hash = Column(String(64), nullable=True, unique=True, index=True)
    # COLLECTIONS-ONLY keys — for kiosks/devices. Can create charges, vending
    # orders and payment links, but NOT move money out (payouts/refunds are
    # 403'd). A key on a public machine that gets decompiled can then only take
    # money IN, never drain the merchant. Generated on demand; NULL = none yet.
    collections_key = Column(String(64), nullable=True)
    collections_key_hash = Column(String(64), nullable=True, unique=True, index=True)
    test_collections_key = Column(String(64), nullable=True)
    test_collections_key_hash = Column(String(64), nullable=True, unique=True, index=True)
    handle = Column(String(40), nullable=True, unique=True, index=True)
    logo_filename = Column(String(255), nullable=True)   # uploaded business logo
    webhook_url = Column(String(500), nullable=True)
    # Per-merchant outbound webhook signing secret (whsec_...). The global
    # WEBHOOK_SIGNING_SECRET is now INBOUND-ONLY (rail callbacks) — signing
    # merchant deliveries with it meant merchants could never verify (the
    # secret was never exposed) and exposing it would have let anyone forge
    # inbound "payment succeeded" callbacks. Key separation.
    webhook_secret = Column(String(80), nullable=True)
    # OPTIONAL separate SANDBOX webhook endpoint + secret. webhook_url/secret
    # above are the LIVE (and default) target; when webhook_url_test is set,
    # test-mode events deliver there instead, signed with webhook_secret_test —
    # so sandbox events never reach a merchant's production endpoint. When it's
    # unset, test events fall back to webhook_url (prior single-URL behaviour).
    webhook_url_test = Column(String(500), nullable=True)
    webhook_secret_test = Column(String(80), nullable=True)
    kyc_status = Column(String(20), default="pending")  # pending|verified|rejected
    is_active = Column(Boolean, default=True, nullable=False)
    # Admin suspend audit (is_active stays the source of truth; api._auth and
    # orchestrator already refuse is_active=False, so suspend halts everything).
    suspended_at = Column(DateTime, nullable=True)
    suspended_by = Column(String(120), nullable=True)
    suspend_reason = Column(String(500), nullable=True)
    # When True, succeeded charges skip the 24h settlement hold and land directly
    # in available balance (used for our own products, e.g. KarlPOS).
    instant_settlement = Column(Boolean, default=False, nullable=False)
    # When True, this merchant can use the vending (XY) dispense connector:
    # a succeeded charge for a vending order auto-triggers the machine to dispense.
    vending_enabled = Column(Boolean, default=False, nullable=False)
    # The merchant's OWN credentials with the machine supplier (XY Vending issues
    # a key/secret/merchant-number per operator). Held per merchant, not globally,
    # so any number of vending operators can run on the platform at once.
    # The secret is encrypted at rest (see services/secrets_box.py) because the
    # signing algorithm needs it back in plaintext — it cannot be hashed.
    xy_key = Column(String(120), nullable=True)
    xy_secret_encrypted = Column(Text, nullable=True)
    xy_merchant_no = Column(String(60), nullable=True)   # shbh
    xy_base_url = Column(String(200), nullable=True)
    # Which machine-vendor signing profile this merchant's callbacks/dispenses
    # use (Machine Integration Standard). Defaults to "xy"; a new vendor sets its
    # own. Resolved via services.signing.resolve_profile.
    signing_profile_vendor = Column(String(40), nullable=False,
                                    server_default="xy", default="xy")
    # ── Split payments / subaccounts ──
    # A subaccount is a MANAGED Merchant: parent_merchant_id points at the
    # platform that owns it, is_managed=True means it can never log in or call
    # the API (see api._auth). Because a subaccount IS a Merchant it already
    # owns MERCHANT_PENDING/AVAILABLE ledger accounts (keyed merchant+mode),
    # so settlement, payouts and balance reads work for it with no new plumbing.
    parent_merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=True, index=True)
    is_managed = Column(Boolean, default=False, nullable=False)
    # ── Per-merchant money limits + fee override (admin-set, optional) ──
    # NULL = no limit / standard fee. Enforced BEFORE any write in
    # orchestrator.create_charge (max_charge_amount) and payouts.create_payout
    # (max_payout_amount, before the earmark); fee_bps_override (basis points)
    # feeds fees.calculate_fee and preserves the standard UGX min/cap. Set from
    # the admin merchant console (/admin/merchants/<id>/limits).
    max_charge_amount = Column(BigInteger, nullable=True)
    max_payout_amount = Column(BigInteger, nullable=True)
    fee_bps_override = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    accounts = relationship("Account", back_populates="merchant")
    transactions = relationship("Transaction", back_populates="merchant")


def hash_api_key(raw_key: str | None) -> str | None:
    """SHA-256 of an API key for lookup-by-hash.

    API keys are high-entropy random tokens (token_urlsafe(28) ~= 168 bits), so a
    single SHA-256 is sufficient — there is nothing to brute-force. This is NOT for
    passwords (those use werkzeug's salted hash).
    """
    if not raw_key:
        return None
    import hashlib
    return hashlib.sha256(raw_key.encode()).hexdigest()


@_sa_event.listens_for(Merchant, "before_insert")
@_sa_event.listens_for(Merchant, "before_update")
def _sync_key_hashes(mapper, connection, target: "Merchant") -> None:
    """Keep the *_hash columns in lock-step with the plaintext keys, everywhere a
    Merchant is created or its keys change — no need to edit each creation site."""
    target.secret_key_hash = hash_api_key(target.secret_key)
    target.test_secret_key_hash = hash_api_key(target.test_secret_key)
    target.collections_key_hash = hash_api_key(target.collections_key)
    target.test_collections_key_hash = hash_api_key(target.test_collections_key)


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
    # Sandbox money is a SEPARATE ledger from real money. A charge or payout made
    # with an sk_test_ key posts only to is_test=True accounts, so no amount of
    # integration testing can move a balance a merchant could actually withdraw.
    # Every account is identified by (type, merchant, currency, MODE).
    is_test = Column(Boolean, default=False, nullable=False)
    # Cached for performance; the journal is source of truth.
    cached_balance = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    merchant = relationship("Merchant", back_populates="accounts")

    __table_args__ = (
        UniqueConstraint("type", "merchant_id", "currency", "is_test", name="uq_account"),
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
    # Set when this transaction's funds are swept from merchant_pending to
    # merchant_available. NULL = not yet settled. Lets the sweep settle each
    # transaction exactly once, only after its own hold period.
    settled_at = Column(DateTime, nullable=True, index=True)
    # Direct-dispense consumption claim (POST /v1/vending/dispense): NULL =
    # this charge has not yet paid for a dispense. Set atomically, once.
    vending_consumed_at = Column(DateTime, nullable=True)
    # Immutable split SPEC (JSON list of {subaccount, amount|bps}) captured at
    # create. NULL = ordinary single-merchant charge (unchanged path). Resolved
    # into SplitAllocation rows only when the charge actually SUCCEEDS.
    split_meta = Column(Text, nullable=True)

    merchant = relationship("Merchant", back_populates="transactions")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_txn_amount_positive"),
    )


class Subaccount(db.Model):
    """A platform's sub-merchant (split-payments party).

    Thin metadata over a MANAGED Merchant row: the Merchant carries the ledger
    identity (accounts, settlement, payouts); this row carries the platform
    relationship, the API-facing sub_... id, and the payout destination.
    """
    __tablename__ = "subaccounts"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)   # sub_...
    platform_merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    external_ref = Column(String(120), nullable=True)    # the platform's own id for this party
    payout_phone = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class SplitAllocation(db.Model):
    """One party's share of a SUCCEEDED split charge.

    The settlement unit for splits: each share settles pending->available after
    the charge's own hold (its own settled_at — guardrail 4 per share). The
    platform's rounding-residual is stored as one of these rows too, so it
    settles through the same path.
    """
    __tablename__ = "split_allocations"
    id = Column(Integer, primary_key=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    amount = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, default="UGX")
    is_test = Column(Boolean, default=False, nullable=False)
    settled_at = Column(DateTime, nullable=True, index=True)
    reversed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("transaction_id", "merchant_id", name="uq_split_alloc"),
        CheckConstraint("amount > 0", name="ck_split_alloc_amount_positive"),
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


class Dispute(db.Model):
    """A customer-reported problem with one payment — the public recourse door.

    M-Pesa lesson: a published, time-boxed dispute path is why people trust
    holding money in the system. The dispute is the FRONT DOOR only: it never
    moves money — the merchant's existing refund tooling is the action.
    """
    __tablename__ = "disputes"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), unique=True, nullable=False, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    reason = Column(String(40), nullable=False)      # not_delivered | wrong_amount | double_charge | other
    details = Column(Text, nullable=True)
    contact = Column(String(160), nullable=True)     # customer phone/email for follow-up
    status = Column(String(20), nullable=False, default="open", index=True)  # open | resolved | dismissed
    resolution_note = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)


class Settlement(db.Model):
    """One settlement batch: a sweep releasing pending -> available.

    Paystack lesson: a settlement schedule merchants can set a watch by.
    Making each release a first-class row gives the dashboard a Settlements
    page and the API GET /v1/settlements — predictability, not speed, is
    what makes a merchant leave money in the balance.
    """
    __tablename__ = "settlements"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), unique=True, nullable=False, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    currency = Column(String(3), nullable=False)
    is_test = Column(Boolean, default=False, nullable=False)
    amount = Column(BigInteger, nullable=False)          # released to available
    txn_count = Column(Integer, nullable=False)          # charges (or shares) in the batch
    kind = Column(String(20), nullable=False, default="sweep")  # sweep | split_sweep
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class PlatformFlag(db.Model):
    """Tiny operational key/value switchboard (e.g. the payout kill switch).

    Flags live in the DATABASE so flipping one is a CLI command, not a deploy —
    `flask freeze-payouts on` must work from a phone during an incident.
    """
    __tablename__ = "platform_flags"
    key = Column(String(60), primary_key=True)
    value = Column(String(200), nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by = Column(String(120), nullable=True)


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


class ReconException(db.Model):
    """A mismatch between OUR record and MTN's own answer for one reference.

    This is the third leg of reconciliation the internal check can't provide:
    it proves (or disproves) that our ledger agrees with MTN. One open row per
    rail_reference; a later run that finds them agreeing auto-resolves it
    (backdated reconciliation). The `kind` drives severity.
    """
    __tablename__ = "recon_exceptions"
    id = Column(Integer, primary_key=True)
    rail_reference = Column(String(120), nullable=False, index=True)
    txn_public_id = Column(String(40), nullable=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=True, index=True)
    kind = Column(String(60), nullable=False)   # mtn_succeeded_local_not | local_succeeded_mtn_failed | amount_mismatch | succeeded_no_reference
    severity = Column(String(20), nullable=False, default="critical")  # critical | warning
    our_status = Column(String(30), nullable=True)
    mtn_status = Column(String(30), nullable=True)
    our_amount = Column(BigInteger, nullable=True)
    mtn_amount = Column(BigInteger, nullable=True)
    detail = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="open", index=True)  # open | resolved
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String(120), nullable=True)   # "auto" or an admin email
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)
    last_seen_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("rail_reference", name="uq_recon_exception_ref"),
    )


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
    reference = Column(String(120), nullable=True, index=True)   # merchant's business reference (echoed back + in webhooks)
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
    # Which key created this link. Read by the public checkout page/submit
    # handler (no auth there, so this is the only record of the mode) to scope
    # the resulting charge to the right ledger and exempt it from the
    # simulated-rail guard the way a real sk_test_ charge already is.
    is_test = Column(Boolean, default=False, nullable=False)
    # FK to a transaction once it's been paid — null until then.
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    # For vending orders: JSON {machine, goods:[{spbh,spmc,spdj}], pay_account}.
    # Set when the order is created; read on payment success to auto-dispense.
    vending_meta = Column(Text, nullable=True)
    # Dispense lifecycle for a vending order, kept separate from payment status:
    # None (not a vending order) | pending | dispensing | dispensed | failed.
    # "dispensing" is claimed by an atomic compare-and-set so two concurrent rail
    # callbacks can never dispense the same order twice.
    vending_status = Column(String(20), nullable=True)
    vending_error = Column(String(255), nullable=True)
    vending_dispensed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_link_amount_positive"),
    )


class VendingMachine(db.Model):
    """One physical vending machine belonging to a merchant.

    An XY operator account (shbh) owns many machines, each identified by a
    machine number (jqbh). We mirror them here — synced from the supplier's
    queryMachine — so the operator picks "Kampala Road lobby" instead of
    memorising 1707600112, and so orders can be validated against machines the
    merchant actually owns before we ever call the supplier.
    """
    __tablename__ = "vending_machines"
    id = Column(Integer, primary_key=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    jqbh = Column(String(60), nullable=False)             # machine number
    name = Column(String(160), nullable=True)             # jqmc
    category = Column(String(40), nullable=True)          # jqlb
    machine_type = Column(String(40), nullable=True)      # jqlx
    address = Column(String(255), nullable=True)          # dwmc
    latitude = Column(String(40), nullable=True)          # dwwd
    longitude = Column(String(40), nullable=True)         # dwjd
    image_url = Column(String(500), nullable=True)        # jqtp
    is_active = Column(Boolean, default=True, nullable=False)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        # A machine number is unique per merchant, not globally: two operators
        # could in principle be given overlapping numbering by the supplier.
        UniqueConstraint("merchant_id", "jqbh", name="uq_machine_per_merchant"),
    )


class SigningProfile(db.Model):
    """How ONE machine vendor signs its callbacks and receives dispense commands.

    Machine Integration Standard v1. Replaces the hardcoded XY constants that
    used to live in webhooks_xy.py / xy_vending.py. A new vendor is a row here
    (+ a passed conformance sample), not a code change. XY works from a built-in
    default with NO row needed; a row overrides the built-in when present and is
    how a vendor is edited (`flask signing-profile`) or a new one added.

    Signing is always MD5(secret + timestamp + reqData); a profile only varies
    the knobs a vendor's firmware differs on. It can NEVER disable verification —
    there is no such column (guardrail 9). See app/services/signing.py.
    """
    __tablename__ = "signing_profiles"
    id = Column(Integer, primary_key=True)
    vendor = Column(String(40), nullable=False, unique=True, index=True)
    display_name = Column(String(120), nullable=False)

    # Inbound callback-verification knobs (JSON-encoded where noted).
    non_signed_fields = Column(Text, nullable=False,
                               default='["sign", "key", "timestamp"]')
    field_aliases = Column(Text, nullable=False, default="{}")
    sign_order = Column(String(20), nullable=False, default="alpha")   # alpha | alpha_swap
    sign_order_swaps = Column(Text, nullable=False, default="[]")      # [[a,b],...]
    replay_window_seconds = Column(Integer, nullable=False, default=0)

    # Outbound dispense-command knobs.
    dispense_path = Column(String(255), nullable=False,
                           default="/service-pay-third/third/pay/api/ApplyExportGoods")
    dispense_body_style = Column(String(40), nullable=False, default="xy_orderdto")
    dispense_extra = Column(Text, nullable=False, default="{}")

    is_legacy_shim = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


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
    # For BANK settlements (no automated rail): the operator's bank-transfer
    # reference, recorded when they confirm the money left our bank.
    bank_reference = Column(String(120), nullable=True)
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
    # active | past_due | paused | cancelled | failed
    current_period_start = Column(DateTime, nullable=False)
    next_billing_at = Column(DateTime, nullable=False, index=True)
    cancelled_at = Column(DateTime, nullable=True)
    failure_reason = Column(String(255), nullable=True)
    # Dunning: a failed charge no longer kills the plan on the first miss. It
    # goes 'past_due' and is retried on a backoff up to _MAX_DUNNING_RETRIES
    # before finally churning to 'failed'.
    retry_count = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    plan = relationship("SubscriptionPlan", back_populates="subscriptions")


# ---------- Scheduled payouts / payroll (money-OUT autopilot) ----------

class ScheduledPayout(db.Model):
    """A recurring mobile-money disbursement a merchant sets and forgets.

    The money-OUT mirror of SubscriptionPlan+Subscription (money IN) pointed at
    the existing payout rail. run_due() fires it on an interval and drives
    payouts.create_payout for each recipient. A uniform `amount` is paid to
    every recipient in `recipients`. is_test is fixed ONCE at creation from the
    key's mode (guardrail 12/15). Exactly-once is the atomic claim in run_due():
    next_run_at is advanced BEFORE any payout is created.
    """
    __tablename__ = "scheduled_payouts"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(40), nullable=False, unique=True, index=True)
    merchant_id = Column(Integer, ForeignKey("merchants.id"), nullable=False, index=True)
    name = Column(String(200), nullable=True)
    amount = Column(BigInteger, nullable=False)      # paid to EACH recipient, per cycle
    currency = Column(String(3), nullable=False, default="UGX")
    channel = Column(SAEnum(Channel), nullable=False, default=Channel.MTN_MOMO)
    interval = Column(String(20), nullable=False)    # daily | weekly | monthly
    recipients = Column(Text, nullable=False)        # JSON [{"phone","name"}]
    max_per_recipient = Column(BigInteger, nullable=True)   # NULL = no cap
    status = Column(String(20), nullable=False, default="active", index=True)
    is_test = Column(Boolean, default=False, nullable=False)
    next_run_at = Column(DateTime, nullable=False, index=True)
    last_run_at = Column(DateTime, nullable=True)
    failure_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_scheduled_payout_amount_positive"),
        Index("ix_scheduled_payouts_status_next_run", "status", "next_run_at"),
    )
