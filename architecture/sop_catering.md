# SOP: Catering Flow — Maya 2.0

## Trigger
Order subtotal exceeds `catering_threshold.min_dollars` (default: $150).
This is detected automatically when Claude adds an item and the subtotal crosses the threshold.

## What Maya Collects (Catering Only)
1. Customer name
2. Customer phone number
3. Event date / desired delivery date
4. Headcount (number of people)

Maya does NOT take the full item-by-item order for catering. The restaurant calls back to finalize.

## Why
Catering orders require discussion of setup, delivery, dietary restrictions, deposit, etc.
A voice bot collecting a 20-item catering order creates too many error opportunities.
Capture the lead — let the humans close it.

## Output
```json
{
  "order_type": "catering",
  "status": "pending_callback",
  "customer": {
    "name": "...",
    "phone": "...",
    "event_datetime": "...",
    "headcount": "..."
  }
}
```

## Dashboard Display
Catering leads show with a yellow "Catering" badge and a "🎉 Catering Lead" section inside the card.

## Email / Slack
Email subject: `🎉 CATERING LEAD — {name} | {event_date}`
Slack: `🎉 *CATERING LEAD* | {name} | {event_date} | Party of {headcount}`
