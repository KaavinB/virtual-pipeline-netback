// Static dashboard: reads data.json (exported from the SQL views) and renders five tabs.
// What-if arithmetic lives in netback.js, which is tested against the SQL.
import { evaluate, distanceCurve, crossover, dieselParity } from "./netback.js";

const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const usd = (x, dp = 2) => `${x < 0 ? "−" : ""}$${Math.abs(x).toFixed(dp)}`;
const musd = (x) => `${x < 0 ? "−" : ""}$${(Math.abs(x) / 1e6).toFixed(1)}M`;
const fmtDate = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString("en-US", { day: "numeric", month: "short", year: "numeric" });
const TABS = ["start", "overview", "economics", "sensitivity", "data"];

const CONTROLS = [
  { key: "henry_hub", label: "Henry Hub price", unit: "$/MMBtu", min: 1, max: 10, step: 0.05,
    fmt: (v) => usd(v), base: (d) => d.latest.henry_hub },
  { key: "diesel", label: "Diesel price", unit: "$/gal", min: 2, max: 8, step: 0.05,
    fmt: (v) => usd(v), base: (d) => d.latest.diesel },
  { key: "discount_pct", label: "Customer discount off diesel", unit: "%", min: 0, max: 95, step: 1,
    fmt: (v) => `${Math.round(v)}%`, base: (d) => d.base.discount_pct },
  { key: "waha_discount", label: "Waha discount to Henry Hub", unit: "$", min: 0, max: 4, step: 0.05,
    fmt: (v) => usd(v), base: (d) => d.base.waha_discount },
  { key: "customer_mi", label: "Miles to fuel customer", unit: "mi", min: 0, max: 300, step: 5,
    fmt: (v) => `${Math.round(v)} mi`, base: (d) => d.base.customer_mi },
];

const GLOSSARY = (d) => [
  ["MMBtu", "The unit gas is priced in: one million British thermal units, about 960 cubic feet of gas. Every $ figure here is per MMBtu."],
  ["Netback", "What the producer keeps per MMBtu after paying for every step: processing, trucking, delivery and fees. Flaring's netback is $0."],
  ["Henry Hub", `The US benchmark gas price, set in Louisiana. Latest: ${usd(d.latest.henry_hub)}/MMBtu.`],
  ["Hub, basis", "Pipeline trading points near the sites: Waha (Permian), Agua Dulce (South Texas), Katy (Houston). Basis is how far a hub trades below Henry Hub; Waha is about $2 below because Permian pipelines are full."],
  ["CNG / LNG", "Gas squeezed to high pressure (CNG) or chilled to a liquid (LNG) so it fits on a truck. CNG is cheaper to make; LNG carries more per load."],
  ["Diesel parity", "What the gas is worth as a diesel replacement: the cost of buying the same useful energy as diesel."],
  ["Customer discount", "How far below diesel parity a fuel customer pays, so that switching from diesel is worth it to them."],
  ["Break-even", "The gas price, distance or discount at which trucking earns exactly what flaring does ($0)."],
];

const QUESTIONS = (d) => [
  [`What if gas prices doubled, to ${usd(2 * d.latest.henry_hub)}?`, "overview", { henry_hub: +(2 * d.latest.henry_hub).toFixed(2) }],
  ["What if customers want 70% off the diesel price?", "overview", { discount_pct: 70 }],
  ["What if diesel fell to $3.50 a gallon?", "overview", { diesel: 3.5 }],
  ["What if the nearest customer is 150 miles away?", "overview", { customer_mi: 150 }],
  ["Where does the money go at a Permian site?", "economics", { site: "PB-07" }],
  ["What could change the answer for that site?", "sensitivity", { site: "PB-07" }],
];

let data;
const state = { tab: "start", inputs: {}, site: "EF-02" };
const charts = {};
let map, siteLayer, routeLayer;

// ── URL <-> state (every view is a shareable link) ───────────────────────────
function readHash() {
  const [tab, query = ""] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query);
  state.tab = TABS.includes(tab) ? tab : "start";
  state.inputs = {};
  for (const c of CONTROLS) if (params.has(c.key)) state.inputs[c.key] = Number(params.get(c.key));
  if (params.has("site") && data.sites.some((s) => s.site_name === params.get("site"))) state.site = params.get("site");
}
function writeHash(replace = true) {
  const params = new URLSearchParams();
  for (const c of CONTROLS) if (c.key in state.inputs) params.set(c.key, state.inputs[c.key]);
  if (state.site !== "EF-02") params.set("site", state.site);
  const q = params.toString();
  const hash = `#${state.tab}${q ? `?${q}` : ""}`;
  if (replace) history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);
}
function go(tab, inputs = {}, site) {
  state.tab = tab;
  state.inputs = { ...inputs };
  delete state.inputs.site;
  if (site || inputs.site) state.site = site || inputs.site;
  writeHash(false);
  render();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ── controls ────────────────────────────────────────────────────────────────
function buildControls() {
  $("#controls").innerHTML = CONTROLS.map((c) => `
    <div class="control" data-key="${c.key}">
      <div class="control-top"><label for="in-${c.key}">${c.label}</label><b data-value></b></div>
      <input type="range" id="in-${c.key}" min="${c.min}" max="${c.max}" step="${c.step}" aria-describedby="base-${c.key}">
      <div class="base" id="base-${c.key}">today: ${c.fmt(c.base(data))}</div>
    </div>`).join("");
  for (const c of CONTROLS) {
    $(`#in-${c.key}`).addEventListener("input", (e) => {
      state.inputs[c.key] = Number(e.target.value);
      writeHash();
      renderLive();
    });
  }
  $("#site-select").innerHTML = data.sites.map((s) =>
    `<option value="${s.site_name}">${s.site_name} · ${s.basin}</option>`).join("");
  $("#site-select").addEventListener("change", (e) => { state.site = e.target.value; writeHash(); renderLive(); });
  $("#toggle-controls").addEventListener("click", (e) => {
    const open = $("#controls").classList.toggle("open");
    e.currentTarget.setAttribute("aria-expanded", String(open));
    e.currentTarget.textContent = open ? "Hide what-if inputs ▴" : "Adjust what-if inputs ▾";
  });
  $("#reset").addEventListener("click", () => { state.inputs = {}; writeHash(); render(); });
  $("#share").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(location.href); toast("Link copied: it opens this exact view"); }
    catch { toast("Copy the address bar to share this view"); }
  });
}
function syncControls() {
  for (const c of CONTROLS) {
    const changed = c.key in state.inputs;
    const v = changed ? state.inputs[c.key] : c.base(data);
    const box = document.querySelector(`.control[data-key="${c.key}"]`);
    box.classList.toggle("changed", changed);
    box.querySelector("[data-value]").textContent = c.fmt(v);
    const input = $(`#in-${c.key}`);
    if (document.activeElement !== input) input.value = v;
  }
  $("#site-select").value = state.site;
  const n = Object.keys(state.inputs).length;
  $("#scenario-status").innerHTML = n
    ? `Showing <strong>your what-if</strong>: ${n} input${n > 1 ? "s" : ""} changed from today. <em>Reset to today</em> undoes it.`
    : `Showing <strong>today's base case</strong>: EIA prices of ${fmtDate(data.as_of)}. Drag a slider to ask “what if?”.`;
  $("#reset").disabled = n === 0;
  $("#site-pick").hidden = !["economics", "sensitivity"].includes(state.tab);
  $("#scenario").hidden = ["start", "data"].includes(state.tab);
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2200);
}

// ── chart plumbing ──────────────────────────────────────────────────────────
// Dashed "Flaring = $0" reference line on the value axis.
const flareLine = {
  id: "flareLine",
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || !opts.show) return;
    const horizontal = chart.options.indexAxis === "y";
    const scale = horizontal ? chart.scales.x : chart.scales.y;
    if (!scale || scale.min > 0 || scale.max < 0) return;
    const { ctx, chartArea: a } = chart;
    const p = scale.getPixelForValue(0);
    ctx.save();
    ctx.strokeStyle = css("--ink-2");
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    if (horizontal) { ctx.moveTo(p, a.top); ctx.lineTo(p, a.bottom); }
    else { ctx.moveTo(a.left, p); ctx.lineTo(a.right, p); }
    ctx.stroke();
    if (opts.label && !horizontal) {  // drawn in the right margin, never on top of a bar
      ctx.setLineDash([]);
      ctx.fillStyle = css("--ink-2");
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      opts.label.split("\n").forEach((line, i) => ctx.fillText(line, a.right + 6, p + (i - 0.5) * 13));
    }
    ctx.restore();
  },
};
// Value labels above the bars of a money walk.
const walkLabels = {
  id: "walkLabels",
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || !opts.values) return;
    const { ctx } = chart;
    const meta = chart.getDatasetMeta(0);
    ctx.save();
    ctx.fillStyle = css("--ink");
    ctx.font = "600 11px system-ui, sans-serif";
    ctx.textAlign = "center";
    meta.data.forEach((bar, i) => {
      const top = Math.min(bar.y, bar.base);
      ctx.fillText(opts.values[i], bar.x, top - 6);
    });
    ctx.restore();
  },
};

function baseOptions(extra = {}) {
  return {
    layout: { padding: { right: 62 } },  // room for the "Flaring = $0" label
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 250 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "top", align: "start", labels: { boxWidth: 12, boxHeight: 12, color: css("--ink-2"), usePointStyle: true, pointStyle: "rectRounded" } },
      tooltip: { backgroundColor: css("--ink"), titleColor: css("--surface"), bodyColor: css("--surface"), padding: 10, cornerRadius: 6 },
    },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--muted") } },
      y: { grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted") } },
    },
    ...extra,
  };
}
function draw(id, config) {
  const existing = charts[id];
  if (existing) {
    existing.data = config.data;
    existing.options = config.options;
    existing.update("none");
    return;
  }
  charts[id] = new Chart(document.getElementById(id), { plugins: [flareLine, walkLabels], ...config });
}
function resetCharts() {
  for (const id of Object.keys(charts)) { charts[id].destroy(); delete charts[id]; }
}
const yTitle = (text) => ({ display: true, text, color: css("--ink-2") });
const weekTick = (labels) => (value, i) => {
  const d = new Date(`${labels[i]}T00:00:00`);
  return d.getMonth() === 0 && d.getDate() <= 7 ? String(d.getFullYear()) : "";
};

// ── Start here ──────────────────────────────────────────────────────────────
function renderStart() {
  const base = evaluate(data, {});
  const med = (arr) => { const s = [...arr].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  const fuel = med(base.sites.map((s) => s.bestFuel.netback));
  const hub = med(base.sites.map((s) => s.bestHub.netback));
  const fuelWins = base.sites.filter((s) => s.bestFuel.netback > 0).length;
  const hubWins = base.sites.filter((s) => s.bestHub.netback > 0).length;
  const be = base.sites.map((s) => s.breakEvenHenryHub);
  const cngAll = base.sites.every((s) => s.best.mode === "CNG" && s.bestHub.mode === "CNG");
  $("#short-answer").innerHTML = `
    <li><strong>Sell the gas as fuel</strong> to drilling rigs and frac crews nearby, in place of diesel: the
      producer keeps about <strong>${usd(fuel)} per MMBtu</strong>. ${fuelWins} of ${base.sites.length} sites beat flaring.</li>
    <li><strong>Sell it into a pipeline</strong> at a trading hub: about <strong>${usd(hub)} per MMBtu</strong>,
      ${hubWins === 0 ? `a loss at all ${base.sites.length} sites` : `better than flaring at ${hubWins} sites`}. Pipeline gas is cheap
      (${usd(data.latest.henry_hub)}) and trucking is not: it would take ${usd(Math.min(...be))}–${usd(Math.max(...be))} gas to break even.</li>
    ${cngAll ? "<li><strong>CNG</strong> (compressed gas) beats <strong>LNG</strong> (liquefied) on every route here.</li>" : ""}
    <li class="hint">Prices of ${fmtDate(data.as_of)} (EIA).</li>`;
  $("#questions").innerHTML = QUESTIONS(data).map(([text], i) => `<button type="button" data-q="${i}">${text}</button>`).join("");
  document.querySelectorAll("#questions button").forEach((b) => b.addEventListener("click", () => {
    const [, tab, inputs] = QUESTIONS(data)[Number(b.dataset.q)];
    go(tab, inputs);
  }));
  $("#glossary").innerHTML = GLOSSARY(data).map(([t, d]) => `<div><dt>${t}</dt><dd>${d}</dd></div>`).join("");
}

// ── Overview ────────────────────────────────────────────────────────────────
function renderOverview(result) {
  const sites = result.sites;
  const med = (arr) => { const s = [...arr].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  const wins = sites.filter((s) => s.best.netback > 0).length;
  const value = sites.reduce((t, s) => t + Math.max(0, s.annualValueUsd), 0);
  const fuelMed = med(sites.map((s) => s.bestFuel.netback));
  const hubMed = med(sites.map((s) => s.bestHub.netback));
  const whatif = Object.keys(state.inputs).length ? "Under your what-if, " : "At today's prices, ";
  $("#headline").innerHTML = `${whatif}<strong>${wins} of ${sites.length} sites</strong> should truck their gas rather than flare it.
    Selling it as fuel nets a median <strong class="${fuelMed >= 0 ? "pos" : "neg"}">${usd(fuelMed)}</strong>/MMBtu; selling it into a pipeline, <strong class="${hubMed >= 0 ? "pos" : "neg"}">${usd(hubMed)}</strong>.`;
  $("#kpis").innerHTML = [
    ["Sites where trucking beats flaring", `${wins} of ${sites.length}`, "best option nets more than $0"],
    ["Selling as fuel (median)", usd(fuelMed), "$ kept per MMBtu"],
    ["Selling into a pipeline (median)", usd(hubMed), "$ kept per MMBtu"],
    ["Value vs flaring, per year", musd(value), "if each site's flared gas went to its best option"],
  ].map(([l, v, n]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="note">${n}</div></div>`).join("");

  draw("chart-sites", {
    type: "bar",
    data: {
      labels: sites.map((s) => s.site.site_name),
      datasets: [
        { label: "Sell as fuel (in-basin customer)", data: sites.map((s) => s.bestFuel.netback), backgroundColor: css("--s1"), borderRadius: 4, borderSkipped: false, barPercentage: 0.85, categoryPercentage: 0.75 },
        { label: "Sell into a pipeline hub", data: sites.map((s) => s.bestHub.netback), backgroundColor: css("--s2"), borderRadius: 4, borderSkipped: false, barPercentage: 0.85, categoryPercentage: 0.75 },
      ],
    },
    options: baseOptions({
      plugins: {
        ...baseOptions().plugins,
        flareLine: { show: true, label: "Flaring\n= $0" },
        tooltip: { ...baseOptions().plugins.tooltip, callbacks: {
          label: (c) => {
            const s = sites[c.dataIndex];
            const o = c.datasetIndex === 0 ? s.bestFuel : s.bestHub;
            return ` ${c.dataset.label}: ${usd(c.parsed.y)} (${o.label})`;
          } } },
      },
      scales: { ...baseOptions().scales, y: { ...baseOptions().scales.y, title: yTitle("$ kept per MMBtu") } },
    }),
  });
  renderMap(result);
  renderScorecard(result);
}

function renderMap(result) {
  if (!window.L) return;
  if (!map) {
    map = L.map("map", { scrollWheelZoom: false, attributionControl: true }).setView([30.0, -100.3], 6);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 12, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    for (const h of data.hubs) {
      L.marker([h.lat, h.lon], { icon: L.divIcon({ className: "", html: '<div class="hub-icon"></div>', iconSize: [14, 14] }) })
        .bindTooltip(`<b>${h.hub_name}</b><br>pipeline hub`).addTo(map);
    }
    siteLayer = L.layerGroup().addTo(map);
    routeLayer = L.layerGroup().addTo(map);
    map.fitBounds(L.latLngBounds([...data.sites.map((s) => [s.lat, s.lon]), ...data.hubs.map((h) => [h.lat, h.lon])]).pad(0.08));
  }
  siteLayer.clearLayers();
  for (const r of result.sites) {
    const ok = r.best.netback > 0;
    const m = L.circleMarker([r.site.lat, r.site.lon], {
      radius: 5 + 3 * Math.sqrt(r.site.flared_mmscfd), color: css("--surface"), weight: 2,
      fillColor: ok ? css("--good") : css("--bad"), fillOpacity: 0.95,
    }).bindTooltip(`<b>${r.site.site_name}</b> · ${r.site.basin}<br>${ok ? "✓ Truck: " + r.best.label : "✗ Flare: nothing pays"}<br>${usd(r.best.netback)}/MMBtu · ${r.site.flared_mmscfd.toFixed(2)} MMcf/d flared`);
    m.on("click", () => { state.site = r.site.site_name; writeHash(); drawRoute(result); renderScorecard(result); });
    siteLayer.addLayer(m);
  }
  drawRoute(result);
}
function drawRoute(result) {
  if (!routeLayer) return;
  routeLayer.clearLayers();
  const r = result.sites.find((s) => s.site.site_name === state.site);
  if (!r) return;
  const path = data.routes[`${r.site.site_name}|${r.bestHub.hub}`];
  if (path) {
    L.polyline(path, { color: css("--s2"), weight: 3, opacity: 0.9 })
      .bindTooltip(`${r.site.site_name} → ${r.bestHub.label.split("→ ")[1]}: ${r.bestHub.oneWayMi.toFixed(0)} mi by road`).addTo(routeLayer);
  }
}

function renderScorecard(result) {
  const rows = result.sites.map((r) => {
    const ok = r.best.netback > 0;
    return `<tr class="${r.site.site_name === state.site ? "selected" : ""}" data-site="${r.site.site_name}">
      <td class="nowrap">${r.site.site_name}</td>
      <td class="num">${r.site.flared_mmscfd.toFixed(2)}</td>
      <td><span class="status ${ok ? "good" : "bad"}">${ok ? "✓" : "✗"}</span>${ok ? "Truck: " + r.best.label : "Flare: nothing pays"}</td>
      <td class="num ${ok ? "pos" : "neg"}">${usd(r.best.netback)}</td>
      <td class="num">${musd(r.annualValueUsd)}</td>
      <td class="num">${usd(r.breakEvenHenryHub)}</td></tr>`;
  }).join("");
  $("#scorecard").innerHTML = `<thead><tr><th>Site</th><th class="num">Flared MMcf/d</th><th>Recommendation</th>
    <th class="num">$ kept /MMBtu</th><th class="num">vs flaring /yr</th><th class="num">Pipeline break-even</th></tr></thead><tbody>${rows}</tbody>`;
  document.querySelectorAll("#scorecard tbody tr").forEach((tr) => tr.addEventListener("click", () => {
    state.site = tr.dataset.site; writeHash(); renderScorecard(result); drawRoute(result);
  }));
}

// ── Economics ───────────────────────────────────────────────────────────────
function walk(id, option, color) {
  const steps = [
    ["Price", option.price],
    [option.mode === "CNG" ? "Compress" : "Liquefy", -option.conditioning],
    ["Trucking", -option.trucking],
    ["Delivery", -option.delivery],
    ["Fees", -option.fee],
  ];
  let run = 0;
  const bars = steps.map(([, v]) => { const from = run; run += v; return [from, run]; });
  bars.push([0, option.netback]);
  const labels = [...steps.map(([s]) => s), "Kept"];
  const values = [...steps.map(([, v]) => usd(v)), usd(option.netback)];
  const colors = [color, ...steps.slice(1).map(() => css("--cost")), css("--kept")];
  draw(id, {
    type: "bar",
    data: { labels, datasets: [{ label: "$/MMBtu", data: bars, backgroundColor: colors, borderRadius: 4, borderSkipped: false, barPercentage: 0.7 }] },
    options: baseOptions({
      interaction: { mode: "nearest", intersect: true },
      layout: { padding: { top: 18, right: 62 } },
      plugins: {
        ...baseOptions().plugins,
        legend: { display: false },
        walkLabels: { values },
        flareLine: { show: true, label: "Flaring\n= $0" },
        tooltip: { ...baseOptions().plugins.tooltip, callbacks: { label: (c) => ` ${labels[c.dataIndex]}: ${values[c.dataIndex]} per MMBtu` } },
      },
      scales: { ...baseOptions().scales, y: { ...baseOptions().scales.y, title: yTitle("$ per MMBtu") } },
    }),
  });
}
function renderEconomics(result) {
  const r = result.sites.find((s) => s.site.site_name === state.site);
  $("#walk-fuel-title").textContent = `Selling as fuel at ${r.site.site_name}: where each $ goes`;
  $("#walk-fuel-hint").textContent = `Best option: ${r.bestFuel.label}, ${r.bestFuel.oneWayMi.toFixed(0)} mi to the customer. Kept: ${usd(r.bestFuel.netback)}/MMBtu.`;
  $("#walk-hub-title").textContent = `Selling into a pipeline at ${r.site.site_name}: where each $ goes`;
  $("#walk-hub-hint").textContent = `Best option: ${r.bestHub.label}, ${r.bestHub.oneWayMi.toFixed(0)} mi by road. Kept: ${usd(r.bestHub.netback)}/MMBtu.`;
  walk("chart-walk-fuel", r.bestFuel, css("--s1"));
  walk("chart-walk-hub", r.bestHub, css("--s2"));

  const curve = distanceCurve(data, state.inputs);
  const palette = [css("--s1"), css("--s2"), css("--s3"), css("--s4")];
  draw("chart-distance", {
    type: "line",
    data: { labels: curve.miles, datasets: curve.series.map((s, i) => ({
      label: s.label, data: s.values, borderColor: palette[i], backgroundColor: palette[i], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
      borderDash: s.mode === "LNG" ? [6, 4] : [] })) },
    options: baseOptions({
      plugins: { ...baseOptions().plugins, flareLine: { show: true, label: "Flaring\n= $0" },
        tooltip: { ...baseOptions().plugins.tooltip, callbacks: { title: (c) => `${c[0].label} miles one way`, label: (c) => ` ${c.dataset.label}: ${usd(c.parsed.y)}` } } },
      scales: {
        x: { ...baseOptions().scales.x, title: yTitle("One-way miles"), ticks: { color: css("--muted"), maxTicksLimit: 11 } },
        y: { ...baseOptions().scales.y, title: yTitle("$ kept per MMBtu") },
      },
    }),
  });

  const pw = data.prices_weekly;
  draw("chart-prices", {
    type: "line",
    data: { labels: pw.map((p) => p.week), datasets: [
      { label: "As a diesel replacement (LHV-adjusted)", data: pw.map((p) => p.diesel_parity), borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 0 },
      { label: "As pipeline gas (Henry Hub)", data: pw.map((p) => p.henry_hub), borderColor: css("--s2"), backgroundColor: css("--s2"), borderWidth: 2, pointRadius: 0 },
    ] },
    options: baseOptions({
      plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: { title: (c) => `Week of ${fmtDate(c[0].label)}`, label: (c) => ` ${c.dataset.label}: ${usd(c.parsed.y)}` } } },
      scales: { x: { ...baseOptions().scales.x, ticks: { color: css("--muted"), autoSkip: false, maxRotation: 0, callback: weekTick(pw.map((p) => p.week)) } },
        y: { ...baseOptions().scales.y, beginAtZero: true, title: yTitle("$ per MMBtu") } },
    }),
  });

  const hw = data.history_weekly;
  draw("chart-history", {
    type: "line",
    data: { labels: hw.map((h) => h.week), datasets: [
      { label: "Sell as fuel", data: hw.map((h) => h.fuel), borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 0 },
      { label: "Sell into a pipeline", data: hw.map((h) => h.hub), borderColor: css("--s2"), backgroundColor: css("--s2"), borderWidth: 2, pointRadius: 0 },
    ] },
    options: baseOptions({
      plugins: { ...baseOptions().plugins, flareLine: { show: true, label: "Flaring\n= $0" }, tooltip: { ...baseOptions().plugins.tooltip, callbacks: { title: (c) => `Week of ${fmtDate(c[0].label)}`, label: (c) => ` ${c.dataset.label}: ${usd(c.parsed.y)}` } } },
      scales: { x: { ...baseOptions().scales.x, ticks: { color: css("--muted"), autoSkip: false, maxRotation: 0, callback: weekTick(hw.map((h) => h.week)) } },
        y: { ...baseOptions().scales.y, title: yTitle("$ kept per MMBtu (site average)") } },
    }),
  });

  const x = crossover(data, state.inputs);
  $("#crossover").innerHTML = x.map((c) => `
    <div class="stat"><div class="label">${c.market === "fuel" ? "Selling as fuel" : "Selling into a pipeline"}: LNG is cheaper only beyond</div>
      <div class="value">${Math.round(c.miles).toLocaleString()} mi</div>
      <div class="label">one way. LNG starts ${usd(c.lngFixed - c.cngFixed)}/MMBtu behind on liquefaction.</div></div>`).join("")
    + `<p class="hint">Every route in this study is under 600 miles, so CNG wins everywhere.</p>`;
}

// ── Sensitivity ─────────────────────────────────────────────────────────────
function tornado(id, market, hintId) {
  const rows = data.tornado.filter((t) => t.site_name === state.site && t.market === market).slice(0, 8);
  const unit = (t, v) => (t.unit.startsWith("usd") ? usd(v) : t.unit === "fraction" ? `${Math.round(v * 100)}%` : `${(+v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`);
  $(`#${hintId}`).textContent = `${state.site}, at today's base case. Bars = change in $ kept per MMBtu.`;
  draw(id, {
    type: "bar",
    data: { labels: rows.map((t) => t.driver_label), datasets: [
      { label: "Assumption at its low end", data: rows.map((t) => t.low_delta), backgroundColor: css("--s3"), borderRadius: 4, borderSkipped: false },
      { label: "Assumption at its high end", data: rows.map((t) => t.high_delta), backgroundColor: css("--s7"), borderRadius: 4, borderSkipped: false },
    ] },
    options: baseOptions({
      indexAxis: "y",
      interaction: { mode: "nearest", intersect: true },
      plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: {
        label: (c) => {
          const t = rows[c.dataIndex];
          const end = c.datasetIndex === 0 ? t.low_input : t.high_input;
          return ` ${c.datasetIndex === 0 ? "Low" : "High"} end (${unit(t, end)}): ${c.parsed.x >= 0 ? "+" : ""}${usd(c.parsed.x)}`;
        } } } },
      scales: {
        x: { stacked: true, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: (v) => usd(v, 1) }, title: yTitle("Change vs today's base case ($/MMBtu)") },
        y: { stacked: true, grid: { display: false }, border: { color: css("--axis") }, ticks: { color: css("--ink-2") } },
      },
    }),
  });
}
function renderSensitivity(result) {
  tornado("chart-tornado-fuel", "fuel", "tornado-fuel-hint");
  tornado("chart-tornado-hub", "hub", "tornado-hub-hint");
  const parity = dieselParity(data, result.inputs.diesel);
  $("#breakevens").innerHTML = `<thead><tr><th>Site</th><th>Basin</th><th class="num">Fuel: $ kept</th>
    <th class="num">Fuel pays until a discount of</th><th>Best pipeline option</th><th class="num">Pipeline: $ kept</th>
    <th class="num">Pipeline pays above Henry Hub of</th></tr></thead><tbody>${result.sites.map((r) => `
    <tr class="${r.site.site_name === state.site ? "selected" : ""}"><td class="nowrap">${r.site.site_name}</td><td>${r.site.basin}</td>
      <td class="num ${r.bestFuel.netback > 0 ? "pos" : "neg"}">${usd(r.bestFuel.netback)}</td>
      <td class="num">${Math.round(100 * r.breakEvenDiscount)}%</td><td>${r.bestHub.label}</td>
      <td class="num ${r.bestHub.netback > 0 ? "pos" : "neg"}">${usd(r.bestHub.netback)}</td>
      <td class="num">${usd(r.breakEvenHenryHub)}</td></tr>`).join("")}</tbody>
    <caption class="hint" style="caption-side:bottom;text-align:left;padding-top:8px">Diesel parity under these inputs: ${usd(parity)}/MMBtu.</caption>`;
}

// ── Data & method ───────────────────────────────────────────────────────────
function renderData() {
  const label = { sourced: "sourced", derived: "derived", rough_estimate: "estimate" };
  $("#assumptions").innerHTML = `<thead><tr><th>Assumption</th><th class="num">Value</th><th>Unit</th><th>Basis</th><th>Source</th></tr></thead><tbody>${
    data.assumptions.map((a) => `<tr><td>${a.key.replaceAll("_", " ")}</td>
      <td class="num">${a.value === null ? a.value_text : (+a.value).toLocaleString(undefined, { maximumFractionDigits: 4 })}</td>
      <td>${a.unit.replaceAll("_", " ")}</td>
      <td><span class="chip ${a.basis === "rough_estimate" ? "est" : ""}">${label[a.basis]}</span></td>
      <td class="src">${a.url ? `<a href="${a.url}" target="_blank" rel="noopener">${a.source}</a>` : a.source}</td></tr>`).join("")}</tbody>`;
  $("#dq").innerHTML = `<thead><tr><th>Status</th><th>Check</th><th class="num">Observed</th><th class="num">Threshold</th></tr></thead><tbody>${
    data.dq.map((q) => {
      const cls = q.passed ? "good" : q.severity === "error" ? "bad" : "warn";
      const txt = q.passed ? "Pass" : q.severity === "error" ? "Fail" : "Warning";
      return `<tr><td><span class="status ${cls}">${q.passed ? "✓" : "!"}</span>${txt}</td><td>${q.check_name.replaceAll("_", " ")}</td>
        <td class="num">${q.observed}</td><td class="num">${q.threshold}</td></tr>`;
    }).join("")}</tbody>`;
  $("#generated").textContent = `Snapshot built ${new Date(data.generated_at).toLocaleString()} · prices as of ${fmtDate(data.as_of)}.`;
}

// ── render loop ─────────────────────────────────────────────────────────────
function renderLive() {
  syncControls();
  const result = evaluate(data, state.inputs);
  if (state.tab === "overview") renderOverview(result);
  if (state.tab === "economics") renderEconomics(result);
  if (state.tab === "sensitivity") renderSensitivity(result);
}
function render() {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === state.tab));
  renderLive();
  if (state.tab === "overview" && map) setTimeout(() => map.invalidateSize(), 0);
}

async function main() {
  data = await (await fetch("data.json")).json();
  $("#asof").textContent = `Prices as of ${fmtDate(data.as_of)}`;
  Chart.defaults.font.family = "system-ui, -apple-system, 'Segoe UI', sans-serif";
  buildControls();
  renderStart();
  renderData();
  readHash();
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
    state.tab = b.dataset.tab; writeHash(false); render();
  }));
  window.addEventListener("hashchange", () => { readHash(); render(); });
  window.addEventListener("popstate", () => { readHash(); render(); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { resetCharts(); render(); });
  render();
}
main().catch((err) => {
  document.querySelector("main").innerHTML = `<div class="card"><h3>Could not load the data</h3><p class="hint">${err}</p></div>`;
});
