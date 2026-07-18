/*
 * Tests for due-time.js — run with:  node --test dashboard/due-time.test.js
 *
 * Deterministic: every "now" and timestamp is an explicit ISO string with an
 * offset, so results don't depend on the machine's timezone.
 */
const { test } = require("node:test");
const assert = require("node:assert");
const DueTime = require("./due-time.js");

const ms = (iso) => Date.parse(iso);

// ── ASAP orders: due_at = kitchen_received_at + prep ─────────────────────────

test("not yet due — ASAP order still within prep window", () => {
  const order = { orderId: "A", prepMinutes: 15, pickupTime: "ASAP",
    orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" };
  const now = ms("2026-07-16T12:02:00-07:00"); // 2 min in
  assert.strictEqual(DueTime.getSurfaceState(order, now), "active");
  assert.deepStrictEqual(DueTime.formatDueBadge(order, now), { level: "ontime", text: "" });
});

test("just due — exactly at received + prep", () => {
  const order = { orderId: "B", prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" };
  const now = ms("2026-07-16T12:10:00-07:00");
  assert.deepStrictEqual(DueTime.formatDueBadge(order, now), { level: "late", text: "Late 0:00" });
});

test("4 minutes late — m:ss format", () => {
  const order = { orderId: "C", prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" };
  const now = ms("2026-07-16T12:14:12-07:00"); // due 12:10, +4:12
  assert.deepStrictEqual(DueTime.formatDueBadge(order, now), { level: "late", text: "Late 4:12" });
});

test("90 minutes late — h/m format", () => {
  const order = { orderId: "D", prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" };
  const now = ms("2026-07-16T13:40:00-07:00"); // due 12:10, +90m
  assert.deepStrictEqual(DueTime.formatDueBadge(order, now), { level: "late", text: "Late 1h 30m" });
});

test("null / missing timestamp — never crashes, renders 'Late —'", () => {
  const order = { orderId: "E", prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: null, kitchenReceivedAt: null };
  const badge = DueTime.formatDueBadge(order, ms("2026-07-16T12:00:00-07:00"));
  assert.strictEqual(badge.level, "unknown");
  assert.strictEqual(badge.text, "Late —");
});

test("display cap — beyond 999 minutes renders 'Late —', never a raw count", () => {
  const order = { orderId: "F", prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: "2026-07-09T18:47:00-07:00", kitchenReceivedAt: "2026-07-09T18:47:00-07:00" };
  const now = ms("2026-07-17T12:00:00-07:00"); // ~7.7 days later (the real "11114m" case)
  const badge = DueTime.formatDueBadge(order, now);
  assert.strictEqual(badge.text, "Late —");
  assert.ok(!/\d{4,}/.test(badge.text), "must never contain a 4+ digit minute count");
});

// ── Scheduled orders: due_at = pickup, surface_at = pickup - prep ────────────

test("scheduled — not yet surfaced (before pickup - prep)", () => {
  const order = { orderId: "G", prepMinutes: 15, pickupTime: "6:00 PM",
    orderTimestamp: "2026-07-16T16:00:00-07:00", kitchenReceivedAt: "2026-07-16T16:00:00-07:00" };
  const now = ms("2026-07-16T17:00:00-07:00"); // surfaces at 17:45
  assert.strictEqual(DueTime.getSurfaceState(order, now), "scheduled-hidden");
});

test("scheduled — just surfaced at pickup - prep, on time", () => {
  const order = { orderId: "H", prepMinutes: 15, pickupTime: "6:00 PM",
    orderTimestamp: "2026-07-16T16:00:00-07:00", kitchenReceivedAt: "2026-07-16T16:00:00-07:00" };
  const now = ms("2026-07-16T17:45:00-07:00");
  assert.strictEqual(DueTime.getSurfaceState(order, now), "active");
  assert.deepStrictEqual(DueTime.formatDueBadge(order, now), { level: "ontime", text: "" });
});

test("scheduled — late (past pickup time)", () => {
  const order = { orderId: "I", prepMinutes: 15, pickupTime: "6:00 PM",
    orderTimestamp: "2026-07-16T16:00:00-07:00", kitchenReceivedAt: "2026-07-16T16:00:00-07:00" };
  const now = ms("2026-07-16T18:05:00-07:00"); // due 18:00, +5m
  assert.strictEqual(DueTime.getSurfaceState(order, now), "active");
  assert.deepStrictEqual(DueTime.formatDueBadge(order, now), { level: "late", text: "Late 5:00" });
});

test("missing scheduled_pickup_time — falls back to ASAP semantics", () => {
  const order = { orderId: "J", prepMinutes: 10, pickupTime: "",
    orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" };
  assert.strictEqual(DueTime.parseScheduledPickup(order.pickupTime, order.orderTimestamp), null);
  // behaves as received + prep
  const now = ms("2026-07-16T12:05:00-07:00");
  assert.strictEqual(DueTime.getSurfaceState(order, now), "active"); // immediate
  assert.deepStrictEqual(DueTime.formatDueBadge(order, now), { level: "ontime", text: "" });
});

test("relative pickup strings are not treated as scheduled", () => {
  const base = "2026-07-16T12:00:00-07:00";
  for (const pk of ["ASAP", "~10 minutes", "In 45 minutes", "Delivery to 123 Main St", ""]) {
    assert.strictEqual(DueTime.parseScheduledPickup(pk, base), null, `'${pk}' should not parse as scheduled`);
  }
});

// ── Live countdown chip (item 5) ─────────────────────────────────────────────

const chipOrder = (prep) => ({ orderId: "CH", prepMinutes: prep, pickupTime: "ASAP",
  orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" });

test("chip green — more than 3:00 remaining", () => {
  // prep 15 -> due 12:15; at 12:05 there is 10:00 left
  assert.deepStrictEqual(DueTime.getChipState(chipOrder(15), ms("2026-07-16T12:05:00-07:00")),
    { level: "green", text: "10:00", label: "LEFT" });
});

test("chip amber — at or under 3:00 remaining", () => {
  // due 12:15; at 12:12:30 there is 2:30 left
  assert.deepStrictEqual(DueTime.getChipState(chipOrder(15), ms("2026-07-16T12:12:30-07:00")),
    { level: "amber", text: "2:30", label: "LEFT" });
});

test("chip green/amber boundary — exactly 3:00 left is amber", () => {
  assert.strictEqual(DueTime.getChipState(chipOrder(15), ms("2026-07-16T12:12:00-07:00")).level, "amber");
  assert.strictEqual(DueTime.getChipState(chipOrder(15), ms("2026-07-16T12:11:59-07:00")).level, "green");
});

test("chip red — past due, counts up", () => {
  // due 12:15; at 12:19:12 it is 4:12 late
  assert.deepStrictEqual(DueTime.getChipState(chipOrder(15), ms("2026-07-16T12:19:12-07:00")),
    { level: "red", text: "4:12", label: "LATE" });
});

test("chip red past an hour — h/m format", () => {
  assert.deepStrictEqual(DueTime.getChipState(chipOrder(15), ms("2026-07-16T13:45:00-07:00")),
    { level: "red", text: "1h 30m", label: "LATE" });
});

test("chip respects the 999-minute cap", () => {
  const stale = { orderId: "S", prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: "2026-07-09T18:47:00-07:00", kitchenReceivedAt: "2026-07-09T18:47:00-07:00" };
  const chip = DueTime.getChipState(stale, ms("2026-07-17T12:00:00-07:00"));
  assert.deepStrictEqual(chip, { level: "red", text: "—", label: "LATE" });
});

test("chip with unknown due_at never crashes", () => {
  const chip = DueTime.getChipState({ orderId: "X", prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: null, kitchenReceivedAt: null }, Date.now());
  assert.strictEqual(chip.level, "unknown");
  assert.strictEqual(chip.text, "—");
});

test("ageMinutes — order age since kitchen received it", () => {
  const o = chipOrder(15);
  assert.strictEqual(DueTime.ageMinutes(o, ms("2026-07-16T12:14:00-07:00")), 14);
  assert.strictEqual(DueTime.ageMinutes(o, ms("2026-07-16T12:00:00-07:00")), 0);
  assert.strictEqual(DueTime.ageMinutes({ orderTimestamp: null, kitchenReceivedAt: null }, Date.now()), null);
});

test("surface_at collapses to received for ASAP, pickup-prep for scheduled", () => {
  const asap = { prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" };
  assert.strictEqual(DueTime.computeSurfaceAt(asap), ms("2026-07-16T12:00:00-07:00"));

  const sched = { prepMinutes: 15, pickupTime: "6:00 PM",
    orderTimestamp: "2026-07-16T16:00:00-07:00", kitchenReceivedAt: "2026-07-16T16:00:00-07:00" };
  assert.strictEqual(DueTime.computeSurfaceAt(sched), ms("2026-07-16T17:45:00-07:00"));
});
