"""XY §2.2.3 dispense-result callback — /inbound/xy/dispense-result.

The gap this closes: `ApplyExportGoods` returning code "1" only means the
supplier ACCEPTED the dispense command. It does not mean a soda came out. This
callback is where XY tells us what the machine actually did, so an order we
optimistically marked `dispensed` can be corrected when the tray jammed.

What this proves:
  1. A confirming callback (finished, chsl=1) keeps the order `dispensed`.
  2. A callback reporting chsl=0 FLIPS a `dispensed` order to `failed`.   <- the money case
  3. A supplier-side refund (tkje > 0) also marks it failed.
  4. An unsigned or wrongly-signed callback is rejected and changes NOTHING.
  5. The doc's alternate field spellings (`state`/`dsfshbh`) still verify.
  6. An unknown order is acknowledged (no retry storm) but changes nothing.
  7. The callback never moves money — the charge stays SUCCEEDED throughout.
"""
import atexit
import hashlib
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["RAIL_CALLBACK_DELAY_SECONDS"] = "1"
os.environ["RAIL_SUCCESS_PROBABILITY"] = "1.0"

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="xycb_test_")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _DB_PATH.replace("\\", "/")


@atexit.register
def _cleanup_db():
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass


XY_SECRET = "test-secret"
os.environ["XY_KEY"] = "test-key"
os.environ["XY_SECRET"] = XY_SECRET

from app import create_app
from app.extensions import db
from app.models import Merchant, PaymentLink, Transaction, TxnStatus
from app.services import vending, xy_vending

# Supplier stub — the dispense command always "succeeds" (accepted), which is
# exactly the false confidence this callback exists to correct.
xy_vending.apply_export_goods = lambda **kw: {"code": "1", "message": "ok"}
vending.xy_vending.apply_export_goods = xy_vending.apply_export_goods

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def sign_payload(payload, secret=XY_SECRET, alias=False):
    """Build the doc's signature: MD5(secret + timestamp + sorted k=v&k=v)."""
    aliases = {"status": "state", "dsfshdh": "dsfshbh"}
    scalars = {k: v for k, v in payload.items()
               if k not in ("sign", "key", "timestamp", "splist")
               and not isinstance(v, (dict, list))}
    if alias:
        scalars = {aliases.get(k, k): v for k, v in scalars.items()}
    base = "&".join(f"{k}={'' if scalars[k] is None else scalars[k]}"
                    for k in sorted(scalars))
    raw = f"{secret}{payload.get('timestamp', '')}{base}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def make_payload(link, txn, *, status="1", chsl=1, tkje=0):
    p = {
        "jqbh": "XY000123",
        "shbh": "0023",
        "paytype": "forwardPayCode",
        "ddbh": link.public_id,
        "dsfjybh": txn.public_id,
        "dsfshdh": "123456789",
        "cjsj": "2026-08-17 11:53:00",
        "status": status,
        "zfplat": "1",
        "zflx": "2",
        "zfzh": "256783647260",
        "zfsj": "2026-08-17 11:53:00",
        "payamount": 250000,
        "yhamount": 0,
        "yhtype": "0",
        "tkje": tkje,
        "tksj": "",
        "timestamp": int(time.time() * 1000),
        "splist": [{"chsl": str(chsl), "hdbh": "001", "spbh": "0001",
                    "spmc": "Coca-Cola 500ml", "dsfspbh": "am-0001",
                    "spjg": 250000, "yhje": 0}],
    }
    return p


def paid_order(app, client, hdr, ref):
    """Create a vending order, pay it, and wait for the optimistic dispense."""
    r = client.post("/v1/vending/orders", headers={**hdr, "Idempotency-Key": ref},
                    json={"machine": "XY000123", "amount": 2500,
                          "goods": [{"spbh": "0001", "spmc": "Coca-Cola 500ml",
                                     "spdj": 2500}]})
    assert r.status_code in (200, 201), r.data
    order_id = r.get_json()["order_id"] if "order_id" in r.get_json() else r.get_json()["id"]

    client.post(f"/pay/{order_id}/submit",
                data={"channel": "mtn_momo", "phone": "256783647260"})

    deadline = time.time() + 12
    while time.time() < deadline:
        with app.app_context():
            link = PaymentLink.query.filter_by(public_id=order_id).one()
            if link.vending_status == vending.DISPENSED:
                return order_id
        time.sleep(0.25)
    raise AssertionError("order never reached dispensed within timeout")


def status_of(app, order_id):
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=order_id).one()
        return link.vending_status, link.vending_error


def charge_status(app, order_id):
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=order_id).one()
        txn = db.session.get(Transaction, link.transaction_id)
        return txn.status


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        db.session.add(Merchant(
            name="TK Vending", email="tkcb@x.com",
            public_key="pk_cb", secret_key="sk_cb", kyc_status="verified",
            vending_enabled=True,
        ))
        db.session.commit()

    client = app.test_client()
    hdr = {"Authorization": "Bearer sk_cb", "Content-Type": "application/json",
           "X-Timestamp": str(int(time.time()))}

    # ---- 1. confirming callback keeps it dispensed ----
    oid = paid_order(app, client, hdr, "cb1")
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=oid).one()
        txn = db.session.get(Transaction, link.transaction_id)
        payload = make_payload(link, txn, chsl=1)
    payload["sign"] = sign_payload(payload)
    r = client.post("/inbound/xy/dispense-result", json=payload)
    st, _ = status_of(app, oid)
    check("confirming callback -> stays dispensed",
          r.status_code == 200 and r.get_json().get("code") == "1"
          and st == vending.DISPENSED)

    # ---- 2. THE MONEY CASE: machine says nothing came out ----
    oid2 = paid_order(app, client, hdr, "cb2")
    before, _ = status_of(app, oid2)
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=oid2).one()
        txn = db.session.get(Transaction, link.transaction_id)
        payload = make_payload(link, txn, chsl=0)
    payload["sign"] = sign_payload(payload)
    client.post("/inbound/xy/dispense-result", json=payload)
    st2, err2 = status_of(app, oid2)
    check("optimistic 'dispensed' before the callback", before == vending.DISPENSED)
    check("chsl=0 flips dispensed -> failed", st2 == vending.FAILED)
    check("failure reason names the supplier", err2 and "supplier" in err2)

    # ---- 3. supplier-side refund also disconfirms ----
    oid3 = paid_order(app, client, hdr, "cb3")
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=oid3).one()
        txn = db.session.get(Transaction, link.transaction_id)
        payload = make_payload(link, txn, chsl=1, tkje=250000)
    payload["sign"] = sign_payload(payload)
    client.post("/inbound/xy/dispense-result", json=payload)
    st3, err3 = status_of(app, oid3)
    check("supplier refund -> failed", st3 == vending.FAILED and "refund" in (err3 or ""))

    # ---- 4. a forged callback changes nothing ----
    oid4 = paid_order(app, client, hdr, "cb4")
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=oid4).one()
        txn = db.session.get(Transaction, link.transaction_id)
        payload = make_payload(link, txn, chsl=0)
    payload["sign"] = "0" * 32           # wrong signature
    r = client.post("/inbound/xy/dispense-result", json=payload)
    st4, _ = status_of(app, oid4)
    check("bad signature rejected with 401", r.status_code == 401)
    check("bad signature left the order untouched", st4 == vending.DISPENSED)

    payload.pop("sign")
    r = client.post("/inbound/xy/dispense-result", json=payload)
    st4b, _ = status_of(app, oid4)
    check("missing signature rejected", r.status_code == 401 and st4b == vending.DISPENSED)

    # ---- 5. the doc's alternate spellings still verify ----
    oid5 = paid_order(app, client, hdr, "cb5")
    with app.app_context():
        link = PaymentLink.query.filter_by(public_id=oid5).one()
        txn = db.session.get(Transaction, link.transaction_id)
        payload = make_payload(link, txn, chsl=0)
    payload["sign"] = sign_payload(payload, alias=True)   # state=/dsfshbh= form
    r = client.post("/inbound/xy/dispense-result", json=payload)
    st5, _ = status_of(app, oid5)
    check("alternate doc spelling verifies", r.status_code == 200 and st5 == vending.FAILED)

    # ---- 6. unknown order is acknowledged, not retried forever ----
    ghost = {"ddbh": "lnk_doesnotexist", "dsfjybh": "txn_nope",
             "jqbh": "XY000123", "status": "1", "timestamp": int(time.time() * 1000),
             "splist": []}
    ghost["sign"] = sign_payload(ghost)
    r = client.post("/inbound/xy/dispense-result", json=ghost)
    check("unknown order acknowledged with code 1",
          r.status_code == 200 and r.get_json().get("code") == "1")

    # ---- 7. the callback never touches the money ----
    check("charge stayed SUCCEEDED through a disconfirming callback",
          charge_status(app, oid2) == TxnStatus.SUCCEEDED)

    print()
    failed = [label for label, ok in CHECKS if not ok]
    if failed:
        print(f"{len(failed)} CHECK(S) FAILED:")
        for label in failed:
            print("  - " + label)
        sys.exit(1)
    print(f"All {len(CHECKS)} XY dispense-callback checks passed.")


if __name__ == "__main__":
    main()
