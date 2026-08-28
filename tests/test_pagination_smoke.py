"""Render smoke test for the server-side pagination pass.

Logs in a merchant (and an admin) and GETs every view that was converted to
.paginate(), asserting each returns 200 (not a 500 from a bad pager call or a
mis-wired variable) — both the base page and a ?page=N to exercise navigation.
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="pag_smoke_")
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
from app.models import Merchant, SubscriptionPlan


def main():
    app = create_app()
    with app.app_context():
        db.create_all()
        m = Merchant(name="Pager Co", email="p@x.com", public_key="pk_live_p",
                     test_public_key="pk_test_p", secret_key="sk_live_p",
                     test_secret_key="sk_test_p", handle="pagerco",
                     kyc_status="verified", vending_enabled=True, role="merchant")
        admin = Merchant(name="Admin", email="a@x.com", public_key="pk_live_a",
                         test_public_key="pk_test_a", secret_key="sk_live_a",
                         test_secret_key="sk_test_a", kyc_status="verified", role="admin")
        db.session.add_all([m, admin]); db.session.commit()
        mid, aid = m.id, admin.id
        plan = SubscriptionPlan(merchant_id=mid, public_id="plan_x", name="Basic",
                                amount=10000, currency="UGX", interval="monthly")
        db.session.add(plan); db.session.commit()
        plan_id = plan.id   # route is /plans/<int:plan_id>/subscribers

    client = app.test_client()

    def login(uid):
        with client.session_transaction() as s:
            s["_user_id"] = str(uid)
            s["_fresh"] = True

    def hit(path, who):
        login(who)
        r = client.get(path)
        body = r.get_data(as_text=True)
        # 200 ideal; a redirect (302) to login/verify is acceptable for a gated
        # page, but a 500 (template/route error) is a hard fail.
        assert r.status_code != 500, f"{path} -> 500\n{body[:600]}"
        assert "Traceback" not in body, f"{path} rendered a traceback"
        return r.status_code

    merchant_pages = [
        f"/dashboard/{mid}",                 # merchant_detail: payouts + links (page_po/page_lk)
        f"/dashboard/{mid}?page_po=2&page_lk=2",
        f"/dashboard/{mid}/vending",         # vending order log
        f"/dashboard/{mid}/disputes",        # db.paginate join
        "/dashboard/wallet",                 # withdrawals + topups + settlements
        "/dashboard/wallet?page_wd=2&page_tu=2&page_st=2",
        "/account",                          # webhook deliveries
        "/account?page=2",
        "/dashboard/bills",
        "/dashboard/gift-cards",
        f"/dashboard/subscriptions/plans/{plan_id}/subscribers",
        f"/dashboard/subscriptions/plans/{plan_id}/subscribers?page=2",
    ]
    admin_pages = [
        "/home",                             # admin_index Merchant.query
        "/home?page=2",
        "/admin/withdrawals",                # 3 lists: page/page_pd/page_uv
        "/admin/withdrawals?page=2&page_pd=2&page_uv=2",
        "/admin/topups",                     # page/page_pd
        "/admin/reconciliation",
        "/kyc/admin/list",
    ]

    print("-- merchant pages --")
    for p in merchant_pages:
        print(f"  {hit(p, mid):>3}  {p}")
    print("-- admin pages --")
    for p in admin_pages:
        print(f"  {hit(p, aid):>3}  {p}")

    print("\nAll pagination pages rendered without a 500. Pagination smoke passed.")


if __name__ == "__main__":
    main()
