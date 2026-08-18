"""Stale-transaction sweeper.

When the Flask process restarts, all in-flight background threads (mock timers
and real MTN polling threads) are killed. Any PENDING/AUTHORIZED transaction
they were tracking stays stuck forever.

This module finds those transactions and resolves them:
- With MOMO_USE_REAL=1: makes one synchronous status call to the MTN API.
  If MTN says SUCCESSFUL/FAILED we settle accordingly. If MTN still shows
  PENDING or the call fails, we expire the transaction as failed.
- Without real rail: marks them failed immediately (they're mock orphans).

Call sweep_stale_transactions() from the /admin/sweep-pending route or
the `flask sweep-pending` CLI command.
"""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import requests
from flask import current_app

from ..extensions import db
from ..models import Channel, Transaction, TxnStatus, utcnow
from .orchestrator import complete_transaction


def sweep_stale_transactions(*, stale_minutes: int = 10) -> dict:
    """Resolve PENDING/AUTHORIZED transactions older than *stale_minutes*.

    Returns a summary dict:
        {
            "swept": <int>,          # number of transactions processed
            "succeeded": <int>,
            "failed": <int>,
            "items": [               # one entry per transaction
                {"id": "txn_...", "result": "succeeded"|"failed"|"expired"},
                ...
            ]
        }
    """
    cutoff = utcnow() - timedelta(minutes=stale_minutes)

    stale = (
        Transaction.query.filter(
            Transaction.status.in_([TxnStatus.PENDING, TxnStatus.AUTHORIZED]),
            Transaction.created_at <= cutoff,
        )
        .all()
    )

    results = []
    use_real = current_app.config.get("MOMO_USE_REAL", False)

    for txn in stale:
        # Passthrough channels (Visa via Flutterwave, crypto via ChangeNow)
        # are settled EXTERNALLY before create_charge is ever called — a
        # stuck PENDING one means the in-process completion timer was lost,
        # not that the payment failed. Expiring it marked already-received
        # money as failed; and the MTN branch below would have queried MTN's
        # status API with a ChangeNow reference forever.
        if txn.channel in (Channel.VISA, Channel.CRYPTO):
            complete_transaction(
                txn.id,
                success=True,
                rail_reference=txn.rail_reference or f"sweep_{txn.public_id}",
            )
            results.append({"id": txn.public_id, "result": "succeeded"})
            continue

        if use_real and txn.rail_reference and txn.channel == Channel.MTN_MOMO:
            mtn_status = _query_mtn_status(txn.rail_reference)
            if mtn_status == "SUCCESSFUL":
                complete_transaction(
                    txn.id,
                    success=True,
                    rail_reference=txn.rail_reference,
                )
                results.append({"id": txn.public_id, "result": "succeeded"})
                continue
            elif mtn_status == "FAILED":
                complete_transaction(
                    txn.id,
                    success=False,
                    rail_reference=txn.rail_reference,
                    reason="momo_failed",
                )
                results.append({"id": txn.public_id, "result": "failed"})
                continue
            elif mtn_status is None:
                # Network error — we DON'T KNOW the outcome. Expiring here
                # marked charges failed that MTN had actually completed
                # ("money taken, no credit"). Skip; the next sweep retries.
                results.append({"id": txn.public_id, "result": "skipped_network_error"})
                continue
            # else: MTN definitively says PENDING and the txn is past the
            # cutoff — the customer never approved; safe to expire.

        # Mock rail orphan or MTN still pending — expire as failed
        ref = txn.rail_reference or f"sweep_{txn.public_id}"
        complete_transaction(
            txn.id,
            success=False,
            rail_reference=ref,
            reason="expired_by_sweep",
        )
        results.append({"id": txn.public_id, "result": "expired"})

    succeeded = sum(1 for r in results if r["result"] == "succeeded")
    skipped = sum(1 for r in results if r["result"] == "skipped_network_error")
    failed_count = len(results) - succeeded - skipped

    return {
        "swept": len(results),
        "succeeded": succeeded,
        "failed": failed_count,
        "items": results,
    }


# ---------- MTN API helpers (synchronous, one-shot) ----------

def _get_mtn_token() -> str | None:
    """Get a cached OAuth token for MTN Collections. Returns None on error."""
    cfg = current_app.config
    try:
        from .rails_mtn_real import _get_token
        return _get_token(
            subscription_key=cfg["MOMO_SUBSCRIPTION_KEY"],
            api_user=cfg["MOMO_API_USER"],
            api_key=cfg["MOMO_API_KEY"],
            base_url=cfg["MOMO_BASE_URL"],
        )
    except Exception:
        return None


def _query_mtn_status(rail_reference: str) -> str | None:
    """Query the MTN Collections status endpoint for a single reference.

    Returns "SUCCESSFUL", "FAILED", "PENDING", or None (on error).
    """
    token = _get_mtn_token()
    if not token:
        return None
    cfg = current_app.config
    try:
        r = requests.get(
            f"{cfg['MOMO_BASE_URL']}/collection/v1_0/requesttopay/{rail_reference}",
            headers={
                "Authorization": f"Bearer {token}",
                "Ocp-Apim-Subscription-Key": cfg["MOMO_SUBSCRIPTION_KEY"],
                "X-Target-Environment": cfg["MOMO_TARGET_ENV"],
            },
            timeout=10,
        )
        if r.status_code == 200:
            return (r.json().get("status") or "").upper()
    except requests.RequestException:
        pass
    return None
