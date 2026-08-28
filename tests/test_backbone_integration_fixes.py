"""Fixes prompted by Backbone's integration audit (SAMSOFTPAY_OPEN_QUESTIONS.md).

Proves:
  Q1c  A webhook URL pointing at our OWN API host is recognised as self-addressed
       (is_self_addressed_url) — the thing that had SamsoftPay POSTing to itself.
  Q3b  GET /v1/payment-links now LISTS links (was 405), newest-first, mode-scoped,
       with cursor pagination — same envelope as GET /v1/charges.
  Q3d  Every bulk-payout result item carries a non-null `reference`, even a
       malformed row with no reference (falls back to <idem-key>-<index>).
  Q3c  A bulk call that is a pure replay (nothing new disbursed) sets the
       batch-level `Idempotent-Replayed: true` response header.
"""
import atexit
import os
import time
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="backbone_fix_")
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
from app.models import AccountType, Merchant, PaymentLink
from app.services import ledger
from app.services.url_guard import is_self_addressed_url


def fund(mid, amount, is_test, currency="UGX"):
    acct = ledger.get_or_create_account(
        type=AccountType.MERCHANT_AVAILABLE, merchant_id=mid, currency=currency, is_test=is_test)
    rail = ledger.get_or_create_account(
        type=AccountType.RAIL_CLEARING, merchant_id=None, currency=currency, is_test=is_test)
    ledger.post([(rail, +amount), (acct, -amount)], currency=currency, memo="funding")
    db.session.commit()


def main():
    # ---- Q1c: self-addressed URL detection (pure unit, no app needed) ----
    assert is_self_addressed_url("https://api.samsoftpay.com/webhooks/samsoftpay") is True
    assert is_self_addressed_url("https://samsoftpay.com/x") is True
    assert is_self_addressed_url("https://samsoftpay.onrender.com/hook") is True
    assert is_self_addressed_url("https://API.SamsoftPay.com/x") is True  # case-insensitive
    assert is_self_addressed_url("https://hooks.backbone.co.ug/samsoftpay") is False
    assert is_self_addressed_url("https://samsoftpay.com.evil.com/x") is False  # not our domain
    assert is_self_addressed_url("") is False
    assert is_self_addressed_url("https://karlpos.com/webhooks/samsoftpay",
                                 ("api.samsoftpay.com",)) is False
    print("[Q1c] self-addressed URL detection correct "
          "(own host rejected, merchant hosts allowed, no suffix-spoof)")

    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="BB Co", email="bb@x.com", public_key="pk_bb",
                     secret_key="sk_live_bb", test_secret_key="sk_test_bb",
                     kyc_status="verified")
        db.session.add(m); db.session.commit()
        mid = m.id
        fund(mid, 5_000_000, is_test=True)   # sandbox wallet for the bulk test

        # Two TEST links + one LIVE link, created oldest->newest.
        for pid, is_test, ref in [
            ("lnk_test_a", True, "BB-A"),
            ("lnk_test_b", True, "BB-B"),
            ("lnk_live_c", False, "BB-C"),
        ]:
            db.session.add(PaymentLink(public_id=pid, merchant_id=mid, amount=10_000,
                                       currency="UGX", reference=ref, is_test=is_test))
        db.session.commit()

    client = app.test_client()
    _TS = str(int(time.time()))

    # ---- Q3b: GET /v1/payment-links no longer 405; lists + scopes by mode ----
    r = client.get("/v1/payment-links", headers={"Authorization": "Bearer sk_test_bb"})
    assert r.status_code == 200, (r.status_code, r.data)   # was 405
    body = r.json
    assert body["object"] == "list", body
    ids = [row["id"] for row in body["data"]]
    assert ids == ["lnk_test_b", "lnk_test_a"], f"expected newest-first test links, got {ids}"
    assert all("transaction_status" in row for row in body["data"]), body
    print(f"[Q3b] GET /v1/payment-links -> 200 list (was 405); sk_test sees {ids} newest-first")

    # Live key sees only the live link.
    rl = client.get("/v1/payment-links", headers={"Authorization": "Bearer sk_live_bb"})
    assert [row["id"] for row in rl.json["data"]] == ["lnk_live_c"], rl.json
    print("[Q3b] sk_live sees only the live link (mode scoping holds)")

    # Cursor pagination: limit=1 then starting_after.
    p1 = client.get("/v1/payment-links?limit=1", headers={"Authorization": "Bearer sk_test_bb"})
    assert p1.json["has_more"] is True and p1.json["next_cursor"] == "lnk_test_b", p1.json
    p2 = client.get("/v1/payment-links?limit=1&starting_after=lnk_test_b",
                    headers={"Authorization": "Bearer sk_test_bb"})
    assert [row["id"] for row in p2.json["data"]] == ["lnk_test_a"], p2.json
    assert p2.json["has_more"] is False, p2.json
    print("[Q3b] cursor pagination (limit + starting_after) works")

    # Filter by reference.
    rf = client.get("/v1/payment-links?reference=BB-A", headers={"Authorization": "Bearer sk_test_bb"})
    assert [row["id"] for row in rf.json["data"]] == ["lnk_test_a"], rf.json
    print("[Q3b] reference filter works")

    # ---- Q3d: bulk malformed item still carries a non-null reference ----
    idem = "bb-batch-1"
    bad_batch = {"payouts": [
        {"amount": 10_000, "recipient": {"phone": "256780000001", "name": "Emp One"},
         "reference": "SAL-1"},
        {"amount": 5_000},                      # <-- missing phone, NO reference
    ]}
    rb = client.post("/v1/payouts/bulk", json=bad_batch,
                     headers={"Authorization": "Bearer sk_test_bb",
                              "Idempotency-Key": idem, "X-Timestamp": _TS})
    assert rb.status_code == 200, (rb.status_code, rb.data)
    results = rb.json["results"]
    bad = next(x for x in results if x["index"] == 1)
    assert bad["ok"] is False and "invalid item" in bad["error"], bad
    assert bad["reference"], f"malformed item must still carry a reference: {bad}"
    assert bad["reference"] == f"{idem}-1", bad
    assert "Idempotent-Replayed" not in rb.headers, "fresh batch must NOT be flagged replayed"
    print(f"[Q3d] malformed row carries reference={bad['reference']!r} (not null); "
          f"fresh batch has no replay header")

    # ---- Q3c: a pure-replay batch sets the batch-level header ----
    # Re-POST the SAME batch (same idem key + items). Item 0 replays its stored
    # success; item 1 is invalid again (never stored) -> NOT all replayed, so no
    # header. Then a clean 1-item batch, replayed, DOES get the header.
    good_idem = "bb-good-1"
    good_batch = {"payouts": [
        {"amount": 10_000, "recipient": {"phone": "256780000002", "name": "Emp Two"},
         "reference": "SAL-2"}]}
    r1 = client.post("/v1/payouts/bulk", json=good_batch,
                     headers={"Authorization": "Bearer sk_test_bb",
                              "Idempotency-Key": good_idem, "X-Timestamp": _TS})
    assert r1.status_code == 200 and r1.json["accepted"] == 1, (r1.status_code, r1.data)
    assert "Idempotent-Replayed" not in r1.headers, "first send is not a replay"
    r2 = client.post("/v1/payouts/bulk", json=good_batch,
                     headers={"Authorization": "Bearer sk_test_bb",
                              "Idempotency-Key": good_idem, "X-Timestamp": _TS})
    assert r2.status_code == 200, (r2.status_code, r2.data)
    assert all(x.get("replayed") for x in r2.json["results"]), r2.json
    assert r2.headers.get("Idempotent-Replayed") == "true", \
        f"pure-replay batch must set the header, got {dict(r2.headers)}"
    print("[Q3c] pure-replay bulk sets Idempotent-Replayed: true (nothing re-disbursed)")

    # And the mixed batch (one replay + one invalid) must NOT set it.
    r3 = client.post("/v1/payouts/bulk", json=bad_batch,
                     headers={"Authorization": "Bearer sk_test_bb",
                              "Idempotency-Key": idem, "X-Timestamp": _TS})
    assert "Idempotent-Replayed" not in r3.headers, \
        "a batch with a non-replayed (invalid) item must not be flagged all-replayed"
    print("[Q3c] mixed batch (replay + invalid) does NOT set the header")

    print("\nAll Backbone integration-fix checks passed.")


if __name__ == "__main__":
    main()
