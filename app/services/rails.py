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
transaction to succeeded/failed after RAIL_CALLBACK_DELAY_SECONDS, with
RAIL_SUCCESS_PROBABILITY success rate.
"""
from __future__ import annotations

import json
import random
import threading
import uuid
from dataclasses import dataclass

from flask import current_app

from ..extensions import db
from ..models import Channel, RailEvent, Transaction, TxnStatus


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

        def _fire():
            # Late import to dodge circular deps.
            from .orchestrator import complete_transaction

            with app.app_context():
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


def get_adapter(channel: Channel) -> RailAdapter:
    if channel == Channel.MTN_MOMO:
        if current_app.config.get("MOMO_USE_REAL"):
            # Late import to avoid a circular dependency at module load.
            from .rails_mtn_real import RealMTNMoMoAdapter
            return RealMTNMoMoAdapter()
        return MTNMoMoAdapter()
    if channel == Channel.AIRTEL_MONEY:
        return AirtelMoneyAdapter()
    if channel == Channel.CARD:
        return CardAdapter()
    raise ValueError(f"unknown channel: {channel}")
