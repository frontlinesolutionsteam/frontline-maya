/*
 * due-time.js — the Kitchen Display's clock logic, in one testable place.
 *
 * This is the module that broke (rendering "OVERDUE 11114m"), so it's the one
 * piece of the front-end extracted into pure, DOM-free functions with node
 * tests (see due-time.test.js). Loaded in the browser via <script> (exposes
 * window.DueTime) and imported directly by the test runner.
 *
 * Model (single consistent definition of "due"):
 *   due_at      = the moment the food should be READY.
 *                 scheduled pickup -> the pickup time itself
 *                 otherwise        -> kitchen_received_at + prep
 *   surface_at  = due_at - prep. When a ticket should appear on the kitchen
 *                 screen. Collapses to `received` for ASAP (immediate) and
 *                 `pickup - prep` for scheduled orders (held back until then).
 *   lateness    = now - due_at, only meaningful once positive.
 *
 * Order shape expected by these functions:
 *   { orderId, prepMinutes, pickupTime, orderTimestamp, kitchenReceivedAt }
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.DueTime = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var LATE_CAP_MINUTES = 999; // above this we render "Late —" rather than a fake number

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  /*
   * Parse a specific clock-time pickup ("6:00 PM", "18:00") into epoch ms,
   * anchored to the ORDER's own local date + UTC offset so the result is
   * timezone-deterministic no matter where this runs (kitchen tablet or CI).
   * Returns null when the pickup isn't a specific time — ASAP, "~10 minutes",
   * "Delivery to ...", or empty — i.e. an ASAP-style order.
   */
  function parseScheduledPickup(pickupTime, orderTimestamp) {
    if (!pickupTime || !orderTimestamp) return null;
    var p = String(pickupTime).trim();
    if (!p) return null;
    if (/^asap$/i.test(p)) return null;
    if (/~|\bmin\b|minute|delivery/i.test(p)) return null; // relative / non-time strings

    var clock = p.match(/(\d{1,2}):(\d{2})\s*(AM|PM)?/i);
    if (!clock) return null;
    var hh = parseInt(clock[1], 10);
    var mm = parseInt(clock[2], 10);
    var ap = clock[3] ? clock[3].toUpperCase() : null;
    if (ap === "PM" && hh !== 12) hh += 12;
    if (ap === "AM" && hh === 12) hh = 0;
    if (hh > 23 || mm > 59) return null;

    // Reuse the order stamp's calendar date + offset for the pickup wall-clock.
    var m = String(orderTimestamp).match(/^(\d{4}-\d{2}-\d{2})T[\d:.]+(Z|[+-]\d{2}:?\d{2})?/);
    if (!m) return null;
    var datePart = m[1];
    var offset = m[2] || "Z";
    if (offset !== "Z" && offset.indexOf(":") === -1) {
      offset = offset.slice(0, 3) + ":" + offset.slice(3); // +0700 -> +07:00
    }
    var ms = Date.parse(datePart + "T" + pad2(hh) + ":" + pad2(mm) + ":00" + offset);
    return isNaN(ms) ? null : ms;
  }

  function isScheduled(order) {
    return parseScheduledPickup(order.pickupTime, order.orderTimestamp) != null;
  }

  /* The moment the food should be READY (drives lateness). Epoch ms, or null. */
  function computeDueAt(order) {
    var scheduled = parseScheduledPickup(order.pickupTime, order.orderTimestamp);
    if (scheduled != null) return scheduled;
    var recv = Date.parse(order.kitchenReceivedAt || order.orderTimestamp);
    if (isNaN(recv)) return null;
    var prepMs = (Number(order.prepMinutes) || 0) * 60000;
    return recv + prepMs;
  }

  /* The moment the ticket should APPEAR on the kitchen screen. Epoch ms, or null. */
  function computeSurfaceAt(order) {
    var due = computeDueAt(order);
    if (due == null) return null;
    var prepMs = (Number(order.prepMinutes) || 0) * 60000;
    return due - prepMs;
  }

  /*
   * 'scheduled-hidden' -> a future scheduled order the kitchen shouldn't see yet.
   * 'active'           -> belongs on the In Progress screen now.
   * Undated orders fail safe to 'active' (better a cook sees it than misses it).
   */
  function getSurfaceState(order, now) {
    var surface = computeSurfaceAt(order);
    if (surface == null) return "active";
    return now < surface ? "scheduled-hidden" : "active";
  }

  /*
   * Lateness badge. Returns { level, text }:
   *   level 'ontime'  -> not yet due (text "" — the live countdown chip is item 5)
   *   level 'late'    -> "Late 4:12" (< 1h, m:ss) / "Late 1h 04m" (>= 1h)
   *   level 'unknown' -> due_at couldn't be computed; "Late —" + logged error
   * Never prints a raw minute count above 999 — a clock that admits it doesn't
   * know beats one that prints a fake number.
   */
  function formatDueBadge(order, now) {
    var due = computeDueAt(order);
    if (due == null) {
      logError("due_at unknown for order " + (order && order.orderId));
      return { level: "unknown", text: "Late —" };
    }
    var lateMs = now - due;
    if (lateMs < 0) return { level: "ontime", text: "" };

    var lateMin = Math.floor(lateMs / 60000);
    if (lateMin > LATE_CAP_MINUTES) {
      logError("lateness " + lateMin + "m exceeds cap for order " + (order && order.orderId) + " — rendering 'Late —'");
      return { level: "late", text: "Late —" };
    }
    if (lateMin < 60) {
      var s = Math.floor((lateMs % 60000) / 1000);
      return { level: "late", text: "Late " + lateMin + ":" + pad2(s) };
    }
    return { level: "late", text: "Late " + Math.floor(lateMin / 60) + "h " + pad2(lateMin % 60) + "m" };
  }

  function mmss(msSpan) {
    var total = Math.floor(msSpan / 1000);
    return Math.floor(total / 60) + ":" + pad2(total % 60);
  }

  /*
   * Live countdown chip. Returns { level, text, label }:
   *   green  > 3:00 remaining  -> "8:32 LEFT"
   *   amber  3:00 .. 0:00      -> "2:14 LEFT"
   *   red    past due          -> "4:12 LATE" (counts up), "1h 04m LATE" past an hour
   * The 999-minute cap applies here too: "— LATE" rather than a fake number.
   */
  function getChipState(order, now) {
    var due = computeDueAt(order);
    if (due == null) {
      logError("due_at unknown for order " + (order && order.orderId));
      return { level: "unknown", text: "—", label: "LATE" };
    }
    var remaining = due - now;
    if (remaining > 0) {
      return { level: remaining > 180000 ? "green" : "amber", text: mmss(remaining), label: "LEFT" };
    }
    var lateMs = -remaining;
    var lateMin = Math.floor(lateMs / 60000);
    if (lateMin > LATE_CAP_MINUTES) {
      logError("lateness " + lateMin + "m exceeds cap for order " + (order && order.orderId) + " — rendering '—'");
      return { level: "red", text: "—", label: "LATE" };
    }
    if (lateMin < 60) return { level: "red", text: mmss(lateMs), label: "LATE" };
    return { level: "red", text: Math.floor(lateMin / 60) + "h " + pad2(lateMin % 60) + "m", label: "LATE" };
  }

  /* Minutes since the kitchen got the ticket — order age, distinct from the countdown. */
  function ageMinutes(order, now) {
    var recv = Date.parse(order.kitchenReceivedAt || order.orderTimestamp);
    if (isNaN(recv)) return null;
    return Math.max(0, Math.floor((now - recv) / 60000));
  }

  function logError(msg) {
    if (typeof console !== "undefined" && console.error) console.error("[kds] " + msg);
  }

  return {
    LATE_CAP_MINUTES: LATE_CAP_MINUTES,
    parseScheduledPickup: parseScheduledPickup,
    isScheduled: isScheduled,
    computeDueAt: computeDueAt,
    computeSurfaceAt: computeSurfaceAt,
    getSurfaceState: getSurfaceState,
    formatDueBadge: formatDueBadge,
    getChipState: getChipState,
    ageMinutes: ageMinutes,
  };
});
