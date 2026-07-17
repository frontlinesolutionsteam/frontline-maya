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

test("surface_at collapses to received for ASAP, pickup-prep for scheduled", () => {
  const asap = { prepMinutes: 10, pickupTime: "ASAP",
    orderTimestamp: "2026-07-16T12:00:00-07:00", kitchenReceivedAt: "2026-07-16T12:00:00-07:00" };
  assert.strictEqual(DueTime.computeSurfaceAt(asap), ms("2026-07-16T12:00:00-07:00"));

  const sched = { prepMinutes: 15, pickupTime: "6:00 PM",
    orderTimestamp: "2026-07-16T16:00:00-07:00", kitchenReceivedAt: "2026-07-16T16:00:00-07:00" };
  assert.strictEqual(DueTime.computeSurfaceAt(sched), ms("2026-07-16T17:45:00-07:00"));
});
