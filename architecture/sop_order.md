# SOP: Order Lifecycle — Maya 2.0

## Order Statuses
```
confirmed → in_prep → ready → picked_up
                            ↘ escalated
                            ↘ pending_callback  (catering)
```

## Status Definitions
| Status | Meaning | Who sets it |
|--------|---------|-------------|
| `confirmed` | Order taken by Maya, submitted to kitchen | Maya (automatic) |
| `in_prep` | Kitchen acknowledged, preparing | Dashboard button |
| `ready` | Order ready for pickup | Dashboard button |
| `picked_up` | Customer collected order | Dashboard button |
| `escalated` | Call transferred to manager (partial or complaint) | Maya (automatic) |
| `pending_callback` | Catering lead, awaiting callback | Maya (automatic) |

## Order Payload Schema
```json
{
  "order_id":               "uuid",
  "restaurant_id":          "string",
  "timestamp":              "ISO8601",
  "customer": {
    "name":                 "string",
    "phone":                "string",
    "pickup_time":          "string"
  },
  "order_type":             "standard | catering",
  "items": [{
    "menu_item_id":         "string",
    "name":                 "string",
    "quantity":             1,
    "modifiers":            ["string"],
    "unit_price":           0.00,
    "line_total":           0.00
  }],
  "subtotal":               0.00,
  "estimated_prep_minutes": 10,
  "special_instructions":   "string",
  "status":                 "confirmed",
  "call_sid":               "string",
  "call_duration_seconds":  0
}
```

## Required Fields Before Submit
Maya will NOT submit an order unless all three are collected:
1. `customer.name`
2. `customer.phone`
3. `customer.pickup_time`

If Claude tries to submit early, the server detects missing fields and redirects back to `collect_info`.

## Delivery Targets (in order, simultaneous)
1. **SQLite** — always, synchronous, primary store
2. **Airtable** — async, optional (skipped if `AIRTABLE_API_KEY` not set)
3. **Dashboard WebSocket** — broadcasts to all connected browsers
4. **SendGrid Email** — to `ORDER_NOTIFICATION_EMAIL`, optional
5. **Slack** — to `SLACK_ORDERS_CHANNEL_ID`, optional

A failure in any external sink does NOT affect order durability (SQLite already written).
