# SOP: Menu Management — Maya 2.0

## Menu Upload
1. Prepare JSON matching the schema below (use `sample_menu.json` as a template)
2. `POST /menu/upload` with the file as multipart form data
3. Server validates + normalizes the menu
4. Saved to SQLite immediately, mirrored to Airtable async
5. Loaded into memory cache — Maya uses it on next call

## Updating a Menu
Re-upload with the same `restaurant_id` — the record is upserted (overwritten).

## Retrieving a Menu
`GET /menu/{restaurant_id}` — returns the full config JSON.

## Multi-Restaurant Routing
Each restaurant config has a `twilio_phone` field.
When a call comes in, `To` (the called number) is matched against `twilio_phone` in the database.
This means one Maya 2.0 deployment can serve unlimited restaurants simultaneously.

## Menu Schema
```json
{
  "restaurant_id":   "unique-slug",
  "restaurant_name": "Display Name",
  "manager_phone":   "6692489997",
  "manager_email":   "manager@restaurant.com",
  "twilio_phone":    "+16692489997",
  "hours": {
    "monday":    { "open": "11:00", "close": "22:00" },
    "tuesday":   { "open": "11:00", "close": "22:00" },
    "sunday":    null
  },
  "prep_time_estimate_minutes": 15,
  "catering_threshold": { "min_dollars": 150 },
  "menu": [{
    "id":          "item-slug",
    "category":    "Category Name",
    "name":        "Item Name",
    "description": "Brief description",
    "price":       12.99,
    "available":   true,
    "modifiers": [
      { "name": "Extra cheese", "price_delta": 1.50 },
      { "name": "No onions",    "price_delta": 0 }
    ],
    "combos": [
      { "name": "Meal Deal", "items": ["item-slug", "drink-slug"], "price": 15.99 }
    ]
  }]
}
```

## Availability
Set `"available": false` on any item to hide it from Maya's menu and prevent her from taking orders for it.

## Hours Format
- Times in 24-hour `HH:MM` format
- Set a day to `null` for closed that day
- If hours are misconfigured, Maya defaults to open (safer than refusing calls)
