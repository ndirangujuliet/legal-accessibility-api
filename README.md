# Haki Legal Aid — USSD/SMS Civic-Tech App

Makes legal rights information and case reporting accessible over **USSD and SMS**,
so it works on any phone — not just smartphones. Built on Flask + Africa's Talking.

## What it does

- **USSD menu** (`/ussd`): "Know Your Rights", "Legal Aid Hotline", "Report a Case",
  "Track My Case" — fully menu-driven, no smartphone or data needed.
- **SMS confirmations**: after key actions (viewing rights info, getting the hotline
  number, submitting a report), the user gets a follow-up SMS with the full detail
  and their case tracking code.
- **Anonymous-ish case reporting**: reports are stored without the reporter's raw
  phone number — only a one-way hash of it (see `_pseudonymise` in `app.py`).
- **Case tracking**: users can dial back in and check status with their tracking code.
- **Admin extension point** (`/admin/cases/<code>/status`): example of how a
  caseworker dashboard would update case status (unauthenticated in this demo —
  **do not ship that part as-is**, see Production Notes below).

## 1. Setup

```bash
python -m venv venv
source venv/Scripts/activate      # Windows Git Bash
# or: .\venv\Scripts\Activate.ps1  # Windows PowerShell

pip install -r requirements.txt
cp .env.example .env
```

Get free sandbox credentials at [account.africastalking.com](https://account.africastalking.com):
1. Sign up / log in.
2. Go to **Sandbox** (top left switcher).
3. **Settings > API Key** — generate one, paste it into `.env` as `AT_API_KEY`.
4. Your sandbox username is always literally `sandbox` — not your real username.

Load the `.env` file (either `python-dotenv` in code, or just `export` the vars
manually before running, e.g. `export AT_API_KEY=xxxx` in Git Bash).

## 2. Run it locally

```bash
python app.py
```

This starts Flask on `http://127.0.0.1:5000`. But Africa's Talking needs a
**public URL** to send USSD callbacks to — your laptop's `localhost` isn't
reachable from their servers. Use a tunnel for local testing:

```bash
# using ngrok (or cloudflared, localtunnel, etc.)
ngrok http 5000
```

Copy the `https://xxxx.ngrok-free.app` URL it gives you.

## 3. Wire it up in the Africa's Talking sandbox

1. In the sandbox dashboard, go to **USSD**.
2. Create a channel, set the callback URL to `https://xxxx.ngrok-free.app/ussd`.
3. Under **Sandbox > Simulator**, use the built-in simulator (enter a test
   phone number) to dial your shortcode and walk through the menu.
4. SMS sent by the app will show up in the sandbox's **SMS logs**, since
   sandbox SMS doesn't actually deliver to real phones.

SMS log records created by this app are available at `/admin/sms/logs`. On
Render, attach a persistent disk and set `SMS_DB_PATH` to its mount path,
for example `/var/data/sms_logs.db`; otherwise SQLite data is lost on a
redeploy or service restart.

## 4. Testing the flow

Try walking through:
- `1` → `1` → see arrest rights + confirm SMS logged in sandbox
- `2` → hotline number + SMS
- `3` → `1` → type a description → get a tracking code (e.g. `HK-3F9A2B`)
- `4` → enter that tracking code → see status "Received"
- POST to `/admin/cases/HK-3F9A2B/status` with `status=In+Progress` (e.g. via
  curl or Postman), then repeat step 4 — status should now show "In Progress"

## Production notes (before a real deployment)

This is hackathon-ready, not production-ready as-is. Before going live:

- **Persistent storage**: replace the in-memory `CASE_REPORTS` dict with a
  real database (SQLite for small scale, Postgres for anything serious).
  The in-memory version resets every time the server restarts.
- **Secure the admin endpoint**: `/admin/cases/<code>/status` currently has
  no authentication. Add an API key check or proper auth before deploying.
- **Real Sender ID**: sandbox SMS uses a shared sender; for production you
  need an approved Sender ID / shortcode from Africa's Talking (paid, and
  takes review time — start this early).
- **Salt secrecy**: set `PHONE_HASH_SALT` to a long random value and keep
  it out of git — if it leaks alongside the DB, the phone-number hashing
  stops protecting anyone.
- **Rate limiting**: add basic rate limiting on `/ussd` and `/admin/*` to
  prevent abuse/spam reports.
- **Run behind gunicorn**, not Flask's dev server:
  ```bash
  gunicorn -w 4 -b 0.0.0.0:8000 app:app
  ```
- **Legal review**: "anonymous reporting" claims should be reviewed by
  someone with legal/privacy expertise for your jurisdiction — code alone
  can't guarantee legal anonymity guarantees to end users.

## Extending further

- **Voice**: Africa's Talking also has a Voice API — you could add an IVR
  version of the same rights-education menu for users who prefer to listen.
- **Airtime**: could reward people who complete a "rights education" quiz
  with a small airtime top-up via the Airtime API, to drive engagement.
- **Insights/Chat**: could add a WhatsApp/Chat channel using the same
  underlying `_route()` logic, since it's already separated from the raw
  USSD Flask handler.
