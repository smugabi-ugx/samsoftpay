"""Admin hard-delete must never wipe an admin or a protected account (security-5).

The destructive POST protected a SMALLER email set than the confirm page and
protected no admin by role, so the primary admin + all ledger data could be
POST-deleted. Now one shared _is_protected_merchant guard (role-first) gates both.

What this proves:
  1. An admin-role merchant cannot be hard-deleted (still exists after POST).
  2. A merchant matching ADMIN_EMAIL cannot be deleted.
  3. A plain test merchant CAN still be deleted (the tool still works).
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MOMO_USE_REAL"] = "0"
os.environ["ADMIN_EMAIL"] = "boss@samsoftpay.test"
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="admindel_")
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
from app.models import Merchant

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    from werkzeug.security import generate_password_hash
    with app.app_context():
        db.create_all()
        admin = Merchant(name="Admin", email="admin@x.com", public_key="pk_a",
                         secret_key="sk_a", role="admin", kyc_status="verified",
                         password_hash=generate_password_hash("x"), handle="admin")
        boss = Merchant(name="Boss", email="boss@samsoftpay.test", public_key="pk_b",
                        secret_key="sk_b", kyc_status="verified",
                        password_hash=generate_password_hash("x"), handle="boss")
        victim = Merchant(name="Throwaway", email="temp@x.com", public_key="pk_v",
                          secret_key="sk_v", kyc_status="pending",
                          password_hash=generate_password_hash("x"), handle="temp")
        db.session.add_all([admin, boss, victim])
        db.session.commit()
        admin_id, boss_id, victim_id = admin.id, boss.id, victim.id

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)   # act as the admin
        sess["_fresh"] = True

    def delete(mid):
        return c.post(f"/admin/merchants/{mid}/delete", follow_redirects=False)

    delete(admin_id)
    with app.app_context():
        check("admin-role merchant NOT deleted", db.session.get(Merchant, admin_id) is not None)

    delete(boss_id)
    with app.app_context():
        check("ADMIN_EMAIL merchant NOT deleted", db.session.get(Merchant, boss_id) is not None)

    delete(victim_id)
    with app.app_context():
        check("plain test merchant IS deleted (tool still works)",
              db.session.get(Merchant, victim_id) is None)

    failed = [lbl for lbl, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL ADMIN-DELETE-GUARD TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
