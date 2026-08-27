# Samsoftpay — Go-Live Config Runbook

> The code is done. This is the **config/accounts checklist to take real money and actually
> send receipts + alerts.** Everything here is set on **Render → each service → Environment**
> (web, worker AND beat — env vars are per-service on Render), plus three external accounts.
>
> **Verify every step with one URL:** `https://api.samsoftpay.com/ops/readiness` returns each item
> as a boolean and a single `go_live_ready` flag. `ops/status` shows the rail + worker liveness.

---

## 0. Where things stand right now (live, as of this runbook)
- `mtn_rail: real`, `momo_target_env: **sandbox**`, MTN collections+disbursement creds present.
- Worker up (heartbeat fresh). Web + worker + beat deployed.
- **Email, SMS, Slack alerts, Sentry: NOT configured** (receipts/OTP print to the console;
  no tax-receipt SMS; a stuck-money alert goes nowhere).
- So today the platform *works* end-to-end on **MTN sandbox**, but sends nothing and pages no one.

`go_live_ready` will flip to `true` once §1, §2 and §4 below are done.

---

## 1. Email — Resend (receipts, OTP, tax receipts, alerts) — **REQUIRED**
The whole notification layer is built (`email_service.send_email`, `receipts.py`); it's a no-op
until `MAIL_HOST` is set. Resend exposes standard SMTP, so no code change.

1. Create an account at **resend.com**, add & **verify the `samsoftpay.com` domain** (DNS: SPF +
   DKIM records they give you — add in Namecheap). This is what stops receipts landing in spam.
2. Create an SMTP credential / API key.
3. Set on **web + worker + beat**:
   | Var | Value |
   |---|---|
   | `MAIL_HOST` | `smtp.resend.com` |
   | `MAIL_PORT` | `587` |
   | `MAIL_USERNAME` | `resend` |
   | `MAIL_PASSWORD` | `re_...` (the Resend API key) |
   | `MAIL_FROM` | `noreply@samsoftpay.com` (must be on the verified domain) |
4. Verify: `/ops/readiness` → `checks.email_configured: true`. Then trigger a signup OTP and
   confirm the email arrives (not the console).

> Alternative: any SMTP provider (SendGrid, Mailgun, Gmail SMTP for low volume). Same 5 vars.

## 2. SMS — Africa's Talking (customer tax receipts by SMS) — **REQUIRED for the URA receipt**
`sms_service.send_sms` is built; it's a no-op until `AT_API_KEY` is set. The tax receipt goes to
the final consumer by **both** email and SMS (`receipts.py`), so SMS closes the URA requirement
for phone-only customers.

1. Create an **africastalking.com** account, top up, and (for live) request an **alphanumeric
   Sender ID / short code** for Uganda (`SAMSOFTPAY`). Sandbox works immediately for testing.
2. Set on **web + worker + beat**:
   | Var | Value |
   |---|---|
   | `AT_API_KEY` | your Africa's Talking API key |
   | `AT_USERNAME` | `sandbox` for testing, your app username for live |
   | `SMS_SENDER_ID` | `SAMSOFTPAY` (once approved) — optional |
3. Verify: `/ops/readiness` → `checks.sms_configured: true`, then make a live-mode charge with a
   customer phone and confirm the receipt SMS arrives.

## 3. On-call alerts + error tracking — **STRONGLY RECOMMENDED**
`alerts.send_alert` routes stuck-money conditions to Slack + email + Sentry and **fires from the
WORKER** (set these on the worker/beat too, not just web). Without them a lost MTN callback or a
stranded payout sits silent.

| Var | Where | Value |
|---|---|---|
| `SLACK_WEBHOOK_URL` | worker + beat (+web) | Slack Incoming Webhook URL |
| `ALERT_EMAIL` | worker + beat (+web) | your on-call inbox (falls back to `ADMIN_EMAIL`) |
| `SENTRY_DSN` | all three | from sentry.io project (optional but cheap insurance) |

Verify: `/ops/readiness` → `alerts_slack_configured` / `alerts_email_configured` /
`sentry_configured: true`. Point an **external uptime monitor** (UptimeRobot) at
`https://api.samsoftpay.com/ops/status` — it 503s if the worker dies (the worker can't alert on
its own death).

## 4. MTN production rail — **REQUIRED to take real money** (institutional, not a code change)
Code fixes for production are already in (MSISDN normalize, payeeNote sanitize — see
`memory: mtn-production-golive`). Remaining is MTN onboarding + swapping keys.

1. Finish MTN onboarding with Felix Oluka (SIT sheet + Web Access Form + Email form). Papers PAID.
2. Once granted production credentials, set on **web + worker + beat**:
   | Var | Value |
   |---|---|
   | `MOMO_USE_REAL` | `1` |
   | `MOMO_BASE_URL` | `https://proxy.momoapi.mtn.com` (production) |
   | `MOMO_TARGET_ENV` | `production` |
   | `MOMO_CURRENCY` | `UGX` |
   | `MOMO_SUBSCRIPTION_KEY`, `MOMO_API_USER`, `MOMO_API_KEY` | production collections creds |
   | `MOMO_DISBURSEMENT_SUBSCRIPTION_KEY`, `..._API_USER`, `..._API_KEY` | production disbursement creds |
3. Verify: `/ops/status` → `momo_target_env: "production"`, `mtn_rail: "real"`, both creds present.
   Do a **small real charge** to your own MTN number and confirm it settles + a receipt sends.

## 5. Data durability — **REQUIRED before real volume**
- Move off the **free Render Postgres** (expires ~90 days) to a **paid instance with automated
  backups**. This is a money ledger — a lost DB is lost money records.
- Confirm `flask db current` == the migration head in CLAUDE.md after the next deploy.

---

## One-glance verification after each step
```bash
curl -s https://api.samsoftpay.com/ops/readiness | jq
curl -s https://api.samsoftpay.com/ops/status    | jq
```
`go_live_ready: true` means: production + base URL + webhook secret + MTN real/production creds +
email are all set. SMS and alerts are tracked separately in `checks` — turn them on too.

## What is deliberately NOT a launch blocker
Load test (run against staging), BOU PSP licence + EFRIS/URA registration (institutional track,
in parallel), Shopify/Wix approval, Binance/forex rails. None of these block taking MTN money.
