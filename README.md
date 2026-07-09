# Maya 2.0 — AI Voice Receptionist for Restaurants

Maya answers every inbound restaurant call, takes full orders by voice, and delivers confirmed orders to your kitchen — zero missed calls.

---

## What's Included

| Component | Description |
|-----------|-------------|
| `main.py` | FastAPI server — all webhooks, APIs, dashboard |
| `execution/conversation.py` | Claude AI conversation engine |
| `execution/audio_pipeline.py` | ElevenLabs TTS + Deepgram STT |
| `execution/delivery.py` | Order delivery — SQLite → Airtable + Email + Slack |
| `execution/menu_parser.py` | Menu upload, validation, storage |
| `execution/billing.py` | Stripe $500/month subscriptions |
| `execution/order_store.py` | SQLite database (primary order store) |
| `dashboard/index.html` | Live order dashboard with status management |
| `dashboard/staff.html` | Employee dashboard — Orders (online+called-in) + In-Person (Active/Register) |
| `dashboard/onboard.html` | Restaurant self-serve signup page |
| `execution/square_payments.py` | Square payment integration seam (**stubbed**, see below) |
| `sample_menu.json` | Ready-to-upload test menu |

---

## Deploy to Railway (Step-by-Step)

### Step 1 — Push to GitHub

```bash
# From Blueprint_Ai root
git add maya2.0/
git commit -m "feat: Maya 2.0 production-ready voice agent"
git push origin main
```

### Step 2 — Create Railway Service

1. Go to [railway.app](https://railway.app) → New Project
2. Select **Deploy from GitHub repo**
3. Choose your `Blueprint_Ai` repo
4. Under **Settings → Source**: set **Root Directory** to `maya2.0`
5. Railway auto-detects Python via Nixpacks and uses `Procfile`

### Step 3 — Set Environment Variables

In Railway → your service → **Variables**, add these (copy from `.env.example`):

**Required:**
```
BASE_URL          = https://your-app.up.railway.app   ← set AFTER deploy
ANTHROPIC_API_KEY = sk-ant-...
TWILIO_ACCOUNT_SID = AC...
TWILIO_AUTH_TOKEN  = ...
```

**Voice (pick one):**
```
USE_ELEVENLABS       = true          ← ElevenLabs (better voice)
ELEVENLABS_API_KEY   = ...
ELEVENLABS_VOICE_ID  = 21m00Tcm4TlvDq8ikWAM   ← Rachel voice
```
OR
```
USE_ELEVENLABS = false               ← Twilio Polly (lower latency)
```

**Optional but recommended:**
```
SENDGRID_API_KEY           = SG...
ORDER_NOTIFICATION_EMAIL   = orders@yourrestaurant.com
SENDGRID_FROM_EMAIL        = noreply@yourdomain.com
SLACK_BOT_TOKEN            = xoxb-...
SLACK_ORDERS_CHANNEL_ID    = C...
AIRTABLE_API_KEY           = ...
AIRTABLE_BASE_ID           = app...
```

**Billing (when you're ready to charge):**
```
STRIPE_SECRET_KEY    = sk_live_...
STRIPE_WEBHOOK_SECRET = whsec_...
STRIPE_PRICE_ID      = price_...
```

### Step 4 — Get Your Railway URL

After first deploy, copy your Railway URL (e.g. `https://maya2.up.railway.app`).
Go back to Variables and set:
```
BASE_URL = https://maya2.up.railway.app
```
Then **redeploy** so the URL is baked in.

### Step 5 — Configure Twilio

1. Go to [console.twilio.com](https://console.twilio.com) → Phone Numbers → Manage → Active Numbers
2. Click the phone number you want Maya to answer
3. Under **Voice & Fax → A CALL COMES IN**:
   - Set to **Webhook**
   - URL: `https://your-app.up.railway.app/twilio/voice`
   - Method: **HTTP POST**
4. Save

### Step 6 — Upload Your Menu

```bash
curl -X POST https://your-app.up.railway.app/menu/upload \
  -F "file=@sample_menu.json"
```

Or use the dashboard at `https://your-app.up.railway.app/onboard`

### Step 7 — Test It

Call your Twilio number. Maya answers.

Check the dashboard at `https://your-app.up.railway.app/` to see orders in real time.

---

## Multi-Restaurant Setup

To serve multiple restaurants from one deployment:

1. Each restaurant registers at `/onboard` with their unique `restaurant_id` and `twilio_phone`
2. Uploads their menu JSON with their `twilio_phone` set
3. Points their Twilio number to the same webhook URL: `https://your-app.up.railway.app/twilio/voice`
4. Maya automatically routes each call to the right restaurant based on the number called

---

## Key API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Live order dashboard |
| `/onboard` | GET | Restaurant signup page |
| `/health` | GET | System health check (all integrations) |
| `/twilio/voice` | POST | Twilio inbound call webhook |
| `/twilio/gather` | POST | Twilio speech input webhook |
| `/menu/upload` | POST | Upload restaurant menu JSON |
| `/menu/{id}` | GET | Get restaurant menu |
| `/menus` | GET | List all restaurants |
| `/api/orders` | GET | Fetch orders (filter: `?status=confirmed`) |
| `/api/orders/{id}/status` | PATCH | Update order status |
| `/api/orders/manual` | POST | Staff-entered order (In-Person Register or a manually logged Phone order) |
| `/api/payment/square/charge` | POST | Charge an order via Square — **stubbed**, mocks a successful charge |
| `/ws/dashboard` | WS | Real-time order stream |
| `/staff` | GET | Employee dashboard — Orders + In-Person |
| `/subscribe` | POST | Create Stripe checkout session |

---

## Employee Dashboard (`/staff`)

Two pages behind one persistent top nav, no reloads. The only state that matters anywhere in this dashboard is **paid vs. unpaid** — derived from the existing `payment_status` column (`unpaid` vs. any `paid_*` value). The underlying `status` column (`confirmed`/`in_prep`/etc.) is untouched server-side since `kitchen.html`/`index.html` still read it, but `/staff` doesn't surface it.

- **Orders** — read-only feed of every order from the website, chatbot, voice agent, and manually-logged phone call-ins, each tagged with a source badge. Filter pills: **Unpaid** / **Paid**. There is no order-creation affordance here — orders only arrive automatically from those four channels.
- **In-Person** — **Active Orders** (a single list of *paid* walk-in orders only — unpaid ones aren't persisted as visible orders yet, see below) and **Register** (search + category-dropdown item picker, modifiers, quantities, running total).

Every order card on the Orders page and in Active Orders has a **Pay** button (hidden once paid). Tapping it opens a Square checkout modal; on confirmation the order's `payment_status` flips to `paid_square` and the card updates live via the existing `/ws/dashboard` WebSocket — no polling.

In Register, "Charge & Place Order" creates the order (`POST /api/orders/manual`) and immediately opens the same Square checkout modal for it — the cart itself *is* the "order being built" state, so an unpaid in-person order is never something staff can see or act on outside of Register. (Edge case: if the Square charge fails after order creation, that order is created but stays invisible until charged — a known, low-blast-radius gap given the stub has no real failure modes yet.)

Gated by `DASHBOARD_PASSWORD` the same way `/`, `/kitchen`, `/admin`, and `/revenue` are.

### What's stubbed — Square payments

`execution/square_payments.py` → `process_square_payment(order)` **always mocks a successful charge** — there is no real Square account wired up yet. It's the single seam every payment call goes through, so swapping in the real integration later doesn't require touching `main.py` or the dashboard:

1. Add `SQUARE_ACCESS_TOKEN` and `SQUARE_LOCATION_ID` to Railway env vars.
2. On the client, integrate the Square **Web Payments SDK** to tokenize the card and get a `source_id` (the current UI's "Confirm Payment" step is where that card-entry flow would live).
3. On the server, replace the mocked block in `process_square_payment()` with a real call to Square's **Payments API** `CreatePayment` using that `source_id`.
4. Return the real payment id/status instead of the `sq_mock_...` placeholder.

Everything else — the `orders` table, the `/api/payment/square/charge` endpoint, and the dashboard's Pay button/paid-state transition — already works end-to-end against the stub, so this is a drop-in swap.

Note: Maya already has a separate, **real** (non-stubbed) Stripe payment flow (`execution/payments.py` — Terminal charge, Payment Link SMS, Cash) used by `dashboard/index.html`. It's untouched by this change; the `/staff` dashboard's Pay button is Square-only by design.

---

## Kitchen Display System (`/kds`)

Two pages, same top-bar pattern as `/staff`:

- **In Progress** (default) — every order not yet completed, **paid and unpaid both** (unlike `/staff`'s New Orders queue, payment status never gates visibility here — a Paid/Unpaid badge is shown on every card instead). Sorted by a unified due time: orders with a specific pickup time sort by that time; `ASAP` orders get a computed due time of `order timestamp + estimated_prep_minutes`, so they naturally interleave with scheduled pickups and overdue ASAP orders float to the top (FIFO). Cards get an amber ring within 5 minutes of due and a red ring once overdue.
- **Completed** — most recently completed first, with a small "Move back to In Progress" undo.

**Data source:** reads the same `orders` table as `/staff`, via the same `/ws/dashboard` WebSocket (`init`/`new_order`/`status_update`/`payment_update` events) — no new table, no polling, no duplicate order records.

**What marks "Completed":** the existing `status` column, set to `picked_up` — the same value `dashboard/kitchen.html` already used for its own "done" state, via the existing `PATCH /api/orders/{id}/status` endpoint. No new column or endpoint was added. `/staff` doesn't read `status` at all (it only cares about `payment_status`), so the two pages don't step on each other; `/kds` and the original `/kitchen` do share that field, and both react live to the same `status_update` broadcast.

Gated by `DASHBOARD_PASSWORD` like the other internal dashboard pages.

---

## Local Development

```bash
cd maya2.0
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Copy and fill in your keys
cp .env.example .env

# Run
python main.py
```

For Twilio webhooks locally, use [ngrok](https://ngrok.com):
```bash
ngrok http 8000
# Set BASE_URL=https://xxxx.ngrok.io in .env
# Set Twilio webhook to https://xxxx.ngrok.io/twilio/voice
# Set VALIDATE_TWILIO_SIG=false for local dev
```

---

## Stripe Setup (When Ready to Charge)

1. Stripe Dashboard → Products → Create Product: "Maya 2.0" at $500/month
2. Copy the **Price ID** (`price_...`) → set `STRIPE_PRICE_ID`
3. Stripe Dashboard → Webhooks → Add endpoint:
   - URL: `https://your-app.up.railway.app/billing/webhook`
   - Events: `customer.subscription.created`, `customer.subscription.deleted`, `invoice.payment_succeeded`, `invoice.payment_failed`
4. Copy **Signing Secret** → set `STRIPE_WEBHOOK_SECRET`

---

## Architecture

```
Caller
  │
  ▼
Twilio (phone number + webhook)
  │
  ▼
Maya 2.0 FastAPI Server (Railway)
  │
  ├── Deepgram (optional language detect)
  ├── Claude claude-sonnet-4-6 (order understanding)
  ├── ElevenLabs (voice synthesis) or Twilio Polly (fallback)
  │
  ▼ Order confirmed
  │
  ├── SQLite (primary, always)
  ├── Airtable (optional backup)
  ├── SendGrid Email (optional)
  ├── Slack (optional)
  └── Dashboard WebSocket (live)
```
