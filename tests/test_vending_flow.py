"""End-to-end test for the vending flow: order -> QR -> pay -> auto-dispense.

Runs fully offline. The mock rail settles the charge; the supplier's cloud is
replaced with a recording stub, so nothing here touches XY Vending's server.

What it proves:
  1. A vending order creates a payment link + a scannable QR.
  2. Paying it dispenses the machine automatically, exactly once.
  3. A FAILED payment never dispenses.               <- the money guard
  4. A supplier outage marks the order failed but keeps the payment intact,
     and the order can be retried afterwards.
  5. A merchant without vending enabled cannot create an order.
  6. Two concurrent completions dispense only once.  <- the double-drop guard
"""
import atexit
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock rails — never touch MTN's sandbox from a test. (.env sets this to 1, and
# dotenv would fill it back in if we merely popped it, so pin it explicitly.)
os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"

# A FILE database, not :memory:. The mock rail completes the charge on a timer
# thread, and SQLite's in-memory database is per-connection — the timer thread
# would look into a different, empty database and the dispense would silently
# never fire. A temp file is shared by every thread.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="vending_test_")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")


@atexit.register
def _cleanup_db():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


os.environ["XY_KEY"] = "test-key"
os.environ["XY_SECRET"] = "test-secret"

from app import create_app
from app.extensions import db
from app.models import Merchant, PaymentLink, Transaction, TxnStatus
from app.services import vending, xy_vending

# ---- supplier stub -------------------------------------------------------

CALLS = []
FAIL_NEXT = {"on": False}


def fake_apply_export_goods(**kwargs):
    if FAIL_NEXT["on"]:
        raise xy_vending.XYVendingError("supplier unreachable")
    CALLS.append(kwargs)
    return {"code": "1", "message": "ok"}


xy_vending.apply_export_goods = fake_apply_export_goods
vending.xy_vending.apply_export_goods = fake_apply_export_goods


# Machine list the fake supplier returns, keyed by the operator key that asked.
MACHINE_DIRECTORY = {
    "tk-key": [
        {"jqbh": "XY000123", "jqmc": "Kampala Road lobby", "jqlb": "1",
         "dwmc": "Kampala Road", "dwjd": "32.58", "dwwd": "0.31"},
        {"jqbh": "XY000124", "jqmc": "Garden City", "jqlb": "1",
         "dwmc": "Yusuf Lule Rd", "dwjd": "32.59", "dwwd": "0.33"},
    ],
    "other-key": [
        {"jqbh": "ZZ999", "jqmc": "Someone else's machine", "jqlb": "1"},
    ],
}


def fake_query_machines(shbh=None, *, creds=None):
    """Mimics the supplier: the machines you get depend on WHOSE key asked."""
    if creds is None or not creds.configured:
        raise xy_vending.XYVendingError("XY connector not configured")
    return {"code": "1", "message": "ok",
            "data": MACHINE_DIRECTORY.get(creds.key, [])}


xy_vending.query_machines = fake_query_machines
vending.xy_vending.query_machines = fake_query_machines


def wait_for(fn, timeout=12.0, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(
            name="TK Vending", email="tk@x.com",
            public_key="pk_v", secret_key="sk_v", kyc_status="verified",
            vending_enabled=True,
        )
        off = Merchant(
            name="No Vending Shop", email="nv@x.com",
            public_key="pk_n", secret_key="sk_n", kyc_status="verified",
            vending_enabled=False,
        )
        db.session.add_all([m, off]); db.session.commit()

    client = app.test_client()
    hdr = {"Authorization": "Bearer sk_v", "Content-Type": "application/json",
           "Idempotency-Key": "k1", "X-Timestamp": str(int(time.time()))}

    # 1. Create a vending order
    r = client.post("/v1/vending/orders", headers=hdr, json={
        "machine": "XY000123",
        "amount": 2500,
        "goods": [{"spbh": "0001", "spmc": "Coca-Cola 500ml", "spdj": 2500}],
        "reference": "slot-3",
    })
    assert r.status_code == 201, r.data
    order_id = r.json["order_id"]
    assert r.json["payment_status"] == "unpaid"
    assert r.json["vending_status"] == "pending"
    assert "/pay/" in r.json["pay_url"]
    print(f"[1] Order created: {order_id} on machine {r.json['machine']}")

    # 2. The QR the machine shows
    q = client.get(f"/pay/{order_id}/qr.png")
    assert q.status_code == 200 and q.mimetype == "image/png"
    assert q.data[:8] == b"\x89PNG\r\n\x1a\n", "not a real PNG"
    qs = client.get(f"/pay/{order_id}/qr.svg")
    assert qs.status_code == 200 and b"<svg" in qs.data
    d = client.get(f"/vending/display/{order_id}")
    assert d.status_code == 200 and b"Scan with your phone" in d.data
    print(f"[2] QR renders ({len(q.data)} B PNG, {len(qs.data)} B SVG) + machine display page")

    # 3. Customer scans, lands on checkout, pays
    r3 = client.post(f"/pay/{order_id}/submit",
                     data={"channel": "mtn_momo", "phone": "256780000001"})
    assert r3.status_code == 302, r3.status_code
    print("[3] Customer paid through the hosted checkout")

    # 4. The mock rail completes -> auto-dispense must fire
    def dispensed():
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=order_id).one()
            return link.vending_status == vending.DISPENSED

    assert wait_for(dispensed), "charge succeeded but the machine was never told to dispense"
    assert len(CALLS) == 1, f"expected exactly 1 supplier call, got {len(CALLS)}"
    call = CALLS[0]
    assert call["jqbh"] == "XY000123"
    assert call["order_id"] == order_id
    assert call["third_party_txn_id"].startswith("txn_")
    assert call["pay_account"] == "256780000001", call["pay_account"]
    assert call["goods"][0]["spbh"] == "0001"
    print(f"[4] Auto-dispensed once: machine={call['jqbh']} payer={call['pay_account']}")

    # 5. Polling the order reports both statuses
    r5 = client.get(f"/v1/vending/orders/{order_id}",
                    headers={"Authorization": "Bearer sk_v"})
    assert r5.status_code == 200
    assert r5.json["payment_status"] == "succeeded"
    assert r5.json["vending_status"] == "dispensed"
    assert r5.json["dispensed_at"]
    print("[5] Machine poll: paid + dispensed")

    # 6. Re-polling must NOT dispense a second time
    client.get(f"/v1/vending/orders/{order_id}", headers={"Authorization": "Bearer sk_v"})
    assert len(CALLS) == 1, "re-polling caused a second dispense"
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=order_id).one()
        txn = db.session.get(Transaction, link.transaction_id)
        assert not vending.dispense_for_link(link, txn), "double dispense was allowed"
    assert len(CALLS) == 1, "concurrent dispense claim leaked a second drop"
    print("[6] Double-dispense guard holds (still 1 supplier call)")

    # 7. A FAILED payment must never dispense
    # (the rail reads this from app.config at initiate time, not from the env)
    app.config["RAIL_SUCCESS_PROBABILITY"] = 0.0
    hdr2 = dict(hdr, **{"Idempotency-Key": "k2"})
    r7 = client.post("/v1/vending/orders", headers=hdr2, json={
        "machine": "XY000123", "amount": 3000,
        "goods": [{"spbh": "0002", "spmc": "Water", "spdj": 3000}],
    })
    fail_order = r7.json["order_id"]
    client.post(f"/pay/{fail_order}/submit",
                data={"channel": "mtn_momo", "phone": "256780000002"})

    def charge_failed():
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=fail_order).one()
            if not link.transaction_id:
                return False
            t = db.session.get(Transaction, link.transaction_id)
            return t.status == TxnStatus.FAILED

    assert wait_for(charge_failed), "mock rail never failed the charge"
    time.sleep(1.0)
    assert len(CALLS) == 1, "a FAILED payment triggered a dispense"
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=fail_order).one()
        assert link.vending_status == vending.PENDING
    print("[7] Failed payment did NOT dispense - money guard holds")

    # 8. Supplier outage: order marked failed, payment untouched, retry works
    app.config["RAIL_SUCCESS_PROBABILITY"] = 1.0
    FAIL_NEXT["on"] = True
    hdr3 = dict(hdr, **{"Idempotency-Key": "k3"})
    r8 = client.post("/v1/vending/orders", headers=hdr3, json={
        "machine": "XY000123", "amount": 1500,
        "goods": [{"spbh": "0003", "spmc": "Biscuits", "spdj": 1500}],
    })
    out_order = r8.json["order_id"]
    client.post(f"/pay/{out_order}/submit",
                data={"channel": "mtn_momo", "phone": "256780000003"})

    def marked_failed():
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=out_order).one()
            return link.vending_status == vending.FAILED

    assert wait_for(marked_failed), "supplier outage was not recorded on the order"
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=out_order).one()
        txn = db.session.get(Transaction, link.transaction_id)
        assert txn.status == TxnStatus.SUCCEEDED, "payment was lost by a supplier failure"
        assert "unreachable" in (link.vending_error or "")
    print("[8] Supplier outage: order failed, payment preserved")

    FAIL_NEXT["on"] = False
    r9 = client.post(f"/v1/vending/orders/{out_order}/dispense",
                     headers=dict(hdr, **{"Idempotency-Key": "k4"}))
    assert r9.status_code == 200 and r9.json["ok"] is True, r9.data
    assert r9.json["vending_status"] == "dispensed"
    assert len(CALLS) == 2
    print("[9] Retry after outage dispensed successfully")

    # 10. A merchant without vending enabled is refused
    r10 = client.post("/v1/vending/orders", headers={
        "Authorization": "Bearer sk_n", "Content-Type": "application/json",
        "Idempotency-Key": "k5", "X-Timestamp": str(int(time.time()))},
        json={"machine": "XY000123", "amount": 1000,
              "goods": [{"spbh": "0001", "spmc": "X", "spdj": 1000}]})
    assert r10.status_code == 400, r10.status_code
    print("[10] Vending-disabled merchant refused")

    # 11. A retry can never dispense an unpaid order
    with app.app_context():
        m = Merchant.query.filter_by(email="tk@x.com").one()
        unpaid = vending.create_order(
            merchant=m, machine="XY000123", amount=900,
            goods=[{"spbh": "0009", "spmc": "Gum", "spdj": 900}])
        try:
            vending.retry_dispense(unpaid)
            raise AssertionError("retry dispensed an unpaid order")
        except vending.VendingError as exc:
            assert "no payment" in str(exc)
    assert len(CALLS) == 2
    print("[11] Retry on an unpaid order refused")

    # 12. The merchant-facing page renders and the on/off switch works
    app.config["WTF_CSRF_ENABLED"] = False   # exercise the route, not the token
    with app.app_context():
        m = Merchant.query.filter_by(email="tk@x.com").one()
        mid = m.id
    with client.session_transaction() as s:
        s["_user_id"] = str(mid)
        s["_fresh"] = True

    r12 = client.get(f"/dashboard/{mid}/vending")
    assert r12.status_code == 200, r12.status_code
    page = r12.data.decode()
    assert "Vending machines" in page
    assert "Enabled" in page
    assert order_id in page, "the order log does not list the orders"
    print("[12] Merchant vending page renders with the order log")

    r13 = client.post(f"/dashboard/{mid}/vending/toggle", data={"enabled": "0"})
    assert r13.status_code == 302
    with app.app_context():
        assert db.session.get(Merchant, mid).vending_enabled is False
    print("[13] Toggle OFF persisted")

    # With vending off, a succeeded payment must NOT reach the machine.
    with app.app_context():
        m = Merchant.query.filter_by(email="tk@x.com").one()
        m.vending_enabled = True
        db.session.commit()
        blocked = vending.create_order(
            merchant=m, machine="XY000123", amount=700,
            goods=[{"spbh": "0007", "spmc": "Sweets", "spdj": 700}])
        blocked_id = blocked.public_id
        m.vending_enabled = False
        db.session.commit()
    client.post(f"/pay/{blocked_id}/submit",
                data={"channel": "mtn_momo", "phone": "256780000007"})

    def blocked_marked():
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=blocked_id).one()
            return link.vending_status == vending.FAILED

    assert wait_for(blocked_marked), "disabled merchant's order was not marked"
    assert len(CALLS) == 2, "vending was OFF but the machine was still told to dispense"
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=blocked_id).one()
        assert "disabled" in (link.vending_error or "")
        m = Merchant.query.filter_by(email="tk@x.com").one()
        m.vending_enabled = True
        db.session.commit()
    print("[14] Kill switch honoured: payment taken, no dispense")

    # 15. Per-merchant credentials: stored encrypted, never plaintext
    from app.services.secrets_box import decrypt, encrypt
    with app.app_context():
        m = Merchant.query.filter_by(email="tk@x.com").one()
        m.xy_key = "tk-key"
        m.xy_secret_encrypted = encrypt("tk-secret-value")
        m.xy_merchant_no = "0023"
        db.session.commit()
        assert m.xy_secret_encrypted != "tk-secret-value", "secret stored in plaintext!"
        assert "tk-secret" not in m.xy_secret_encrypted, "secret leaks into the ciphertext"
        assert decrypt(m.xy_secret_encrypted) == "tk-secret-value", "round-trip failed"
        creds = xy_vending.for_merchant(m)
        assert creds.key == "tk-key" and creds.secret == "tk-secret-value"
    print("[15] XY secret encrypted at rest, decrypts for signing")

    # 16. Sync machines — the operator's own list
    with app.app_context():
        m = Merchant.query.filter_by(email="tk@x.com").one()
        added, updated = vending.sync_machines(m)
        assert added == 2 and updated == 0, (added, updated)
        names = sorted(x.jqbh for x in vending.machines_for(m))
        assert names == ["XY000123", "XY000124"], names
        # Re-syncing updates rather than duplicating
        added2, updated2 = vending.sync_machines(m)
        assert added2 == 0 and updated2 == 2, (added2, updated2)
    print("[16] Machine registry synced: 2 machines, re-sync does not duplicate")

    # 17. Two operators are isolated — different credentials, different machines
    with app.app_context():
        other = Merchant(
            name="Other Vending Co", email="other@x.com",
            public_key="pk_o", secret_key="sk_o", kyc_status="verified",
            vending_enabled=True, xy_key="other-key",
            xy_secret_encrypted=encrypt("other-secret"), xy_merchant_no="0099")
        db.session.add(other); db.session.commit()
        vending.sync_machines(other)
        mine = {x.jqbh for x in vending.machines_for(
            Merchant.query.filter_by(email="tk@x.com").one())}
        theirs = {x.jqbh for x in vending.machines_for(other)}
        assert mine == {"XY000123", "XY000124"}, mine
        assert theirs == {"ZZ999"}, theirs
        assert not mine & theirs, "operators can see each other's machines"

        # And one operator cannot address the other's machine
        try:
            vending.create_order(merchant=other, machine="XY000123", amount=1000,
                                 goods=[{"spbh": "0001", "spmc": "X", "spdj": 1000}])
            raise AssertionError("cross-operator dispense was allowed")
        except vending.VendingError as exc:
            assert "not registered" in str(exc), exc
    print("[17] Operators isolated: own machines only, no cross-addressing")

    # 18. A dispense uses the OWNING merchant's credentials, not a global one
    before = len(CALLS)
    with app.app_context():
        m = Merchant.query.filter_by(email="tk@x.com").one()
        order = vending.create_order(
            merchant=m, machine="XY000124", amount=1200,
            goods=[{"spbh": "0004", "spmc": "Juice", "spdj": 1200}])
        oid = order.public_id
    client.post(f"/pay/{oid}/submit", data={"channel": "mtn_momo", "phone": "256780000004"})

    def routed():
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=oid).one()
            return link.vending_status == vending.DISPENSED

    assert wait_for(routed), "order on the second machine never dispensed"
    call = CALLS[-1]
    assert len(CALLS) == before + 1
    assert call["jqbh"] == "XY000124", call["jqbh"]
    assert call["creds"].key == "tk-key", "dispense used the wrong operator's key"
    assert call["creds"].secret == "tk-secret-value"
    print("[18] Dispense routed to the right machine with the owner's credentials")

    print("\nALL VENDING FLOW TESTS PASSED")


if __name__ == "__main__":
    main()
