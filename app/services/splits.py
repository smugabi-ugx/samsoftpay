"""Split payments — one charge fans out to multiple subaccounts, exactly.

Adversarially money-audited design (see memory: split-payments-design). The
rules that make it safe:

- Shares are ALWAYS validated and resolved against N = amount − fee (net).
  There is no "gross" base: an over-N split is rejected at create with ZERO
  database writes (audit critical #1).
- The platform's residual r = N − Σshares is the ROUNDING SINK: every leftover
  shilling from bps floors lands in the platform's own pending balance, so
  Σshares + r == N holds by construction — no fractional loss, no minting.
- Posting happens inside complete_transaction's row-locked terminal block and
  shares the caller's SINGLE commit with txn.status/settled_at (audit high #4).
- If resolution at success time fails for ANY reason (corrupt spec, deactivated
  platform math error), we fall back to crediting the platform in full — money
  is never lost and never minted, and the failure is logged loudly.
- A subaccount that was deactivated between create and success does NOT get
  credited: its share folds into the platform residual (audit medium #7).

Split REFUNDS are deliberately NOT implemented in v1 — the audit found three
critical hazards in refunding a split (clawback stranding, cross-mode leak,
in-hold funding). refunds.refund_charge rejects a split charge with zero writes
until the v2 hardening lands.
"""
from __future__ import annotations

import json
import uuid

from flask import current_app

from ..extensions import db
from ..models import AccountType, Merchant, SplitAllocation, Subaccount, utcnow
from . import ledger

MAX_SPLIT_PARTIES = 20


class SplitError(Exception):
    pass


# ── subaccount management ────────────────────────────────────────────────────

def create_subaccount(*, platform: Merchant, name: str,
                      payout_phone: str | None = None,
                      external_ref: str | None = None) -> Subaccount:
    """Create a managed Merchant + its Subaccount row. Commits."""
    import secrets as _secrets
    from werkzeug.security import generate_password_hash

    name = (name or "").strip()
    if not name:
        raise SplitError("subaccount name is required")
    if platform.is_managed:
        raise SplitError("a subaccount cannot own subaccounts")

    token = _secrets.token_urlsafe(16)
    managed = Merchant(
        name=name,
        # Synthetic, unique, never-displayed identity. The random password hash
        # and unguessable keys are belt-and-braces: api._auth() additionally
        # refuses is_managed rows outright.
        email=f"sub+{token}@managed.samsoftpay.local",
        password_hash=generate_password_hash(_secrets.token_urlsafe(32)),
        public_key="pk_managed_" + _secrets.token_urlsafe(20),
        secret_key="sk_managed_" + _secrets.token_urlsafe(28),
        kyc_status="managed",       # not self-serve KYC; platform is merchant of record
        is_managed=True,
        parent_merchant_id=platform.id,
        email_verified=True,
        two_fa_enabled=False,
    )
    db.session.add(managed)
    db.session.flush()   # need managed.id for the subaccount row

    sub = Subaccount(
        public_id=f"sub_{uuid.uuid4().hex[:16]}",
        platform_merchant_id=platform.id,
        merchant_id=managed.id,
        name=name,
        external_ref=(external_ref or None),
        payout_phone=(payout_phone or None),
    )
    db.session.add(sub)
    db.session.commit()
    return sub


# ── validation + resolution ──────────────────────────────────────────────────

def resolve_split(*, platform_id: int, split: list, net: int,
                  require_active: bool = True) -> tuple[list[dict], int]:
    """Resolve a split spec into exact integer shares against net N.

    Returns ([{subaccount_public_id, merchant_id, amount}], residual) with the
    invariant Σamounts + residual == net, residual >= 0. Raises SplitError on
    any invalid input. Deterministic — safe to re-run at success time.

    require_active=False keeps a deactivated subaccount's entry (used only for
    inspection); the posting path uses require_active=True so a killed sub's
    share folds into the platform residual instead of being credited.
    """
    if not isinstance(split, list) or not split:
        raise SplitError("split must be a non-empty list")
    if len(split) > MAX_SPLIT_PARTIES:
        raise SplitError(f"split supports at most {MAX_SPLIT_PARTIES} parties")
    if net <= 0:
        raise SplitError("nothing to split: net amount is zero")

    seen: set[str] = set()
    resolved: list[dict] = []
    total = 0
    for i, entry in enumerate(split):
        if not isinstance(entry, dict):
            raise SplitError(f"split[{i}] must be an object")
        sub_id = str(entry.get("subaccount") or "")
        if not sub_id:
            raise SplitError(f"split[{i}].subaccount is required")
        if sub_id in seen:
            raise SplitError(f"duplicate subaccount {sub_id} in split")
        seen.add(sub_id)

        sub = Subaccount.query.filter_by(
            public_id=sub_id, platform_merchant_id=platform_id).one_or_none()
        if sub is None:
            raise SplitError(f"unknown subaccount {sub_id}")
        managed = db.session.get(Merchant, sub.merchant_id)
        active = bool(managed and managed.is_active)
        if require_active and not active:
            # Create-time gate: reject loudly with zero writes. The success-time
            # posting path uses require_active=False and folds an inactive
            # sub's share into the platform residual instead (see
            # post_split_success) — a deactivation mid-flight must not fail
            # money that was already collected.
            raise SplitError(f"subaccount {sub_id} is not active")

        has_amount = "amount" in entry
        has_bps = "bps" in entry
        if has_amount == has_bps:
            raise SplitError(f"split[{i}] must have exactly one of amount|bps")
        if has_amount:
            try:
                share = int(entry["amount"])
            except (TypeError, ValueError):
                raise SplitError(f"split[{i}].amount must be an integer")
            if share <= 0:
                raise SplitError(f"split[{i}].amount must be positive")
        else:
            try:
                bps = int(entry["bps"])
            except (TypeError, ValueError):
                raise SplitError(f"split[{i}].bps must be an integer")
            if not (0 < bps <= 10000):
                raise SplitError(f"split[{i}].bps must be in 1..10000")
            # Floor division: the shortfall vs the exact fraction is exactly
            # what accumulates into the platform residual. Never rounds up, so
            # Σshares can never exceed what the bps describe.
            share = (net * bps) // 10000
            if share <= 0:
                raise SplitError(f"split[{i}].bps of {bps} resolves to zero on net {net}")

        total += share
        resolved.append({"subaccount_public_id": sub_id,
                         "merchant_id": sub.merchant_id,
                         "amount": share,
                         "active": active})

    if total > net:
        raise SplitError(
            f"split total {total} exceeds the net amount {net} (amount minus fee)")
    residual = net - total
    assert residual >= 0 and total + residual == net   # exact by construction
    return resolved, residual


def validate_split(*, merchant: Merchant, split: list, amount: int, fee: int,
                   currency: str) -> None:
    """Create-time gate: raises SplitError with ZERO writes on any problem."""
    if currency != "UGX":
        raise SplitError("splits support UGX only")
    resolve_split(platform_id=merchant.id, split=split, net=amount - fee,
                  require_active=True)


# ── posting at charge success ────────────────────────────────────────────────

def post_split_success(txn, *, rail_acct, revenue, mode: bool) -> bool:
    """Post the split legs + allocation rows for a SUCCEEDED split charge.

    Called from complete_transaction INSIDE its row-locked terminal block, and
    adds to the session WITHOUT committing — the caller's single commit makes
    status + settled_at + legs + allocations atomic (audit high #4).

    Returns True if the split was posted; False if RESOLUTION failed, in which
    case the caller takes the ordinary single-merchant path (money never lost,
    never minted — the platform just gets all of N).

    Structure matters: phase 1 (resolution) is read-only and is the only
    fallible-by-design part — a failure there returns False having staged
    NOTHING, so the caller's locked session is untouched (no rollback that
    would discard the RailEvent or release the txn row lock). Phase 2 (staging)
    has the same failure semantics as the ordinary posting path: an unexpected
    exception propagates and the whole completion attempt rolls back and is
    retried by the poller/webhook/sweep.
    """
    # ── phase 1: fallible, read-only resolution ──
    try:
        spec = json.loads(txn.split_meta)
        net = txn.amount - (txn.fee_amount or 0)
        resolved, residual = resolve_split(
            platform_id=txn.merchant_id, split=spec, net=net,
            require_active=False)
        # Fold any share whose subaccount was DEACTIVATED since create into the
        # platform residual — never credit a killed sub (audit medium #7).
        shares = [r for r in resolved if r["active"]]
        residual += sum(r["amount"] for r in resolved if not r["active"])
        assert residual >= 0
        assert sum(r["amount"] for r in shares) + residual == net
    except Exception:
        try:
            current_app.logger.exception(
                "split resolution failed for %s — falling back to single-merchant credit",
                txn.public_id)
        except Exception:
            pass
        return False

    # ── phase 2: staging (no catch — same semantics as the ordinary path) ──
    legs = [(rail_acct, +txn.amount), (revenue, -(txn.fee_amount or 0))]
    now = utcnow()
    for r in shares:
        pending = ledger.get_or_create_account(
            type=AccountType.MERCHANT_PENDING, merchant_id=r["merchant_id"],
            currency=txn.currency, is_test=mode)
        legs.append((pending, -r["amount"]))
        db.session.add(SplitAllocation(
            transaction_id=txn.id, merchant_id=r["merchant_id"],
            amount=r["amount"], currency=txn.currency, is_test=mode))
    if residual > 0:
        platform_pending = ledger.get_or_create_account(
            type=AccountType.MERCHANT_PENDING, merchant_id=txn.merchant_id,
            currency=txn.currency, is_test=mode)
        legs.append((platform_pending, -residual))
        db.session.add(SplitAllocation(
            transaction_id=txn.id, merchant_id=txn.merchant_id,
            amount=residual, currency=txn.currency, is_test=mode))

    ledger.post(legs, currency=txn.currency, transaction_id=txn.id,
                memo=f"charge {txn.public_id} succeeded (split, "
                     f"{len(shares)} shares + residual {residual})")
    # The ORIGINAL Transaction-based sweep must skip this charge — its money
    # lives in the allocations, which settle per-share after the hold.
    txn.settled_at = now
    return True


# ── settlement (second pass) ─────────────────────────────────────────────────

def sweep_split_allocations(*, cutoff, batch_size: int = 500) -> dict:
    """Settle due split allocations pending->available, grouped per party.

    Mirrors settlement._settle_one_merchant: FOR UPDATE SKIP LOCKED per group,
    committed per group, each share settled exactly once via its own settled_at
    (guardrail 4 per share). Returns {merchant_id: amount_moved}.
    """
    from ..models import Transaction, TxnStatus

    groups = (
        db.session.query(SplitAllocation.merchant_id, SplitAllocation.currency,
                         SplitAllocation.is_test)
        .join(Transaction, Transaction.id == SplitAllocation.transaction_id)
        .filter(
            SplitAllocation.settled_at.is_(None),
            SplitAllocation.reversed_at.is_(None),
            Transaction.status == TxnStatus.SUCCEEDED,
            Transaction.completed_at <= cutoff,
        )
        .distinct()
        .all()
    )

    moved: dict[int, int] = {}
    for merchant_id, currency, is_test in groups:
        try:
            due = (
                SplitAllocation.query
                .join(Transaction, Transaction.id == SplitAllocation.transaction_id)
                .filter(
                    SplitAllocation.merchant_id == merchant_id,
                    SplitAllocation.currency == currency,
                    SplitAllocation.is_test.is_(True) if is_test
                    else SplitAllocation.is_test.is_(False),
                    SplitAllocation.settled_at.is_(None),
                    SplitAllocation.reversed_at.is_(None),
                    Transaction.status == TxnStatus.SUCCEEDED,
                    Transaction.completed_at <= cutoff,
                )
                .order_by(SplitAllocation.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True, of=SplitAllocation)
                .all()
            )
            if not due:
                db.session.commit()
                continue
            pending = ledger.get_or_create_account(
                type=AccountType.MERCHANT_PENDING, merchant_id=merchant_id,
                currency=currency, is_test=bool(is_test))
            available = ledger.get_or_create_account(
                type=AccountType.MERCHANT_AVAILABLE, merchant_id=merchant_id,
                currency=currency, is_test=bool(is_test))
            total = sum(a.amount for a in due)
            ledger.post(
                [(pending, +total), (available, -total)],
                currency=currency,
                memo=f"split settlement merchant={merchant_id} ({len(due)} shares)")
            now = utcnow()
            for a in due:
                a.settled_at = now
            db.session.commit()
            moved[merchant_id] = moved.get(merchant_id, 0) + total
        except Exception:
            db.session.rollback()
            try:
                current_app.logger.exception(
                    "split settlement failed for merchant %s", merchant_id)
            except Exception:
                pass
    return moved
