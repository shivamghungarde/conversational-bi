"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
};
const SVG_NS = "http://www.w3.org/2000/svg";
const s = (tag, attrs = {}) => {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};
// Chart colours: Tableau 10's hue order (blue, orange, red, teal, green, yellow, purple, pink, brown, grey),
// deepened to stand out on the warm cream theme. Every colour is at least 3:1 against the chart panel.
const TABLEAU_10 = ["#4453b5", "#d0652c", "#c4414b", "#2b8a84", "#4f8a42", "#b5861f", "#8a569b", "#c95c7c", "#86603f", "#7f7770"];
// For more than 10 members, each hue is paired with a deeper shade (light tints would vanish on cream).
const TABLEAU_20 = ["#4453b5", "#2f3b8a", "#d0652c", "#9e4a1d", "#c4414b", "#8f2e36", "#2b8a84", "#1d625e", "#4f8a42", "#36632d",
  "#b5861f", "#84611a", "#8a569b", "#633d70", "#c95c7c", "#94415a", "#86603f", "#5f442c", "#7f7770", "#57514c"];
const SERIES = TABLEAU_10;

// A member keeps one colour everywhere ("India" is the same in every chart), like a dashboard's colour legend.
// Tableau 10 for up to 10 members; Tableau 20 for larger breakdowns, as Tableau does.
function memberColor(dim, value) {
  const d = state.model && state.model.dimensions.find((x) => x.name === dim);
  const values = d ? d.values : [];
  const palette = values.length > 10 ? TABLEAU_20 : TABLEAU_10;
  const i = values.indexOf(String(value));
  return i < 0 ? "#7f7770" : palette[i % palette.length];
}
const GRAINS = ["day", "week", "month", "quarter", "year"];

const state = { datasets: [], models: {}, samples: {}, selected: "auto", model: null, context: null,
                contextDataset: null, placeholderTimer: null };
let VW = 760; // chart width in px, measured from the message column so text stays legible on phones

// ---------- formatting ----------
function fmt(v, kind) {
  if (v === null || v === undefined) return "–";
  if (kind === "percent") return `${(v * 100).toFixed(1)}%`;
  if (kind === "pts") return `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)} pts`;
  if (kind === "currency") {
    const a = Math.abs(v), sign = v < 0 ? "-" : "";
    if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
    if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(2)}M`;
    if (a >= 1e4) return `${sign}$${(a / 1e3).toFixed(1)}K`;
    return `${sign}$${a.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (typeof v === "number") return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
  return String(v);
}
function fmtChange(v, kind = "percent") {
  if (v === null || v === undefined) return "–";
  const arrow = v > 0 ? "▲" : v < 0 ? "▼" : "";
  return kind === "pts" ? `${arrow} ${Math.abs(v * 100).toFixed(1)} pts` : `${arrow} ${Math.abs(v * 100).toFixed(1)}%`;
}
const fmtContribution = (v, kind) => (kind === "percent" ? fmt(v, "pts") : (v > 0 ? "+" : "") + fmt(v, kind));
function ago(iso) {
  if (!iso) return "";
  const t = new Date(String(iso).replace(" ", "T"));
  const secs = Math.round((Date.now() - t) / 1000);
  if (Number.isNaN(secs)) return iso;
  if (secs < 60) return `${Math.max(secs, 0)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)} h ago`;
  return t.toLocaleDateString();
}
const metricOf = (name) => state.model.metrics.find((m) => m.name === name);
const dimOf = (name) => state.model.dimensions.find((d) => d.name === name) || { label: name };
const clone = (o) => JSON.parse(JSON.stringify(o));
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------- API ----------
async function api(path, body, dataset) {
  const ds = dataset === undefined ? state.selected : dataset;
  const url = body ? path : `${path}${path.includes("?") ? "&" : "?"}dataset=${encodeURIComponent(ds || "")}`;
  const res = await fetch(url, body ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dataset: ds, ...body }),
  } : {});
  return res.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 4000);
}

// ---------- data sources ----------
const colorOf = (id) => SERIES[Math.max(0, state.datasets.findIndex((d) => d.id === id)) % SERIES.length];
const sourceOf = (id) => state.datasets.find((d) => d.id === id);
const listJoin = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

async function boot(selectId) {
  const { datasets } = await api("/api/datasets", undefined, "");
  state.datasets = datasets;
  const loaded = await Promise.all(datasets.map((d) =>
    Promise.all([api("/api/model", undefined, d.id), api("/api/samples", undefined, d.id)])));
  datasets.forEach((d, i) => { state.models[d.id] = loaded[i][0]; state.samples[d.id] = loaded[i][1].samples; });
  const pick = $("#source");
  pick.replaceChildren(
    ...(datasets.length > 1 ? [h("option", { value: "auto", title: "Each question goes to the source whose measures match it" }, "All sources")] : []),
    ...datasets.map((d) => h("option", { value: d.id }, `${d.label} · ${d.type}`)));
  const remembered = recall("source");
  const wanted = selectId || remembered;
  state.selected = wanted && (wanted === "auto" || sourceOf(wanted)) && (wanted !== "auto" || datasets.length > 1)
    ? wanted : (datasets.length > 1 ? "auto" : datasets[0].id);
  pick.value = state.selected;
  pick.onchange = () => selectSource(pick.value);
  renderSources();
  refreshSuggestions();
  renderHelp();
}

// Choosing a source up front skips automatic routing and "which source?" questions (and keeps any LLM prompt small).
function selectSource(id) {
  state.selected = id;
  $("#source").value = id;
  remember("source", id);
  renderSources();
  refreshSuggestions();
}
function recall(key) {
  try { return localStorage.getItem(`askdata.${key}`); } catch (e) { return null; }
}
function remember(key, value) {
  try { localStorage.setItem(`askdata.${key}`, value); } catch (e) { /* private mode: fine without it */ }
}

function activeSamples() {
  if (state.selected !== "auto") return state.samples[state.selected] || [];
  const lists = state.datasets.map((d) => state.samples[d.id] || []);  // interleave every source's samples
  const out = [];
  for (let i = 0; i < Math.max(0, ...lists.map((l) => l.length)); i++) for (const l of lists) if (l[i]) out.push(l[i]);
  return out;
}

function refreshSuggestions() {
  const samples = activeSamples();
  rotatePlaceholder(samples);
  if (!$("#thread").children.length) renderEmpty(samples);
}

function renderEmpty(samples) {
  $("#empty").hidden = false;
  // Three starters: a headline number, a comparison and a "why" if there is one.
  const isWhy = (q) => /^(why|what drove)/i.test(q);
  const why = samples.find(isWhy);
  const picks = [...new Set([...samples.filter((q) => q !== why).slice(0, why ? 2 : 3), why].filter(Boolean))];
  const kind = (q) => (isWhy(q) ? "Understand a change" : /trend|monthly|weekly|quarterly|over time/i.test(q) ? "See a trend"
    : /\bby\b|\btop\b|which|highest|lowest/i.test(q) ? "Compare" : "Get a number");
  $("#starters").replaceChildren(...picks.map((q) =>
    h("button", { class: "starter", type: "button", onclick: () => submit(q) }, h("small", {}, kind(q)), q)));
}

function rotatePlaceholder(samples) {
  const input = $("#question");
  clearInterval(state.placeholderTimer);
  let i = 0;
  const tick = () => {
    if (!input.value && samples.length) input.placeholder = `Try: “${samples[i % samples.length]}”`;
    i += 1;
  };
  tick();
  state.placeholderTimer = setInterval(tick, 4000);
}
$("#question").addEventListener("keydown", (e) => {
  const input = e.target;
  if (e.key === "Tab" && !input.value && input.placeholder.startsWith("Try:")) {
    e.preventDefault();
    input.value = input.placeholder.slice(6, -1);
  }
});

// Date and time, e.g. "25 Oct 2026, 23:22", used for every timestamp in the UI.
function refreshedAt(iso, withSeconds) {
  if (!iso) return "";
  const t = new Date(String(iso).replace(" ", "T"));
  if (Number.isNaN(+t)) return "";
  const date = t.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}) });
  return `${date}, ${time}`;
}
const typeBadge = (word) => h("span", { class: "type-badge" }, word);

// The header button mirrors the source chosen in the question bar, followed by its last refresh time.
function renderSources() {
  const btn = $("#sources-btn");
  const chosen = state.selected === "auto" ? null : sourceOf(state.selected);
  const pool = chosen ? [chosen] : state.datasets;
  const latest = pool.map((d) => d.freshness && d.freshness.latest_update).filter(Boolean).sort().pop();
  btn.classList.toggle("is-live", pool.some((d) => d.live));
  $("#sources-text").replaceChildren(
    chosen ? chosen.label : "All sources",
    ...(chosen ? [typeBadge(chosen.type)] : []),
    ...(latest ? [h("span", { class: "refreshed" }, refreshedAt(latest))] : []));
  btn.title = latest ? `Last refreshed ${new Date(String(latest).replace(" ", "T")).toLocaleString()}` : "";
  $("#sources-list").replaceChildren(...state.datasets.map((d) => {
    const f = d.freshness || {};
    return h("div", { class: "source-card", style: `--c:${colorOf(d.id)}` },
      h("div", { class: "name" }, d.label, typeBadge(d.type)),
      h("div", { class: `status ${d.live ? "live" : ""}` },
        f.latest_update ? `Refreshed ${refreshedAt(f.latest_update)}` : (d.live ? "Live" : "")));
  }));
}
$("#sources-btn").addEventListener("click", async () => { await pollFreshness(true); $("#sources").showModal(); });

async function pollFreshness(force) {
  if ((document.hidden && !force) || !state.datasets.length) return;
  try {
    const { datasets } = await api("/api/datasets", undefined, "");
    state.datasets = datasets;
    renderSources();
  } catch (e) { /* server restarting */ }
}

function renderHelp() {
  $("#help-body").replaceChildren(
    ...state.datasets.flatMap((d) => {
      const m = state.models[d.id];
      return [
        h("h3", { style: `color:var(--text-2)` }, d.label),
        h("ul", {},
          h("li", {}, h("b", {}, "Measures: "), m.metrics.map((x) => x.label).join(", ")),
          h("li", {}, h("b", {}, "Break down by: "), m.dimensions.map((x) => x.label).join(", "))),
      ];
    }),
    h("h3", {}, "Ways to ask"),
    h("ul", {},
      h("li", {}, "A number: “What is utilization this month?”"),
      h("li", {}, "A comparison: “Revenue by client this quarter”, “Top 5 clients by revenue”"),
      h("li", {}, "A change: “Why did utilization drop last month?”"),
      h("li", {}, "A follow-up: “now by region”, “what about India?”")),
  );
}
$("#help-btn").addEventListener("click", () => $("#help").showModal());
$("#upload-btn").addEventListener("click", () => $("#upload").click());
$("#upload").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  toast(`Reading ${file.name}…`);
  const res = await fetch("/api/upload", { method: "POST", headers: { "X-Filename": file.name }, body: file }).then((r) => r.json());
  if (!res.ok) return toast(res.error);
  $("#sources").close();
  await boot(res.dataset);
  toast(`${res.label} is connected. Questions now go to it; switch to Auto in the question bar to use every source.`);
});

// ---------- conversation ----------
$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim();
  if (q) submit(q);
});

function addUser(text) {
  const el = $("#msg-user").content.firstElementChild.cloneNode(true);
  $(".bubble", el).textContent = text;
  $("#thread").append(el);
  return el;
}

function addBot() {
  const el = $("#msg-bot").content.firstElementChild.cloneNode(true);
  const msg = { el, query: null };
  $(".answer", el).textContent = "Looking at the data";
  el.classList.add("loading");
  $("#thread").append(el);
  return msg;
}

function scrollToBottom() {
  const sc = $("#scroller");
  sc.scrollTop = sc.scrollHeight;
}

async function submit(question, clarifications, msg) {
  $("#question").value = "";
  $("#empty").hidden = true;
  if (!msg) {
    addUser(question);
    msg = addBot();
  } else {
    msg.el.classList.add("loading");
  }
  scrollToBottom();
  $("#ask-btn").disabled = true;
  try {
    const res = await api("/api/ask", { question, context: state.context, context_dataset: state.contextDataset,
                                        clarifications: clarifications || null });
    if (res.ok && res.query) { state.context = res.query; state.contextDataset = res.dataset && res.dataset.id; }
    render(msg, res, { question, clarifications });
  } catch (err) {
    render(msg, { ok: false, error: "The server didn't respond. Is it still running?" });
  } finally {
    $("#ask-btn").disabled = false;
    $("#question").focus();
  }
}

async function rerun(msg, query) {
  msg.el.classList.add("loading");
  const res = await api("/api/query", { query }, msg.dataset);
  if (res.ok) { state.context = res.query; state.contextDataset = msg.dataset; }
  render(msg, res, null, true);
}

function followUp(label, query, dataset) {
  $("#empty").hidden = true;
  addUser(label);
  const msg = addBot();
  msg.dataset = dataset;
  scrollToBottom();
  rerun(msg, query);
}

// ---------- rendering ----------
function render(msg, res, origin, keepExplore) {
  const el = msg.el;
  const wasOpen = keepExplore && !$(".explore", el).hidden;
  if (res.dataset) msg.dataset = res.dataset.id;
  if (msg.dataset) state.model = state.models[msg.dataset];
  el.classList.remove("loading", "error");
  for (const sel of [".options", ".viz", ".chips", ".controls", ".explore-viz", ".next"]) $(sel, el).replaceChildren();
  $(".explore", el).hidden = true;
  $(".explore-btn", el).hidden = true;
  $(".refresh", el).hidden = true;
  $(".asof", el).textContent = "";
  VW = Math.max(280, Math.round($(".body", el).clientWidth - 34));

  if (!res.ok) {
    el.classList.add("error");
    $(".answer", el).replaceChildren(h("p", {}, res.error));
    if (res.samples) {
      $(".options", el).replaceChildren(...res.samples.slice(0, 3).map((q) =>
        h("button", { type: "button", class: "option", onclick: () => submit(q) }, q)));
    }
    return;
  }
  if (res.type === "clarify") {
    $(".answer", el).replaceChildren(h("p", {}, res.prompt));
    $(".options", el).replaceChildren(...res.options.map((o) => h("button", {
      type: "button", class: "option", title: o.hint || "",
      onclick: () => {
        const merged = { ...((origin && origin.clarifications) || {}) };
        for (const [k, v] of Object.entries(o.clarification)) merged[k] = k === "values" ? [...(merged.values || []), ...v] : v;
        submit(origin.question, merged, msg);
      },
    }, o.label)));
    return;
  }

  msg.query = res.query;
  setupMeta(msg, res, wasOpen);
  if (res.type === "explain") renderExplain(msg, res);
  else renderResult(msg, res);
  $(".sql", el).textContent = res.sql.map((st, i) => `${i ? "\n\n" : ""}${st.sql}\n-- parameters: ${JSON.stringify(st.params)}`).join("");
  $(".next", el).replaceChildren(...(res.suggestions || []).map((sug) =>
    h("button", { type: "button", onclick: () => followUp(sug.label, { ...clone(res.query), ...sug.patch }, msg.dataset) }, sug.label)));
}

function setupMeta(msg, res, open) {
  const el = msg.el;
  const btn = $(".explore-btn", el), panel = $(".explore", el), refresh = $(".refresh", el);
  btn.hidden = false;
  panel.hidden = !open;
  btn.setAttribute("aria-expanded", String(!!open));
  btn.onclick = () => {
    panel.hidden = !panel.hidden;
    btn.setAttribute("aria-expanded", String(!panel.hidden));
  };
  refresh.hidden = false;
  refresh.onclick = () => rerun(msg, clone(msg.query));
  const tag = $(".source-tag", el), d = res.dataset;
  if (d) {
    tag.hidden = false;
    tag.replaceChildren(d.label, typeBadge(d.type));
    tag.title = d.routed ? "Chosen automatically from all sources" : "";
    tag.style.setProperty("--c", colorOf(d.id));
  }
  const f = res.freshness;
  const notes = (res.notes || []).filter((n) => !n.startsWith("Building on"));
  $(".asof", el).textContent = [f ? `As of ${refreshedAt(f.checked_at, true)}` : "", ...notes].filter(Boolean).join(" · ");
}

function renderResult(msg, res) {
  const el = msg.el, q = res.query;
  $(".answer", el).replaceChildren(h("p", { class: "lead" }, res.answer));
  const viz = $(".viz", el);
  if (res.rows.length) {
    if (!q.dimensions.length && !q.time_grain) viz.append(heroTiles(res));
    else if (q.time_grain && q.dimensions.length) viz.append(...multiLine(res));
    else if (q.time_grain) viz.append(lineChart(res, q.metrics[0]));
    else viz.append(barChart(res, q.metrics[0]));
  }
  renderChips(msg, res);
  renderControls(msg, res);
  renderTable(msg, res.columns, res.rows, res, "Result set");
}

function renderExplain(msg, res) {
  const el = msg.el;
  const kind = res.metric_meta.format;
  const [what, ...rest] = res.sections;
  $(".answer", el).replaceChildren(
    h("p", { class: "lead" }, what.text),
    ...rest.map((sec) => h("div", {}, h("div", { class: "section-title" }, sec.title.replace(" it happened", "")), h("p", {}, sec.text))));

  const viz = $(".viz", el);
  const up = res.change > 0;
  viz.append(h("div", { class: "hero" },
    h("div", { class: "tile" }, h("div", { class: "sub" }, res.previous.range.label), h("div", { class: "big muted" }, fmt(res.previous.value, kind))),
    h("div", { class: "arrow", "aria-hidden": "true" }, "→"),
    h("div", { class: "tile" }, h("div", { class: "sub" }, res.current.range.label), h("div", { class: "big" }, fmt(res.current.value, kind)),
      h("span", { class: `delta ${up ? "up" : "down"}` }, fmtChange(res.change, res.change_kind === "pts" ? "pts" : "percent")))));

  const box = h("div", {});
  const pick = h("select", { "aria-label": "Explain by" }, res.dimensions.map((d) => h("option", { value: d.name }, d.label)));
  pick.value = res.primary_dimension;
  const draw = () => {
    const members = res.drivers[pick.value] || [];
    box.replaceChildren(driverChart(members.slice(0, 8), kind, res.decomposition));
    renderTable(msg, driverColumns(res), members, null, `Contribution by ${dimOf(pick.value).label.toLowerCase()}`);
  };
  pick.onchange = draw;
  viz.append(h("div", { class: "chart-title", style: "margin-top:16px" }, "What drove it, by ", pick), box);
  draw();

  const ev = $(".explore-viz", el);
  if (res.trend.rows.length > 1) {
    ev.append(h("div", { class: "chart-title" }, `${res.metric_meta.label} by ${res.trend.grain}, compared periods shaded`), trendChart(res.trend.rows, kind));
  }
  ev.append(h("div", { class: "notes" }, res.decomposition === "rate_mix"
    ? "Each group's contribution = its own rate moving (rate effect) + capacity shifting toward or away from it (mix effect). They add up to the total change."
    : "Each group's contribution is its own change; together they add up to the total change."));
  $(".chips", el).replaceChildren(...res.interpretation.map((c) => h("span", { class: "chip static" }, c.label)));
}

function renderChips(msg, res) {
  const q = res.query;
  const remove = {
    metric: (c) => q.metrics.length > 1 && { ...q, metrics: q.metrics.filter((m) => m !== c.value) },
    dimension: (c) => ({ ...q, dimensions: q.dimensions.filter((d) => d !== c.value), order_by: null }),
    grain: () => ({ ...q, time_grain: null }),
    time: () => !q.compare && !q.time_grain && { ...q, time_range: null },
    filter: (c) => ({ ...q, filters: q.filters.filter((f) => !(f.dimension === c.value && f.op === c.op)) }),
    compare: () => ({ ...q, compare: null }),
    limit: () => ({ ...q, limit: null }),
  };
  $(".chips", msg.el).replaceChildren(...res.interpretation.map((c) => {
    const next = remove[c.kind] && remove[c.kind](c);
    const chip = h("span", { class: `chip ${next ? "" : "static"}` }, c.label);
    if (next) chip.append(h("button", { class: "x", type: "button", "aria-label": `Remove ${c.label}`, onclick: () => rerun(msg, clone(next)) }, "×"));
    return chip;
  }));
}

function renderControls(msg, res) {
  const q = res.query;
  const hasTime = !!state.model.time_dimension;
  const sel = (label, options, value, onchange, disabled) => {
    const el = h("select", { disabled: disabled || null }, options.map(([v, l]) => h("option", { value: v }, l)));
    el.value = value;
    el.onchange = () => onchange(el.value);
    return h("label", {}, label, el);
  };
  $(".controls", msg.el).replaceChildren(
    sel("Break down by", [["", "—"], ...state.model.dimensions.map((d) => [d.name, d.label])], q.dimensions[0] || "",
      (v) => rerun(msg, { ...clone(q), dimensions: v ? [v] : [], order_by: null, limit: v ? q.limit : null })),
    sel("Over time", [["", "—"], ...GRAINS.map((g) => [g, g[0].toUpperCase() + g.slice(1)])], q.time_grain || "",
      (v) => rerun(msg, { ...clone(q), time_grain: v || null, compare: v ? null : q.compare, time_range: v && !q.time_grain ? null : q.time_range }),
      !hasTime),
    sel("Add measure", [["", "—"], ...state.model.metrics.filter((m) => !q.metrics.includes(m.name)).map((m) => [m.name, m.label])], "",
      (v) => v && rerun(msg, { ...clone(q), metrics: [...q.metrics, v] })),
    sel("Compare", [["", "—"], ["previous_period", "Previous period"], ["previous_year", "Last year"]], q.compare || "",
      (v) => rerun(msg, { ...clone(q), compare: v || null }), !!q.time_grain || !hasTime),
  );
}

function driverColumns(res) {
  const f = res.metric_meta.format;
  const cols = [
    { name: "member", label: "Group", role: "label" },
    { name: "previous", label: res.previous.range.label, role: "metric", format: f },
    { name: "current", label: res.current.range.label, role: "metric", format: f },
    { name: "contribution", label: "Contribution", role: "contribution", format: f },
  ];
  if (res.decomposition === "rate_mix") {
    cols.push({ name: "rate", label: "Rate effect", role: "contribution", format: f },
      { name: "mix", label: "Mix effect", role: "contribution", format: f },
      { name: "share_now", label: "Share of capacity", role: "metric", format: "percent" });
  }
  return cols;
}

// ---------- charts ----------
function heroTiles(res) {
  const r = res.rows[0];
  return h("div", { class: "hero" }, res.query.metrics.map((m) => {
    const meta = metricOf(m);
    const ch = r[`${m}__change`];
    return h("div", { class: "tile" },
      h("div", { class: "sub" }, meta.label),
      h("div", { class: "big" }, fmt(r[m], meta.format)),
      ch !== undefined && ch !== null ? h("span", { class: `delta ${ch > 0 ? "up" : "down"}` },
        `${fmtChange(ch, meta.format === "percent" ? "pts" : "percent")} vs ${fmt(r[`${m}__prev`], meta.format)}`) : null);
  }));
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((x) => x * p).find((x) => x >= v);
}
function barPath(x0, y, w, hgt, r = 4) {
  if (w <= r) return `M${x0},${y}h${Math.max(w, 0)}v${hgt}h${-Math.max(w, 0)}z`;
  return `M${x0},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${hgt - 2 * r}a${r},${r} 0 0 1 ${-r},${r}h${-(w - r)}z`;
}
function barPathLeft(x0, y, w, hgt, r = 4) {
  if (w <= r) return `M${x0},${y}h${-Math.max(w, 0)}v${hgt}h${Math.max(w, 0)}z`;
  return `M${x0},${y}h${-(w - r)}a${r},${r} 0 0 0 ${-r},${r}v${hgt - 2 * r}a${r},${r} 0 0 0 ${r},${r}h${w - r}z`;
}
const truncate = (name, px) => {
  const n = Math.floor(px / 7.2);
  return name.length > n ? name.slice(0, n - 1) + "…" : name;
};

function barChart(res, m) {
  const meta = metricOf(m);
  const dims = res.query.dimensions;
  const rows = res.rows.slice(0, 12);
  const W = VW, labelW = W < 480 ? 96 : 160, rowH = 30, barH = 16;
  const H = rows.length * rowH + 4;
  const max = niceMax(Math.max(...rows.map((r) => r[m] || 0)));
  const plotW = W - labelW - (W < 480 ? 110 : 140);
  const chKind = meta.format === "percent" ? "pts" : "percent";
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${meta.label} by ${dims.map((d) => dimOf(d).label).join(" and ")}` });
  rows.forEach((r, i) => {
    const y = i * rowH + (rowH - barH) / 2;
    const w = Math.max(0, (r[m] || 0) / max * plotW);
    const name = dims.map((d) => r[d]).join(" / ");
    const label = s("text", { x: labelW - 10, y: y + barH / 2 + 4, "text-anchor": "end", class: "label" });
    label.textContent = truncate(name, labelW);
    svg.append(label, s("path", { d: barPath(labelW, y, w, barH), fill: memberColor(dims[0], r[dims[0]]) }));
    const val = s("text", { x: labelW + w + 8, y: y + barH / 2 + 4, class: "value" });
    const ch = r[`${m}__change`];
    val.textContent = fmt(r[m], meta.format) + (ch !== undefined && ch !== null ? `  ${fmtChange(ch, chKind)}` : "");
    svg.append(val);
    const hit = s("rect", { x: 0, y: i * rowH, width: W, height: rowH, class: "hit" });
    hit.addEventListener("mousemove", (e) => tip(e, `<b>${esc(name)}</b><br>${esc(meta.label)}: ${esc(fmt(r[m], meta.format))}` +
      (ch !== undefined && ch !== null ? `<br>vs prior: ${esc(fmt(r[`${m}__prev`], meta.format))} (${esc(fmtChange(ch, chKind))})` : "")));
    hit.addEventListener("mouseleave", hideTip);
    svg.append(hit);
  });
  if (res.rows.length > rows.length) {
    return h("div", {}, svg, h("div", { class: "notes" }, `Top ${rows.length} of ${res.rows.length}; all are under Explore.`));
  }
  return svg;
}

function driverChart(members, kind, decomposition) {
  if (!members.length) return h("div", { class: "notes" }, "No drivers to show.");
  const W = VW, labelW = W < 480 ? 100 : 170, rowH = 28, barH = 14;
  const H = members.length * rowH + 4;
  const max = Math.max(...members.map((m) => Math.abs(m.contribution))) || 1;
  const allNeg = members.every((m) => m.contribution <= 0), allPos = members.every((m) => m.contribution >= 0);
  // Put the axis where the bars need room: centre only when bars go both ways.
  const plotL = labelW + 60, plotR = W - 60;
  const mid = allNeg ? plotR : allPos ? plotL : (plotL + plotR) / 2;
  const half = allNeg || allPos ? plotR - plotL : (plotR - plotL) / 2;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Contribution to the change" });
  svg.append(s("line", { x1: mid, x2: mid, y1: 0, y2: H, stroke: "var(--border)", "stroke-width": 1 }));
  members.forEach((m, i) => {
    const y = i * rowH + (rowH - barH) / 2;
    const w = Math.abs(m.contribution) / max * half;
    const pos = m.contribution >= 0;
    const name = String(m.member);
    const label = s("text", { x: labelW - 10, y: y + barH / 2 + 4, "text-anchor": "end", class: "label" });
    label.textContent = truncate(name, labelW);
    svg.append(label, s("path", { d: pos ? barPath(mid, y, w, barH) : barPathLeft(mid, y, w, barH), fill: pos ? "var(--pos)" : "var(--neg)" }));
    const val = s("text", { x: pos ? mid + w + 6 : mid - w - 6, y: y + barH / 2 + 4, class: "value", "text-anchor": pos ? "start" : "end" });
    val.textContent = fmtContribution(m.contribution, kind);
    svg.append(val);
    let detail = `<b>${esc(name)}</b><br>${esc(fmt(m.previous, kind))} → ${esc(fmt(m.current, kind))}<br>Contribution: ${esc(fmtContribution(m.contribution, kind))}`;
    if (decomposition === "rate_mix") {
      detail += `<br>Rate effect: ${esc(fmtContribution(m.rate, kind))} · Mix effect: ${esc(fmtContribution(m.mix, kind))}`;
    }
    const hit = s("rect", { x: 0, y: i * rowH, width: W, height: rowH, class: "hit" });
    hit.addEventListener("mousemove", (e) => tip(e, detail));
    hit.addEventListener("mouseleave", hideTip);
    svg.append(hit);
  });
  return svg;
}

function lineFrame(W, H, pad, max, kind, periods, min = 0) {
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}` });
  const g = s("g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (H - pad.t - pad.b) * (1 - i / 4);
    g.append(s("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "grid" }));
    const t = s("text", { x: pad.l - 8, y: y + 4, "text-anchor": "end" });
    t.textContent = fmt(min + (max - min) * i / 4, kind);
    g.append(t);
  }
  const step = Math.ceil(periods.length / Math.max(2, Math.floor(W / 110)));
  periods.forEach((p, i) => {
    if (i % step && i !== periods.length - 1) return;
    const t = s("text", { x: xAt(i, periods.length, W, pad), y: H - 6, "text-anchor": "middle" });
    t.textContent = p;
    g.append(t);
  });
  svg.append(g);
  return svg;
}
const xAt = (i, n, W, pad) => pad.l + (n === 1 ? (W - pad.l - pad.r) / 2 : i * (W - pad.l - pad.r) / (n - 1));

function drawLines(svg, series, periods, max, W, H, pad, kind, min = 0) {
  const yAt = (v) => pad.t + (H - pad.t - pad.b) * (1 - ((v || 0) - min) / ((max - min) || 1));
  for (const se of series) {
    const d = periods.map((p, i) => `${i ? "L" : "M"}${xAt(i, periods.length, W, pad)},${yAt(se.values[p])}`).join("");
    svg.append(s("path", { d, fill: "none", stroke: se.color, "stroke-width": 2.25, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  }
  const cross = s("line", { y1: pad.t, y2: H - pad.b, stroke: "var(--border)", "stroke-width": 1, visibility: "hidden" });
  svg.append(cross);
  const dots = series.map((se) => {
    const c = s("circle", { r: 4.5, fill: se.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" });
    svg.append(c);
    return c;
  });
  const colW = (W - pad.l - pad.r) / Math.max(periods.length - 1, 1);
  periods.forEach((p, i) => {
    const x = xAt(i, periods.length, W, pad);
    const hit = s("rect", { x: x - colW / 2, y: 0, width: colW, height: H, class: "hit" });
    hit.addEventListener("mousemove", (e) => {
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      series.forEach((se, j) => {
        dots[j].setAttribute("cx", x); dots[j].setAttribute("cy", yAt(se.values[p])); dots[j].setAttribute("visibility", "visible");
      });
      const lines = [...series].sort((a, b) => (b.values[p] || 0) - (a.values[p] || 0))
        .map((se) => `${series.length > 1 ? esc(se.name) + ": " : ""}${esc(fmt(se.values[p] ?? null, kind))}`);
      tip(e, `<b>${esc(p)}</b><br>${lines.join("<br>")}`);
    });
    hit.addEventListener("mouseleave", () => {
      hideTip(); cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    });
    svg.append(hit);
  });
}

function yRange(vals, kind) {
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (kind === "percent") return [Math.max(0, Math.floor(lo * 20) / 20 - 0.05), Math.min(1, Math.ceil(hi * 20) / 20 + 0.05)];
  return [0, niceMax(hi)];
}

function lineChart(res, m) {
  const meta = metricOf(m);
  const periods = res.rows.map((r) => r.period);
  const values = Object.fromEntries(res.rows.map((r) => [r.period, r[m]]));
  const W = VW, H = 220, pad = { t: 10, r: 12, b: 26, l: 62 };
  const [lo, hi] = yRange(res.rows.map((r) => r[m] || 0), meta.format);
  const svg = lineFrame(W, H, pad, hi, meta.format, periods, lo);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${meta.label} trend`);
  drawLines(svg, [{ name: meta.label, color: SERIES[0], values }], periods, hi, W, H, pad, meta.format, lo);
  return svg;
}

function trendChart(rows, kind) {
  const periods = rows.map((r) => r.period);
  const values = Object.fromEntries(rows.map((r) => [r.period, r.value]));
  const W = VW - 28, H = 180, pad = { t: 10, r: 12, b: 26, l: 62 };
  const [lo, hi] = yRange(rows.map((r) => r.value || 0), kind);
  const svg = lineFrame(W, H, pad, hi, kind, periods, lo);
  const colW = (W - pad.l - pad.r) / Math.max(periods.length - 1, 1);
  rows.forEach((r, i) => {
    if (!r.segment) return;
    const x = xAt(i, periods.length, W, pad);
    svg.insertBefore(s("rect", { x: x - colW / 2, y: pad.t, width: colW, height: H - pad.t - pad.b, rx: 6,
      class: r.segment === "current" ? "band-current" : "band-previous" }), svg.firstChild);
  });
  drawLines(svg, [{ name: "", color: SERIES[0], values }], periods, hi, W, H, pad, kind, lo);
  return svg;
}

function multiLine(res) {
  const q = res.query, m = q.metrics[0], meta = metricOf(m), dim = q.dimensions[0];
  const periods = [...new Set(res.rows.map((r) => r.period))].sort();
  const byMember = new Map();
  for (const r of res.rows) {
    const k = String(r[dim]);
    if (!byMember.has(k)) byMember.set(k, { name: k, values: {}, total: 0 });
    const se = byMember.get(k);
    se.values[r.period] = r[m];
    se.total += r[m] || 0;
  }
  // Colour follows the member (same as in every other chart), never its rank; cap at 8 lines to stay readable.
  const all = [...byMember.values()].sort((a, b) => b.total - a.total);
  const shown = all.slice(0, 8).sort((a, b) => a.name.localeCompare(b.name));
  shown.forEach((se) => { se.color = memberColor(dim, se.name); });
  const W = VW, H = 240, pad = { t: 10, r: 12, b: 26, l: 62 };
  const [lo, hi] = yRange(shown.flatMap((se) => Object.values(se.values).map((v) => v || 0)), meta.format);
  const svg = lineFrame(W, H, pad, hi, meta.format, periods, lo);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${meta.label} trend by ${dimOf(dim).label}`);
  drawLines(svg, shown, periods, hi, W, H, pad, meta.format, lo);
  const out = [svg, h("div", { class: "legend" }, shown.map((se) => h("span", { style: `--c:${se.color}` }, se.name)))];
  if (all.length > shown.length) out.push(h("div", { class: "notes" }, `Showing the top 8 of ${all.length}; all are under Explore.`));
  return out;
}

// ---------- tooltip ----------
function tip(e, html) {
  const t = $("#tooltip");
  t.innerHTML = html;
  t.hidden = false;
  t.style.left = `${Math.min(e.clientX + 14, window.innerWidth - t.offsetWidth - 8)}px`;
  t.style.top = `${e.clientY + 14}px`;
}
function hideTip() { $("#tooltip").hidden = true; }

// ---------- table ----------
function renderTable(msg, cols, sourceRows, res, title) {
  const el = msg.el, table = $("table", el);
  const rows = sourceRows.slice();
  let sort = null;
  $(".table-title", el).textContent = `${title} · ${sourceRows.length} row${sourceRows.length === 1 ? "" : "s"}`;
  const numeric = (c) => ["metric", "change", "contribution"].includes(c.role);
  const cell = (c, r) => {
    const v = r[c.name];
    if (c.role === "change") return h("td", { class: `num ${v > 0 ? "up" : v < 0 ? "down" : ""}` }, fmtChange(v, c.format));
    if (c.role === "contribution") return h("td", { class: `num ${v > 0 ? "up" : v < 0 ? "down" : ""}` }, fmtContribution(v, c.format));
    if (c.role === "metric") return h("td", { class: "num" }, fmt(v, c.format));
    if (c.role === "dimension" && res) return h("td", { class: "drill", title: `Drill into ${v}`, onclick: () => drill(msg, res.query, c.name, v) }, String(v));
    return h("td", {}, String(v ?? "–"));
  };
  const draw = () => {
    const head = h("tr", {}, cols.map((c) => h("th", {
      class: numeric(c) ? "num" : "",
      onclick: () => {
        sort = sort && sort.col === c.name ? { col: c.name, dir: -sort.dir } : { col: c.name, dir: numeric(c) ? -1 : 1 };
        rows.sort((a, b) => (a[c.name] > b[c.name] ? 1 : a[c.name] < b[c.name] ? -1 : 0) * sort.dir);
        draw();
      },
    }, c.label + (sort && sort.col === c.name ? (sort.dir > 0 ? " ↑" : " ↓") : ""))));
    const body = rows.map((r) => h("tr", {}, cols.map((c) => cell(c, r))));
    if (res && res.totals && (res.query.dimensions.length || res.query.time_grain) && !res.query.limit) {
      body.push(h("tr", { class: "total" }, cols.map((c, i) => {
        if (i === 0) return h("td", {}, "Total");
        if (c.role === "metric" || c.role === "change") {
          const v = res.totals[c.name];
          return h("td", { class: "num" }, c.role === "change" ? fmtChange(v, c.format) : fmt(v, c.format));
        }
        return h("td", {}, "");
      })));
    }
    table.replaceChildren(h("thead", {}, head), h("tbody", {}, body));
  };
  draw();
  $(".csv", el).onclick = (e) => {
    e.preventDefault();
    const line = (vals) => vals.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",");
    const csv = [line(cols.map((c) => c.label)), ...sourceRows.map((r) => line(cols.map((c) => r[c.name])))].join("\n");
    const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: "result.csv" });
    a.click();
    URL.revokeObjectURL(a.href);
  };
}

function drill(msg, query, dim, value) {
  const q = clone(query);
  q.filters = q.filters.filter((f) => f.dimension !== dim).concat([{ dimension: dim, op: "in", values: [value] }]);
  const used = new Set(q.filters.map((f) => f.dimension));
  const next = state.model.dimensions.find((d) => !used.has(d.name));
  q.dimensions = next ? [next.name] : [];
  q.order_by = null;
  q.limit = null;
  followUp(`Drill into ${value}${next ? ` by ${next.label.toLowerCase()}` : ""}`, q, msg.dataset);
}

boot();
setInterval(pollFreshness, 5000);
