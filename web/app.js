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
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"].map((v) => `var(${v})`);
const GRAINS = ["day", "week", "month", "quarter", "year"];

const state = { model: null, context: null };

// ---------- formatting ----------
function fmt(v, kind) {
  if (v === null || v === undefined) return "–";
  if (kind === "percent") return `${(v * 100).toFixed(1)}%`;
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
function fmtChange(v) {
  if (v === null || v === undefined) return "–";
  const arrow = v > 0 ? "▲" : v < 0 ? "▼" : "";
  return `${arrow} ${Math.abs(v * 100).toFixed(1)}%`;
}
const metricOf = (name) => state.model.metrics.find((m) => m.name === name);
const dimOf = (name) => state.model.dimensions.find((d) => d.name === name);
const clone = (o) => JSON.parse(JSON.stringify(o));

// ---------- API ----------
async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : {});
  return res.json();
}

// ---------- boot ----------
async function boot() {
  const [model, samples] = await Promise.all([api("/api/model"), api("/api/samples")]);
  state.model = model;
  $("#model-label").textContent = `Data: ${model.label}`;
  $("#engine-label").textContent = model.llm_enabled ? "Rules + Claude" : "Instant rules engine";
  $("#engine-label").title = model.llm_enabled
    ? "Common questions are answered instantly by rules; unusual phrasings go to Claude."
    : "Set ANTHROPIC_API_KEY to let Claude handle unusual phrasings.";
  renderSamples(samples.samples);
  rotatePlaceholder(samples.samples);
  renderGlossary(model);
}

function renderSamples(samples) {
  const box = $("#samples");
  box.replaceChildren(...samples.slice(0, 6).map((q) =>
    h("button", { class: "sample", type: "button", onclick: () => submit(q) }, q)));
}

function rotatePlaceholder(samples) {
  const input = $("#question");
  let i = 0;
  const tick = () => {
    if (!input.value) input.placeholder = `Try: “${samples[i % samples.length]}”`;
    i += 1;
  };
  tick();
  setInterval(tick, 3500);
  // Tab accepts the placeholder suggestion
  input.addEventListener("keydown", (e) => {
    if (e.key === "Tab" && !input.value && input.placeholder.startsWith("Try:")) {
      e.preventDefault();
      input.value = input.placeholder.slice(6, -1);
    }
  });
}

function renderGlossary(model) {
  const body = $("#glossary-body");
  body.replaceChildren(
    h("h3", {}, "Metrics"),
    h("ul", {}, model.metrics.map((m) => h("li", {}, h("b", {}, m.label), h("span", {}, ` — ${m.description || ""}`)))),
    h("h3", {}, "Break down or filter by"),
    h("ul", {}, model.dimensions.map((d) => h("li", {}, h("b", {}, d.label), h("span", {}, ` — ${d.values.join(", ")}`)))),
    h("h3", {}, "Time"),
    h("ul", {},
      h("li", {}, "Periods: today, last 7 days, last month, this quarter, Q3 2025, March 2026, 2025, year to date…"),
      h("li", {}, "Trends: daily, weekly, monthly, quarterly, yearly, “over time”"),
      h("li", {}, "Comparisons: “vs last year”, “compared to previous period”, “growth”")),
  );
  $("#glossary-btn").addEventListener("click", () => $("#glossary").showModal());
}

// ---------- ask / run ----------
$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim();
  if (q) submit(q);
});

async function submit(question) {
  $("#question").value = "";
  const card = newCard(question);
  $("#ask-btn").disabled = true;
  try {
    const res = await api("/api/ask", { question, context: state.context });
    if (res.ok) state.context = res.query;
    render(card, res);
  } catch (err) {
    render(card, { ok: false, error: "The server didn't respond. Is it still running?" });
  } finally {
    $("#ask-btn").disabled = false;
  }
}

async function rerun(card, query, label) {
  card.el.classList.add("loading");
  const res = await api("/api/query", { query });
  res.question = label || card.question;
  if (res.ok) state.context = res.query;
  render(card, res);
}

function newCard(question) {
  const el = $("#card-tpl").content.firstElementChild.cloneNode(true);
  const card = { el, question, query: null };
  $(".q", el).textContent = question;
  $(".answer", el).textContent = "Thinking…";
  for (const sel of [".controls", ".table-wrap", ".sql-wrap"]) $(sel, el).hidden = true;
  el.classList.add("loading");
  $("#thread").prepend(el);
  return card;
}

// ---------- rendering ----------
function render(card, res) {
  const el = card.el;
  el.classList.remove("loading", "error");
  card.question = res.question || card.question;
  $(".q", el).textContent = card.question;
  const stats = [];
  if (res.elapsed_ms !== undefined) stats.push(`${res.elapsed_ms} ms`);
  if (res.engine) stats.push(res.engine === "claude" ? "interpreted by Claude" : "instant");
  $(".stats", el).textContent = stats.join(" · ");

  if (!res.ok) {
    el.classList.add("error");
    $(".answer", el).textContent = res.error;
    for (const sel of [".chips", ".viz", ".suggestions", ".notes"]) $(sel, el).replaceChildren();
    $(".controls", el).hidden = true;
    $(".table-wrap", el).hidden = true;
    $(".sql-wrap", el).hidden = true;
    if (res.samples) {
      $(".suggestions", el).replaceChildren(...res.samples.map((q) =>
        h("button", { type: "button", onclick: () => submit(q) }, q)));
    }
    return;
  }
  card.query = res.query;
  $(".controls", el).hidden = false;
  $(".table-wrap", el).hidden = false;
  $(".sql-wrap", el).hidden = false;
  $(".answer", el).textContent = res.answer;
  $(".notes", el).replaceChildren(...(res.notes || []).map((n) => h("div", {}, n)));
  renderChips(card, res);
  renderControls(card, res);
  renderViz($(".viz", el), res);
  $(".suggestions", el).replaceChildren(...res.suggestions.map((sug) =>
    h("button", { type: "button", onclick: () => rerun(card, { ...clone(res.query), ...sug.patch }, sug.label) }, sug.label)));
  renderTable(card, res);
  $(".sql", el).textContent = res.sql.map((st, i) =>
    `${i ? "\n\n-- comparison period\n" : ""}${st.sql}\n-- parameters: ${JSON.stringify(st.params)}`).join("");
}

function renderChips(card, res) {
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
  const kindLabel = { metric: "Metric", dimension: "", grain: "", time: "When", filter: "Filter", compare: "", limit: "" };
  $(".chips", card.el).replaceChildren(...res.interpretation.map((c) => {
    const next = remove[c.kind] && remove[c.kind](c);
    const chip = h("span", { class: `chip ${next ? "" : "static"}`, title: next ? "" : "" },
      kindLabel[c.kind] ? `${kindLabel[c.kind]}: ` : "", h("b", {}, c.label));
    if (next) chip.append(h("button", {
      class: "x", type: "button", "aria-label": `Remove ${c.label}`,
      onclick: () => rerun(card, clone(next), `${card.question} (edited)`),
    }, "×"));
    return chip;
  }));
}

function renderControls(card, res) {
  const q = res.query;
  const sel = (name, options, value, onchange, disabled) => {
    const el = $(`select[data-ctl="${name}"]`, card.el);
    el.replaceChildren(...options.map(([v, label]) => h("option", { value: v }, label)));
    el.value = value;
    el.disabled = !!disabled;
    el.onchange = () => onchange(el.value);
  };
  sel("dimension", [["", "—"], ...state.model.dimensions.map((d) => [d.name, d.label])], q.dimensions[0] || "",
    (v) => rerun(card, { ...clone(q), dimensions: v ? [v] : [], order_by: null, limit: v ? q.limit : null },
      `${card.question} (by ${v ? dimOf(v).label.toLowerCase() : "total"})`));
  sel("grain", [["", "—"], ...GRAINS.map((g) => [g, g[0].toUpperCase() + g.slice(1)])], q.time_grain || "",
    (v) => rerun(card, { ...clone(q), time_grain: v || null, compare: v ? null : q.compare,
      time_range: v && !q.time_grain ? null : q.time_range }, `${card.question} (${v || "no"} trend)`));
  sel("metric", [["", "—"], ...state.model.metrics.filter((m) => !q.metrics.includes(m.name)).map((m) => [m.name, m.label])], "",
    (v) => v && rerun(card, { ...clone(q), metrics: [...q.metrics, v] }, `${card.question} (+ ${metricOf(v).label.toLowerCase()})`));
  sel("compare", [["", "—"], ["previous_period", "Previous period"], ["previous_year", "Last year"]], q.compare || "",
    (v) => rerun(card, { ...clone(q), compare: v || null }, `${card.question} (compare)`), !!q.time_grain);
}

// ---------- charts ----------
let VW = 960; // chart width in px, measured from the card so text stays legible on phones

function renderViz(box, res) {
  const q = res.query;
  box.replaceChildren();
  VW = Math.max(300, Math.round(box.clientWidth || 960));
  if (!res.rows.length) return;
  if (!q.dimensions.length && !q.time_grain) return box.append(heroTiles(res));
  if (q.time_grain) {
    if (q.dimensions.length) return box.append(...multiLine(res));
    for (const m of q.metrics.slice(0, 3)) box.append(lineChart(res, m));
    return;
  }
  box.append(barChart(res, q.metrics[0]));
  if (q.metrics.length > 1) box.append(h("div", { class: "notes" }, h("div", {}, `Chart shows ${metricOf(q.metrics[0]).label}; every metric is in the table below.`)));
}

function heroTiles(res) {
  const r = res.rows[0];
  return h("div", { class: "hero" }, res.query.metrics.map((m) => {
    const meta = metricOf(m);
    const ch = r[`${m}__change`];
    return h("div", { class: "tile" },
      h("div", { class: "sub" }, meta.label),
      h("div", { class: "big" }, fmt(r[m], meta.format)),
      ch !== undefined ? h("div", { class: `sub ${ch > 0 ? "up" : ch < 0 ? "down" : ""}` },
        `${fmtChange(ch)} vs ${fmt(r[`${m}__prev`], meta.format)}`) : null);
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

function barChart(res, m) {
  const meta = metricOf(m);
  const dims = res.query.dimensions;
  const rows = res.rows.slice(0, 20);
  const W = VW, labelW = VW < 500 ? 96 : 170, valueW = VW < 500 ? 120 : 140, rowH = 30, barH = 18;
  const H = rows.length * rowH + 4;
  const max = niceMax(Math.max(...rows.map((r) => r[m] || 0)));
  const plotW = W - labelW - valueW;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${meta.label} by ${dims.map((d) => dimOf(d).label).join(" and ")}` });
  rows.forEach((r, i) => {
    const y = i * rowH + (rowH - barH) / 2;
    const w = Math.max(0, (r[m] || 0) / max * plotW);
    const name = dims.map((d) => r[d]).join(" / ");
    const label = s("text", { x: labelW - 10, y: y + barH / 2 + 4, "text-anchor": "end", class: "label" });
    const maxChars = Math.floor(labelW / 7.5);
    label.textContent = name.length > maxChars ? name.slice(0, maxChars - 1) + "…" : name;
    svg.append(label);
    svg.append(s("path", { d: barPath(labelW, y, w, barH), fill: SERIES[0] }));
    const val = s("text", { x: labelW + w + 8, y: y + barH / 2 + 4, class: "value" });
    const ch = r[`${m}__change`];
    val.textContent = fmt(r[m], meta.format) + (ch !== undefined ? `  ${fmtChange(ch)}` : "");
    svg.append(val);
    const hit = s("rect", { x: 0, y: i * rowH, width: W, height: rowH, class: "hit" });
    hit.addEventListener("mousemove", (e) => tip(e, `<b>${esc(name)}</b><br>${esc(meta.label)}: ${esc(fmt(r[m], meta.format))}` +
      (ch !== undefined ? `<br>vs prior: ${esc(fmt(r[`${m}__prev`], meta.format))} (${esc(fmtChange(ch))})` : "")));
    hit.addEventListener("mouseleave", hideTip);
    svg.append(hit);
  });
  return svg;
}

function lineFrame(W, H, pad, max, kind, periods) {
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}` });
  const g = s("g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (H - pad.t - pad.b) * (1 - i / 4);
    g.append(s("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "grid" }));
    const t = s("text", { x: pad.l - 8, y: y + 4, "text-anchor": "end" });
    t.textContent = fmt(max * i / 4, kind);
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

function drawLines(svg, series, periods, max, W, H, pad, kind) {
  const yAt = (v) => pad.t + (H - pad.t - pad.b) * (1 - (v || 0) / max);
  for (const se of series) {
    const d = periods.map((p, i) => `${i ? "L" : "M"}${xAt(i, periods.length, W, pad)},${yAt(se.values[p])}`).join("");
    svg.append(s("path", { d, fill: "none", stroke: se.color, "stroke-width": 2, "stroke-linejoin": "round" }));
  }
  const cross = s("line", { y1: pad.t, y2: H - pad.b, stroke: "var(--text-3)", "stroke-width": 1, visibility: "hidden" });
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

function lineChart(res, m) {
  const meta = metricOf(m);
  const periods = res.rows.map((r) => r.period);
  const values = Object.fromEntries(res.rows.map((r) => [r.period, r[m]]));
  const W = VW, H = 240, pad = { t: 10, r: 16, b: 26, l: 70 };
  const max = niceMax(Math.max(...res.rows.map((r) => r[m] || 0)));
  const wrap = h("div", {}, h("div", { class: "chart-title" }, meta.label));
  const svg = lineFrame(W, H, pad, max, meta.format, periods);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${meta.label} trend`);
  drawLines(svg, [{ name: meta.label, color: SERIES[0], values }], periods, max, W, H, pad, meta.format);
  wrap.append(svg);
  return wrap;
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
  // Colour follows the entity (alphabetical order), never its rank; cap at 8 series.
  const all = [...byMember.values()].sort((a, b) => b.total - a.total);
  const shown = all.slice(0, 8).sort((a, b) => a.name.localeCompare(b.name));
  shown.forEach((se, i) => { se.color = SERIES[i]; });
  const W = VW, H = 260, pad = { t: 10, r: 16, b: 26, l: 70 };
  const max = niceMax(Math.max(...shown.flatMap((se) => Object.values(se.values).map((v) => v || 0))));
  const svg = lineFrame(W, H, pad, max, meta.format, periods);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${meta.label} trend by ${dimOf(dim).label}`);
  drawLines(svg, shown, periods, max, W, H, pad, meta.format);
  const legend = h("div", { class: "legend" }, shown.map((se) => h("span", { style: `--c:${se.color}` }, se.name)));
  const out = [h("div", { class: "chart-title" }, `${meta.label} by ${dimOf(dim).label.toLowerCase()}`), svg, legend];
  if (all.length > shown.length) out.push(h("div", { class: "notes" }, h("div", {}, `Showing the top 8 of ${all.length}; all are in the table.`)));
  return out;
}

// ---------- tooltip ----------
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function tip(e, html) {
  const t = $("#tooltip");
  t.innerHTML = html;
  t.hidden = false;
  const x = Math.min(e.clientX + 14, window.innerWidth - t.offsetWidth - 8);
  t.style.left = `${x}px`;
  t.style.top = `${e.clientY + 14}px`;
}
function hideTip() { $("#tooltip").hidden = true; }

// ---------- table ----------
function renderTable(card, res) {
  const table = $("table", card.el);
  const cols = res.columns;
  let rows = res.rows.slice();
  let sort = null;
  $(".rowcount", card.el).textContent = `(${res.rows.length} row${res.rows.length === 1 ? "" : "s"})`;
  const cell = (c, r) => {
    const v = r[c.name];
    if (c.role === "change") return h("td", { class: `num ${v > 0 ? "up" : v < 0 ? "down" : ""}` }, fmtChange(v));
    if (c.role === "metric") return h("td", { class: "num" }, fmt(v, c.format));
    if (c.role === "dimension") {
      return h("td", {
        class: "drill", title: `Drill into ${v}`,
        onclick: () => drill(card, res.query, c.name, v),
      }, String(v));
    }
    return h("td", {}, String(v));
  };
  const draw = () => {
    const head = h("tr", {}, cols.map((c) => h("th", {
      class: c.role === "metric" || c.role === "change" ? "num" : "",
      onclick: () => {
        sort = sort && sort.col === c.name ? { col: c.name, dir: -sort.dir } : { col: c.name, dir: c.role === "metric" ? -1 : 1 };
        rows.sort((a, b) => (a[c.name] > b[c.name] ? 1 : a[c.name] < b[c.name] ? -1 : 0) * sort.dir);
        draw();
      },
    }, c.label + (sort && sort.col === c.name ? (sort.dir > 0 ? " ↑" : " ↓") : ""))));
    const body = rows.map((r) => h("tr", {}, cols.map((c) => cell(c, r))));
    const q = res.query;
    if (res.totals && (q.dimensions.length || q.time_grain) && !q.limit) {
      body.push(h("tr", { class: "total" }, cols.map((c, i) => {
        if (i === 0) return h("td", {}, "Total");
        if (c.role === "metric" || c.role === "change") {
          const v = res.totals[c.name];
          return h("td", { class: "num" }, c.role === "change" ? fmtChange(v) : fmt(v, c.format));
        }
        return h("td", {}, "");
      })));
    }
    table.replaceChildren(h("thead", {}, head), h("tbody", {}, body));
  };
  draw();
  $(".csv", card.el).onclick = (e) => {
    e.preventDefault();
    const line = (vals) => vals.map((v) => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",");
    const csv = [line(cols.map((c) => c.label)), ...res.rows.map((r) => line(cols.map((c) => r[c.name])))].join("\n");
    const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: "result.csv" });
    a.click();
    URL.revokeObjectURL(a.href);
  };
}

function drill(card, query, dim, value) {
  const q = clone(query);
  q.filters = q.filters.filter((f) => f.dimension !== dim).concat([{ dimension: dim, op: "in", values: [value] }]);
  const used = new Set([...q.filters.map((f) => f.dimension)]);
  const next = state.model.dimensions.find((d) => !used.has(d.name));
  q.dimensions = next ? [next.name] : [];
  q.order_by = null;
  q.limit = null;
  const label = `${dimOf(dim).label}: ${value}${next ? `, by ${next.label.toLowerCase()}` : ""}`;
  const fresh = newCard(label);
  rerun(fresh, q, `Drill into ${label}`);
}

boot();
