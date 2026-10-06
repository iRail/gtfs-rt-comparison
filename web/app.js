"use strict";

const byId = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]));
const number = (value) => value == null ? "—" : new Intl.NumberFormat("en-BE").format(value);
const dateTime = (value, epoch = false) => value == null ? "Unknown" : new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Brussels", day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short" }).format(new Date(epoch ? value * 1000 : value));
function delay(seconds, sign = false) {
  if (seconds == null) return "—";
  const absolute = Math.abs(seconds);
  const minutes = Math.floor(absolute / 60);
  const remainder = Math.round((absolute % 60) * 10) / 10;
  const prefix = seconds < 0 ? "−" : sign && seconds > 0 ? "+" : "";
  return prefix + (minutes ? `${minutes}m${remainder ? ` ${remainder}s` : ""}` : `${remainder}s`);
}
function notices(messages) {
  byId("notice").innerHTML = messages.map((message) => `<p class="notice">${escapeHTML(message)}</p>`).join("");
}
function discrepancy(value, reference, isDelay = false) {
  if (value == null || reference == null || value === reference) return "";
  const difference = reference - value;
  if (isDelay) return `${delay(Math.abs(difference))} ${difference > 0 ? "lower" : "higher"} than SNCB`;
  return `${number(Math.abs(difference))} ${difference > 0 ? "missing" : "extra"} compared with SNCB`;
}
function metrics(feeds) {
  const definitions = [
    ["trips", "Trips announced", (stats) => `${number(stats.trips_with_delay_values)} with explicit delay values`],
    ["delayed_trips", "Trips with a delay", (stats) => stats.trips ? `${(stats.delayed_trips / stats.trips * 100).toFixed(1)}% of announced trips` : "No trips announced"],
    ["stop_updates", "Stop updates", (stats) => `${number(stats.usable_stop_updates)} with usable arrival / departure data`],
    ["delay_values", "Explicit delay values", () => "Includes zero and negative delay values"],
    ["cancelled_trips", "Cancelled trips", () => "Excluded from delayed-trip counts"],
    ["largest_delay_seconds", "Largest trip delay", (stats) => stats.delayed_trips ? `Mean maximum: ${delay(stats.mean_max_delay_seconds)}` : "No positive delays announced"],
  ];
  byId("metrics").innerHTML = definitions.map(([field, label, detail]) => {
    const maximum = Math.max(1, ...Object.values(feeds).map((feed) => feed.stats?.[field] ?? 0));
    return ["sncb", "bmc"].map((name) => {
      const stats = feeds[name].stats;
      const value = stats?.[field];
      const difference = name === "bmc" ? discrepancy(value, feeds.sncb.stats?.[field], field === "largest_delay_seconds") : "";
      return `<div class="metric ${name}${difference ? " is-mismatch" : ""}"><div class="metric-top"><span class="metric-label">${label}</span><div class="metric-reading"><span class="metric-value">${field === "largest_delay_seconds" ? delay(value) : number(value)}</span>${difference ? `<span class="metric-difference">${escapeHTML(difference)}</span>` : ""}</div></div><div class="metric-bar" aria-hidden="true"><span style="width:${(value ?? 0) / maximum * 100}%"></span></div><p class="metric-detail">${stats ? escapeHTML(detail(stats)) : "Feed unavailable"}</p></div>`;
    }).join("");
  }).join("");
  byId("metrics").setAttribute("aria-busy", "false");
  for (const name of ["sncb", "bmc"]) {
    const feed = feeds[name];
    byId(`${name}-status`).textContent = feed.status === "error" ? "Unavailable" : feed.age_seconds == null ? "Age unknown" : feed.age_seconds > 300 ? "Stale at capture" : feed.age_seconds < -60 ? "Clock mismatch" : "Fetched";
    byId(`${name}-meta`).innerHTML = feed.status === "error" ? `<p class="warn">${escapeHTML(feed.error)}</p>` : `<p>Feed published ${escapeHTML(dateTime(feed.feed_timestamp, true))}</p><p>Fetched ${escapeHTML(dateTime(feed.fetched_at, true))} · ${number(feed.entities)} entities · ${number(Math.round(feed.bytes / 1024))} KiB</p>${feed.warnings.map((warning) => `<p class="warn">${escapeHTML(warning)}</p>`).join("")}`;
  }
}
function agreement(comparison) {
  const blocks = [[comparison.matched_trips, "Matched trips"], [comparison.sncb_only, "Missing from BMC"], [comparison.bmc_only, "Extra in BMC"]];
  byId("agreement").innerHTML = `<div class="agreement-grid">${blocks.map(([value, label]) => `<div><span class="agreement-number">${number(value)}</span><span class="agreement-label">${label}</span></div>`).join("")}</div>` + (comparison.available ? `<p><strong>${number(comparison.same_max_delay)}</strong> same maximum delay · <strong>${number(comparison.different_max_delay)}</strong> different</p><p>${number(comparison.comparable_delays)} matched trips have explicit delays in both feeds. Comparing maxima does not establish equality at every stop.</p>` : `<p>Matching is unavailable until both feeds can be fetched. Missing feed data is not counted as missing trips.</p>`);
}
function tripDelay(trip, feed) {
  if (feed.status !== "ok") return '<span class="unknown">Feed unavailable</span>';
  if (!trip) return '<span class="unknown mismatch">Not in feed</span>';
  if (trip.cancelled) return '<span class="unknown">Cancelled</span>';
  if (trip.max_delay_seconds == null) return '<span class="unknown">No delay value</span>';
  return `<span class="delay-value">${delay(trip.max_delay_seconds, true)}</span>`;
}
function serviceDate(value) {
  return /^\d{8}$/.test(value) ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}` : value || "Date not supplied";
}
function renderTrips(report) {
  const rows = report.comparison.rows;
  byId("trip-count").textContent = number(rows.length);
  byId("trips").innerHTML = rows.length ? rows.map((row, index) => `<tr><td><span class="trip-id">${escapeHTML(row.trip_id)}</span><span class="trip-instance">${escapeHTML(serviceDate(row.start_date))} · ${escapeHTML(row.start_time || "Start time not supplied")}</span></td><td>${tripDelay(row.sncb, report.feeds.sncb)}</td><td>${tripDelay(row.bmc, report.feeds.bmc)}</td><td class="delta${row.difference_seconds != null && row.difference_seconds !== 0 ? " mismatch" : ""}">${delay(row.difference_seconds, true)}</td><td><button type="button" class="details-button" data-row="${index}" aria-label="Show stops for ${escapeHTML(row.trip_id)}" aria-expanded="false" aria-controls="details-${index}">Stops</button></td></tr><tr id="details-${index}" class="details-row" hidden><td colspan="5"></td></tr>`).join("") : `<tr><td colspan="5" class="empty">${report.comparison.available ? "Neither feed announces a positive delay in this snapshot." : "No positive delays found in the available feed data. The comparison is incomplete."}</td></tr>`;
  byId("trips").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-row]");
    if (!button) return;
    const index = Number(button.dataset.row);
    const detailRow = byId(`details-${index}`);
    const open = button.getAttribute("aria-expanded") !== "true";
    if (open && !detailRow.dataset.rendered) {
      detailRow.firstElementChild.innerHTML = stopDetails(rows[index], report.feeds);
      detailRow.dataset.rendered = "true";
    }
    button.setAttribute("aria-expanded", String(open));
    button.setAttribute("aria-label", `${open ? "Hide" : "Show"} stops for ${rows[index].trip_id}`);
    button.textContent = open ? "Hide" : "Stops";
    detailRow.hidden = !open;
  });
}
function stopIndex(trip) {
  const counts = new Map();
  const index = new Map();
  for (const stop of trip?.stops ?? []) {
    const id = stop.stop_id.startsWith("gs:nmbssncb:") ? stop.stop_id.slice("gs:nmbssncb:".length) : stop.stop_id;
    // Sequence numbers are absent in SNCB. Align repeated visits by occurrence.
    const base = id || `sequence:${stop.stop_sequence ?? "unknown"}`;
    const occurrence = (counts.get(base) ?? 0) + 1;
    counts.set(base, occurrence);
    index.set(`${base}|${occurrence}`, { ...stop, canonical_id: id, occurrence });
  }
  return index;
}
function eventCell(stop, event, feed, trip) {
  if (feed.status !== "ok") return '<td class="unknown">Unavailable</td>';
  if (!trip) return '<td class="unknown mismatch">Trip not in feed</td>';
  if (!stop) return '<td class="unknown mismatch">Stop not reported</td>';
  if (trip.cancelled) return '<td class="unknown">Cancelled</td>';
  if (["SKIPPED", "NO_DATA"].includes(stop.relationship)) return `<td class="unknown">${escapeHTML(stop.relationship)}</td>`;
  const value = stop[event];
  const timestamp = value?.time == null ? "No event time supplied" : dateTime(value.time, true);
  return `<td title="${escapeHTML(timestamp)}" class="event-delay">${value?.delay_seconds == null ? '<span class="unknown">No delay value</span>' : delay(value.delay_seconds, true)}</td>`;
}
function stopDetails(row, feeds) {
  const a = stopIndex(row.sncb);
  const b = stopIndex(row.bmc);
  const keys = [...new Set([...a.keys(), ...b.keys()])];
  const metadata = ["sncb", "bmc"].map((name) => {
    const trip = row[name];
    return `<div><strong class="${name}-text">${name.toUpperCase()}</strong>${trip ? `<p>Original trip ID: <code>${escapeHTML(trip.trip_id)}</code></p><p>${escapeHTML(trip.relationship)} · trip-level delay: <span class="delay-value">${delay(trip.trip_delay_seconds, true)}</span> · updated: ${escapeHTML(dateTime(trip.updated_at, true))}</p>` : `<p class="${feeds[name].status === "ok" ? "mismatch" : "unknown"}">${feeds[name].status === "ok" ? "Trip not in feed" : "Feed unavailable"}</p>`}</div>`;
  }).join("");
  return `<div class="details-meta">${metadata}</div>${keys.length ? `<div class="stop-details"><table class="stop-table"><caption class="sr-only">Stop arrival and departure delays for ${escapeHTML(row.trip_id)}</caption><thead><tr><th scope="col" rowspan="2">Stop ID</th><th scope="colgroup" colspan="2" class="sncb-text">SNCB</th><th scope="colgroup" colspan="2" class="bmc-text">BMC</th></tr><tr><th scope="col">Arrival</th><th scope="col">Departure</th><th scope="col">Arrival</th><th scope="col">Departure</th></tr></thead><tbody>${keys.map((key) => {
    const stop = b.get(key) ?? a.get(key);
    return `<tr><td>${escapeHTML(stop.canonical_id || "No stop ID")}${stop.occurrence > 1 ? ` (visit ${stop.occurrence})` : ""}<span class="stop-state">Sequence SNCB: ${number(a.get(key)?.stop_sequence)} · BMC: ${number(b.get(key)?.stop_sequence)}</span></td>${eventCell(a.get(key), "arrival", feeds.sncb, row.sncb)}${eventCell(a.get(key), "departure", feeds.sncb, row.sncb)}${eventCell(b.get(key), "arrival", feeds.bmc, row.bmc)}${eventCell(b.get(key), "departure", feeds.bmc, row.bmc)}</tr>`;
  }).join("")}</tbody></table></div><p class="muted" style="margin-top:12px">BMC’s gs:nmbssncb: stop namespace is removed for matching; platform suffixes are retained. Repeated stop visits match by occurrence. Event times appear on hover.</p>` : '<p class="muted">No stop updates announced; the delay is reported at trip level.</p>'}`;
}
async function main() {
  try {
    const response = await fetch("report.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const report = await response.json();
    byId("generated-at").textContent = dateTime(report.generated_at);
    const messages = [];
    if (report.fixture_mode) messages.push("Local verification snapshot: saved feed files were used. This is not a live collection.");
    for (const name of ["sncb", "bmc"]) {
      if (report.feeds[name].status === "error") messages.push(`${name.toUpperCase()} is unavailable. ${report.feeds[name].error} Its statistics and comparisons are unknown.`);
    }
    if (report.comparison.available && !report.comparison.matched_trips && Object.values(report.feeds).some((feed) => feed.stats.trips > 0)) messages.push("No trip instances match across these snapshots. Compare coverage independently; identifiers, service dates or start times may differ.");
    notices(messages);
    metrics(report.feeds);
    agreement(report.comparison);
    renderTrips(report);
  } catch (error) {
    byId("generated-at").textContent = "Report unavailable";
    notices(["The snapshot could not be loaded. Check the latest GitHub Pages workflow run and reload the page."]);
    byId("metrics").innerHTML = '<p class="empty">No report data available.</p>';
    byId("metrics").setAttribute("aria-busy", "false");
    for (const name of ["sncb", "bmc"]) byId(`${name}-status`).textContent = "Unknown";
    byId("agreement").innerHTML = '<p class="muted">Comparison unavailable.</p>';
    byId("trips").innerHTML = '<tr><td colspan="5" class="empty">Trip details unavailable.</td></tr>';
    console.error("Could not load GTFS-RT report", error);
  }
}
main();
