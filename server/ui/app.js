// Memory console. Vanilla JS; every piece of stored text is rendered with textContent (never innerHTML),
// because memories are written by agents and other apps.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.setAttribute("style", v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== "/ui/login") { showLogin(); throw new Error("Sign in again."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
  return data;
}

function toast(msg) {
  const t = h("div", { class: "toast", role: "status", text: msg });
  document.body.append(t);
  setTimeout(() => t.remove(), 2600);
}

const day = (iso) => (iso || "").slice(0, 10);
const fmtBytes = (n) => n == null ? "unknown" : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} KB`;
const chip = (status) => h("span", { class: `chip ${status}`, text: status });

// ── auth ────────────────────────────────────────────────────────────────────────────────────────
function showLogin() { $("#app").hidden = true; $("#login").hidden = false; $("#key").focus(); }
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/ui/login", { method: "POST", body: { key: $("#key").value } });
    $("#key").value = "";
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await api("/ui/logout", { method: "POST" }); showLogin(); });

// ── drawer ──────────────────────────────────────────────────────────────────────────────────────
function openDrawer(...content) {
  const body = $("#drawer-body");
  body.replaceChildren(...content.filter((c) => c !== null && c !== undefined && c !== false && c !== ""));
  $("#drawer").hidden = false;
  $("#drawer-close").focus();
}
function closeDrawer() { $("#drawer").hidden = true; }
$("#drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ── views ───────────────────────────────────────────────────────────────────────────────────────
const views = {
  async overview() {
    const o = await api("/ui/api/overview");
    const total = Object.values(o.status_counts).reduce((a, b) => a + b, 0);
    const active = o.status_counts.active || 0;
    const reads14 = o.reads.reduce((a, b) => a + b, 0), writes14 = o.writes.reduce((a, b) => a + b, 0);
    const judge = o.judge.backend === "none" ? "No judge: only exact subject+relation matches replace old values."
      : o.judge.backend === "jev" && !o.judge.jev.configured ? "Judge set to Jev, but no TYPESAFE_API_KEY is configured, so only exact key matches replace old values."
      : `Judge: ${o.judge.backend}.`;
    view.replaceChildren(
      h("h2", { class: "headline", text: `${active} memories are current. Agents read ${reads14} times and wrote ${writes14} in the last two weeks.` }),
      h("p", { class: "lede", text: judge }),
      chart(o.days, o.reads, o.writes),
      h("div", { class: "legend" },
        h("span", {}, h("i", { class: "swatch", style: "background:var(--current)" }), "Reads"),
        h("span", {}, h("i", { class: "swatch", style: "background:var(--pending)" }), "Writes")),
      h("div", { class: "facts" },
        fact(o.facts, "keyed facts (one current value each)"),
        fact(o.supersessions_30d, "old values retired in 30 days"),
        fact(o.entities, "entities in the graph"),
        fact(o.links, "links between related memories"),
        fact(o.skills, "skills"),
        fact(o.open_reviews, "items waiting for your review"),
        fact(total - active, "past, pending or archived memories"),
        fact(`${o.rss_mb ?? "?"} MB`, `server memory, database ${fmtBytes(o.db_bytes)}`)),
      h("h3", { text: "Who uses memory" }),
      table(["Agent", "Reads", "Writes"], Object.entries(o.agents).sort((a, b) => b[1].reads - a[1].reads)
        .map(([a, v]) => [a, v.reads, v.writes]), [1, 2],
        "No agent has used memory in the last two weeks."),
      h("h3", { text: "How fast each tool answers (last 14 days)" }),
      table(["Tool", "Calls", "Typical", "Slowest 5%"], Object.entries(o.latency).sort((a, b) => b[1].n - a[1].n)
        .map(([t, v]) => [t.replace(/_tool$/, "").replaceAll("_", " "), v.n, `${Math.round(v.p50)} ms`, `${Math.round(v.p95)} ms`]),
        [1, 2, 3], "No tool calls recorded yet."),
      h("h3", { text: "Current memories by domain" }),
      table(["Domain", "Memories"], o.domains.map((d) => [d.domain, d.n]), [1], "Nothing stored yet."),
    );
  },

  async graph() {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const domain = params.get("domain") || "", history = params.get("history") === "1";
    let notes = params.get("notes");
    const data0 = await api(`/ui/api/graph?domain=${encodeURIComponent(domain)}&history=${history ? 1 : 0}&notes=0`);
    if (notes === null) notes = data0.facts.length ? "0" : "1"; // nothing keyed yet: show the note network
    const data = notes === "1" ? await api(`/ui/api/graph?domain=${encodeURIComponent(domain)}&history=${history ? 1 : 0}&notes=1`) : data0;

    const setParam = (k, v) => { params.set(k, v); location.hash = `graph?${params}`; };
    const box = h("div", { id: "cy" });
    const empty = h("div", { class: "graph-empty" });
    view.replaceChildren(
      h("h2", { text: "Graph" }),
      h("p", { class: "lede", text: "Entities and the facts that connect them. Click an entity to see how its facts changed over time." }),
      h("div", { class: "toolbar" },
        domainSelect(domain, (v) => setParam("domain", v)),
        toggle("Show past values", history, (v) => setParam("history", v ? 1 : 0)),
        toggle("Show notes and their links", notes === "1", (v) => setParam("notes", v ? 1 : 0))),
      h("div", { class: "graph-wrap" }, box, empty),
      h("div", { class: "graph-key" },
        h("span", { class: "k-current", text: "Current fact" }),
        h("span", { class: "k-past", text: "Past value" }),
        h("span", { class: "k-link", text: "Related notes" })),
    );
    const els = graphElements(data);
    if (!els.length) {
      empty.textContent = "No keyed facts yet. They appear when an agent stores a memory with a subject and relation, like subject “Vivek”, relation “editor”, object “Zed”.";
      return;
    }
    empty.remove();
    if (!window.cytoscape) { box.textContent = "The graph library didn't load (it comes from cdnjs). Check the network and reload."; return; }
    const css = getComputedStyle(document.documentElement);
    const c = (n) => css.getPropertyValue(n).trim();
    const cy = cytoscape({
      container: box, elements: els, wheelSensitivity: 0.25,
      style: [
        { selector: "node", style: { "background-color": c("--current"), label: "data(label)", color: c("--paper"),
          "font-family": "Atkinson Hyperlegible, sans-serif", "font-size": 11, "text-valign": "bottom",
          "text-margin-y": 4, width: 18, height: 18, "text-wrap": "ellipsis", "text-max-width": 140 } },
        { selector: "node.value", style: { shape: "round-rectangle", "background-color": c("--line"), width: 10, height: 10,
          color: c("--muted"), "font-size": 10 } },
        { selector: "node.note", style: { "background-color": c("--muted"), width: 8, height: 8, label: "" } },
        { selector: "node.note:selected, node.note.hover", style: { label: "data(label)" } },
        { selector: "edge", style: { width: 1.5, "line-color": c("--current"), "curve-style": "bezier",
          "target-arrow-shape": "triangle", "target-arrow-color": c("--current"), "arrow-scale": 0.7,
          label: "data(label)", "font-size": 9, color: c("--muted"), "text-rotation": "autorotate",
          "text-background-color": c("--panel"), "text-background-opacity": 1, "text-background-padding": 2 } },
        { selector: "edge.past", style: { "line-style": "dashed", "line-color": c("--past"), "target-arrow-color": c("--past") } },
        { selector: "edge.link", style: { "line-color": c("--line"), "target-arrow-shape": "none", label: "", width: "mapData(w, 0.6, 1, 0.5, 2.5)" } },
      ],
      layout: { name: "cose", animate: false, nodeRepulsion: () => 60000, idealEdgeLength: () => 130,
        nodeOverlap: 40, componentSpacing: 120, nodeDimensionsIncludeLabels: true, gravity: 0.3, padding: 40, randomize: true },
      maxZoom: 2.5, minZoom: 0.15,
    });
    if (cy.zoom() > 1.1) { cy.zoom(1.1); cy.center(); }  // few nodes: don't blow labels up to headline size
    cy.on("tap", "node.entity", (e) => showEntity(e.target.data("eid")));
    cy.on("tap", "node.note", (e) => showMemory(e.target.data("uid")));
    cy.on("mouseover", "node.note", (e) => e.target.addClass("hover"));
    cy.on("mouseout", "node.note", (e) => e.target.removeClass("hover"));
  },

  async memories() {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const q = params.get("q") || "", domain = params.get("domain") || "", history = params.get("history") === "1";
    const asOf = params.get("as_of") || "";
    const input = h("input", { type: "search", value: q, placeholder: "Search memories the way an agent would", "aria-label": "Search memories" });
    const date = h("input", { type: "date", value: asOf, "aria-label": "As of date" });
    const go = (e) => {
      e?.preventDefault();
      const p = new URLSearchParams({ q: input.value, domain: sel.value, history: hist.checked ? 1 : 0, as_of: date.value });
      location.hash = `memories?${p}`;
    };
    const sel = domainSelect(domain, () => go());
    const hist = h("input", { type: "checkbox", checked: history, onchange: go });
    date.addEventListener("change", go);
    const results = h("div", { class: "results" }, h("p", { class: "muted", text: "Loading…" }));
    view.replaceChildren(
      h("h2", { text: "Memories" }),
      h("p", { class: "lede", text: q ? "Ranked exactly as agents see them. The bars show where each result came from: meaning (vector), matching words (keyword) and connections (graph)." : "Newest first. Search to see how retrieval ranks things." }),
      h("form", { class: "toolbar", onsubmit: go }, input, sel,
        h("label", {}, hist, "Include past values"),
        h("label", {}, "As of", date),
        h("button", { type: "submit", class: "primary", text: "Search" })),
      results,
    );
    const asOfIso = asOf ? `${asOf}T23:59:59+00:00` : "";
    const data = await api(`/ui/api/memories?q=${encodeURIComponent(q)}&domain=${encodeURIComponent(domain)}&history=${history ? 1 : 0}&as_of=${encodeURIComponent(asOfIso)}&limit=${q ? 15 : 60}`);
    results.replaceChildren(...(data.items.length ? data.items.map(memoryRow) :
      [h("p", { class: "muted", text: q ? "Nothing matched. Try fewer words, or include past values." : "No memories yet. Agents add them with store_memory_tool." })]));
  },

  async review() {
    const items = await api("/ui/api/reviews");
    updateReviewCount(items.length);
    view.replaceChildren(
      h("h2", { text: "Review" }),
      h("p", { class: "lede", text: "The server asks you when it isn't sure: whether a new statement replaces an old one, whether two notes say the same thing, or whether two names are the same thing." }),
      ...(items.length ? items.map(reviewCard) : [h("p", { class: "muted", text: "Nothing to review. Uncertain updates will show up here." })]),
    );
  },

  async skills() {
    const list = await api("/ui/api/skills");
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const current = list.find((s) => s.name === params.get("name"));
    const nav = h("div", {}, h("button", { class: "list-button", "aria-current": !current ? "true" : "false",
      onclick: () => location.hash = "skills", text: "New skill" }),
      ...list.map((s) => h("button", { class: "list-button", "aria-current": s === current ? "true" : "false",
        onclick: () => location.hash = `skills?name=${encodeURIComponent(s.name)}` },
        h("div", { text: s.name }), h("small", { class: "muted", text: `${s.domain}, version ${s.version}` }))));
    const f = (name, label, value, multiline) => h("label", {}, label, multiline
      ? h("textarea", { name, text: value || "" }) : h("input", { name, value: value || "" }));
    const form = h("form", { class: "form-grid", onsubmit: async (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(form));
      try {
        const r = await api("/ui/api/skills", { method: "POST", body: d });
        toast(r.status === "unchanged" ? "No changes to save." : `Saved ${r.name} (version ${r.version}).`);
        location.hash = `skills?name=${encodeURIComponent(r.name)}`;
        views.skills();
      } catch (err) { toast(err.message); }
    } },
      f("name", "Name", current?.name), f("description", "What it's for", current?.description),
      f("domain", "Domain", current?.domain || "general"),
      f("trigger_tags", "Trigger words (comma-separated)", (current?.trigger_tags || []).join(", ")),
      f("examples", "Example requests (comma-separated)", (current?.examples || []).join(", ")),
      f("instructions", "Instructions (markdown)", current?.instructions, true),
      h("div", { class: "actions" }, h("button", { type: "submit", class: "primary", text: current ? "Save changes" : "Create skill" }),
        current && h("button", { type: "button", class: "danger", text: "Delete skill", onclick: async () => {
          await api("/ui/api/skills/delete", { method: "POST", body: { name: current.name } });
          toast(`Deleted ${current.name}.`); location.hash = "skills";
        } })));
    view.replaceChildren(h("h2", { text: "Skills" }),
      h("p", { class: "lede", text: "Reusable procedures agents fetch with find_skill_tool. Saving an existing name updates it." }),
      h("div", { class: "split" }, nav, form));
  },

  async events() {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const op = params.get("op") || "";
    const sel = h("select", { "aria-label": "Activity type", onchange: () => location.hash = `events?op=${sel.value}` },
      ...[["", "Everything"], ["store", "Stored"], ["supersede", "Replaced old value"], ["review", "Reviewed"],
        ["delete", "Retired"], ["recall", "Prompt recall"], ["tool_call", "Tool calls"], ["consolidate", "Nightly cleanup"]]
        .map(([v, l]) => h("option", { value: v, selected: v === op, text: l })));
    const rows = await api(`/ui/api/events?op=${op}&limit=300`);
    view.replaceChildren(h("h2", { text: "Activity" }),
      h("p", { class: "lede", text: "Every write, replacement and review, newest first. This is the audit trail." }),
      h("div", { class: "toolbar" }, sel),
      table(["When", "What", "Who", "Details"], rows.map((r) => [
        r.ts.replace("T", " ").slice(0, 19), r.tool ? `${r.op}: ${r.tool}` : r.op, r.agent || "", describe(r)]),
        [], "No activity recorded."));
  },

  async settings() {
    const rows = await api("/ui/api/settings");
    const human = (k) => k.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
    const save = async (key, value) => {
      try { await api("/ui/api/settings", { method: "POST", body: { key, value } }); toast(`${human(key)} saved. It applies to the next request.`); }
      catch (err) { toast(err.message); }
    };
    view.replaceChildren(
      h("h2", { text: "Settings" }),
      h("p", { class: "lede", text: "Changes apply immediately, without a restart (the web console switch applies on restart). Values you haven't changed follow the server's environment." }),
      h("div", { class: "toolbar" }, h("button", { text: "Run nightly cleanup now", onclick: async () => {
        const r = await api("/ui/api/consolidate", { method: "POST" });
        toast(`Cleanup done: ${r.merge_proposals} merge and ${r.alias_proposals} name proposals, ${r.archived} archived.`);
      } })),
      ...rows.map((s) => {
        let control;
        if (typeof s.default === "boolean") control = h("input", { type: "checkbox", checked: s.value, "aria-label": human(s.key), onchange: (e) => save(s.key, e.target.checked) });
        else if (s.key === "judge_backend") control = h("select", { "aria-label": human(s.key), onchange: (e) => save(s.key, e.target.value) },
          ...["jev", "laya", "none"].map((v) => h("option", { value: v, selected: v === s.value, text: v })));
        else control = h("input", { type: typeof s.default === "number" ? "number" : "text", step: "any", value: s.value,
          "aria-label": human(s.key), onchange: (e) => save(s.key, e.target.value) });
        const changed = JSON.stringify(s.value) !== JSON.stringify(s.default);
        return h("div", { class: "setting" },
          h("div", {}, h("b", { text: human(s.key) })),
          h("p", { text: s.description + (changed ? ` (default ${s.default})` : "") }),
          h("div", { class: "control" }, control, changed && h("button", { class: "link-button", text: "Reset", onclick: async () => {
            await api("/ui/api/settings", { method: "POST", body: { key: s.key, reset: true } }); toast("Reset."); views.settings();
          } })));
      }));
  },
};

// ── pieces ──────────────────────────────────────────────────────────────────────────────────────
function fact(value, label) { return h("div", { class: "fact" }, h("b", { text: value }), h("span", { class: "muted", text: label })); }

function table(headers, rows, numeric = [], emptyText = "") {
  if (!rows.length) return h("p", { class: "muted", text: emptyText });
  return h("table", {}, h("thead", {}, h("tr", {}, ...headers.map((t, i) => h("th", { class: numeric.includes(i) ? "num" : null, text: t })))),
    h("tbody", {}, ...rows.map((r) => h("tr", {}, ...r.map((c, i) => h("td", { class: numeric.includes(i) ? "num" : null, text: c }))))));
}

function chart(days, reads, writes) {
  const W = 760, H = 150, pad = 22, n = days.length, max = Math.max(1, ...reads, ...writes);
  const bw = (W - pad) / n;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H + 20}`);
  svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Reads and writes per day for the last ${n} days. Peak ${max}.`);
  const rect = (x, y, w, hh, fill, title) => {
    const r = document.createElementNS(ns, "rect");
    Object.entries({ x, y, width: w, height: Math.max(0, hh), rx: 2, fill }).forEach(([k, v]) => r.setAttribute(k, v));
    const t = document.createElementNS(ns, "title"); t.textContent = title; r.append(t);
    svg.append(r);
  };
  days.forEach((d, i) => {
    const x = pad + i * bw;
    const rh = (reads[i] / max) * H, wh = (writes[i] / max) * H;
    rect(x + 2, H - rh, bw / 2 - 3, rh, "var(--current)", `${d}: ${reads[i]} reads`);
    rect(x + bw / 2, H - wh, bw / 2 - 3, wh, "var(--pending)", `${d}: ${writes[i]} writes`);
    if (i % 2 === 0) {
      const t = document.createElementNS(ns, "text");
      t.setAttribute("x", x + 2); t.setAttribute("y", H + 15); t.textContent = d.slice(5);
      svg.append(t);
    }
  });
  const t = document.createElementNS(ns, "text");
  t.setAttribute("x", 0); t.setAttribute("y", 10); t.textContent = max;
  svg.append(t);
  return svg;
}

function domainSelect(value, onChange) {
  return h("select", { "aria-label": "Domain", onchange: (e) => onChange(e.target.value) },
    ...[["", "All domains"], ["identity", "Identity"], ["projects", "Projects"], ["code", "Code"], ["general", "General"], ["diary", "Diary"]]
      .map(([v, l]) => h("option", { value: v, selected: v === value, text: l })));
}

function toggle(label, checked, onChange) {
  return h("label", {}, h("input", { type: "checkbox", checked, onchange: (e) => onChange(e.target.checked) }), label);
}

function memoryRow(m) {
  const ex = m.explain;
  const why = ex ? h("div", { class: "why", "aria-label": "Why this result" },
    ...[["vector", "Meaning"], ["bm25", "Keyword"], ["graph", "Graph"]].flatMap(([k, l]) => {
      const rank = ex.ranks[k];
      return [h("span", { text: rank ? `${l} #${rank}` : `${l}: none` }),
        h("span", { class: "bar" }, h("i", { style: `width:${rank ? Math.max(6, 100 - (rank - 1) * 3.3) : 0}%` }))];
    }),
    ex.rerank !== null ? h("span", { style: "grid-column: 1 / -1", text: `Reranker ${ex.rerank.toFixed(2)}` }) : null) : h("span");
  const row = h("div", { class: "result", tabindex: 0, role: "button", onclick: () => showMemory(m.id),
    onkeydown: (e) => { if (e.key === "Enter") showMemory(m.id); } },
    h("div", {}, h("p", { text: m.content }),
      h("div", { class: "meta" }, chip(m.status), h("span", { text: m.domain }), h("span", { text: day(m.observed_at) }),
        m.relation && h("span", { text: `${m.subject}: ${m.relation.replaceAll("_", " ")}` }),
        m.agent && h("span", { text: `by ${m.agent}` }))),
    why);
  return row;
}

async function showMemory(uid) {
  const m = await api(`/ui/api/memory/${encodeURIComponent(uid)}`);
  openDrawer(
    h("h2", { text: m.subject ? `${m.subject}: ${(m.relation || "").replaceAll("_", " ")}` : "Memory" }),
    h("div", { class: "meta" }, chip(m.status), h("span", { text: m.domain }), h("span", { text: `observed ${day(m.observed_at)}` }),
      m.valid_to && h("span", { text: `until ${day(m.valid_to)}` }), h("span", { text: `read ${m.access_count} times` })),
    h("pre", { text: m.content }),
    m.raw_text && h("p", { class: "muted", text: `Original wording: ${m.raw_text}` }),
    m.tags?.length ? h("p", { class: "muted", text: `Tags: ${m.tags.join(", ")}` }) : null,
    h("h3", { text: "Related memories" }),
    m.related.length ? h("div", {}, ...m.related.map((r) => h("p", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); showMemory(r.uid); }, text: r.content.slice(0, 140) }),
      h("span", { class: "muted", text: `  ${Math.round(r.weight * 100)}% similar` })))) : h("p", { class: "muted", text: "No linked memories." }),
    h("h3", { text: "History" }),
    table(["When", "What", "Who"], m.history.map((e) => [e.ts.replace("T", " ").slice(0, 16), e.op, e.agent || ""]), [], "No recorded events."),
    m.status === "active" && h("div", { class: "actions", style: "margin-top:20px" },
      h("button", { class: "danger", text: "Retire this memory", onclick: async () => {
        await api(`/ui/api/memory/${encodeURIComponent(uid)}/delete`, { method: "POST" });
        toast("Retired. It stays in history but agents won't see it."); closeDrawer(); route();
      } })),
  );
}

async function showEntity(id) {
  const e = await api(`/ui/api/entity/${id}`);
  const tl = e.timeline;
  const now = Date.now();
  const starts = tl.map((f) => Date.parse(f.valid_from)).filter((x) => !isNaN(x));
  const t0 = Math.min(...starts, now - 86400000), span = Math.max(1, now - t0);
  const byRel = {};
  tl.forEach((f) => (byRel[f.relation] ||= []).push(f));
  openDrawer(
    h("h2", { text: e.entity.name }),
    e.entity.aliases?.length ? h("p", { class: "muted", text: `Also called ${e.entity.aliases.join(", ")}` }) : null,
    h("h3", { text: "How its facts changed" }),
    Object.keys(byRel).length ? h("div", { class: "timeline" }, ...Object.entries(byRel).map(([rel, facts]) =>
      h("div", {}, h("div", { class: "track-label", text: rel.replaceAll("_", " ") }),
        h("div", { class: "track" }, ...facts.map((f) => {
          const a = Date.parse(f.valid_from), b = f.valid_to ? Date.parse(f.valid_to) : now;
          const left = ((a - t0) / span) * 100, width = Math.max(1.5, ((b - a) / span) * 100);
          return h("div", { class: `seg ${f.status}`, style: `left:${left}%;width:${width}%`,
            title: `${f.object_text || f.content}\n${day(f.valid_from)} to ${f.valid_to ? day(f.valid_to) : "now"} (${f.status})`,
            text: f.object_text || f.content });
        }))))) : h("p", { class: "muted", text: "No facts recorded with this as the subject." }),
    Object.keys(byRel).length ? h("div", { class: "axis" }, h("span", { text: day(new Date(t0).toISOString()) }), h("span", { text: "now" })) : null,
    h("h3", { text: "Connected to" }),
    e.neighbors.length ? h("p", {}, ...e.neighbors.map((n, i) => [i ? ", " : "", h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); showEntity(n.id); }, text: n.name })]))
      : h("p", { class: "muted", text: "No connected entities." }),
    e.mentioned_in.length ? h("div", {}, h("h3", { text: "Mentioned by" }),
      ...e.mentioned_in.map((m) => h("p", { text: `${m.subject} ${m.relation.replaceAll("_", " ")}: ${m.content}` }))) : null,
  );
}

function reviewCard(r) {
  const labels = {
    supersede: ["Does the new statement replace the old value?", "Replace old value", "Keep both"],
    conflict: ["The new note seems to contradict an older fact. The older one is hidden until you decide.", "New one is right", "Old one is still true"],
    merge: ["These two notes look like they say the same thing.", "Keep only the newer", "Keep both"],
    alias: ["Are these two names the same thing?", "Same thing", "Different things"],
  }[r.kind];
  const decide = async (approve) => {
    try { await api(`/ui/api/reviews/${r.id}`, { method: "POST", body: { approve } }); toast(approve ? labels[1] + "." : labels[2] + "."); views.review(); }
    catch (err) { toast(err.message); }
  };
  const body = r.kind === "alias"
    ? h("div", { class: "pair" }, h("div", {}, h("small", { text: "Name" }), r.names?.[0]), h("div", {}, h("small", { text: "Name" }), r.names?.[1]))
    : h("div", { class: "pair" },
      h("div", {}, h("small", { text: `Older, ${day(r.other?.observed_at)}` }), r.other?.content || "(gone)"),
      h("div", {}, h("small", { text: `Newer, ${day(r.memory?.observed_at)}` }), r.memory?.content || "(gone)"));
  return h("div", { class: "review" },
    h("p", { style: "margin:0", text: labels[0] }),
    h("p", { class: "muted", style: "margin:2px 0 0", text: r.confidence != null ? `Model confidence ${Math.round(r.confidence * 100)}%` : "" }),
    body,
    h("div", { class: "actions" }, h("button", { class: "primary", text: labels[1], onclick: () => decide(true) }),
      h("button", { text: labels[2], onclick: () => decide(false) })));
}

function describe(e) {
  const d = e.detail || {};
  if (e.op === "supersede") return `Replaced “${(d.old_value || "").slice(0, 80)}” (${d.key || ""})`;
  if (e.op === "store") return `${d.domain || ""}${d.keyed ? ", keyed fact" : ""}`;
  if (e.op === "tool_call") return [d.domain, d.n_results != null ? `${d.n_results} results` : "", e.latency_ms != null ? `${e.latency_ms} ms` : "", d.error || ""].filter(Boolean).join(", ");
  if (e.op === "review") return `${d.kind}: ${d.approved ? "approved" : "rejected"}`;
  if (e.op === "recall") return `${d.n} memories injected`;
  return Object.entries(d).map(([k, v]) => `${k} ${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ").slice(0, 160);
}

function graphElements(data) {
  const els = [];
  const ids = new Set();
  data.entities.forEach((e) => { ids.add(`e${e.id}`); els.push({ data: { id: `e${e.id}`, eid: e.id, label: e.name }, classes: "entity" }); });
  data.facts.forEach((f) => {
    const past = f.status !== "active";
    let target = f.object_entity_id ? `e${f.object_entity_id}` : null;
    if (!target) {
      target = `v${f.id}`;
      els.push({ data: { id: target, label: (f.object_text || f.content).slice(0, 40) }, classes: "value" });
    }
    els.push({ data: { id: `f${f.id}`, source: `e${f.subject_id}`, target, label: (f.relation || "").replaceAll("_", " ") }, classes: past ? "past" : "" });
  });
  (data.notes || []).forEach((n) => els.push({ data: { id: `m${n.id}`, uid: n.uid, label: n.content.slice(0, 60) }, classes: "note" }));
  const noteIds = new Set((data.notes || []).map((n) => n.id));
  (data.links || []).forEach((l) => {
    if (noteIds.has(l.src) && noteIds.has(l.dst)) els.push({ data: { id: `l${l.src}-${l.dst}`, source: `m${l.src}`, target: `m${l.dst}`, w: l.weight }, classes: "link" });
  });
  return els;
}

function updateReviewCount(n) { const c = $("#review-count"); c.hidden = !n; c.textContent = n; }

// ── routing ─────────────────────────────────────────────────────────────────────────────────────
async function route() {
  const name = (location.hash.slice(1).split("?")[0]) || "overview";
  const fn = views[name] || views.overview;
  document.querySelectorAll(".rail a").forEach((a) => a.setAttribute("aria-current", a.dataset.view === name ? "page" : "false"));
  closeDrawer();
  try { await fn(); }
  catch (err) { if (err.message !== "Sign in again.") view.replaceChildren(h("p", { class: "error", text: `Couldn't load this view: ${err.message}` })); }
  view.focus({ preventScroll: true });
}

async function start() {
  try { await api("/ui/api/session"); } catch { return; }
  $("#login").hidden = true; $("#app").hidden = false;
  api("/ui/api/reviews").then((r) => updateReviewCount(r.length)).catch(() => {});
  route();
}
window.addEventListener("hashchange", route);
start();
