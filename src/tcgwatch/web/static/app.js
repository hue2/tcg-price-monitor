"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const WEBHOOK_MASK = "__saved__";
const PAGE = 200;

let authLost = false;  // a 401 was seen: show one banner, stop firing requests
const AUTH_MSG = "Signed out: your token expired or changed. Log in again.";
async function api(path, opts = {}) {
  if (authLost) throw new Error(AUTH_MSG);
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) {
    authLost = true;
    $("#authBanner").hidden = false;
    throw new Error(AUTH_MSG);
  }
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null ? "" : "$" + v.toFixed(2));

// ---- tabs --------------------------------------------------------------------------
$$(".tabs button").forEach((b) => (b.onclick = () => {
  $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$("main > section").forEach((s) => (s.hidden = s.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "settings" && !settings) loadSettings();
  if (b.dataset.tab === "watchlist") refreshWatchlist();
  if (b.dataset.tab === "alerts") loadAlerts();
  if (b.dataset.tab === "runs") loadRuns();
}));

// ---- status and jobs ----------------------------------------------------------------
async function loadStatus() {
  const st = await api("/api/status");
  $("#status").textContent = st.games.map((g) =>
    `${g.name}: ${g.days} day(s) of prices${g.latest ? ` (${g.first} to ${g.latest})` : ""}`).join(" | ")
    + ` | Discord ${st.discord ? "on" : "off"}`;
  $("#logout").hidden = !st.auth_enabled;
  const hb = $("#health");
  hb.hidden = !st.health;
  if (st.health) {
    hb.className = "health " + (st.health.ok ? "ok" : "bad");
    hb.textContent = st.health.ok ? "Healthy" : "Unhealthy";
    hb.title = st.health.detail || "";
  }
  const sel = $("#cardControls [name=game]");
  const current = sel.value;
  sel.innerHTML = st.games.filter((g) => g.enabled)
    .map((g) => `<option value="${esc(g.key)}">${esc(g.name)}</option>`).join("");
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
  showJob(st.job);
  return st;
}

// Only http(s) card links are rendered; anything else (javascript:, data:) shows as plain text.
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? u : null);

// Latest-request-wins: each load takes a generation; only the newest may write the DOM
// (both success and failure), so a slow older response can't clobber a newer one.
let alertsGen = 0, runsGen = 0;

async function loadAlerts() {
  const gen = ++alertsGen;
  const note = $("#alertsError");
  const q = new URLSearchParams({ limit: "200" });
  if ($("#alertDays").value) q.set("days", $("#alertDays").value);
  if ($("#alertFilter").value) q.set("discord", $("#alertFilter").value);
  let rows;
  try {
    rows = await api("/api/alerts?" + q);
  } catch (e) {
    if (gen !== alertsGen) return; // superseded by a newer request
    // Never leave old rows looking current: clear them and say why (textContent = safe).
    $("#alerts tbody").innerHTML = `<tr><td colspan="10" class="muted">Alert history unavailable.</td></tr>`;
    $("#alertsSummary").textContent = "";
    note.textContent = "Could not load alerts: " + (e && e.message ? e.message : "network error")
      + ". Change a filter or click the Alerts tab to retry.";
    note.hidden = false;
    return;
  }
  if (gen !== alertsGen) return;
  note.textContent = "";
  note.hidden = true;
  $("#alertsSummary").textContent = `${rows.length} alert(s), newest first.`;
  $("#alerts tbody").innerHTML = rows.length ? rows.map((r) => {
    const href = safeUrl(r.url);
    const name = r.name == null ? "(card removed)" : r.name;
    const card = href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(name)}</a>` : esc(name);
    const pct = r.pct_change == null ? "" : (r.pct_change > 0 ? "+" : "") + Number(r.pct_change).toFixed(1) + "%";
    return `<tr class="alert-${r.discord ? "posted" : "quiet"}"><td>${esc(r.alerted_on)}</td>`
      + `<td>${r.discord ? "posted" : "suppressed"}</td><td class="num">${esc(pct)}</td>`
      + `<td class="num">${esc(money(r.baseline))}</td><td class="num">${esc(money(r.current))}</td>`
      + `<td>${card}</td><td>${esc(r.rarity)}</td><td>${esc(r.printing)}</td><td>${esc(r.game)}</td>`
      + `<td>${esc(r.window_days)}d</td></tr>`;
  }).join("") : `<tr><td colspan="10" class="muted">No alerts recorded for this filter.</td></tr>`;
}
$("#alertFilter").onchange = loadAlerts;
$("#alertDays").onchange = loadAlerts;

async function loadRuns() {
  const gen = ++runsGen;
  const note = $("#runsError");
  let rows;
  try {
    rows = await api("/api/job-runs?limit=20");
  } catch (e) {
    if (gen !== runsGen) return; // superseded by a newer request
    // Never leave old rows looking current: clear them and say why (textContent = safe).
    $("#runs tbody").innerHTML = `<tr><td colspan="7" class="muted">Run history unavailable.</td></tr>`;
    note.textContent = "Could not load job runs: " + (e && e.message ? e.message : "network error")
      + ". Click the Runs tab to retry.";
    note.hidden = false;
    return;
  }
  if (gen !== runsGen) return;
  note.textContent = "";
  note.hidden = true;
  $("#runs tbody").innerHTML = rows.length ? rows.map((r) => {
    const res = r.ok == null ? "running" : r.ok ? "ok" : "failed";
    return `<tr class="run-${res}"><td>${esc(res)}</td><td>${esc(r.kind)}</td><td>${esc(r.game)}</td>`
      + `<td>${esc(r.started)}</td><td>${esc(r.finished)}</td><td>${esc(r.dataset_date)}</td>`
      + `<td>${esc(r.note)}</td></tr>`;
  }).join("") : `<tr><td colspan="7" class="muted">No job runs yet.</td></tr>`;
}

let polling = false;
function showJob(job) {
  $$("[data-job]").forEach((b) => (b.disabled = !!(job && job.running)));
  if (!job) return;
  $("#job").hidden = false;
  $("#jobSummary").textContent = `${job.kind}: ${job.running ? "running..." : job.ok ? "done" : "failed"} (started ${job.started})`;
  $("#jobLog").textContent = job.log || "(no output yet)";
  if (job.running && !polling) {
    polling = true;
    setTimeout(async () => {
      polling = false;
      const j = await api("/api/jobs");
      showJob(j);
      if (j && !j.running) { await loadStatus(); loadCards(); }
    }, 1500);
  }
}

$$("[data-job]").forEach((b) => (b.onclick = async () => {
  if (b.dataset.job === "run" && !confirm("Snapshot today's prices, run the check, and post new movers to Discord?")) return;
  try {
    showJob(await api("/api/jobs/" + b.dataset.job, { method: "POST" }));
    $("#job").open = true;
  } catch (e) { alert(e.message); }
}));

// ---- sparkline and watching ----------------------------------------------------------
// Inline SVG line; green if the span ended higher than it started, red if lower.
function sparkline(points, field, w = 90, h = 24) {
  const vals = points.map((p) => p[field]).filter((v) => v != null);
  if (vals.length < 2) return `<span class="muted" title="Not enough history">&ndash;</span>`;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const xy = vals.map((v, i) => `${(i / (vals.length - 1) * (w - 2) + 1).toFixed(1)},${(h - 2 - (v - lo) / span * (h - 4)).toFixed(1)}`);
  const dir = vals.at(-1) > vals[0] ? "up" : vals.at(-1) < vals[0] ? "down" : "";
  const first = points.find((p) => p[field] != null).date, last = points.findLast((p) => p[field] != null).date;
  return `<svg class="spark ${dir}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img"
    aria-label="Price ${money(vals[0])} to ${money(vals.at(-1))}"><title>${esc(first)} ${money(vals[0])} to ${esc(last)} ${money(vals.at(-1))}</title>
    <polyline points="${xy.join(" ")}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
}

const starBtn = (r) => `<button class="star ${r.watched ? "on" : ""}" data-star="${r.product_id}" data-printing="${esc(r.printing)}"
  title="${r.watched ? "Remove from" : "Add to"} watchlist" aria-pressed="${!!r.watched}">${r.watched ? "&#9733;" : "&#9734;"}</button>`;

async function toggleWatch(pid, printing, on) {
  const q = `${pid}?printing=${encodeURIComponent(printing)}`;
  if (on) await api("/api/watchlist", { method: "PUT", body: JSON.stringify({ product_id: pid, printing }) });
  else await api("/api/watchlist/" + q, { method: "DELETE" });
}

document.addEventListener("click", async (e) => {
  const star = e.target.closest("[data-star]");
  if (star) {
    const pid = Number(star.dataset.star), printing = star.dataset.printing;
    const on = star.getAttribute("aria-pressed") !== "true";
    try { await toggleWatch(pid, printing, on); } catch (err) { return alert(err.message); }
    const row = rows.find((r) => r.product_id === pid && r.printing === printing);
    if (row) row.watched = on;
    if (star.closest("#watchlist")) refreshWatchlist();
    else renderCards();
    return;
  }
  const trend = e.target.closest("[data-trend]");
  if (trend) {
    const tr = trend.closest("tr");
    if (tr.nextElementSibling?.classList.contains("detail")) { tr.nextElementSibling.remove(); return; }
    try {
      const h = await api(`/api/history/${trend.dataset.trend}?printing=${encodeURIComponent(trend.dataset.printing)}`);
      const d = document.createElement("tr");
      d.className = "detail";
      d.innerHTML = `<td colspan="11">${sparkline(h.points, trend.dataset.field || "market", 360, 70)}
        <span class="muted">${h.points.length} day(s), ${esc(h.points[0].date)} to ${esc(h.points.at(-1).date)} (${esc(trend.dataset.field || "market")})</span></td>`;
      tr.after(d);
    } catch (err) { alert(err.message); }
  }
});

async function loadWatchlist() {
  const game = $("#cardControls [name=game]").value;
  if (!game) return;
  const items = await api(`/api/watchlist/${encodeURIComponent(game)}?days=${$("#wlDays").value}`);
  $("#wlSummary").textContent = items.length ? `${items.length} watched card(s).`
    : "Nothing watched yet. Star a card on the Cards tab.";
  $("#watchlist tbody").innerHTML = items.map((r) => `<tr>
    <td>${starBtn({ ...r, watched: true })}</td>
    <td class="num ${r.pct > 0 ? "up" : r.pct < 0 ? "down" : ""}">${r.pct == null ? "" : (r.pct > 0 ? "+" : "") + r.pct.toFixed(1) + "%"}</td>
    <td class="num">${money(r.cur)}</td><td>${sparkline(r.points, r.field)}</td>
    <td>${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>` : esc(r.name)}</td>
    <td>${esc(r.number)}</td><td>${esc(r.rarity)}</td><td>${esc(r.printing)}</td><td>${esc(r.set)}</td></tr>`).join("");
}
// every UI caller goes through this: a failed load shows its message in #wlSummary, never an unhandled rejection
function refreshWatchlist() {
  return loadWatchlist().catch((err) => { $("#wlSummary").textContent = err.message; });
}
$("#wlDays").onchange = refreshWatchlist;

// ---- cards list -------------------------------------------------------------------------
let rows = [], meta = null, shown = PAGE, sortKey = "pct", sortDir = -1;

async function loadCards() {
  const params = new URLSearchParams();
  for (const [k, v] of new FormData($("#cardControls"))) if (v !== "") params.set(k, v);
  const game = params.get("game");
  if (!game) return;
  params.delete("game");
  $("#summary").textContent = "Loading...";
  try {
    meta = await api(`/api/cards/${encodeURIComponent(game)}?${params}`);
  } catch (e) {
    $("#summary").textContent = e.message;
    rows = []; renderCards(); return;
  }
  rows = meta.rows;
  fillSelect("#setFilter", "All sets", [...new Set(rows.map((r) => r.set))].sort());
  fillSelect("#rarityFilter", "All rarities", [...new Set(rows.map((r) => r.rarity))].sort());
  shown = PAGE;
  renderCards();
}

function fillSelect(sel, label, values) {
  const el = $(sel), cur = el.value;
  el.innerHTML = `<option value="">${label}</option>` + values.map((v) => `<option>${esc(v)}</option>`).join("");
  el.value = values.includes(cur) ? cur : "";
}

function visibleRows() {
  const q = $("#search").value.trim().toLowerCase();
  const set = $("#setFilter").value, rarity = $("#rarityFilter").value, show = $("#show").value;
  const out = rows.filter((r) =>
    (show === "all" || r.in_scope) && (show !== "flagged" || r.flagged)
    && (!set || r.set === set) && (!rarity || r.rarity === rarity)
    && (!q || r.name.toLowerCase().includes(q) || (r.number || "").toLowerCase().includes(q)));
  out.sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    const d = typeof x === "number" && typeof y === "number" ? x - y : String(x ?? "").localeCompare(String(y ?? ""));
    return d * sortDir;
  });
  return out;
}

function renderCards() {
  const view = visibleRows();
  const tbody = $("#cards tbody");
  tbody.innerHTML = view.slice(0, shown).map((r) => {
    const badges = [
      r.flagged ? `<span class="badge flag">flagged</span>` : "",
      r.discord ? `<span class="badge discord">Discord</span>` : "",
      r.last_alert ? `<span class="badge">alerted ${esc(r.last_alert)}${r.posted ? ", posted" : ""}</span>` : "",
      r.in_scope ? "" : `<span class="badge">filtered out</span>`,
    ].join("");
    const name = r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a>` : esc(r.name);
    return `<tr class="${r.flagged ? "flagged" : ""} ${r.in_scope ? "" : "out"}">
      <td>${starBtn(r)}</td>
      <td class="num ${r.pct > 0 ? "up" : r.pct < 0 ? "down" : ""}">${r.pct > 0 ? "+" : ""}${r.pct.toFixed(1)}%</td>
      <td class="num" title="${esc(r.base_date)} (${esc(r.field)})">${money(r.base)}</td>
      <td class="num">${money(r.cur)}</td>
      <td>${name}</td><td>${esc(r.number)}</td><td>${esc(r.rarity)}</td>
      <td>${esc(r.printing)}</td><td>${esc(r.set)}</td>
      <td><button class="linkish" data-trend="${r.product_id}" data-printing="${esc(r.printing)}" data-field="${esc(r.field)}" title="Show price history">&#128200;</button></td>
      <td>${badges}</td></tr>`;
  }).join("");
  $("#more").hidden = view.length <= shown;
  $$("#cards th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === sortKey);
    th.classList.toggle("asc", th.dataset.sort === sortKey && sortDir === 1);
  });
  if (!meta) return;
  if (meta.message) { $("#summary").textContent = meta.message; return; }
  const scope = rows.filter((r) => r.in_scope);
  $("#summary").textContent =
    `Rule ${meta.rule}. Comparing ${meta.current_date} against ${meta.window_days} day(s) earlier. `
    + `${scope.length} cards in scope, ${scope.filter((r) => r.flagged).length} flagged, `
    + `${scope.filter((r) => r.discord).length} at or above the Discord threshold (${meta.discord_threshold_pct}%). `
    + `Showing ${Math.min(shown, view.length)} of ${view.length}.`;
}

$("#cardControls").onsubmit = (e) => { e.preventDefault(); loadCards(); };
$("#cardControls [name=game]").onchange = loadCards;
["#search", "#setFilter", "#rarityFilter", "#show"].forEach((s) =>
  $(s).addEventListener("input", () => { shown = PAGE; renderCards(); }));
$("#more").onclick = () => { shown += PAGE; renderCards(); };
$$("#cards th[data-sort]").forEach((th) => (th.onclick = () => {
  if (sortKey === th.dataset.sort) sortDir = -sortDir;
  else { sortKey = th.dataset.sort; sortDir = ["pct", "base", "cur"].includes(sortKey) ? -1 : 1; }
  renderCards();
}));

// ---- cards CSV export (client-side, RFC 4180, formula-injection guarded) -----------------
const CSV_COLS = ["name", "number", "rarity", "printing", "set", "base_date", "base", "cur", "pct", "flagged", "url"];
function csvCell(v) {
  if (v == null) return "";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";  // our own numbers, never user text
  let s = String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
function cardsCsv(view) {
  return [CSV_COLS.join(","), ...view.map((r) => CSV_COLS.map((c) => csvCell(r[c])).join(","))].join("\r\n") + "\r\n";
}
function downloadCardsCsv() {
  const blob = new Blob([cardsCsv(visibleRows())], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "cards.csv";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
$("#csvBtn").onclick = downloadCardsCsv;

// ---- settings ------------------------------------------------------------------------------
let settings = null;      // working copy of config.yaml
let webhookRemoved = false;

const getPath = (o, path) => path.split(".").reduce((x, k) => (x == null ? undefined : x[k]), o);
function setPath(o, path, value) {
  const keys = path.split(".");
  let cur = o;
  for (const k of keys.slice(0, -1)) cur = cur[k] ??= {};
  if (value === undefined) delete cur[keys.at(-1)];
  else cur[keys.at(-1)] = value;
}

// One form control bound to a config path. Blank optional values are removed (= inherit/default).
function field(path, label, type = "text", opts = {}) {
  const v = getPath(settings, path);
  const attrs = `data-path="${esc(path)}" data-type="${type}"`;
  let control;
  if (type === "bool") {
    return `<label class="field inline"><input type="checkbox" ${attrs} ${v ?? opts.default ? "checked" : ""}> ${esc(label)}</label>`;
  } else if (type === "select") {
    control = `<select ${attrs}>${opts.blank ? `<option value="">${esc(opts.blank)}</option>` : ""}${opts.options
      .map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`;
  } else if (type === "lines") {
    control = `<textarea ${attrs} placeholder="${esc(opts.placeholder || "one per line")}">${esc((v || []).join("\n"))}</textarea>`;
  } else {
    const shown = type === "csv" ? (v || []).join(", ") : v ?? "";
    const inputType = type === "num" || type === "int" ? "number" : "text";
    control = `<input type="${inputType}" step="any" ${attrs} value="${esc(shown)}" placeholder="${esc(opts.placeholder || "")}">`;
  }
  return `<label class="field ${opts.wide ? "wide" : ""}"><span>${esc(label)}</span>${control}</label>`;
}

function readField(el) {
  const raw = el.type === "checkbox" ? el.checked : el.value.trim();
  switch (el.dataset.type) {
    case "bool": return raw;
    case "num": return raw === "" ? undefined : Number(raw);
    case "int": return raw === "" ? undefined : parseInt(raw, 10);
    case "csv": return raw.split(",").map((s) => s.trim()).filter(Boolean);
    case "lines": return raw.split("\n").map((s) => s.trim()).filter(Boolean);
    default: return raw === "" ? undefined : raw;
  }
}

async function loadSettings() {
  $("#saveMsg").textContent = "";
  settings = (await api("/api/settings")).config;
  webhookRemoved = false;
  renderSettings();
}

function renderSettings() {
  const games = settings.games || {};
  const discord = getPath(settings, "notify.discord") || {};
  const hasHook = discord.webhook_url === WEBHOOK_MASK;
  $("#settings").innerHTML = `
    <fieldset><legend>Price movement (defaults for every game)</legend><div class="grid">
      ${field("alert.window", "Window (compare against N days ago)", "text", { placeholder: "7d" })}
      ${field("alert.threshold_pct", "Flag at % change", "num", { placeholder: "30" })}
      ${field("alert.direction", "Direction", "select", { options: ["up", "down", "both"], blank: "up (default)" })}
      ${field("alert.min_price", "Ignore cards under $", "num", { placeholder: "0.50" })}
      ${field("alert.min_abs_change", "Ignore moves under $", "num", { placeholder: "0.25" })}
      ${field("alert.cooldown", "Cooldown before re-alerting", "text", { placeholder: "same as window" })}
      ${field("alert.price_fields", "Price fields, in order", "csv", { placeholder: "market, mid" })}
      ${field("schedule.at", "Daily run time, UTC (tcgwatch schedule)", "text", { placeholder: "20:30" })}
    </div></fieldset>

    <fieldset><legend>Discord</legend><div class="grid">
      ${field("alert.discord_threshold_pct", "Post to Discord at % change", "num", { placeholder: "same as flag %" })}
      <label class="field wide"><span>Webhook URL</span>
        <input type="password" id="webhook" placeholder="${hasHook && !webhookRemoved ? "Saved. Leave blank to keep it, or paste a new one" : "https://discord.com/api/webhooks/..."}">
      </label>
      ${field("notify.discord.webhook_env", "Or read it from env var", "text", { placeholder: "TCGWATCH_DISCORD_WEBHOOK" })}
      <div class="field inline">
        ${hasHook && !webhookRemoved ? `<button type="button" id="removeHook">Remove saved webhook</button>` : ""}
        <button type="button" id="testHook">Send test message</button>
        <span class="muted">Test uses the saved settings.</span>
      </div>
      ${field("notify.console", "Print movers to the console", "bool", { default: true })}
    </div></fieldset>

    ${Object.keys(games).map(renderGame).join("")}

    <fieldset><legend>Add a game</legend><div class="grid">
      <label class="field"><span>Key (a-z, 0-9, _)</span><input id="newKey" placeholder="lorcana"></label>
      <label class="field"><span>Name</span><input id="newName" placeholder="Disney Lorcana"></label>
      <label class="field"><span>TCGplayer category ID</span><input id="newCat" type="number"></label>
      <div class="field inline"><button type="button" id="addGame">Add game</button>
        <span class="muted">Then set its rarities and save. Run a snapshot to load its sets.</span></div>
    </div></fieldset>`;

  $$("[data-remove]").forEach((b) => (b.onclick = () => {
    if (!confirm(`Remove ${b.dataset.remove} from settings? Stored prices stay in the database.`)) return;
    collectSettings();
    delete settings.games[b.dataset.remove];
    renderSettings();
  }));
  $("#addGame").onclick = () => {
    const key = $("#newKey").value.trim(), cat = parseInt($("#newCat").value, 10);
    if (!/^[a-z0-9_]+$/.test(key)) return alert("Key must be lowercase letters, digits and _");
    if (!cat) return alert("Category ID is required");
    collectSettings();
    settings.games ??= {};
    if (settings.games[key]) return alert("That key already exists");
    settings.games[key] = { name: $("#newName").value.trim() || key, enabled: true, source: "tcgcsv",
      category_id: cat, rarities: [], exclude_name_patterns: [], printings: [], include_sets: [], exclude_sets: [] };
    renderSettings();
  };
  const remove = $("#removeHook");
  if (remove) remove.onclick = () => { collectSettings(); webhookRemoved = true; renderSettings(); };
  $("#testHook").onclick = async () => {
    try { await api("/api/discord-test", { method: "POST" }); alert("Test message posted."); }
    catch (e) { alert(e.message); }
  };
  Object.keys(games).forEach(loadSetTable);
}

function renderGame(key) {
  const p = `games.${key}`;
  const g = settings.games[key];
  const patterns = (list) => (list || []).filter((s) => typeof s !== "number");
  return `<fieldset data-game="${esc(key)}"><legend>${esc(g.name || key)} <span class="muted">(${esc(key)})</span></legend>
    <div class="game-head">${field(`${p}.enabled`, "Enabled", "bool", { default: true })}
      <button type="button" class="remove" data-remove="${esc(key)}">Remove game</button></div>
    <div class="grid">
      ${field(`${p}.name`, "Name")}
      ${field(`${p}.category_id`, "TCGplayer category ID", "int")}
      ${field(`${p}.rarities`, "Rarities to include (blank = all)", "csv", { wide: true })}
      ${field(`${p}.exclude_name_patterns`, "Exclude cards whose name matches (regex, one per line)", "lines", { wide: true })}
      ${field(`${p}.printings`, "Printings (blank = all, e.g. Normal, Foil)", "csv")}
      ${field(`${p}.alert.window`, "Window override", "text", { placeholder: "inherit" })}
      ${field(`${p}.alert.threshold_pct`, "Flag % override", "num", { placeholder: "inherit" })}
      ${field(`${p}.alert.discord_threshold_pct`, "Discord % override", "num", { placeholder: "inherit" })}
      <label class="field"><span>Include sets by name (regex, one per line)</span>
        <textarea data-patterns="include" placeholder="blank = all sets">${esc(patterns(g.include_sets).join("\n"))}</textarea></label>
      <label class="field"><span>Exclude sets by name (regex, one per line)</span>
        <textarea data-patterns="exclude">${esc(patterns(g.exclude_sets).join("\n"))}</textarea></label>
      <div class="field wide"><span>Sets (pick individual sets; "Checked" reflects saved settings)</span>
        <div class="sets"><table><thead><tr><th>Set</th><th>ID</th><th class="num">Singles</th><th>Checked</th><th>Rule</th></tr></thead>
        <tbody data-sets="${esc(key)}"><tr><td colspan="5" class="muted">Loading...</td></tr></tbody></table></div></div>
    </div></fieldset>`;
}

async function loadSetTable(key) {
  const tbody = $(`[data-sets="${CSS.escape(key)}"]`);
  const g = settings.games[key];
  const ids = (list) => new Set((list || []).filter((s) => typeof s === "number"));
  const inc = ids(g.include_sets), exc = ids(g.exclude_sets);
  let sets = [];
  try { sets = await api(`/api/sets/${encodeURIComponent(key)}`); } catch { /* new, unsaved game */ }
  // Keep IDs from the config even if they aren't in the stored data.
  for (const id of [...inc, ...exc]) if (!sets.some((s) => s.id === id)) sets.push({ id, name: "(not in stored data)", singles: 0, checked: null });
  if (!sets.length) { tbody.innerHTML = `<tr><td colspan="5" class="muted">No sets stored yet. Run a snapshot first.</td></tr>`; return; }
  tbody.innerHTML = sets.map((s) => {
    const state = inc.has(s.id) ? "include" : exc.has(s.id) ? "exclude" : "";
    return `<tr><td>${esc(s.name)}</td><td>${s.id}</td><td class="num">${s.singles}</td>
      <td>${s.checked == null ? "" : s.checked ? "yes" : "no"}</td>
      <td><select data-set-id="${s.id}">
        <option value="" ${state === "" ? "selected" : ""}>by patterns</option>
        <option value="include" ${state === "include" ? "selected" : ""}>include</option>
        <option value="exclude" ${state === "exclude" ? "selected" : ""}>exclude</option>
      </select></td></tr>`;
  }).join("");
}

// Read the form back into `settings`.
function collectSettings() {
  $$("#settings [data-path]").forEach((el) => setPath(settings, el.dataset.path, readField(el)));
  $$("#settings fieldset[data-game]").forEach((fs) => {
    const g = settings.games[fs.dataset.game];
    const lines = (sel) => $(sel, fs).value.split("\n").map((s) => s.trim()).filter(Boolean);
    const picks = (state) => $$("[data-set-id]", fs).filter((s) => s.value === state).map((s) => Number(s.dataset.setId));
    const loaded = $$("[data-set-id]", fs).length > 0;
    const keepIds = (list) => (list || []).filter((s) => typeof s === "number");
    g.include_sets = [...lines("[data-patterns=include]"), ...(loaded ? picks("include") : keepIds(g.include_sets))];
    g.exclude_sets = [...lines("[data-patterns=exclude]"), ...(loaded ? picks("exclude") : keepIds(g.exclude_sets))];
    if (g.alert && !Object.keys(g.alert).length) delete g.alert;
  });
  const hook = $("#webhook");
  if (hook) {
    settings.notify ??= {};
    settings.notify.discord ??= {};
    const typed = hook.value.trim();
    const wasSaved = settings.notify.discord.webhook_url === WEBHOOK_MASK;
    settings.notify.discord.webhook_url = typed || (webhookRemoved ? "" : wasSaved ? WEBHOOK_MASK : "");
  }
}

$("#save").onclick = async () => {
  const msg = $("#saveMsg");
  collectSettings();
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ config: settings }) });
    msg.className = "ok"; msg.textContent = "Saved to config.yaml.";
    await loadSettings();
    $("#saveMsg").className = "ok"; $("#saveMsg").textContent = "Saved to config.yaml.";
    await loadStatus();
    loadCards();
  } catch (e) {
    msg.className = "err"; msg.textContent = e.message;
  }
};
$("#reload").onclick = loadSettings;

// ---- start ----------------------------------------------------------------------------------
loadStatus().then(loadCards);
