"""Airtel Money rail SCAFFOLD - selection, gating and safety (no live Airtel).

The real adapters (rails_airtel_real / rails_airtel_disbursement) are not yet
certified against Airtel, so this test never makes a real Airtel call. It proves
the SAFE contract instead:

  [1] Default (AIRTEL_USE_REAL off): a live Airtel charge resolves to the MOCK,
      so is_simulated(AIRTEL) is True and the production guard still refuses it
      (guardrail 14) - merging the scaffold changes nothing in prod.
  [2] A sandbox key always gets the mock, even with the real flag on.
  [3] The REAL collections adapter is selected ONLY when AIRTEL_USE_REAL is on
      AND credentials are present AND the key is live; then is_simulated is False
      (the guard opens by itself, as designed).
  [4] Missing creds with the flag on -> still the mock (fails safe).
  [5] Payout gating mirrors MTN: sandbox -> mock; production with no real rail
      -> rejected with zero writes (guardrail 13); production + real -> the real
      adapter.
  [6] The real disbursement adapter REFUSES to send without an encrypted PIN
      (no malformed/garbage transfer).
  [7] MTN is unaffected by any of this (regression).
"""
import atexit
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MOMO_USE_REAL"] = "0"
os.environ.pop("AIRTEL_USE_REAL", None)
_FD, _P = tempfile.mkstemp(suffix=".db", prefix="airtel_scaffold_")
os.close(_FD)
os.environ["DATABASE_URL"] = "sqlite:///" + _P.replace("\\", "/")


@atexit.register
def _c():
    try:
        os.unlink(_P)
    except OSError:
        pass


from app import create_app
from app.extensions import db
from app.models import Channel, Payout, PayoutStatus
from app.services.rails import get_adapter, is_simulated, _MockRail
from app.services.rails_airtel_real import RealAirtelMoneyAdapter
from app.services.payouts import _get_disbursement_adapter, _MockDisbursementAdapter, PayoutError
from app.services.rails_airtel_disbursement import RealAirtelDisbursementAdapter, AirtelNotCertifiedError

CHECKS = []


def check(label, cond):
    CHECKS.append((label, bool(cond)))
    print(("[ok]   " if cond else "[FAIL] ") + label)


def main():
    app = create_app({"WTF_CSRF_ENABLED": False})
    from flask import g
    with app.app_context():
        db.create_all()

    def set_cfg(**kw):
        for k, v in kw.items():
            app.config[k] = v

    # [1] default off -> live Airtel is the mock (refused in prod)
    with app.app_context():
        g.api_mode = "live"
        set_cfg(AIRTEL_USE_REAL=False, AIRTEL_CLIENT_ID="", AIRTEL_CLIENT_SECRET="")
        check("[1] live Airtel defaults to the mock adapter",
              isinstance(get_adapter(Channel.AIRTEL_MONEY), _MockRail))
        check("[1] is_simulated(AIRTEL) is True by default (guard refuses it)",
              is_simulated(Channel.AIRTEL_MONEY) is True)

    # [2] sandbox always mock, even with the real flag on
    with app.app_context():
        g.api_mode = "test"
        set_cfg(AIRTEL_USE_REAL=True, AIRTEL_CLIENT_ID="cid", AIRTEL_CLIENT_SECRET="sec")
        check("[2] sandbox key -> mock even with AIRTEL_USE_REAL on",
              isinstance(get_adapter(Channel.AIRTEL_MONEY), _MockRail))

    # [3] real adapter only when flag + creds + live
    with app.app_context():
        g.api_mode = "live"
        set_cfg(AIRTEL_USE_REAL=True, AIRTEL_CLIENT_ID="cid", AIRTEL_CLIENT_SECRET="sec",
                AIRTEL_BASE_URL="https://openapiuat.airtel.africa")
        check("[3] live + flag + creds -> RealAirtelMoneyAdapter",
              isinstance(get_adapter(Channel.AIRTEL_MONEY), RealAirtelMoneyAdapter))
        check("[3] is_simulated(AIRTEL) now False (guard opens on its own)",
              is_simulated(Channel.AIRTEL_MONEY) is False)

    # [4] flag on but no creds -> still mock (fail safe)
    with app.app_context():
        g.api_mode = "live"
        set_cfg(AIRTEL_USE_REAL=True, AIRTEL_CLIENT_ID="", AIRTEL_CLIENT_SECRET="")
        check("[4] flag on but missing creds -> stays mock",
              isinstance(get_adapter(Channel.AIRTEL_MONEY), _MockRail))

    # [5] payout gating
    with app.app_context():
        g.api_mode = "test"
        set_cfg(AIRTEL_USE_REAL=False)
        d = _get_disbursement_adapter(Channel.AIRTEL_MONEY)
        check("[5] sandbox Airtel payout -> mock disbursement (Airtel channel)",
              isinstance(d, _MockDisbursementAdapter) and d.channel == Channel.AIRTEL_MONEY)
    with app.app_context():
        g.api_mode = "live"
        set_cfg(AIRTEL_USE_REAL=False)
        try:
            _get_disbursement_adapter(Channel.AIRTEL_MONEY)
            check("[5] prod Airtel payout, no real rail -> rejected", False)
        except PayoutError:
            check("[5] prod Airtel payout, no real rail -> rejected (guardrail 13)", True)
    with app.app_context():
        g.api_mode = "live"
        set_cfg(AIRTEL_USE_REAL=True, AIRTEL_CLIENT_ID="cid")
        check("[5] prod + real -> RealAirtelDisbursementAdapter",
              isinstance(_get_disbursement_adapter(Channel.AIRTEL_MONEY), RealAirtelDisbursementAdapter))

    # [6] real disbursement refuses without an encrypted PIN
    with app.app_context():
        set_cfg(AIRTEL_DISBURSEMENT_PIN="")
        adapter = RealAirtelDisbursementAdapter()
        p = Payout(public_id="pout_air1", merchant_id=1, amount=1000, currency="UGX",
                   recipient_phone="256751234567", channel=Channel.AIRTEL_MONEY,
                   status=PayoutStatus.PENDING, is_test=False)
        try:
            adapter.initiate(p)
            check("[6] real disbursement without PIN refuses", False)
        except AirtelNotCertifiedError:
            check("[6] real disbursement without encrypted PIN refuses loudly (no wire)", True)

    # [7] MTN unaffected
    with app.app_context():
        g.api_mode = "live"
        set_cfg(MOMO_USE_REAL=False)
        check("[7] MTN still mock when MOMO_USE_REAL off",
              isinstance(get_adapter(Channel.MTN_MOMO), _MockRail))
        d = _get_disbursement_adapter(Channel.MTN_MOMO)
        check("[7] MTN disbursement still mock (MTN channel)",
              isinstance(d, _MockDisbursementAdapter) and d.channel == Channel.MTN_MOMO)

    print()
    failed = [lbl for lbl, ok in CHECKS if not ok]
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}: " + "; ".join(failed))
        sys.exit(1)
    print(f"ALL AIRTEL SCAFFOLD TESTS PASSED ({len(CHECKS)}/{len(CHECKS)})")


if __name__ == "__main__":
    main()
