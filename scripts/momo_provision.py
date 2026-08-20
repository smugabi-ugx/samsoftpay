"""Create an MTN MoMo API User + API Key for one product subscription.

WHY THIS EXISTS
---------------
The MTN developer portal gives you, per product (Collections, Disbursements),
a PRIMARY KEY and a SECONDARY KEY. Those are the *subscription key* only —
the `Ocp-Apim-Subscription-Key` header. To actually call the API you also need
an **API User** (a UUID you choose) and an **API Key** (MTN generates it for
that user). This script creates both for a given subscription key.

USAGE (run once per product, from the repo root):

    # Collections product -> use its PRIMARY key
    py -3.10 scripts/momo_provision.py --subscription-key <COLLECTIONS_PRIMARY_KEY>

    # Disbursements product -> use its PRIMARY key
    py -3.10 scripts/momo_provision.py --subscription-key <DISBURSEMENT_PRIMARY_KEY>

Optional:
    --base-url   default https://sandbox.momodeveloper.mtn.com
                 (production: https://proxy.momoapi.mtn.com)
    --callback   provider callback host, default samsoftpay.com

It prints the three values to paste into Render. Nothing is written to disk,
and the API key is shown once — copy it immediately.

NOTE: the sandbox provisioning endpoints (/v1_0/apiuser) exist on the SANDBOX
host. In production MTN issues the API User/Key through onboarding; if your
production subscription supports self-provisioning, pass the production
--base-url.
"""
import argparse
import sys
import uuid

import requests


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subscription-key",
                    help="PRIMARY key of the product (Collections or Disbursements)")
    ap.add_argument("--from-env", metavar="NAME",
                    help="read the key from an environment variable instead of "
                         "typing it (e.g. --from-env MOMO_SUBSCRIPTION_KEY). "
                         "Avoids pasting a long key into a browser shell.")
    ap.add_argument("--base-url", default="https://sandbox.momodeveloper.mtn.com")
    ap.add_argument("--callback", default="samsoftpay.com",
                    help="providerCallbackHost (a bare host, no scheme/path)")
    args = ap.parse_args()

    import os
    sub = (args.subscription_key
           or (os.environ.get(args.from_env, "") if args.from_env else "")).strip()
    if not sub:
        # Last resort: ask interactively, so nothing long has to be pasted onto
        # the command line (some browser shells mangle long paste).
        try:
            sub = input("Paste the product PRIMARY key: ").strip()
        except EOFError:
            sub = ""
    if not sub:
        print("No subscription key given. Use --subscription-key, --from-env NAME, "
              "or paste it when prompted.")
        return 1

    base = args.base_url.rstrip("/")
    api_user = str(uuid.uuid4())
    headers = {"Ocp-Apim-Subscription-Key": sub, "Content-Type": "application/json"}

    print(f"Host          : {base}")
    print(f"API User (new): {api_user}")

    # 1. Create the API user.
    r = requests.post(
        f"{base}/v1_0/apiuser",
        headers={**headers, "X-Reference-Id": api_user},
        json={"providerCallbackHost": args.callback},
        timeout=30,
    )
    if r.status_code not in (201, 409):
        print(f"\nFAILED to create API user: HTTP {r.status_code}\n{r.text[:400]}")
        print("\nCommon causes: wrong subscription key, or this key belongs to a "
              "product on a different host than --base-url.")
        return 1
    print("API user created" if r.status_code == 201 else "API user already existed")

    # 2. Generate its API key (shown ONCE).
    r = requests.post(f"{base}/v1_0/apiuser/{api_user}/apikey",
                      headers=headers, timeout=30)
    if r.status_code != 201:
        print(f"\nFAILED to create API key: HTTP {r.status_code}\n{r.text[:400]}")
        return 1
    api_key = (r.json() or {}).get("apiKey", "")
    if not api_key:
        print(f"\nNo apiKey in response: {r.text[:400]}")
        return 1

    # 3. Read it back so you know the credential really exists.
    v = requests.get(f"{base}/v1_0/apiuser/{api_user}", headers=headers, timeout=30)
    print(f"Verified      : HTTP {v.status_code} {v.text[:120]}")

    print("\n" + "=" * 68)
    print("PASTE THESE INTO RENDER (choose the block for THIS product):")
    print("=" * 68)
    print("\n--- if this was the COLLECTIONS product ---")
    print(f"MOMO_SUBSCRIPTION_KEY={sub}")
    print(f"MOMO_API_USER={api_user}")
    print(f"MOMO_API_KEY={api_key}")
    print("\n--- if this was the DISBURSEMENTS product ---")
    print(f"MOMO_DISBURSEMENT_SUBSCRIPTION_KEY={sub}")
    print(f"MOMO_DISBURSEMENT_API_USER={api_user}")
    print(f"MOMO_DISBURSEMENT_API_KEY={api_key}")
    print("\nAlso required to switch the real rail on:")
    print("MOMO_USE_REAL=1")
    print(f"MOMO_BASE_URL={base}")
    print("MOMO_TARGET_ENV=sandbox        # production: mtnuganda")
    print("MOMO_CURRENCY=EUR              # production: UGX")
    print("\nSet them on the WEB, WORKER and BEAT services, then run:")
    print("    flask preflight")
    return 0


if __name__ == "__main__":
    sys.exit(main())
