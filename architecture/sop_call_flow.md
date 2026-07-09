# SOP: Call Flow — Maya 2.0

## Overview
Every inbound restaurant call follows this exact path. No exceptions.

## Step-by-Step

### 1. Call Arrives
- Twilio receives inbound call on the restaurant's number
- Twilio POSTs to `POST /twilio/voice`
- Maya validates the Twilio signature (rejects spoofed requests)
- Maya resolves which restaurant this number belongs to (`load_menu_by_phone`)
- If no match: falls back to `RESTAURANT_ID` env var

### 2. Hours Check
- `is_restaurant_open(config)` checks current time against restaurant hours
- **Closed:** Maya reads the hours aloud, gives manager number, hangs up
- **Open:** Proceed to greeting

### 3. Greeting
- Pre-warmed ElevenLabs audio plays inside `<Gather>` (barge-in enabled)
- Polly `<Say>` fallback if ElevenLabs is unavailable or too slow (>4s)
- `<Gather>` listens for speech with `speech_timeout=2`, `speech_model=phone_call`
- 4-minute call timer starts (`asyncio.create_task`)

### 4. Gather Loop
- Each customer utterance POSTs to `POST /twilio/gather`
- **Confidence < 0.55:** ask to repeat
- **Empty transcript:** re-prompt
- **Valid transcript:** pass to `process_turn(call_sid, transcript, config)`

### 5. Claude Processing
- `conversation.py` builds the system prompt with full menu + current call state
- Claude returns JSON: `{speech, action, item, customer_field, customer_value}`
- State mutated: items added, subtotal updated, customer fields collected
- History trimmed to last 20 messages (prevent context overflow)

### 6. Response Actions
| Action | Behavior |
|--------|---------|
| `continue` | Say response inside next `<Gather>` |
| `add_item` | Add item to state, say response, continue |
| `upsell` | Mark upsell attempted, say response, continue |
| `readback` | Read full order back, continue to collect_info |
| `collect_info` | Store customer field, continue |
| `submit` | Verify all 3 fields, submit order, hang up |
| `escalate_ask` | Ask consent to transfer, continue |
| `escalate` | Transfer live call to manager via `<Dial>` |
| `catering` | Collect name/phone/date/headcount, submit callback, hang up |
| `end` | Say goodbye, hang up |

### 7. Order Submission
- `_submit_order()` builds full order payload
- `deliver_order()` writes to SQLite first, then fans out to Airtable + Email + Slack + Dashboard
- Call state cleared

### 8. 4-Minute Timeout
- Timer fires at exactly 240 seconds
- Maya gives manager number twice
- Live call transferred to manager via `<Dial>`
- Partial order (if any items) submitted as `escalated`

### 9. Silence Handling
- First silence: re-prompt once
- Second silence: graceful goodbye, hang up
