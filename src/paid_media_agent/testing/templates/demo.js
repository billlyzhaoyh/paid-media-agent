// The demo page. It only draws: every figure comes from the embedded story
// (testing/demo_story.py). One clock, `t` in days elapsed, drives the replay, and
// `render(t)` depends on nothing else, so scrubbing and video frames match playback.
(function () {
  "use strict";
  const S = JSON.parse(document.getElementById("story").textContent);
  const N = S.days.length;
  const SVG = "http://www.w3.org/2000/svg";
  const C = { ink: "#1b150f", muted: "#655c53", line: "#e4ddd3", agent: "#a5432b", alone: "#787069", fill: "#efeae4", model: "#1f6fb0", paper: "#f5f2eb" };
  const params = new URLSearchParams(location.search);
  // A fixed moment (?t=) is drawn finished, like reduced motion: nothing waits to animate.
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches || params.has("t");
  const names = Object.fromEntries(S.campaigns.map((c) => [c.ref, c.name]));
  const planted = new Map(S.planted.map((p) => [p.ref + "|" + p.i, p.kind]));

  const $ = (id) => document.getElementById(id);
  function svg(tag, attrs, parent) {
    const node = document.createElementNS(SVG, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }
  function html(tag, cls, parent, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  const fmt = (v, d) => Number(v).toLocaleString("en-US", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  const amount = (v, metric) => (metric === "spend" ? fmt(v) : fmt(v, v % 1 ? 1 : 0));
  const dayText = (i) => new Date(S.days[Math.max(0, Math.min(N - 1, i))] + "T00:00:00Z").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  const lerp = (a, b, f) => a + (b - a) * f;
  const ease = (f) => (f < 0 ? 0 : f > 1 ? 1 : f * f * (3 - 2 * f));
  const scale = (d0, d1, r0, r1) => (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");
  function ticks(max, count) {
    const raw = max / count, mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const out = [];
    for (let v = 0; v <= max + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
    return out;
  }
  function key(parent, draw, label) {
    const k = html("span", "key", parent);
    const icon = svg("svg", { width: 24, height: 12, "aria-hidden": "true" }, k);
    draw(icon);
    k.appendChild(document.createTextNode(label));
  }
  const ring = (parent, x, y, real) => svg("circle", Object.assign({ cx: x, cy: y, r: 6.5, fill: "none", stroke: C.ink, "stroke-width": 2 }, real ? {} : { "stroke-dasharray": "3 2.5" }), parent);

  // ---- Tooltip -------------------------------------------------------------------------
  const tip = $("tip");
  function showTip(event, markup) {
    tip.innerHTML = markup;
    tip.hidden = false;
    const x = Math.min(window.innerWidth - tip.offsetWidth - 12, event.clientX + 14);
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = event.clientY + 16 + "px";
  }
  const hideTip = () => { tip.hidden = true; };

  // ---- Opening tiles -------------------------------------------------------------------
  (function hero() {
    const model = S.scores[0], rule = S.scores[S.scores.length - 1];
    const tiles = [
      { value: model.real + " of " + S.planted.length, label: "real problems caught by " + S.meta.model, sub: plural(model.false, "false alarm") + ". The ±50% rule raised " + rule.false + "." },
      { value: S.result.gain === null ? "" : (S.result.gain >= 0 ? "+" : "") + fmt(100 * S.result.gain, 1) + "%", label: "more conversions at the same total budget", sub: S.result.captured === null ? "" : fmt(100 * S.result.captured) + "% of the gain there was to have" },
      { value: "0", label: "changes without a person's approval", sub: "Every change is proposed, approved, then read back." },
    ];
    for (const t of tiles) {
      const tile = html("div", "tile", $("hero-tiles"));
      html("p", "value", tile, t.value);
      html("p", "label", tile, t.label);
      html("p", "sub", tile, t.sub);
    }
  })();

  // ---- Job 1: a fixed rule against a learned range -------------------------------------
  (function ruleAgainstRange() {
    const pick = S.intuition.noisy;
    const pts = S.points.filter((p) => p.ref === pick.ref && p.metric === pick.metric).sort((a, b) => a.i - b.i);
    if (!pts.length) return;
    const W = 540, H = 230, L = 34, R = 10, T = 14, B = 24;
    const top = Math.max.apply(null, pts.map((p) => Math.max(p.observed, p.hi))) * 1.08;
    const x = scale(pts[0].i, pts[pts.length - 1].i, L, W - R), y = scale(0, top, H - B, T);
    const ruleHits = new Map(S.alarms.filter((a) => a.method === "rule" && a.ref === pick.ref && a.metric === pick.metric).map((a) => [a.i, a]));
    const line = pts.map((p) => x(p.i) + "," + y(p.observed)).join(" ");
    function frame(root, id) {
      root.setAttribute("viewBox", "0 0 " + W + " " + H);
      for (const v of ticks(top, 4)) {
        svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: C.line }, root);
        svg("text", { x: L - 6, y: y(v) + 3.5, "text-anchor": "end" }, root).textContent = fmt(v);
      }
      for (const p of pts) if (p.i % 14 === 0) svg("text", { x: x(p.i), y: H - 6, "text-anchor": "middle" }, root).textContent = dayText(p.i).slice(4);
      const clip = svg("clipPath", { id: id }, svg("defs", {}, root));
      const sweep = svg("rect", { x: 0, y: 0, width: still ? W : 0, height: H }, clip);
      return { layer: svg("g", { "clip-path": "url(#" + id + ")" }, root), sweep: sweep };
    }
    const left = frame($("intu-rule"), "sweep-rule"), right = frame($("intu-range"), "sweep-range");
    const step = (x(pts[pts.length - 1].i) - x(pts[0].i)) / Math.max(1, pts.length - 1);
    for (let j = 1; j < pts.length; j++) {
      const before = pts[j - 1].observed;
      if (pts[j].i !== pts[j - 1].i + 1 || before <= 0) continue;
      const hi = Math.min(top, before * 1.5);
      svg("rect", { x: x(pts[j].i) - step * 0.36, y: y(hi), width: step * 0.72, height: y(before * 0.5) - y(hi), fill: C.alone, "fill-opacity": 0.22 }, left.layer);
    }
    const band = pts.map((p) => x(p.i) + "," + y(Math.min(top, p.hi))).concat(pts.slice().reverse().map((p) => x(p.i) + "," + y(Math.max(0, p.lo))));
    svg("polygon", { points: band.join(" "), fill: C.model, "fill-opacity": 0.16 }, right.layer);
    svg("polyline", { points: pts.map((p) => x(p.i) + "," + y(p.expected)).join(" "), fill: "none", stroke: C.model, "stroke-width": 1.5 }, right.layer);
    let ruleFalse = 0, modelAlerts = 0, modelFalse = 0;
    for (const side of [left, right]) {
      svg("polyline", { points: line, fill: "none", stroke: C.agent, "stroke-width": 2, "stroke-linejoin": "round" }, side.layer);
      for (const p of pts) svg("circle", { cx: x(p.i), cy: y(p.observed), r: 2.6, fill: C.agent, stroke: C.paper, "stroke-width": 1 }, side.layer);
    }
    for (const p of pts) {
      const hit = ruleHits.get(p.i);
      if (hit) { ring(left.layer, x(p.i), y(p.observed), hit.real); if (!hit.real) ruleFalse++; }
      if (p.flagged) { const real = planted.has(p.ref + "|" + p.i); ring(right.layer, x(p.i), y(p.observed), real); modelAlerts++; if (!real) modelFalse++; }
    }
    $("intu-rule-count").innerHTML = "<b>" + plural(ruleHits.size, "alert") + "</b> <span class='quiet'>· " + ruleFalse + " false</span>";
    $("intu-range-count").innerHTML = "<b>" + plural(modelAlerts, "alert") + "</b> <span class='quiet'>· " + modelFalse + " false</span>";
    const keys = $("intu-keys");
    html("span", "key", keys, names[pick.ref] + " · " + pick.metric + ", " + S.meta.weeks + " weeks");
    key(keys, (i) => { svg("line", { x1: 0, x2: 24, y1: 6, y2: 6, stroke: C.agent, "stroke-width": 2 }, i); svg("circle", { cx: 12, cy: 6, r: 2.6, fill: C.agent }, i); }, "Observed");
    key(keys, (i) => svg("rect", { x: 6, y: 0, width: 12, height: 12, fill: C.alone, "fill-opacity": 0.22 }, i), "±50% of the day before");
    key(keys, (i) => { svg("rect", { x: 0, y: 1, width: 24, height: 10, fill: C.model, "fill-opacity": 0.16 }, i); svg("line", { x1: 0, x2: 24, y1: 6, y2: 6, stroke: C.model, "stroke-width": 1.5 }, i); }, "Expected, with its range");
    key(keys, (i) => ring(i, 12, 6, true).setAttribute("r", 5), "Alert on a real problem");
    key(keys, (i) => ring(i, 12, 6, false).setAttribute("r", 5), "False alarm");
    if (still) return;
    new IntersectionObserver((entries, observer) => {
      if (!entries[0].isIntersecting) return;
      observer.disconnect();
      const began = performance.now();
      (function grow(now) {
        const f = Math.min(1, (now - began) / 2600);
        left.sweep.setAttribute("width", W * f);
        right.sweep.setAttribute("width", W * f);
        if (f < 1) requestAnimationFrame(grow);
      })(began);
    }, { threshold: 0.4 }).observe($("intu-rule"));
  })();

  // ---- Job 2: move budget from the flat curve to the steep one -------------------------
  (function curves() {
    const root = $("intu-curves"), pair = S.intuition.curves;
    if (!pair.length) return;
    const W = 1100, H = 330, L = 46, R = 24, T = 20, B = 34;
    root.setAttribute("viewBox", "0 0 " + W + " " + H);
    const maxX = pair[0].points[pair[0].points.length - 1][0];
    const maxY = Math.max.apply(null, pair.map((c) => c.points[c.points.length - 1][1])) * 1.06;
    const x = scale(0, maxX, L, W - R), y = scale(0, maxY, H - B, T);
    for (const v of ticks(maxY, 4)) { svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: C.line }, root); svg("text", { x: L - 8, y: y(v) + 3.5, "text-anchor": "end" }, root).textContent = fmt(v); }
    for (const v of ticks(maxX, 6)) svg("text", { x: x(v), y: H - 10, "text-anchor": "middle" }, root).textContent = fmt(v);
    svg("text", { x: W - R, y: H - 10, "text-anchor": "end" }, root).textContent = "daily budget · " + S.meta.currency;
    const valueAt = (curve, b) => {
      const p = curve.points, stepB = p[1][0] - p[0][0], j = Math.max(0, Math.min(p.length - 2, Math.floor(b / stepB)));
      return lerp(p[j][1], p[j + 1][1], (b - p[j][0]) / stepB);
    };
    const colours = [C.agent, C.ink];
    const parts = pair.map((curve, k) => {
      svg("polyline", { points: curve.points.map((p) => x(p[0]) + "," + y(p[1])).join(" "), fill: "none", stroke: colours[k], "stroke-width": 2.5, "stroke-linejoin": "round" }, root);
      svg("circle", { cx: x(curve.start), cy: y(curve.start_value), r: 6, fill: C.paper, stroke: colours[k], "stroke-width": 2 }, root);
      return {
        curve: curve,
        tangent: svg("line", { stroke: colours[k], "stroke-width": 1.5, "stroke-dasharray": "5 4" }, root),
        drop: svg("line", { stroke: C.line, "stroke-width": 1 }, root),
        dot: svg("circle", { r: 7, fill: colours[k], stroke: C.paper, "stroke-width": 2 }, root),
        name: svg("text", { class: "strong" }, root),
        label: svg("text", { class: "label" }, root),
        budget: svg("text", { "text-anchor": "middle" }, root),
      };
    });
    const gap = svg("text", { x: L + 14, y: T + 16, class: "strong" }, root);
    function draw(s) {
      const returns = [];
      for (const part of parts) {
        const c = part.curve, b = lerp(c.start, c.now, s), v = valueAt(c, b), d = maxX / 60;
        const slope = (valueAt(c, Math.min(maxX, b + d)) - valueAt(c, Math.max(0, b - d))) / (Math.min(maxX, b + d) - Math.max(0, b - d));
        const reach = maxX * 0.09;
        part.tangent.setAttribute("x1", x(b - reach)); part.tangent.setAttribute("y1", y(v - slope * reach));
        part.tangent.setAttribute("x2", x(b + reach)); part.tangent.setAttribute("y2", y(v + slope * reach));
        part.drop.setAttribute("x1", x(b)); part.drop.setAttribute("x2", x(b)); part.drop.setAttribute("y1", y(v)); part.drop.setAttribute("y2", H - B);
        part.dot.setAttribute("cx", x(b)); part.dot.setAttribute("cy", y(v));
        const right = x(b) > W * 0.72, side = right ? -14 : 14, below = parts.indexOf(part) ? 1 : -1;
        for (const text of [part.name, part.label]) { text.setAttribute("x", x(b) + side); text.setAttribute("text-anchor", right ? "end" : "start"); }
        part.name.setAttribute("y", y(v) + (below > 0 ? 24 : -30)); part.name.textContent = names[c.ref] + " · " + fmt(b) + " a day";
        part.label.setAttribute("y", y(v) + (below > 0 ? 40 : -14));
        part.label.textContent = "next " + fmt(S.meta.step) + " buys " + fmt(slope * S.meta.step, 2) + " conversions";
        part.budget.textContent = "";
        returns.push(slope * S.meta.step);
      }
      gap.textContent = "Difference between the two: " + fmt(Math.abs(returns[0] - returns[1]), 2) + " conversions per extra " + fmt(S.meta.step);
    }
    let s = 0, target = 0;
    const before = $("curve-before"), now = $("curve-now");
    function go(to) {
      target = to;
      before.setAttribute("aria-pressed", String(to === 0)); now.setAttribute("aria-pressed", String(to === 1));
      if (still) { s = to; draw(s); return; }
      const from = s, began = performance.now();
      (function move(time) {
        const f = Math.min(1, (time - began) / 1400);
        s = lerp(from, target, ease(f)); draw(s);
        if (f < 1 && target === to) requestAnimationFrame(move);
      })(began);
    }
    before.addEventListener("click", () => go(0));
    now.addEventListener("click", () => go(1));
    draw(still ? 1 : 0);
    if (still) { go(1); return; }
    new IntersectionObserver((entries, observer) => { if (entries[0].isIntersecting) { observer.disconnect(); setTimeout(() => go(1), 700); } }, { threshold: 0.5 }).observe(root);
  })();

  // ---- Feature pipelines ---------------------------------------------------------------
  (function pipelines() {
    const host = $("pipelines");
    const kv = (parent, k, v, cls) => { const row = html("p", "kv" + (cls ? " " + cls : ""), parent); html("span", "", row, k); html("span", "", row, v); };
    const chip = (parent, name, value, why, strong) => { const c = html("div", "chip" + (strong ? " key-feature" : ""), parent); html("p", "name", c, name); html("p", "val", c, value); html("p", "why", c, why); };
    function pipe(title, tag, who, raw, chips, note, model, out, evidence) {
      const wrap = html("div", "pipe", host);
      const head = html("div", "pipe-title", wrap);
      html("span", "tag", head, tag); html("b", "", head, title);
      const row = html("div", "pipe-row", wrap);
      const a = html("div", "stage-card", row); html("p", "cap", a, "Raw daily rows"); raw(a);
      html("div", "arrow", row, "→");
      const b = html("div", "stage-card", row); html("p", "cap", b, "One engineered row"); chips(html("div", "chips", b)); html("p", "small", b, note);
      html("div", "arrow", row, "→");
      const c = html("div", "stage-card model", row); html("b", "", c, who); model(c);
      html("div", "arrow", row, "→");
      const d = html("div", "stage-card", row); out(d);
      const ev = html("div", "evidence", wrap);
      html("p", "cap", ev, evidence.title + " · " + evidence.unit);
      const most = Math.max.apply(null, evidence.bars.map((bar) => bar.value));
      evidence.bars.forEach((bar, k) => {
        const line = html("div", "ev-row" + (k ? " good" : ""), ev);
        html("span", "", line, bar.label);
        const track = html("span", "ev-track", line);
        html("span", "ev-bar", track).style.width = (100 * bar.value) / most + "%";
        html("span", "ev-val", line, bar.low === undefined ? fmt(bar.value, 2) : bar.low + " to " + bar.value);
      });
      html("p", "small", ev, evidence.note + " Source: " + evidence.source + ".");
    }
    const fr = S.features.ranges, fb = S.features.budgets, ev = S.features.evidence;
    if (fr) {
      const judged = fr.rows[fr.rows.length - 1].cells, ex = fr.example, extra = Object.fromEntries(fr.extras);
      const money = ex.metric === "spend";
      pipe("Unusual days", "Job 1", fr.model,
        (card) => { kv(card, "Campaign", judged[0]); kv(card, "Day", judged[1]); kv(card, fr.headers[4], judged[4]); kv(card, fr.answer_header, "?", "ask"); html("p", "small", card, fmt(fr.history_rows) + " past rows · " + fr.judged_rows + " to judge"); },
        (grid) => {
          chip(grid, fr.headers[4], judged[4], money ? "A deliberate change is explained, not flagged" : "Conversions follow spend", true);
          chip(grid, "Usual of last 7 days", judged[2], "The campaign's recent level");
          chip(grid, "A week before", judged[3], "Same weekday, last week");
          chip(grid, "Simple estimate", extra["Simple estimate"] || "", money ? "Usual level × budget change" : "Usual level × spend change", true);
          chip(grid, "Weekday", (extra.Weekday || "").slice(0, 3), "Weekly rhythm");
          chip(grid, "Day number", extra["Day number"] || "", "Trend");
        },
        "Conversions are scaled for those still arriving, and the model learns from settled days only.",
        (card) => html("p", "small", card, "One request. Nothing trained for this account."),
        (card) => {
          html("p", "cap", card, "A range for that row");
          kv(card, "Low (" + ex.low_level + "%)", fmt(ex.lo)); kv(card, "Expected", fmt(ex.expected)); kv(card, "High (" + ex.high_level + "%)", fmt(ex.hi));
          kv(card, "Observed", fmt(ex.observed));
          html("p", "small", card, "Outside the range: an alert." + (ex.note ? " It was a " + ex.note.replace(", caught", "") + "." : ""));
        }, ev.anomaly);
    }
    if (fb && fb.unit && fb.sample) {
      const units = fb.sample[0] / fb.unit, asked = fb.rows.filter((r) => r.asked), seen = fb.rows.filter((r) => !r.asked);
      pipe("Budgets", "Job 2", fb.model,
        (card) => { kv(card, "Campaign", fb.entity_name); kv(card, "Day", seen[seen.length - 1].cells[1]); kv(card, "Spend", fmt(fb.sample[0])); kv(card, "Conversions", fmt(fb.sample[1])); html("p", "small", card, fmt(fb.history_rows) + " rows from all " + fb.campaigns + " campaigns"); },
        (grid) => {
          chip(grid, "Cost per conversion", fmt(fb.unit, 1), "The campaign's context", true);
          chip(grid, "Spend in those units", fmt(units, 1), "One scale for every campaign", true);
          chip(grid, "log(units + 1)", fmt(Math.log(units + 1), 2), "Diminishing returns become a line");
          chip(grid, "Weekday", seen[seen.length - 1].cells[1].slice(0, 3), "Weekly rhythm");
          chip(grid, "Target", fmt(Math.log(fb.sample[1] + 1), 2), "log(conversions + 1): small counts, tamed");
          chip(grid, "Asked at", fb.levels + " spends", "Per campaign, on every weekday");
        },
        "The answer is the mean of 19 quantiles. The median of small counts moves in steps and hides the response.",
        (card) => html("p", "small", card, "One request. No curve shape assumed."),
        (card) => {
          html("p", "cap", card, "Conversions at spends not tried");
          for (const r of asked) kv(card, "at " + r.cells[2] + " a day", r.predicted || "");
          html("p", "small", card, "These join the campaign's own days when its curve is fitted.");
        }, ev.budget);
    }
  })();

  // ---- The replay ----------------------------------------------------------------------
  const refs = S.campaigns.map((c) => c.ref);
  const gains = { agent: [0], best: [0] };
  for (let i = 0; i < N; i++) {
    gains.agent.push(gains.agent[i] + S.series.agent[i] - S.series.static[i]);
    gains.best.push(gains.best[i] + S.series.best[i] - S.series.static[i]);
  }
  const at = (list, t) => { const i = Math.min(N - 1, Math.floor(t)); return lerp(list[i], list[i + 1], Math.min(1, t - i)); };
  const reveals = []; // {node, t}: shown once the clock reaches t
  const show = (node, t) => { reveals.push({ node: node, t: t }); return node; };

  // The race
  const race = (function () {
    const root = $("race"), W = 760, H = 184, L = 40, R = 150, T = 10, B = 24;
    root.setAttribute("viewBox", "0 0 " + W + " " + H);
    const top = Math.max(gains.best[N], gains.agent[N], 1) * 1.1;
    const x = scale(0, N, L, W - R), y = scale(0, top, H - B, T);
    for (const v of ticks(top, 4)) { svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: C.line }, root); svg("text", { x: L - 6, y: y(v) + 3.5, "text-anchor": "end" }, root).textContent = (v ? "+" : "") + fmt(v); }
    for (let w = 0; w < N / 7; w++) svg("text", { x: x(7 * w + 3.5), y: H - 7, "text-anchor": "middle" }, root).textContent = "week " + (w + 1);
    svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), stroke: C.alone, "stroke-width": 2 }, root);
    svg("text", { x: W - R + 8, y: y(0) + 4, class: "label" }, root).textContent = S.meta.labels.static;
    const moves = S.decisions.map((d) => show(svg("rect", { x: x(d.t) - 3.5, y: y(0) - 3.5, width: 7, height: 7, fill: C.agent, stroke: C.paper, "stroke-width": 1.5 }, root), d.t));
    void moves;
    const best = svg("polyline", { fill: "none", stroke: C.ink, "stroke-width": 1.5, "stroke-dasharray": "4 3" }, root);
    const agent = svg("polyline", { fill: "none", stroke: C.agent, "stroke-width": 2.5, "stroke-linejoin": "round" }, root);
    const tips = { best: svg("circle", { r: 3.5, fill: C.ink, stroke: C.paper, "stroke-width": 1.5 }, root), agent: svg("circle", { r: 4.5, fill: C.agent, stroke: C.paper, "stroke-width": 1.5 }, root) };
    const labels = { best: svg("text", { class: "label" }, root), agent: svg("text", { class: "strong" }, root) };
    const path = (list, t) => { const pts = []; for (let i = 0; i <= Math.floor(t); i++) pts.push(x(i) + "," + y(list[i])); pts.push(x(t) + "," + y(at(list, t))); return pts.join(" "); };
    root.addEventListener("mousemove", (event) => {
      const box = root.getBoundingClientRect(), i = Math.round(((event.clientX - box.left) / box.width * W - L) / (W - R - L) * N);
      if (i < 1 || i > Math.floor(clock.t)) return hideTip();
      showTip(event, "<b>After " + plural(i, "day") + " · " + dayText(i - 1) + "</b><br>" + S.meta.labels.agent + ": +" + fmt(gains.agent[i]) + " conversions<br>" + S.meta.labels.best + ": +" + fmt(gains.best[i]));
    });
    root.addEventListener("mouseleave", hideTip);
    return function (t) {
      agent.setAttribute("points", path(gains.agent, t)); best.setAttribute("points", path(gains.best, t));
      const ya = y(at(gains.agent, t)); let yb = y(at(gains.best, t));
      tips.agent.setAttribute("cx", x(t)); tips.agent.setAttribute("cy", ya); tips.best.setAttribute("cx", x(t)); tips.best.setAttribute("cy", yb);
      if (ya - yb < 15) yb = ya - 15; // keep the two end labels apart
      labels.agent.setAttribute("x", x(t) + 9); labels.agent.setAttribute("y", ya + 4); labels.agent.textContent = S.meta.labels.agent;
      labels.best.setAttribute("x", x(t) + 9); labels.best.setAttribute("y", yb + 4); labels.best.textContent = S.meta.labels.best;
      $("gain-now").textContent = "+" + fmt(at(gains.agent, t)) + " conversions";
    };
  })();

  // The watch: one strip per campaign and metric
  const strips = (function () {
    const host = $("strips"), W = 360, H = 54, L = 4, R = 4, T = 8, B = 4;
    const x = (i) => L + ((i + 0.5) / N) * (W - L - R);
    const sweeps = [];
    for (const ref of refs) for (const metric of ["spend", "conversions"]) {
      const pts = S.points.filter((p) => p.ref === ref && p.metric === metric).sort((a, b) => a.i - b.i);
      const cell = html("div", "strip", host), name = html("p", "name", cell);
      html("b", "", name, names[ref]); html("span", "", name, metric);
      const root = svg("svg", { class: "chart", viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": names[ref] + " " + metric + ": observed against the expected range" }, cell);
      if (!pts.length) continue;
      const top = Math.max.apply(null, pts.map((p) => Math.max(p.observed, p.hi))) * 1.05 || 1;
      const y = scale(0, top, H - B, T);
      svg("line", { x1: L, x2: W - R, y1: H - B, y2: H - B, stroke: C.line }, root);
      const groups = new Map();
      for (const p of pts) { if (!groups.has(p.reveal)) groups.set(p.reveal, []); groups.get(p.reveal).push(p); }
      for (const [reveal, group] of groups) {
        const half = (W - L - R) / N / 2, edge = (p, side) => x(p.i) + side * half;
        const upper = [], low = [];
        for (const p of group) upper.push(edge(p, -1) + "," + y(Math.min(top, p.hi)), edge(p, 1) + "," + y(Math.min(top, p.hi)));
        for (let k = group.length - 1; k >= 0; k--) low.push(edge(group[k], 1) + "," + y(Math.max(0, group[k].lo)), edge(group[k], -1) + "," + y(Math.max(0, group[k].lo)));
        show(svg("polygon", { points: upper.concat(low).join(" "), fill: C.model, "fill-opacity": 0.18 }, root), reveal);
      }
      const clip = svg("clipPath", { id: "strip-" + ref + "-" + metric }, svg("defs", {}, root));
      const sweep = svg("rect", { x: 0, y: 0, width: 0, height: H }, clip);
      sweeps.push({ rect: sweep, pts: pts, daily: metric === "spend" });
      svg("polyline", { points: pts.map((p) => x(p.i) + "," + y(p.observed)).join(" "), fill: "none", stroke: C.agent, "stroke-width": 1.4, "stroke-linejoin": "round", "clip-path": "url(#strip-" + ref + "-" + metric + ")" }, root);
      for (const a of S.alarms) if (a.method === "rule" && a.ref === ref && a.metric === metric) show(svg("line", { x1: x(a.i), x2: x(a.i), y1: 0, y2: 5, stroke: C.alone, "stroke-width": 1.5 }, root), a.reveal);
      for (const p of pts) {
        if (p.flagged) show(ring(root, x(p.i), y(p.observed), planted.has(ref + "|" + p.i)), p.reveal).setAttribute("r", 5.5);
        else if (p.note) show(svg("path", { d: "M" + x(p.i) + " " + (H - B - 7) + " l4 7 l-8 0 z", fill: C.ink }, root), p.reveal);
      }
      root.addEventListener("mousemove", (event) => {
        const box = root.getBoundingClientRect(), i = Math.floor(((event.clientX - box.left) / box.width * W - L) / (W - L - R) * N);
        const p = pts.find((q) => q.i === i);
        if (!p || (metric === "spend" ? p.i + 1 : p.reveal) > clock.t) return hideTip();
        const judged = p.reveal <= clock.t;
        showTip(event, "<b>" + names[ref] + " " + metric + " · " + dayText(p.i) + "</b><br>observed " + amount(p.observed, metric) + (judged ? "<br>expected " + amount(p.expected, metric) + " (" + amount(p.lo, metric) + " to " + amount(p.hi, metric) + ")" + (p.note ? "<br>" + p.note : "") : "<br>not checked yet"));
      });
      root.addEventListener("mouseleave", hideTip);
    }
    const keys = $("watch-keys");
    key(keys, (i) => svg("line", { x1: 0, x2: 24, y1: 6, y2: 6, stroke: C.agent, "stroke-width": 1.6 }, i), "Observed");
    key(keys, (i) => svg("rect", { x: 0, y: 1, width: 24, height: 10, fill: C.model, "fill-opacity": 0.18 }, i), "Expected range, drawn at each weekly check");
    key(keys, (i) => ring(i, 12, 6, true).setAttribute("r", 5), "Alert, real problem");
    key(keys, (i) => ring(i, 12, 6, false).setAttribute("r", 5), "False alarm");
    key(keys, (i) => svg("line", { x1: 12, x2: 12, y1: 1, y2: 9, stroke: C.alone, "stroke-width": 1.5 }, i), "±50% rule would alert");
    if (S.points.some((p) => p.note && !p.flagged)) key(keys, (i) => svg("path", { d: "M12 2 l5 8 l-10 0 z", fill: C.ink }, i), "Planted, missed");
    const model = S.points.filter((p) => p.flagged), others = { local: S.alarms.filter((a) => a.method === "local"), rule: S.alarms.filter((a) => a.method === "rule") };
    function tally(list, t, isReal) {
      const seen = new Map();
      for (const item of list) if (item.reveal <= t) seen.set(item.ref + "|" + item.i, isReal(item));
      let real = 0; for (const v of seen.values()) if (v) real++;
      return { alerts: seen.size, real: real, false: seen.size - real };
    }
    return function (t) {
      for (const s of sweeps) {
        let upTo = t;
        if (!s.daily) { upTo = 0; for (const p of s.pts) if (p.reveal <= t) upTo = Math.max(upTo, p.i + 1); }
        s.rect.setAttribute("width", L + (Math.min(N, upTo) / N) * (W - L - R));
      }
      const rows = [[S.meta.model, tally(model, t, (p) => planted.has(p.ref + "|" + p.i))]];
      if (S.scores.length > 2) rows.push(["Local model", tally(others.local, t, (a) => a.real)]);
      rows.push(["±50% rule", tally(others.rule, t, (a) => a.real)]);
      $("watch-counts").innerHTML = rows.map((r) => "<span><b>" + r[0] + "</b> " + plural(r[1].alerts, "alert") + " <span class='quiet'>· " + r[1].real + " real · " + r[1].false + " false</span></span>").join("");
    };
  })();

  // Budgets, and what the next step buys in each campaign
  const budgetRows = (function () {
    const host = $("budget-rows");
    const start = Object.fromEntries(refs.map((r) => [r, S.budgets.static[r][0]]));
    const most = Math.max.apply(null, refs.map((r) => Math.max(start[r], Math.max.apply(null, S.budgets.agent[r])))) * 1.04;
    const all = refs.flatMap((r) => S.returns.agent[r].concat([S.returns.before[r]]));
    const lo = Math.min.apply(null, all), hi = Math.max.apply(null, all), pos = scale(lo, hi, 6, 62);
    const rows = refs.map((ref) => {
      const row = html("div", "budget-row", host), head = html("div", "head", row);
      html("b", "", head, names[ref]);
      const value = html("span", "", head);
      const ret = html("div", "ret", row); html("span", "axis", ret);
      const track = html("div", "track", row), bar = html("span", "bar", track), was = html("span", "was", track);
      was.style.left = (100 * start[ref]) / most + "%"; was.title = "Before the agent: " + fmt(start[ref]);
      return { ref: ref, value: value, bar: bar, dot: html("span", "dot", ret), ret: html("span", "val", ret) };
    });
    function value(list, first, t) {
      const i = Math.min(N - 1, Math.floor(t)), before = i === 0 ? first : list[i - 1];
      return t >= N ? list[N - 1] : lerp(before, list[i], ease((t - i) / 0.5));
    }
    return function (t) {
      const now = [];
      for (const row of rows) {
        const b = value(S.budgets.agent[row.ref], start[row.ref], t), r = value(S.returns.agent[row.ref], S.returns.before[row.ref], t);
        row.bar.style.width = (100 * b) / most + "%"; row.value.textContent = fmt(b);
        row.dot.style.left = pos(r) + "%"; row.ret.textContent = fmt(r, 2);
        now.push(r);
      }
      const was = refs.map((r) => S.returns.before[r]);
      $("gap").innerHTML = "Best against worst campaign: <b>" + fmt(Math.max.apply(null, now) - Math.min.apply(null, now), 2) + "</b> apart (" + fmt(Math.max.apply(null, was) - Math.min.apply(null, was), 2) + " before the agent). By the simulation's true curves.";
    };
  })();

  // The log
  const logger = (function () {
    const host = $("log"); let shown = -1;
    return function (t) {
      const visible = S.log.filter((entry) => entry.t <= t);
      if (visible.length === shown) return;
      const grew = visible.length > shown && shown >= 0; shown = visible.length;
      host.textContent = "";
      visible.slice(-9).reverse().forEach((entry, k) => {
        const item = html("li", entry.kind + (grew && k === 0 ? " fresh" : ""), host);
        html("span", "mark", item);
        const body = html("span", "", item); html("span", "when", body, entry.day); body.appendChild(document.createTextNode(entry.text));
      });
    };
  })();

  const clock = { t: 0, playing: false, speed: 1, last: 0 };
  let lastWhole = -1;
  function render(t) {
    clock.t = Math.max(0, Math.min(N, t));
    race(clock.t); strips(clock.t); budgetRows(clock.t); logger(clock.t);
    const whole = Math.floor(clock.t);
    if (whole !== lastWhole) { lastWhole = whole; for (const r of reveals) r.node.style.display = r.t <= clock.t ? "" : "none"; }
    const d = Math.min(N - 1, whole);
    $("clock-week").textContent = clock.t >= N ? S.meta.weeks + " weeks complete" : "Week " + (Math.floor(d / 7) + 1) + " of " + S.meta.weeks;
    $("clock-day").textContent = dayText(d);
    $("scrub").value = clock.t;
    $("play").textContent = clock.playing ? "❚❚" : clock.t >= N ? "↻" : "▶";
    $("play").setAttribute("aria-label", clock.playing ? "Pause" : "Play");
  }
  function tick(now) {
    if (!clock.playing) return;
    const next = clock.t + ((now - clock.last) / 1000) * 2 * clock.speed;
    clock.last = now;
    if (next >= N) { clock.playing = false; render(N); return; }
    render(next); requestAnimationFrame(tick);
  }
  function play() { if (clock.t >= N) render(0); clock.playing = true; clock.last = performance.now(); render(clock.t); requestAnimationFrame(tick); }
  function pause() { clock.playing = false; render(clock.t); }
  $("play").addEventListener("click", () => (clock.playing ? pause() : play()));
  $("scrub").addEventListener("input", (event) => { clock.playing = false; render(Number(event.target.value)); });
  document.querySelectorAll("[data-speed]").forEach((button) => button.addEventListener("click", () => {
    clock.speed = Number(button.dataset.speed);
    document.querySelectorAll("[data-speed]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
  }));
  document.addEventListener("keydown", (event) => { if (event.code === "Space" && event.target === document.body) { event.preventDefault(); clock.playing ? pause() : play(); } });

  // ---- Result --------------------------------------------------------------------------
  (function result() {
    const alerts = $("result-alerts"), cap = html("figcaption", "", alerts);
    html("span", "cap", cap, "Alerts over " + S.meta.weeks + " weeks, on the same days");
    const most = Math.max.apply(null, S.scores.map((s) => s.alerts)) || 1;
    for (const s of S.scores) {
      const row = html("div", "score-row", alerts); html("span", "who", row, s.label.replace(", 95% expected range", ""));
      const bars = html("span", "bars", row);
      if (s.real) html("span", "real", bars).style.width = (100 * s.real) / most + "%";
      if (s.false) html("span", "false", bars).style.width = (100 * s.false) / most + "%";
      html("span", "txt", row, s.real + " real of " + S.planted.length + " planted · " + plural(s.false, "false alarm"));
    }
    const budget = $("result-budget"), cap2 = html("figcaption", "", budget);
    html("span", "cap", cap2, "Conversions a day, at the same total budget");
    const three = html("div", "three", budget), r = S.result;
    const pct = (v) => (v === null ? "" : (v >= 0 ? "+" : "") + fmt(100 * v, 1) + "%");
    [["static", "the comparison"], ["agent", pct(r.gain)], ["best", pct(r.best_gain) + ", knowing the true curves"]].forEach((item) => {
      const cell = html("div", "", three); html("p", "v", cell, fmt(r.per_day[item[0]], 1)); html("p", "l", cell, S.meta.labels[item[0]]); html("p", "s", cell, item[1]);
    });
    if (r.captured !== null) html("p", "small", budget, "The agent captured " + fmt(100 * r.captured) + "% of the gain there was to have.").style.marginTop = "12px";
    const words = { recorded: "recorded answers, replayed with no call", live: "asked live", cached: "from the stored cache", local: "computed locally", rule: "no model ran" };
    $("sources").textContent = "A simulated store, so results are scored against the simulation's truth, not real sales. Weekly budget predictions: " + (words[S.meta.sources.budgets] || S.meta.sources.budgets) + ". Weekly checks: " + S.meta.sources.watch_label + ", " + (words[S.meta.sources.watch] || S.meta.sources.watch) + ". " + S.meta.sources.text + ".";
  })();

  // ---- Start ---------------------------------------------------------------------------
  window.demoRender = render;
  if (params.has("video")) document.body.classList.add("video");
  if (params.has("t")) render(Number(params.get("t")));
  else if (still) render(N);
  else {
    render(0);
    new IntersectionObserver((entries, observer) => { if (entries[0].isIntersecting) { observer.disconnect(); play(); } }, { threshold: 0.3 }).observe($("player"));
  }
})();
