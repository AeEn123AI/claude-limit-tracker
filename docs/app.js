/* Claude limit tracker: static front end. No dependencies; charts are hand-drawn SVG. */
(function () {
  "use strict";

  var state = { unit: "usd", adjust: true, range: "all" };
  var DATA = null;
  var SVGNS = "http://www.w3.org/2000/svg";
  var DAY = 86400;
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var UNIT_NAME = { usd: "API-equivalent $", wtok: "weighted tokens", rawtok: "raw tokens" };

  // ------------------------------------------------------------ helpers
  function $(sel) { return document.querySelector(sel); }
  function el(tag, attrs, text) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }
  function sv(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    if (attrs) for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function cssVar(name) { return getComputedStyle($(".viz-root")).getPropertyValue(name).trim(); }

  function fmt(v, unit, short) {
    if (v == null || !isFinite(v)) return "–";
    if (unit === "usd") {
      if (v >= 100) return "$" + Math.round(v).toLocaleString("en-US");
      if (v >= 10) return "$" + v.toFixed(short ? 0 : 1);
      return "$" + v.toFixed(short ? 1 : 2);
    }
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 || short ? 0 : 1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(0) + "K";
    return String(Math.round(v));
  }
  function d2(n) { return (n < 10 ? "0" : "") + n; }
  function dateLabel(t) { var d = new Date(t * 1000); return MONTHS[d.getUTCMonth()] + " " + d.getUTCDate(); }
  function dateTime(t) {
    var d = new Date(t * 1000);
    return MONTHS[d.getUTCMonth()] + " " + d.getUTCDate() + ", " + d2(d.getUTCHours()) + ":" + d2(d.getUTCMinutes()) + " UTC";
  }
  function isoDate(s) { return s ? s.slice(0, 10) : "–"; }
  function prettyModel(m) {
    if (m === "other") return "Other models";
    var r = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(m);
    if (!r) return m;
    return r[1].charAt(0).toUpperCase() + r[1].slice(1) + " " + r[2] + (r[3] ? "." + r[3] : "");
  }
  function pct(x) { return Math.round(x * 100) + "%"; }

  // colour follows the model: fixed slots by overall usage, everything else is "Other"
  var colorSlots = {};
  function modelSlot(m) { return colorSlots[m] || 3; }
  function seriesColor(slot) { return cssVar("--series-" + slot); }
  function dominant(w) {
    var best = null, bv = -1;
    for (var m in w.mix) if (w.mix[m] > bv) { bv = w.mix[m]; best = m; }
    return best;
  }

  // ------------------------------------------------------------ data access
  function kind() { return state.adjust ? "adj" : "est"; }
  function pointVals(w) {
    var e = (state.adjust && w.adj ? w.adj : w.est);
    return e ? e[state.unit] : null;
  }
  function windowX(w) {
    var gen = DATA.generatedT;
    var s = w.anchored ? w.start : w.t_first;
    var e = Math.min(w.reset, gen, Math.max(w.t_last, s + 1));
    return (s + e) / 2;
  }
  function inRange(w, lastT) {
    if (state.range === "all") return true;
    return windowX(w) >= lastT - Number(state.range) * DAY;
  }
  function overhead() {
    var g = (DATA.gaps || {}).cli_cost_vs_logged || {};
    var rs = [];
    ["stream_results", "transcript_cost_state"].forEach(function (k) {
      if (g[k] && g[k].pooled_ratio) rs.push(g[k].pooled_ratio);
    });
    if (!rs.length) return null;
    return { lo: Math.min.apply(null, rs), hi: Math.max.apply(null, rs) };
  }

  // ------------------------------------------------------------ tiles
  function renderTiles() {
    var box = $("#tiles");
    box.textContent = "";
    var oh = overhead();
    Object.keys(DATA.bars).forEach(function (key) {
      var bar = DATA.bars[key];
      var t = el("article", { "class": "tile" });
      t.appendChild(el("div", { "class": "label" }, bar.label + ": 100% ≈"));
      var cur = bar.current;
      if (!cur) {
        t.appendChild(el("div", { "class": "value" }, "–"));
        t.appendChild(el("div", { "class": "range" }, "Not enough data yet (" + bar.n_windows + " windows seen)."));
        box.appendChild(t);
        return;
      }
      var c = cur[kind()][state.unit];
      var v = el("div", { "class": "value" }, fmt(c.median, state.unit) + " ");
      var sub = state.adjust && bar.ref_model ? "of " + prettyModel(bar.ref_model) : "at actual mix";
      if (state.unit !== "usd") sub = (state.unit === "wtok" ? "weighted tokens " : "raw tokens ") + sub;
      v.appendChild(el("small", null, sub));
      t.appendChild(v);
      t.appendChild(el("div", { "class": "range" },
        "95% CI " + fmt(c.lo, state.unit) + "–" + fmt(c.hi, state.unit) +
        " · typical window " + fmt(c.p25, state.unit) + "–" + fmt(c.p75, state.unit)));

      if (state.adjust && bar.weights) {
        var ul = el("ul");
        var uncounted = [], counted = [];
        Object.keys(bar.weights).forEach(function (g) {
          var w = bar.weights[g];
          if (g === "other" && w.share < 0.03) return;
          var u = w[state.unit];
          var li = el("li");
          li.appendChild(el("span", null, "Only " + prettyModel(g) + " (" + pct(w.share) + " of use)"));
          if (u) {
            li.appendChild(el("b", null, fmt(u.median, state.unit)));
            counted.push(prettyModel(g));
          } else {
            li.appendChild(el("b", null, "not counted"));
            if (w.share >= 0.03) uncounted.push(prettyModel(g));
          }
          ul.appendChild(li);
        });
        t.appendChild(ul);
        if (uncounted.length) {
          t.appendChild(el("div", { "class": "note" }, "In this data only " + counted.join(", ") +
            " usage moves this bar; " + uncounted.join(", ") + " usage doesn't seem to count towards it."));
        }
      }
      if (state.unit === "usd" && oh) {
        t.appendChild(el("div", { "class": "note" }, "Including Claude Code's unlogged helper calls (+" +
          Math.round((oh.lo - 1) * 100) + "–" + Math.round((oh.hi - 1) * 100) + "%): ≈ " +
          fmt(c.median * oh.lo, "usd") + "–" + fmt(c.median * oh.hi, "usd") + "."));
      }
      t.appendChild(el("div", { "class": "note" }, "From " + cur.n + " window" + (cur.n === 1 ? "" : "s") + ", " +
        dateLabel(cur.t_from) + " to " + dateLabel(cur.t_to) + "."));
      box.appendChild(t);
    });
    renderValueTile(box, oh);
  }

  // Rough monthly API value if you max the bars every week, vs the plan's price.
  var DAYS_PER_MONTH = 30.44;
  function renderValueTile(box, oh) {
    var wk = DATA.bars.seven_day && DATA.bars.seven_day.current, fh = DATA.bars.five_hour && DATA.bars.five_hour.current;
    if (!wk && !fh) return;
    var u = state.unit;
    var fromWeek = wk ? wk[kind()][u].median * DAYS_PER_MONTH / 7 : Infinity;
    var fromFive = fh ? fh[kind()][u].median * (24 / 5) * DAYS_PER_MONTH : Infinity;  // 5h bar maxed around the clock
    var month = Math.min(fromWeek, fromFive);
    var t = el("article", { "class": "tile" });
    t.appendChild(el("div", { "class": "label" }, "Rough monthly value if you max it every week"));
    var v = el("div", { "class": "value" }, fmt(month, u) + " ");
    v.appendChild(el("small", null, u === "usd" ? "API-equivalent / month" : (u === "wtok" ? "weighted tokens / month" : "raw tokens / month")));
    t.appendChild(v);
    t.appendChild(el("div", { "class": "range" }, fromWeek <= fromFive ? "Weekly 100% × " + (DAYS_PER_MONTH / 7).toFixed(2) + " weeks (the weekly bar is the real cap)"
      : "5-hour 100% × " + (24 / 5 * DAYS_PER_MONTH).toFixed(0) + " windows"));
    if (u === "usd") {
      var price = state.price || DATA.plan_price || 20;
      var row = el("div", { "class": "mult" });
      row.appendChild(el("b", null, "≈ " + (month / price).toFixed(month / price >= 10 ? 0 : 1) + "× "));
      row.appendChild(document.createTextNode("the "));
      var inp = el("input", { type: "number", min: "1", step: "1", value: String(price), "aria-label": "Plan price per month in USD" });
      inp.addEventListener("change", function () { var n = parseFloat(inp.value); if (n > 0) { state.price = n; renderTiles(); } });
      row.appendChild(document.createTextNode("$"));
      row.appendChild(inp);
      var planName = DATA.plan && price === (DATA.plan_price || 20) ? " " + DATA.plan : "";
      row.appendChild(document.createTextNode("/month" + planName + " plan"));
      t.appendChild(row);
      if (oh) t.appendChild(el("div", { "class": "note" }, "With unlogged helper calls: ≈ " + fmt(month * oh.lo, "usd") + "–" + fmt(month * oh.hi, "usd") +
        " (" + (month * oh.lo / price).toFixed(0) + "–" + (month * oh.hi / price).toFixed(0) + "×)."));
    }
    t.appendChild(el("div", { "class": "note" }, "Upper bound: assumes every weekly bar is used to 100%. Most people use far less. Priced at API list rates, not Anthropic's own cost."));
    box.appendChild(t);
  }

  // ------------------------------------------------------------ charts
  function niceStep(span, target) {
    var raw = span / Math.max(1, target);
    var p = Math.pow(10, Math.floor(Math.log10(raw)));
    var f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }

  var charts = [];

  function renderCharts() {
    var box = $("#charts");
    box.textContent = "";
    charts = [];
    Object.keys(DATA.bars).forEach(function (key) {
      var bar = DATA.bars[key];
      var card = el("section", { "class": "card chart-card" });
      card.appendChild(el("h2", null, bar.label));
      card.appendChild(el("p", { "class": "sub" }, "Estimated value of 100% of the bar per window (" + UNIT_NAME[state.unit] +
        (state.adjust && bar.ref_model ? ", in " + prettyModel(bar.ref_model) + " equivalents" : ", actual model mix") +
        "). Error bars: per-window uncertainty. Dotted line: current estimate."));
      var legend = el("div", { "class": "legend" });
      card.appendChild(legend);
      var holder = el("div", { "class": "chart", tabindex: "0", role: "img",
        "aria-label": bar.label + " estimate chart; data is also in the tables below" });
      card.appendChild(holder);
      box.appendChild(card);
      var c = { bar: bar, holder: holder, legend: legend };
      charts.push(c);
      drawChart(c);
    });
  }

  function legendItem(legend, svgKey, text) {
    var s = el("span");
    var k = sv("svg", { width: 18, height: 10, viewBox: "0 0 18 10", "aria-hidden": "true" });
    k.appendChild(svgKey);
    s.appendChild(k);
    s.appendChild(document.createTextNode(text));
    legend.appendChild(s);
  }

  function drawChart(c) {
    var bar = c.bar, holder = c.holder;
    holder.textContent = "";
    c.legend.textContent = "";
    var all = bar.windows.filter(function (w) { return pointVals(w); });
    var lastT = all.length ? Math.max.apply(null, all.map(windowX)) : DATA.generatedT;
    var pts = all.filter(function (w) { return inRange(w, lastT); });
    if (!pts.length) {
      holder.appendChild(el("div", { "class": "empty" }, "No windows with enough utilization change yet."));
      return;
    }
    var W = Math.max(300, holder.clientWidth || 600);
    var narrow = W < 520;
    var H = narrow ? 240 : 300;
    var m = { l: narrow ? 44 : 54, r: 12, t: 22, b: 26 };
    var pw = W - m.l - m.r, ph = H - m.t - m.b;

    var cur = bar.current ? bar.current[kind()][state.unit] : null;
    var xs = pts.map(windowX);
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    var pad = Math.max((x1 - x0) * 0.03, bar.window_hours * 3600 * 0.6);
    x0 -= pad; x1 += pad;
    var vals = pts.map(function (w) { return pointVals(w)[0]; });
    var his = pts.map(function (w) { return pointVals(w)[2]; }).sort(function (a, b) { return a - b; });
    var top = Math.max(Math.max.apply(null, vals) * 1.12, his[Math.floor(his.length * 0.9)] || 0, cur ? cur.hi * 1.1 : 0);
    var ystep = niceStep(top, narrow ? 4 : 5);
    var ymax = Math.ceil(top / ystep) * ystep;
    function X(t) { return m.l + (t - x0) / (x1 - x0) * pw; }
    function Y(v) { return m.t + ph - Math.min(v, ymax) / ymax * ph; }

    var svg = sv("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, "aria-hidden": "true" });
    // grid + y ticks
    for (var v = 0; v <= ymax + 1e-9; v += ystep) {
      svg.appendChild(sv("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), "class": v === 0 ? "axis" : "grid" }));
      var tl = sv("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" });
      tl.textContent = state.unit === "usd" && ystep >= 1 ? "$" + Math.round(v).toLocaleString("en-US") : fmt(v, state.unit, true);
      svg.appendChild(tl);
    }
    // x ticks (UTC days)
    var spanDays = (x1 - x0) / DAY;
    var maxTicks = Math.max(2, Math.floor(pw / (narrow ? 64 : 80)));
    var steps = [1, 2, 3, 7, 14, 28, 56];
    var st = steps[steps.length - 1];
    for (var i = 0; i < steps.length; i++) if (spanDays / steps[i] <= maxTicks) { st = steps[i]; break; }
    var t0 = Math.ceil(x0 / DAY) * DAY;
    for (var t = t0; t <= x1; t += st * DAY) {
      if (X(t) > W - 22) break;  // no half-clipped label at the right edge
      svg.appendChild(sv("line", { x1: X(t), x2: X(t), y1: m.t + ph, y2: m.t + ph + 4, "class": "axis" }));
      var xl = sv("text", { x: X(t), y: H - 8, "text-anchor": "middle" });
      xl.textContent = dateLabel(t);
      svg.appendChild(xl);
    }
    // early resets + possible changes
    // A reset (e.g. a free limit reset being used) applies to the whole account, so mark it on every chart.
    var resets = [];
    (DATA.events || []).forEach(function (e) {
      if (e.t1 < x0 || e.t1 > x1) return;
      if (!resets.some(function (r) { return Math.abs(r.t1 - e.t1) < 600; })) resets.push(e);
    });
    resets.forEach(function (e) {
      svg.appendChild(sv("line", { x1: X(e.t1), x2: X(e.t1), y1: m.t, y2: m.t + ph, "class": "rst" }));
      var lb = sv("text", { x: X(e.t1) + 3, y: m.t - 8, "class": "lbl" });
      lb.textContent = "reset used";
      svg.appendChild(lb);
    });
    (bar.changes || []).forEach(function (ch) {
      if (ch.t < x0 || ch.t > x1) return;
      svg.appendChild(sv("line", { x1: X(ch.t), x2: X(ch.t), y1: m.t, y2: m.t + ph, "class": "chg" }));
      var lb = sv("text", { x: X(ch.t) + 3, y: m.t + 12, "class": "lbl" });
      lb.textContent = "possible change " + (ch.rel > 0 ? "+" : "") + Math.round(ch.rel * 100) + "%";
      svg.appendChild(lb);
    });
    // current estimate: CI band over the period it is based on + dotted line across the chart
    if (cur) {
      var bx0 = Math.max(X(bar.current.t_from), m.l), bx1 = Math.min(X(bar.current.t_to), W - m.r);
      if (bx1 > bx0) svg.appendChild(sv("rect", { x: bx0, y: Y(cur.hi), width: bx1 - bx0, height: Math.max(1, Y(cur.lo) - Y(cur.hi)), "class": "band" }));
      svg.appendChild(sv("line", { x1: m.l, x2: W - m.r, y1: Y(cur.median), y2: Y(cur.median), "class": "now" }));
      var nl = sv("text", { x: W - m.r - 2, y: Y(cur.median) - 7, "text-anchor": "end", "class": "now-lbl" });
      nl.textContent = "now " + fmt(cur.median, state.unit, true);
      svg.appendChild(nl);
    }
    // trend line: rolling median of the window estimates (raw points when there are only a few)
    if (pts.length > 1) {
      var half = pts.length >= 15 ? 3 : pts.length >= 7 ? 1 : 0;
      var vals = pts.map(function (w) { return pointVals(w)[0]; });
      var dpath = "";
      pts.forEach(function (w, i) {
        var nb = vals.slice(Math.max(0, i - half), i + half + 1).sort(function (a, b) { return a - b; });
        var med = nb.length % 2 ? nb[(nb.length - 1) / 2] : (nb[nb.length / 2 - 1] + nb[nb.length / 2]) / 2;
        dpath += (i ? "L" : "M") + X(windowX(w)).toFixed(1) + " " + Y(med).toFixed(1);
      });
      svg.appendChild(sv("path", { d: dpath, "class": "trend" }));
      var trendNote = half ? "trend (rolling median of " + (2 * half + 1) + ")" : "trend";
    }
    // error bars then points
    var placed = [];
    var present = {};
    pts.forEach(function (w) {
      var pv = pointVals(w), x = X(windowX(w));
      var slot = modelSlot(dominant(w));
      present[slot] = present[slot] || dominant(w);
      var col = seriesColor(slot);
      svg.appendChild(sv("line", { x1: x, x2: x, y1: Y(pv[1]), y2: Y(pv[2]), stroke: col, "class": "err" }));
      placed.push({ w: w, x: x, y: Y(pv[0]), col: col, slot: slot });
    });
    placed.forEach(function (p) {
      var mark;
      if (p.slot === 1) mark = sv("circle", { cx: p.x, cy: p.y, r: 4.5 });
      else if (p.slot === 2) mark = sv("rect", { x: p.x - 4.2, y: p.y - 4.2, width: 8.4, height: 8.4, transform: "rotate(45 " + p.x + " " + p.y + ")" });
      else mark = sv("path", { d: "M" + p.x + " " + (p.y - 5.5) + "L" + (p.x + 5) + " " + (p.y + 3.5) + "L" + (p.x - 5) + " " + (p.y + 3.5) + "Z" });
      mark.setAttribute("fill", p.col);
      mark.setAttribute("class", "dot");
      svg.appendChild(mark);
    });
    var nowLbl = svg.querySelector(".now-lbl");
    if (nowLbl) svg.appendChild(nowLbl);  // keep the label above the marks
    var hl = sv("circle", { r: 8, "class": "hl", visibility: "hidden" });
    svg.appendChild(hl);
    holder.appendChild(svg);

    // legend: dominant-model keys present + current line + band
    Object.keys(present).sort().forEach(function (slot) {
      var col = seriesColor(slot), key;
      if (slot === "1") key = sv("circle", { cx: 9, cy: 5, r: 4, fill: col });
      else if (slot === "2") key = sv("rect", { x: 5.5, y: 1.5, width: 7, height: 7, fill: col, transform: "rotate(45 9 5)" });
      else key = sv("path", { d: "M9 0L14 9L4 9Z", fill: col });
      var name = slot === "3" ? "Mostly other models" : "Mostly " + prettyModel(present[slot]);
      legendItem(c.legend, key, name);
    });
    if (trendNote) legendItem(c.legend, sv("line", { x1: 0, x2: 18, y1: 5, y2: 5, stroke: cssVar("--text-primary"), "stroke-width": 2, "stroke-opacity": 0.6 }), trendNote);
    if (resets.length) legendItem(c.legend, sv("line", { x1: 9, x2: 9, y1: 0, y2: 10, stroke: cssVar("--muted"), "stroke-width": 1.5 }), "limit reset used");
    if (cur) {
      legendItem(c.legend, sv("line", { x1: 0, x2: 18, y1: 5, y2: 5, stroke: cssVar("--text-secondary"), "stroke-width": 2, "stroke-dasharray": "2 4", "stroke-linecap": "round" }), "current estimate");
      legendItem(c.legend, sv("rect", { x: 0, y: 1, width: 18, height: 8, fill: cssVar("--accent-wash") }), "95% CI of current");
    }

    // hover / keyboard: nearest point
    var tip = $("#tip");
    var focusIdx = -1;
    function show(p, cx, cy) {
      hl.setAttribute("cx", p.x); hl.setAttribute("cy", p.y); hl.setAttribute("visibility", "visible");
      tip.textContent = "";
      var pv = pointVals(p.w);
      tip.appendChild(el("div", { "class": "v" }, "100% ≈ " + fmt(pv[0], state.unit)));
      tip.appendChild(el("div", { "class": "k" }, "range " + fmt(pv[1], state.unit) + "–" + fmt(pv[2], state.unit)));
      tip.appendChild(el("div", null, dateTime(p.w.anchored ? p.w.start : p.w.t_first) + " → " + dateTime(Math.min(p.w.reset, p.w.t_last))));
      tip.appendChild(el("div", { "class": "k" }, "utilization +" + Math.round(p.w.du * 100) + " pts, " + p.w.n + " samples, " +
        fmt(p.w.used[state.unit], state.unit) + " used"));
      Object.keys(p.w.mix).forEach(function (mo) {
        var r = el("div", { "class": "row k" });
        var k = sv("svg", { width: 12, height: 4 });
        k.appendChild(sv("line", { x1: 0, x2: 12, y1: 2, y2: 2, stroke: seriesColor(modelSlot(mo)), "stroke-width": 2 }));
        r.appendChild(k);
        r.appendChild(document.createTextNode(prettyModel(mo) + " " + pct(p.w.mix[mo])));
        tip.appendChild(r);
      });
      tip.hidden = false;
      var tw = tip.offsetWidth, th = tip.offsetHeight;
      var left = cx + 14, topp = cy - th - 10;
      if (left + tw > window.innerWidth - 8) left = cx - tw - 14;
      if (topp < 8) topp = cy + 14;
      tip.style.left = Math.max(8, left) + "px";
      tip.style.top = topp + "px";
    }
    function hide() { tip.hidden = true; hl.setAttribute("visibility", "hidden"); }
    function nearest(mx, my) {
      var best = null, bd = Infinity;
      placed.forEach(function (p) {
        var d = Math.pow(p.x - mx, 2) + Math.pow((p.y - my) * 0.5, 2);
        if (d < bd) { bd = d; best = p; }
      });
      return bd <= 40 * 40 ? best : null;
    }
    svg.addEventListener("pointermove", function (ev) {
      var r = svg.getBoundingClientRect();
      var sx = (ev.clientX - r.left) * W / r.width, sy = (ev.clientY - r.top) * H / r.height;
      var p = nearest(sx, sy);
      if (p) show(p, ev.clientX, ev.clientY); else hide();
    });
    svg.addEventListener("pointerleave", hide);
    holder.onkeydown = function (ev) {
      if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft") return;
      ev.preventDefault();
      focusIdx = Math.max(0, Math.min(placed.length - 1, focusIdx + (ev.key === "ArrowRight" ? 1 : -1)));
      var p = placed[focusIdx], r = svg.getBoundingClientRect();
      show(p, r.left + p.x * r.width / W, r.top + p.y * r.height / H);
    };
    holder.onblur = hide;
  }

  // ------------------------------------------------------------ changes, events, tables
  function renderChanges() {
    var ul = $("#changes");
    ul.textContent = "";
    var items = [];
    Object.keys(DATA.bars).forEach(function (key) {
      (DATA.bars[key].changes || []).forEach(function (ch) {
        items.push({ t: ch.t, text: "Possible limit change around " + dateTime(ch.t) + ", " + DATA.bars[key].label + ": " +
          fmt(ch.before_usd, "usd") + " → " + fmt(ch.after_usd, "usd") + " (" + (ch.rel > 0 ? "+" : "") + Math.round(ch.rel * 100) + "%, mix-adjusted)." });
      });
    });
    var groups = [];
    (DATA.events || []).forEach(function (e) {
      var g = groups.filter(function (x) { return Math.abs(x.t1 - e.t1) < 1800; })[0];
      if (!g) { g = { t0: e.t0, t1: e.t1, parts: [] }; groups.push(g); }
      g.t0 = Math.min(g.t0, e.t0);
      var label = DATA.bars[e.bar] ? DATA.bars[e.bar].label : e.bar;
      g.parts.push(label + " " + pct(e.from) + " → " + pct(e.to));
    });
    groups.forEach(function (g) {
      items.push({ t: g.t1, text: "Limit reset used between " + dateTime(g.t0) + " and " + dateTime(g.t1) + ": " + g.parts.join("; ") + "." });
    });
    items.sort(function (a, b) { return b.t - a.t; });
    if (!items.length) items.push({ text: "No step changes or limit resets detected yet." });
    var anyChange = Object.keys(DATA.bars).some(function (k) { return (DATA.bars[k].changes || []).length; });
    if (!anyChange) items.unshift({ text: "No lasting step change in any bar's mix-adjusted estimate so far." });
    items.forEach(function (it) { ul.appendChild(el("li", null, it.text)); });
  }

  function renderTables() {
    var wrap = $("#table-wrap");
    wrap.textContent = "";
    Object.keys(DATA.bars).forEach(function (key) {
      var bar = DATA.bars[key];
      var d = el("details");
      d.appendChild(el("summary", null, bar.label + " (" + bar.windows.length + " windows)"));
      var sc = el("div", { "class": "tablescroll" });
      var tb = el("table");
      var hr = el("tr");
      ["Window start (UTC)", "Resets", "Samples", "Util. rise", "Used ($)", "100% as used ($)", "Range ($)", "Mix-adjusted ($)", "100% (weighted tok)", "Main model"]
        .forEach(function (h) { hr.appendChild(el("th", { scope: "col" }, h)); });
      tb.appendChild(hr);
      bar.windows.slice().reverse().forEach(function (w) {
        var tr = el("tr");
        var dm = dominant(w);
        [dateTime(w.anchored ? w.start : w.t_first) + (w.seg ? " (after reset)" : ""), dateTime(w.reset), w.n,
          Math.round(w.du * 100) + " pts", fmt(w.used.usd, "usd"),
          w.est ? fmt(w.est.usd[0], "usd") : "–", w.est ? fmt(w.est.usd[1], "usd") + "–" + fmt(w.est.usd[2], "usd") : "–",
          w.adj ? fmt(w.adj.usd[0], "usd") : "–", w.est ? fmt(w.est.wtok[0], "wtok") : "–",
          dm ? prettyModel(dm) + " " + pct(w.mix[dm]) : "–"]
          .forEach(function (v, i) { tr.appendChild(el(i === 0 ? "th" : "td", i === 0 ? { scope: "row" } : null, String(v))); });
        tb.appendChild(tr);
      });
      sc.appendChild(tb);
      d.appendChild(sc);
      wrap.appendChild(d);
    });
  }

  function renderMeta() {
    var c = DATA.coverage || {};
    var nb = Object.keys(DATA.bars).map(function (k) { return DATA.bars[k].n_windows + " " + DATA.bars[k].label.split(" (")[0].toLowerCase() + " windows"; });
    var parts = ["Last updated " + dateTime(DATA.generatedT),
      "utilization samples " + isoDate(c.first_sample) + " to " + isoDate(c.last_sample),
      (c.calls || 0).toLocaleString("en-US") + " API calls",
      nb.join(", "),
      "prices checked " + ((DATA.prices || {}).checked || "–")];
    if (DATA.plan) parts.splice(1, 0, "plan: " + DATA.plan);
    $("#meta").textContent = parts.join(" · ");
    $("#foot-updated").textContent = "Data generated " + dateTime(DATA.generatedT) + ".";
    var oh = overhead();
    if (oh) {
      $("#overhead-caveat").lastChild.textContent = " Comparing Claude Code's own cost totals with the logged calls suggests they add about " +
        Math.round((oh.lo - 1) * 100) + "–" + Math.round((oh.hi - 1) * 100) +
        "% on this account; the tiles show a corrected range, the charts don't include it.";
    }
  }

  function repoLink() {
    var a = $("#repo-link");
    var h = location.hostname;
    if (/\.github\.io$/.test(h)) {
      var user = h.split(".")[0];
      var seg = location.pathname.split("/").filter(Boolean)[0];
      var repo = seg && seg.indexOf(".") < 0 ? seg : user + ".github.io";
      a.href = "https://github.com/" + user + "/" + repo + "#readme";
    } else {
      a.href = "../README.md";
    }
  }

  // ------------------------------------------------------------ wiring
  function renderAll() {
    renderTiles();
    renderCharts();
  }

  function bindSeg(attr, key) {
    document.querySelectorAll("[data-" + attr + "]").forEach(function (b) {
      b.addEventListener("click", function () {
        state[key] = b.getAttribute("data-" + attr);
        document.querySelectorAll("[data-" + attr + "]").forEach(function (o) { o.setAttribute("aria-checked", o === b ? "true" : "false"); });
        renderAll();
      });
    });
  }

  function init(data) {
    DATA = data;
    DATA.generatedT = Date.parse(data.generated_at) / 1000;
    var order = Object.keys((data.coverage || {}).model_mix || {});
    if (order[0]) colorSlots[order[0]] = 1;
    if (order[1]) colorSlots[order[1]] = 2;
    renderMeta();
    renderChanges();
    renderTables();
    renderAll();
  }

  bindSeg("unit", "unit");
  bindSeg("range", "range");
  $("#adjust").addEventListener("change", function (e) { state.adjust = e.target.checked; renderAll(); });
  $("#theme").addEventListener("click", function () {
    var root = document.documentElement;
    var dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    localStorage.setItem("lt-theme", root.dataset.theme);
    if (DATA) renderAll();
  });
  var rt;
  window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(function () { if (DATA) renderCharts(); }, 150); });
  repoLink();

  fetch("data/estimates.json", { cache: "no-cache" })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(init)
    .catch(function (e) {
      $("#meta").textContent = "Could not load data/estimates.json (" + e.message + "). If you opened this file directly, serve the docs/ folder over HTTP, e.g. python3 -m http.server -d docs.";
    });
})();
