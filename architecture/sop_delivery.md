# SOP: Order Delivery — Maya 2.0

## Delivery Architecture
Orders are written to SQLite first (durable, local), then fanned out to all external sinks simultaneously.
A failure in any external sink does NOT lose the order.

```
Order confirmed
     │
     ▼
SQLite (always, synchronous)
     │
     ├──▶ Airtable (async, optional)
     ├──▶ Dashboard WebSocket (broadcast to all tabs)
     ├──▶ SendGrid Email (to restaurant inbox)
     └──▶ Slack (to #orders channel)
```

## Retry Policy
- **Airtable**: 2 retries, exponential backoff (1s, 2s)
- **Email**: 2 retries, 5s delay between attempts
- **Slack**: 1 attempt (non-critical)
- **Dashboard**: best-effort, dead connections pruned automatically

## Failure Handling
- All failures logged to `.tmp/email_failures.log` and `.tmp/slack_failures.log`
- Slack system alert fired on `process_turn` crash or server startup/shutdown
- If Airtable is unreachable, order is still in SQLite — recoverable

## Dashboard Real-Time Updates
- `/ws/dashboard` WebSocket connection per browser tab
- On connect: full order list sent as `{"event": "init", "orders": [...]}`
- On new order: `{"event": "new_order", "order": {...}}`
- On status change: `{"event": "status_update", "order_id": "...", "status": "..."}`
- Auto-reconnects every 3 seconds on disconnect

## Order Status API
```
PATCH /api/orders/{order_id}/status
Body: {"status": "in_prep" | "ready" | "picked_up"}
```
Broadcasts update to all connected dashboard tabs via WebSocket.
