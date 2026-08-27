"""Crypto must NEVER phantom-settle live money in production (audit CRITICAL).

Run: MOMO_USE_REAL=0 .venv\\Scripts\\python.exe tests\\test_crypto_prod_guard.py

Crypto uses the passthrough adapter, so the simulated-rail guard (guardrail 14)
does not catch it. With no ChangeNow config, the mock returned a fake deposit
address and then 'finished' with nothing received — crediting the LIVE ledger
with phantom money. Now, on Render + live mode without real config:
  - create_exchange REFUSES (accepted=False),
  - get_status NEVER returns 'finished' (stays 'waiting'),
  - the checkout channel list HIDES crypto.
Sandbox (api_mode='test') and local/dev (no RENDER) keep the mock for testing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")

from flask import g
from app import create_app
from app.extensions import db
from app.services import changenow


def main():
    app = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                      "CHANGENOW_API_KEY": "", "CHANGENOW_RECEIVING_ADDRESS": ""})
    with app.app_context():
        db.create_all()

    # ---- LIVE on Render, no ChangeNow config: the mock must be refused ----
    os.environ["RENDER"] = "true"
    try:
        with app.test_request_context():
            g.api_mode = "live"
            r = changenow.create_exchange(from_coin="btc", amount_ugx=100000, public_id="lnk_x")
            assert r.accepted is False, f"[1] live-on-render mock exchange should be REFUSED: {r}"
            assert changenow.get_status("cn_mock_abc") == "waiting", \
                "[2] live-on-render mock status must never be 'finished'"
        print("[1] PASS — live-on-Render without config: create_exchange refused")
        print("[2] PASS — live-on-Render mock status never 'finished' (no phantom settle)")

        # ---- SANDBOX (api_mode='test') on Render: mock still works for testing ----
        with app.test_request_context():
            g.api_mode = "test"
            r = changenow.create_exchange(from_coin="btc", amount_ugx=100000, public_id="lnk_t")
            assert r.accepted is True, f"[3] sandbox mock should still work: {r}"
            eid = r.exchange_id
            # polls: waiting, waiting, finished
            s = [changenow.get_status(eid) for _ in range(3)]
            assert s[-1] == "finished", f"[3] sandbox mock should settle for testing: {s}"
        print("[3] PASS — sandbox mock still works (testing unaffected)")

        # ---- checkout hides crypto in live-on-render without config ----
        from app.routes.checkout import _channel_options
        with app.test_request_context():
            g.api_mode = "live"
            opts = _channel_options(include_crypto=True)
            assert not any(v == "crypto" for v, _, _ in opts), \
                f"[4] crypto must be hidden on live checkout without config: {opts}"
        print("[4] PASS — crypto hidden from live checkout when unconfigured")
    finally:
        os.environ.pop("RENDER", None)

    # ---- local/dev (no RENDER): mock works so the offline suite runs ----
    with app.test_request_context():
        g.api_mode = "live"
        r = changenow.create_exchange(from_coin="btc", amount_ugx=100000, public_id="lnk_d")
        assert r.accepted is True, "[5] local/dev mock should work (no RENDER)"
    print("[5] PASS — local/dev keeps the mock (no RENDER)")

    print("\nALL CRYPTO-PROD-GUARD ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
