"""Celery tasks: MTN MoMo status polling.

Replaces the daemon threads in rails_mtn_real.py and rails_mtn_disbursement.py.
Each poll attempt is a separate task execution — if the worker dies the task
is re-queued from Redis and retried automatically.

Max 18 retries × 5s countdown = 90 second window, matching the old thread logic.
"""
import base64
import requests
from flask import current_app
from ..celery_app import celery


def _mtn_token(base_url, product, subscription_key, api_user, api_key):
    basic = base64.b64encode(f"{api_user}:{api_key}".encode()).decode()
    resp = requests.post(
        f"{base_url}/{product}/token/",
        headers={
            "Authorization": f"Basic {basic}",
            "Ocp-Apim-Subscription-Key": subscription_key,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@celery.task(bind=True, max_retries=18, default_retry_delay=5, name="app.tasks.polling.poll_mtn_collection")
def poll_mtn_collection(self, txn_id: int, reference_id: str) -> None:
    """Poll MTN Collections for a single transaction. Retries every 5s up to 90s."""
    from ..services.orchestrator import complete_transaction

    cfg = current_app.config
    base_url     = cfg["MOMO_BASE_URL"]
    target_env   = cfg["MOMO_TARGET_ENV"]
    sub_key      = cfg["MOMO_SUBSCRIPTION_KEY"]
    api_user     = cfg["MOMO_API_USER"]
    api_key      = cfg["MOMO_API_KEY"]

    try:
        token = _mtn_token(base_url, "collection", sub_key, api_user, api_key)
        r = requests.get(
            f"{base_url}/collection/v1_0/requesttopay/{reference_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Ocp-Apim-Subscription-Key": sub_key,
                "X-Target-Environment": target_env,
            },
            timeout=15,
        )

        if r.status_code != 200:
            raise self.retry(countdown=5)

        status = (r.json().get("status") or "").upper()

        if status == "SUCCESSFUL":
            complete_transaction(txn_id, success=True,
                                 rail_reference=reference_id, reason=None)
            return

        if status == "FAILED":
            reason = (r.json().get("reason") or "failed").lower()
            complete_transaction(txn_id, success=False,
                                 rail_reference=reference_id, reason=reason)
            return

        # PENDING — retry
        raise self.retry(countdown=5)

    except self.MaxRetriesExceededError:
        # MTN still says PENDING after 90s. The customer may approve at 91s —
        # terminally failing here while the requesttopay is live meant "money
        # taken, no credit": MTN's later success callback hit a FAILED txn and
        # the terminal-status guard (correctly) ignored it. Leave the txn
        # AUTHORIZED; the inbound webhook or the hourly stale sweep resolves
        # it from MTN's own answer.
        print(f"poll timeout for txn {txn_id} — leaving AUTHORIZED for webhook/sweep")

    except requests.RequestException as exc:
        raise self.retry(exc=exc, countdown=5)


@celery.task(bind=True, max_retries=18, default_retry_delay=5, name="app.tasks.polling.poll_mtn_disbursement")
def poll_mtn_disbursement(self, payout_id: int, reference_id: str) -> None:
    """Poll MTN Disbursements for a single payout. Retries every 5s up to 90s."""
    from ..services.payouts import complete_payout

    cfg = current_app.config
    base_url   = cfg["MOMO_BASE_URL"]
    target_env = cfg["MOMO_TARGET_ENV"]
    sub_key    = cfg["MOMO_DISBURSEMENT_SUBSCRIPTION_KEY"]
    api_user   = cfg["MOMO_DISBURSEMENT_API_USER"]
    api_key    = cfg["MOMO_DISBURSEMENT_API_KEY"]

    try:
        token = _mtn_token(base_url, "disbursement", sub_key, api_user, api_key)
        r = requests.get(
            f"{base_url}/disbursement/v1_0/transfer/{reference_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Ocp-Apim-Subscription-Key": sub_key,
                "X-Target-Environment": target_env,
            },
            timeout=15,
        )

        if r.status_code != 200:
            raise self.retry(countdown=5)

        status = (r.json().get("status") or "").upper()

        if status == "SUCCESSFUL":
            complete_payout(payout_id, success=True,
                            rail_reference=reference_id, reason=None)
            return

        if status == "FAILED":
            reason = (r.json().get("reason") or "failed").lower()
            complete_payout(payout_id, success=False,
                            rail_reference=reference_id, reason=reason)
            return

        raise self.retry(countdown=5)

    except self.MaxRetriesExceededError:
        # CRITICAL not to fail-and-refund here: complete_payout(success=False)
        # returns amount+fee to the merchant, but MTN still says PENDING — the
        # disbursement can DELIVER at 91s. That would be money delivered AND
        # refunded (paid twice). Leave the payout AUTHORIZED; `flask
        # stranded-payouts` lists it and reconciliation resolves it against
        # MTN's own final answer.
        print(f"disbursement poll timeout for payout {payout_id} — leaving for reconciliation")

    except requests.RequestException as exc:
        raise self.retry(exc=exc, countdown=5)
