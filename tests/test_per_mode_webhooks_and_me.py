"""Per-mode webhook routing + GET /v1/me (Backbone Q5 + account-id).

Proves:
  /v1/me   identifies the account behind a key: mode, public key, scope, KYC.
  Webhooks a test-mode event delivers to the SANDBOX endpoint (signed with the
           sandbox secret) when one is set; a live event to the live endpoint;
           and a test event FALLS BACK to the live endpoint when no sandbox one
           is configured. A resend retargets to the correct per-mode endpoint.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="permode_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _cleanup():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Merchant, WebhookDelivery
from app.services import webhooks

# Stub the immediate-delivery broker call so enqueue() doesn't block on Redis
# (same pattern as tests/test_webhook_envelope.py).
import app.tasks.webhooks_task as _wt
_wt.deliver_webhook.delay = lambda *a, **k: None


def latest_delivery(mid):
    return (WebhookDelivery.query.filter_by(merchant_id=mid)
            .order_by(WebhookDelivery.id.desc()).first())


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="BB Co", email="bb@x.com", public_key="pk_live_bb",
                     test_public_key="pk_test_bb", secret_key="sk_live_bb",
                     test_secret_key="sk_test_bb", handle="backbone",
                     kyc_status="verified",
                     webhook_url="https://live.backbone.example/hook",
                     webhook_secret="whsec_live",
                     webhook_url_test="https://sandbox.backbone.example/hook",
                     webhook_secret_test="whsec_test")
        db.session.add(m); db.session.commit()
        mid = m.id

        # ---- per-mode routing ----
        # test-mode event -> sandbox URL, signed with the sandbox secret
        webhooks.enqueue(m, "charge.succeeded", {"mode": "test", "id": "txn_1"})
        d = latest_delivery(mid)
        assert d.url == "https://sandbox.backbone.example/hook", d.url
        assert d.signature == webhooks.sign_payload(d.payload, "whsec_test"), "wrong secret for test"
        print("[wh] test event -> sandbox endpoint, signed with sandbox secret")

        # live-mode event -> live URL, signed with the live secret
        webhooks.enqueue(m, "charge.succeeded", {"mode": "live", "id": "txn_2"})
        d = latest_delivery(mid)
        assert d.url == "https://live.backbone.example/hook", d.url
        assert d.signature == webhooks.sign_payload(d.payload, "whsec_live"), "wrong secret for live"
        print("[wh] live event -> live endpoint, signed with live secret")

        # fallback: remove the sandbox URL -> test event goes to the live endpoint
        m.webhook_url_test = None
        db.session.commit()
        webhooks.enqueue(m, "charge.succeeded", {"mode": "test", "id": "txn_3"})
        d = latest_delivery(mid)
        assert d.url == "https://live.backbone.example/hook", d.url
        assert d.signature == webhooks.sign_payload(d.payload, "whsec_live"), "fallback secret"
        print("[wh] test event with no sandbox endpoint -> falls back to live endpoint")

        # resend retargets to the correct per-mode endpoint (and re-signs).
        m.webhook_url_test = "https://sandbox2.backbone.example/hook"
        db.session.commit()
        # d is currently a TEST delivery pointing at the live URL; a resend must
        # move it to the (new) sandbox URL and re-sign with the sandbox secret.
        webhooks.resend_delivery(d)
        assert d.url == "https://sandbox2.backbone.example/hook", d.url
        assert d.signature == webhooks.sign_payload(d.payload, "whsec_test"), "resend re-sign"
        print("[wh] resend of a test delivery retargets to the sandbox endpoint + re-signs")

        # a merchant with NO webhook_url at all -> test event is simply skipped
        m2 = Merchant(name="No Hook", email="nh@x.com", public_key="pk_live_nh",
                      test_secret_key="sk_test_nh", secret_key="sk_live_nh")
        db.session.add(m2); db.session.commit()
        assert webhooks.enqueue(m2, "charge.succeeded", {"mode": "test"}) is False
        print("[wh] no endpoint configured -> event skipped (returns False)")

    # ---- GET /v1/me ----
    client = app.test_client()
    r = client.get("/v1/me", headers={"Authorization": "Bearer sk_test_bb"})
    assert r.status_code == 200, (r.status_code, r.data)
    b = r.json
    assert b["object"] == "account" and b["mode"] == "test", b
    assert b["public_key"] == "pk_test_bb", b
    assert b["id"] == "backbone" and b["handle"] == "backbone", b
    assert b["verified"] is True and b["kyc_status"] == "verified", b
    assert b["scope"] == "full", b
    print(f"[me] sk_test -> id={b['id']} mode={b['mode']} public_key={b['public_key']} scope={b['scope']}")

    rl = client.get("/v1/me", headers={"Authorization": "Bearer sk_live_bb"})
    assert rl.json["mode"] == "live" and rl.json["public_key"] == "pk_live_bb", rl.json
    print("[me] sk_live -> mode=live, live public key")

    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/me", headers={"Authorization": "Bearer sk_live_bogus"}).status_code == 401
    print("[me] no/invalid key -> 401")

    print("\nAll per-mode-webhook + /v1/me checks passed.")


if __name__ == "__main__":
    main()
