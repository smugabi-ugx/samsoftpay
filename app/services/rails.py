"""Mock rail adapters.

The interface every rail must implement:

    initiate(transaction) -> InitiateResult(rail_reference, status)
    handle_callback(payload) -> CallbackResult(rail_reference, success, reason)

In real life:
- MTN MoMo Collections: OAuth2 token, POST to /v1_0/requesttopay, callback comes
  back as a POST to your collection_url with an X-Reference-Id header.
- Airtel Money: similar shape, different endpoints, different signing.
- Card: 3D Secure flow via acquiring bank's gateway.

Here we just persist a RailEvent, schedule an in-process timer that flips the
transaction to succeeded/failed after RAIL_CALLBACK_DELAY_SECONDS.

OUTCOME IS DETERMINISTIC BY DEFAULT — every ordinary phone number succeeds.
A real sandbox (Stripe, Pesapal, Flutterwave) never makes an integrator's
ordinary test transaction randomly fail; failure is something you ask for on
purpose, with a specific documented test value, so error-handling can be
tested reliably instead of by luck. TEST_PHONE_OUTCOMES below is that: a
customer phone matching one of these numbers gets that exact outcome, every
time. Anything else succeeds. RAIL_SUCCESS_PROBABILITY still exists as a
lower-precedence knob (defaults to 1.0) for anyone who deliberately wants
old-style randomness — it no longer governs ordinary testing by default.
"""
from __future__ import annotations

import json
import random
import re
import threading
import uuid
from dataclasses import dataclass

from flask import current_app

from ..extensions import db
from ..models import Channel, RailEvent, Transaction, TxnStatus

# Magic test numbers — documented on the docs page, same spirit as Stripe's
# 4242 4242 4242 4242 / 4000 0000 0000 0002. Matched on the last 9 digits so
# both 07... and 256... (with or without spaces/dashes) hit the same entry.
TEST_PHONE_OUTCOMES = {
    "700000000": None,                 # documented "always succeeds" number
    "700000001": "insufficient_funds",
    "700000002": "user_cancelled",
    "700000003": "timeout",
}


def _phone_key(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-9:] if len(digits) >= 9 else digits


@dataclass
class InitiateResult:
    rail_reference: str
    accepted: bool
    reason: str | None = None


class RailAdapter:
    channel: Channel

    def initiate(self, txn: Transaction) -> InitiateResult:  # pragma: no cover - abstract
        raise NotImplementedError


class _MockRail(RailAdapter):
    def __init__(self, channel: Channel):
        self.channel = channel

    def initiate(self, txn: Transaction) -> InitiateResult:
        rail_ref = f"{self.channel.value}_{uuid.uuid4().hex[:12]}"

        db.session.add(
            RailEvent(
                rail=self.channel,
                rail_reference=rail_ref,
                event_type="initiated",
                amount=txn.amount,
                currency=txn.currency,
                raw_payload=json.dumps(
                    {"txn_public_id": txn.public_id, "phone": txn.customer_phone}
                ),
            )
        )

        # Schedule the async "callback". We capture the *application* and txn id —
        # NOT the txn object — because the timer fires on a different thread and
        # would otherwise have no app context and a detached SQLAlchemy object.
        app = current_app._get_current_object()
        delay = app.config["RAIL_CALLBACK_DELAY_SECONDS"]
        prob = app.config["RAIL_SUCCESS_PROBABILITY"]
        txn_id = txn.id
        phone_key = _phone_key(txn.customer_phone)

        def _fire():
            # Late import to dodge circular deps.
            from .orchestrator import complete_transaction

            with app.app_context():
                if phone_key in TEST_PHONE_OUTCOMES:
                    # A magic test number was used — the outcome is exactly
                    # what it says, every time. Never rolls dice.
                    reason = TEST_PHONE_OUTCOMES[phone_key]
                    success = reason is None
                else:
                    success = random.random() < prob
                    reason = None if success else random.choice(
                        ["insufficient_funds", "user_cancelled", "timeout"]
                    )
                complete_transaction(txn_id, success=success, rail_reference=rail_ref, reason=reason)

        threading.Timer(delay, _fire).start()

        return InitiateResult(rail_reference=rail_ref, accepted=True)


class MTNMoMoAdapter(_MockRail):
    def __init__(self):
        super().__init__(Channel.MTN_MOMO)


class AirtelMoneyAdapter(_MockRail):
    def __init__(self):
        super().__init__(Channel.AIRTEL_MONEY)


class CardAdapter(_MockRail):
    def __init__(self):
        super().__init__(Channel.CARD)


def is_simulated(channel: Channel) -> bool:
    """True if this channel would be settled by a mock, not a real rail.

    A mock rail flips a transaction to SUCCEEDED on a timer without any money
    having moved. In live mode that is not a harmless stub — it credits the
    merchant's ledger with funds nobody paid, and for a vending order it makes
    a real machine dispense a real product for free.

    Airtel Money and Card have no real adapter yet, so they are always
    simulated. MTN is simulated only when MOMO_USE_REAL is off. Visa/Crypto are
    passthrough rather than mock: those are already settled by Flutterwave or
    ChangeNow before create_charge is ever called, so they are NOT simulated.
    """
    return isinstance(get_adapter(channel), _MockRail)


def get_adapter(channel: Channel) -> RailAdapter:
    from flask import g
    sandbox = g.get("api_mode") == "test"  # sandbox key → always mock

    if channel == Channel.MTN_MOMO:
        if not sandbox and current_app.config.get("MOMO_USE_REAL"):
            from .rails_mtn_real import RealMTNMoMoAdapter
            return RealMTNMoMoAdapter()
        return MTNMoMoAdapter()
    if channel == Channel.AIRTEL_MONEY:
        return AirtelMoneyAdapter()
    if channel == Channel.CARD:
        return CardAdapter()
    if channel in (Channel.VISA, Channel.CRYPTO):
        # ChangeNow/Flutterwave have already confirmed settlement before
        # create_charge is called for these channels. Return a passthrough
        # adapter that immediately marks the transaction succeeded.
        return _PassthroughAdapter(channel)
    raise ValueError(f"unknown channel: {channel}")


class _PassthroughAdapter(RailAdapter):
    """No-op rail for channels settled externally (Visa/Flutterwave, ChangeNow crypto)."""
    def __init__(self, channel: Channel):
        self.channel = channel

    def initiate(self, txn) -> InitiateResult:
        from .orchestrator import complete_transaction
        import threading
        app = current_app._get_current_object()
        txn_id = txn.id
        ref = txn.merchant_reference or f"{self.channel.value}_{txn.public_id}"
        def _fire():
            with app.app_context():
                complete_transaction(txn_id, success=True, rail_reference=ref)
        threading.Timer(0.1, _fire).start()
        return InitiateResult(rail_reference=ref, accepted=True)
