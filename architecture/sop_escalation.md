# SOP: Escalation — Maya 2.0

## Escalation Triggers
| Trigger | Action |
|---------|--------|
| Customer asks for manager / human | Ask consent → live transfer |
| Off-menu question Maya can't answer | Ask consent → live transfer |
| Complaint | Ask consent → live transfer immediately |
| Order total > $150 (catering) | Catering flow (collect callback info, not full order) |
| Call exceeds 4 minutes | Automatic timeout → give manager number twice → transfer |
| Claude API error | Emergency message + manager number + hang up |
| Low-confidence transcript (2 strikes) | Re-prompt, then graceful exit |

## Two-Step Transfer Protocol
1. Maya asks: *"Of course! Would you like me to connect you to our manager right now?"*
2. Customer says yes → `action: escalate` → live `<Dial>` transfer
3. Customer says no → `action: continue` → resume order flow

This prevents accidental transfers on misheard requests.

## Partial Order on Escalation
If the customer had items in their cart when escalated:
- Order is submitted with `status: escalated`
- Shows in dashboard with red "Escalated" badge
- Manager has the context to continue the conversation

## Manager Phone Format
Always read digit-by-digit for TTS clarity:
- `6692489997` → "6, 6, 9 — 2, 4, 8 — 9, 9, 9, 7"
- Read twice on timeout

## Catering Escalation
- Triggered when subtotal exceeds `catering_threshold.min_dollars` (default $150)
- Maya collects: name, phone, event date, headcount
- Does NOT take full item list
- Submitted as `order_type: catering`, `status: pending_callback`
- Restaurant calls back to finalize the order
