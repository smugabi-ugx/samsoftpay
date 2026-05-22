"""End-to-end test for the customer-facing hosted checkout flow."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("MOMO_USE_REAL", None)
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from app.extensions import db
from app.models import Merchant


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(
            name="Test Shop", email="t@x.com",
            public_key="pk", secret_key="sk", kyc_status="verified",
        )
        db.session.add(m); db.session.commit()

    client = app.test_client()

    # 1. Merchant creates a payment link
    r = client.post(
        "/v1/payment-links",
        headers={"Authorization": "Bearer sk", "Content-Type": "application/json"},
        json={"amount": 25000, "description": "Team building T-shirt"},
    )
    assert r.status_code == 201, r.data
    link_id = r.json["id"]
    link_url = r.json["url"]
    print(f"[1] Link created: {link_id} -> {link_url}")

    # 2. Customer visits the checkout page
    r2 = client.get(f"/pay/{link_id}")
    assert r2.status_code == 200
    html = r2.data.decode()
    assert "Team building T-shirt" in html
    assert "Test Shop" in html
    assert "25,000" in html
    assert "MTN Mobile Money" in html
    assert "Airtel Money" in html
    assert "Card" in html
    print("[2] Checkout page rendered with all 3 channels")

    # 3. Customer submits the form
    r3 = client.post(
        f"/pay/{link_id}/submit",
        data={"channel": "mtn_momo", "phone": "256780000001"},
    )
    assert r3.status_code == 302, r3.status_code
    assert f"/pay/{link_id}/status" in r3.location
    print(f"[3] Form submission redirected to status page")

    # 4. Status page renders and shows pending
    r4 = client.get(f"/pay/{link_id}/status")
    assert r4.status_code == 200
    print("[4] Status page renders")

    # 5. Wait for mock rail to complete, status JSON should show succeeded
    time.sleep(2)
    r5 = client.get(f"/pay/{link_id}/status.json")
    assert r5.status_code == 200
    assert r5.json["status"] == "succeeded", r5.json
    print(f"[5] Final status: {r5.json['status']}")

    # 6. Single-use link, revisiting checkout goes straight to status
    r6 = client.get(f"/pay/{link_id}", follow_redirects=False)
    assert r6.status_code == 302
    assert "/status" in r6.location
    print("[6] Single-use link redirects to status on revisit")

    print()
    print("CHECKOUT FLOW TESTS PASSED")


if __name__ == "__main__":
    main()
