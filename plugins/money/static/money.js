/* Money plugin — UI runtime: forms, filters, SVG charts (donut, trend, heatmap). */
(function () {
  "use strict";

  var esc = function (s) { return Tomo.escapeHtml(s == null ? "" : String(s)); };

  function fmtRp(minor, opts) {
    if (minor == null) return "—";
    minor = Number(minor) || 0;
    var sign = minor < 0 ? "-" : "";
    var whole = Math.abs(minor) / 100;
    var compact = opts && opts.compact;
    var text;
    if (compact && Math.abs(whole) >= 1e6) {
      text = (whole / 1e6).toFixed(2).replace(/\.?0+$/, "") + "m";
    } else if (compact && Math.abs(whole) >= 1e3) {
      text = (whole / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    } else {
      text = whole % 1 === 0
        ? whole.toLocaleString("en-US", { maximumFractionDigits: 0 })
        : whole.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return sign + "Rp" + text;
  }

  // digits only, for hero numbers that render the "Rp" prefix separately
  function num(minor, opts) {
    return fmtRp(minor, opts).replace(/^-?Rp/, "");
  }

  function baseUrl() {
    var el = document.querySelector("[data-base]");
    return el ? el.getAttribute("data-base") : "/plugins/money";
  }

  function post(url, body, method) {
    return Tomo.api(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  // ── Ask Money → deep link into Tomo chat ─────────────────────────
  document.querySelectorAll(".m-ask").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var input = form.querySelector("input");
      var q = (input.value || "").trim();
      if (!q) return;
      var params = new URLSearchParams({ q: q });
      window.location.href = "/sessions?" + params.toString();
    });
  });

  // ── Add / edit transaction modal ─────────────────────────────────
  var modal = document.getElementById("txnModal");
  var txnForm = document.getElementById("txnForm");
  var editingId = null;

  function setKind(kind) {
    txnForm.querySelectorAll(".m-kind-toggle button").forEach(function (b) {
      b.classList.toggle("sel-expense", kind === "expense" && b.dataset.kind === "expense");
      b.classList.toggle("sel-income", kind === "income" && b.dataset.kind === "income");
    });
    txnForm.elements.kind.value = kind;
  }

  function openTxnModal(txn) {
    editingId = txn && txn.id ? txn.id : null;
    document.getElementById("txnModalTitle").textContent = editingId
      ? "Edit transaction"
      : "Add transaction";
    txnForm.reset();
    txnForm.elements.day.value =
      (txn && txn.day) || new Date().toISOString().slice(0, 10);
    if (txn) {
      txnForm.elements.amount.value = (Math.abs(txn.amount_minor) / 100).toString();
      txnForm.elements.category.value = txn.category || "";
      txnForm.elements.note.value = txn.note || "";
      txnForm.elements.method.value = txn.method || "";
      txnForm.elements.source_id.value = txn.source_id || "";
      setKind(txn.kind || "expense");
    } else {
      txnForm.elements.source_id.value = "";
      txnForm.elements.method.value = "";
      setKind("expense");
    }
    modal.classList.add("open");
    setTimeout(function () { txnForm.elements.amount.focus(); }, 60);
  }

  function closeTxnModal() { modal.classList.remove("open"); editingId = null; }

  if (modal && txnForm) {
    modal.addEventListener("click", function (ev) {
      if (ev.target === modal) closeTxnModal();
    });
    txnForm.querySelectorAll(".m-kind-toggle button").forEach(function (b) {
      b.addEventListener("click", function (ev) {
        ev.preventDefault();
        setKind(b.dataset.kind);
      });
    });
    document.getElementById("txnModalCancel").addEventListener("click", closeTxnModal);
    txnForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var f = txnForm.elements;
      var payload = {
        kind: f.kind.value,
        amount: f.amount.value,
        category: f.category.value,
        note: f.note.value,
        day: f.day.value,
        source_id: f.source_id.value || null,
        method: f.method.value || "",
      };
      var save = editingId
        ? post(baseUrl() + "/api/transactions/" + editingId, payload, "PATCH")
        : post(baseUrl() + "/api/transactions", payload);
      save
        .then(function () {
          closeTxnModal();
          Tomo.toast(editingId ? "Transaction updated" : "Transaction saved", "ok");
          refreshTxnList();
          refreshOverview && refreshOverview();
        })
        .catch(function (e) { Tomo.toast(e.message || "Save failed", "err"); });
    });
  }

  document.querySelectorAll("[data-add-txn]").forEach(function (b) {
    b.addEventListener("click", function (ev) {
      ev.preventDefault();
      openTxnModal(null);
    });
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "n" && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      var tag = (ev.target.tagName || "").toLowerCase();
      if (tag !== "input" && tag !== "textarea" && tag !== "select" && modal) {
        ev.preventDefault();
        openTxnModal(null);
      }
    }
    if (ev.key === "Escape" && modal && modal.classList.contains("open")) {
      closeTxnModal();
    }
  });

  // ── Transactions list (filters + render) ─────────────────────────
  var txnList = document.getElementById("txnList");
  var txnData = window.__TXNS__ || [];
  var refreshTxnList = function () {};

  function groupByDay(rows) {
    var groups = [];
    var map = {};
    rows.forEach(function (r) {
      if (!map[r.day]) {
        map[r.day] = { day: r.day, rows: [], expense: 0, income: 0 };
        groups.push(map[r.day]);
      }
      map[r.day].rows.push(r);
      map[r.day][r.kind] += r.amount_minor;
    });
    return groups;
  }

  function txnRow(r) {
    var inClass = r.kind === "income" ? " in" : "";
    var sign = r.kind === "income" ? "+" : "−";
    var parts = [esc(r.day)];
    if (r.method) parts.push('<span class="mtd">' + esc(r.method.toUpperCase()) + "</span>");
    if (r.source_name) parts.push(esc(r.source_name));
    if (r.note) parts.push(esc(r.note));
    var meta = parts.join(" · ");
    return (
      '<div class="m-txn-row" data-id="' + r.id + '">' +
        '<div class="m-txn-row who">' +
          '<span class="cat">' + esc(r.category) + '</span>' +
          '<span class="meta">' + meta + '</span>' +
        '</div>' +
        '<div class="m-txn-actions">' +
          '<button type="button" class="edit" title="Edit" aria-label="Edit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></button>' +
          '<button type="button" class="del" title="Delete" aria-label="Delete"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6h18M8 6V4h8v2m1 0-1 14H8L7 6"/></svg></button>' +
        '</div>' +
        '<span class="amt' + inClass + '">' + sign + fmtRp(r.amount_minor) + "</span>" +
      "</div>"
    );
  }

  function renderTxns(rows) {
    if (!txnList) return;
    if (!rows.length) {
      txnList.innerHTML =
        '<div class="m-empty"><span class="glyph">🧾</span>No transactions yet. ' +
        "Press <b>N</b> or ask your agent to record one.</div>";
      return;
    }
    txnList.innerHTML = groupByDay(rows)
      .map(function (g) {
        var net = g.income - g.expense;
        var netLabel = net >= 0 ? "+" + fmtRp(net) : "−" + fmtRp(-net);
        return (
          '<div class="m-day-group">' +
            '<div class="m-day-label"><span>' + esc(g.day) + "</span>" +
            '<span class="day-total">' + netLabel + "</span></div>" +
            g.rows.map(txnRow).join("") +
          "</div>"
        );
      })
      .join("");
  }

  if (txnList) {
    // Seed filters from the URL (e.g. ?category=Food&month=2026-01 from reports,
    // ?start=..&end=.. from the heatmap day cells).
    var urlParams = new URLSearchParams(location.search);
    var filters = document.getElementById("txnFilters");
    var seeded = false;
    if (filters) {
      ["q", "kind", "category", "source_id", "method", "month"].forEach(function (name) {
        var value = urlParams.get(name);
        if (value && filters.elements[name]) {
          filters.elements[name].value = value;
          seeded = true;
        }
      });
    }
    var rangeParams = ["start", "end"].filter(function (k) { return urlParams.get(k); });

    refreshTxnList = function () {
      var params = new URLSearchParams();
      if (filters) {
        if (filters.elements.q.value) params.set("q", filters.elements.q.value);
        if (filters.elements.kind.value) params.set("kind", filters.elements.kind.value);
        if (filters.elements.category.value) params.set("category", filters.elements.category.value);
        if (filters.elements.source_id.value) params.set("source_id", filters.elements.source_id.value);
        if (filters.elements.method && filters.elements.method.value) params.set("method", filters.elements.method.value);
        if (filters.elements.month.value) params.set("month", filters.elements.month.value);
      }
      rangeParams.forEach(function (k) { params.set(k, urlParams.get(k)); });
      Tomo.api(baseUrl() + "/api/transactions?" + params.toString())
        .then(function (d) {
          txnData = d.transactions || [];
          renderTxns(txnData);
        })
        .catch(function (e) { Tomo.toast(e.message || "Load failed", "err"); });
    };

    // Server already rendered matching markup; regroup only when filters apply.
    if (seeded) refreshTxnList();
    else renderTxns(txnData);

    if (filters) {
      filters.addEventListener("input", debounce(refreshTxnList, 220));
      filters.addEventListener("change", function () {
        rangeParams = [];
        refreshTxnList();
      });
      filters.addEventListener("submit", function (ev) { ev.preventDefault(); });
    }

    txnList.addEventListener("click", function (ev) {
      var row = ev.target.closest(".m-txn-row");
      if (!row) return;
      var id = Number(row.dataset.id);
      if (ev.target.closest(".del")) {
        if (!confirm("Delete this transaction?")) return;
        Tomo.api(baseUrl() + "/api/transactions/" + id, { method: "DELETE" })
          .then(function () {
            row.remove();
            Tomo.toast("Deleted", "ok");
          })
          .catch(function (e) { Tomo.toast(e.message || "Delete failed", "err"); });
      } else if (ev.target.closest(".edit")) {
        var txn = txnData.find(function (t) { return t.id === id; });
        openTxnModal(txn);
      }
    });
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      clearTimeout(t);
      var args = arguments, self = this;
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  // ── SVG chart helpers ────────────────────────────────────────────
  var SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    var el = document.createElementNS(SVGNS, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  // Donut: segments as circle strokes with dash arrays.
  function renderDonut(host, categories, totalMinor, days, onSelect) {
    host.innerHTML = "";
    var R = 70, C = 2 * Math.PI * R, W = 200;
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + W });
    var track = svgEl("circle", {
      cx: W / 2, cy: W / 2, r: R, fill: "none",
      "stroke-width": 20, class: "seg",
      stroke: "var(--surface-3)",
    });
    svg.appendChild(track);
    var offset = 0;
    categories.forEach(function (c, i) {
      var frac = c.total / Math.max(totalMinor, 1);
      var len = Math.max(frac * C - 3, 0.5);
      var seg = svgEl("circle", {
        cx: W / 2, cy: W / 2, r: R, fill: "none",
        "stroke-width": 20, class: "seg", "stroke-linecap": "butt",
        stroke: c.color || "var(--m-accent)",
        "stroke-dasharray": len + " " + (C - len),
        "stroke-dashoffset": -offset,
        "data-i": i,
      });
      offset += frac * C;
      seg.addEventListener("mouseenter", function () { hot(i); });
      seg.addEventListener("mouseleave", function () { hot(-1); });
      seg.addEventListener("click", function () { onSelect && onSelect(c); });
      svg.appendChild(seg);
    });
    var wrap = document.createElement("div");
    wrap.className = "m-donut";
    wrap.appendChild(svg);
    var center = document.createElement("div");
    center.className = "m-donut-center";
    center.innerHTML =
      '<span class="k">Money out</span><span class="v">' +
      fmtRp(totalMinor, { compact: true }) + '</span><span class="n">' +
      categories.length + " categor" + (categories.length === 1 ? "y" : "ies") + "</span>";
    wrap.appendChild(center);
    host.appendChild(wrap);

    function hot(i) {
      wrap.classList.toggle("dim", i >= 0);
      svg.querySelectorAll("circle.seg[data-i]").forEach(function (s) {
        s.classList.toggle("hot", Number(s.getAttribute("data-i")) === i);
      });
      document.querySelectorAll(".m-cat-row[data-i]").forEach(function (r) {
        r.style.background = Number(r.getAttribute("data-i")) === i
          ? "var(--surface-2)" : "";
      });
      if (i >= 0 && categories[i]) {
        center.innerHTML =
          '<span class="k">' + esc(categories[i].category) + '</span><span class="v">' +
          fmtRp(categories[i].total, { compact: true }) + '</span><span class="n">' +
          categories[i].share + "% · " + fmtRp(Math.round(categories[i].total / Math.max(days, 1)), { compact: true }) + "/day</span>";
      } else {
        center.innerHTML =
          '<span class="k">Money out</span><span class="v">' +
          fmtRp(totalMinor, { compact: true }) + '</span><span class="n">' +
          categories.length + " categories</span>";
      }
    }
  }

  // Cumulative spending trend: solid current vs dashed previous period.
  function renderTrend(host, current, previous, opts) {
    host.innerHTML = "";
    var W = 640, H = 150, padL = 6, padR = 6, padT = 12, padB = 20;
    var iw = W - padL - padR, ih = H - padT - padB;
    var maxV = 1;
    current.forEach(function (p) { maxV = Math.max(maxV, p.expense); });
    previous.forEach(function (p) { maxV = Math.max(maxV, p.expense); });
    var n = Math.max(current.length, previous.length, 2);
    function x(i) { return padL + (i / (n - 1)) * iw; }
    function y(v) { return padT + ih - (v / maxV) * ih; }
    function path(series) {
      return series
        .map(function (p, i) { return (i ? "L" : "M") + x(i).toFixed(1) + "," + y(p.expense).toFixed(1); })
        .join(" ");
    }
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none" });
    var defs = svgEl("defs", {});
    var grad = svgEl("linearGradient", { id: "mTrendGrad", x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(svgEl("stop", { offset: "0%", "stop-color": "var(--m-accent)", "stop-opacity": 0.28 }));
    grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": "var(--m-accent)", "stop-opacity": 0 }));
    defs.appendChild(grad);
    svg.appendChild(defs);
    // grid
    [0.25, 0.5, 0.75, 1].forEach(function (f) {
      svg.appendChild(svgEl("line", {
        x1: padL, x2: W - padR, y1: y(maxV * f), y2: y(maxV * f),
        stroke: "var(--border)", "stroke-dasharray": "2 5", "stroke-width": 1,
      }));
    });
    // previous (dashed)
    if (previous.length) {
      svg.appendChild(svgEl("path", {
        d: path(previous), fill: "none",
        stroke: "var(--text-faint)", "stroke-width": 1.6,
        "stroke-dasharray": "4 4", "stroke-linecap": "round", opacity: 0.8,
      }));
    }
    // current area + line
    if (current.length) {
      var area = path(current) +
        " L" + x(current.length - 1).toFixed(1) + "," + y(0).toFixed(1) +
        " L" + x(0).toFixed(1) + "," + y(0).toFixed(1) + " Z";
      svg.appendChild(svgEl("path", { d: area, fill: "url(#mTrendGrad)", stroke: "none" }));
      svg.appendChild(svgEl("path", {
        d: path(current), fill: "none",
        stroke: "var(--m-accent)", "stroke-width": 2.2, "stroke-linecap": "round",
      }));
    }
    // today marker
    var todayIdx = Math.min((opts && opts.daysElapsed) || current.length, n) - 1;
    if (todayIdx >= 0 && current[todayIdx]) {
      svg.appendChild(svgEl("circle", {
        cx: x(todayIdx), cy: y(current[todayIdx].expense), r: 3.6,
        fill: "var(--m-accent)", stroke: "var(--surface)", "stroke-width": 1.6,
      }));
      svg.appendChild(svgEl("line", {
        x1: x(todayIdx), x2: x(todayIdx), y1: padT, y2: y(0),
        stroke: "var(--m-accent)", "stroke-width": 1, "stroke-dasharray": "2 4", opacity: 0.5,
      }));
    }
    // x labels
    var first = current[0] || previous[0] || { day: "" };
    var last = current[current.length - 1] || previous[previous.length - 1] || { day: "" };
    [first.day, last.day].forEach(function (d, i) {
      if (!d) return;
      var t = svgEl("text", {
        x: i ? W - padR : padL, y: H - 5,
        "text-anchor": i ? "end" : "start",
        fill: "var(--text-faint)", "font-size": 9.5, "font-family": "var(--font-mono)",
      });
      t.textContent = d.slice(5).replace("-", " ") ;
      svg.appendChild(t);
    });
    // hover crosshair
    var tip = host.parentElement.querySelector(".m-trend-tip");
    var focus = svgEl("circle", { r: 4, fill: "var(--m-accent)", opacity: 0, stroke: "var(--surface)", "stroke-width": 1.5 });
    var focusP = svgEl("circle", { r: 3.2, fill: "var(--text-faint)", opacity: 0, stroke: "var(--surface)", "stroke-width": 1.5 });
    svg.appendChild(focusP);
    svg.appendChild(focus);
    svg.addEventListener("mousemove", function (ev) {
      var rect = svg.getBoundingClientRect();
      var fx = ((ev.clientX - rect.left) / rect.width) * W;
      var i = Math.round(((fx - padL) / iw) * (n - 1));
      i = Math.max(0, Math.min(n - 1, i));
      var cur = current[i], prev = previous[i];
      focus.setAttribute("opacity", cur ? 1 : 0);
      focusP.setAttribute("opacity", prev ? 1 : 0);
      if (cur) { focus.setAttribute("cx", x(i)); focus.setAttribute("cy", y(cur.expense)); }
      if (prev) { focusP.setAttribute("cx", x(i)); focusP.setAttribute("cy", y(prev.expense)); }
      if (tip) {
        var d = (cur && cur.day) || (prev && prev.day) || "";
        tip.innerHTML =
          "<div><b>" + esc(d) + "</b></div>" +
          '<div class="cur-row"><span class="dot" style="background:var(--m-accent)"></span>' +
          esc(opts.curLabel || "This period") + " " + fmtRp(cur ? cur.expense : 0, { compact: true }) + "</div>" +
          '<div class="prev-row"><span class="dot" style="background:var(--text-faint)"></span>' +
          esc(opts.prevLabel || "Previous") + " " + fmtRp(prev ? prev.expense : 0, { compact: true }) + "</div>";
        tip.classList.add("on");
        tip.style.left = ((x(i) / W) * 100) + "%";
        tip.style.top = "0px";
      }
    });
    svg.addEventListener("mouseleave", function () {
      focus.setAttribute("opacity", 0);
      focusP.setAttribute("opacity", 0);
      tip && tip.classList.remove("on");
    });
    host.appendChild(svg);
  }

  // ── Reports page ─────────────────────────────────────────────────
  var reportsRoot = document.getElementById("reportsPage");
  if (reportsRoot) {
    var state = {
      period: "month",
      month: reportsRoot.dataset.month,
      year: reportsRoot.dataset.year,
    };

    function monthLabel(m) {
      var d = new Date(m + "-02T00:00:00");
      return d.toLocaleDateString("en-US", { month: "short" }) + " '" + m.slice(2, 4);
    }
    function shiftMonth(m, delta) {
      var y = Number(m.slice(0, 4)), mo = Number(m.slice(5, 7)) + delta;
      while (mo < 1) { mo += 12; y -= 1; }
      while (mo > 12) { mo -= 12; y += 1; }
      return y + "-" + String(mo).padStart(2, "0");
    }

    function loadReport() {
      var q = state.period === "year" ? "year=" + state.year : "month=" + state.month;
      Tomo.api(baseUrl() + "/api/reports?" + q)
        .then(renderReport)
        .catch(function (e) { Tomo.toast(e.message || "Report failed", "err"); });
    }

    function renderReport(d) {
      if (state.period === "year") state.year = String(d.label);
      else state.month = d.label;
      document.getElementById("reportPagerLabel").textContent =
        state.period === "year" ? String(d.label) : monthLabel(d.label);
      document.getElementById("moneyOut").innerHTML =
        '<span class="cur">Rp</span>' + esc(num(d.totals.expense_minor));
      document.getElementById("moneyIn").innerHTML =
        '<span class="cur">Rp</span>' + esc(num(d.totals.income_minor));
      document.getElementById("avgDay").textContent =
        "avg " + fmtRp(d.avg_daily_minor, { compact: true }) + "/day";
      var delta = document.getElementById("deltaPill");
      var up = d.delta_minor > 0;
      delta.className = "m-delta-pill " + (up ? "up" : "down");
      delta.innerHTML =
        (up ? "↑ " : "↓ ") + fmtRp(Math.abs(d.delta_minor), { compact: true }) +
        (up ? " more" : " less") + " than " + esc(d.prev_label);

      // Donut + category list
      var donutHost = document.getElementById("donutHost");
      var catHost = document.getElementById("catList");
      var cats = d.categories || [];
      var periodQS = String(d.label).length === 7
        ? "month=" + encodeURIComponent(d.label)
        : "start=" + d.start + "&end=" + encodeURIComponent(d.end_inclusive || "");
      if (cats.length) {
        renderDonut(donutHost, cats, d.totals.expense_minor, d.days_elapsed, function (c) {
          window.location.href =
            baseUrl() + "/transactions?category=" + encodeURIComponent(c.category) +
            "&" + periodQS;
        });
        catHost.innerHTML = cats
          .map(function (c, i) {
            return (
              '<a class="m-cat-row" data-i="' + i + '" href="' + baseUrl() +
              "/transactions?category=" + encodeURIComponent(c.category) +
              "&" + periodQS + '">' +
              '<span class="m-ico sm" style="background:color-mix(in srgb,' + esc(c.color) + " 18%,var(--surface-3))\">" +
              esc(c.icon || "🏷️") + "</span>" +
              '<span style="flex:1;min-width:0"><span class="nm">' + esc(c.category) + "</span>" +
              '<span class="perday" style="display:block">' + c.share + "% · " +
              fmtRp(Math.round(c.total / Math.max(d.days_elapsed, 1)), { compact: true }) + "/day</span></span>" +
              '<span class="amt">' + fmtRp(c.total) + '</span><span class="chev">›</span></a>'
            );
          })
          .join("");
      } else {
        donutHost.innerHTML = '<div class="m-empty"><span class="glyph">🍩</span>No spending this period.</div>';
        catHost.innerHTML = "";
      }

      // Payment-method breakdown
      var methodCard = document.getElementById("methodCard");
      var methodHost = document.getElementById("methodList");
      var methods = d.methods || [];
      if (methodCard && methodHost) {
        methodCard.hidden = !methods.length;
        methodHost.innerHTML = methods
          .map(function (m) {
            return (
              '<a class="m-cat-row" href="' + baseUrl() + "/transactions?method=" +
              encodeURIComponent(m.method) + "&" + periodQS + '">' +
              '<span class="m-ico sm m-method-ico">' + esc(m.method.slice(0, 1).toUpperCase()) + "</span>" +
              '<span style="flex:1;min-width:0"><span class="nm mtd">' + esc(m.method) + "</span>" +
              '<span class="perday" style="display:block">' + m.share + "% · " +
              m.txns + " txns</span></span>" +
              '<span class="amt">' + fmtRp(m.total) + '</span><span class="chev">›</span></a>'
            );
          })
          .join("");
      }

      renderCalendar(document.getElementById("calGrid"), d);
      renderTrend(
        document.getElementById("trendHost"),
        d.cumulative || [],
        d.prev_cumulative || [],
        { daysElapsed: d.days_elapsed, curLabel: d.label, prevLabel: d.prev_label }
      );
    }

    function renderCalendar(host, d) {
      var daily = d.daily || [];
      if (!daily.length) { host.innerHTML = ""; return; }
      var max = daily.reduce(function (m, p) { return Math.max(m, p.expense); }, 1);
      var first = new Date(daily[0].day + "T12:00:00Z");
      var pad = (first.getUTCDay() + 6) % 7; // Monday-first
      var today = new Date().toISOString().slice(0, 10);
      var cells = [];
      for (var i = 0; i < pad; i++) cells.push('<div class="m-cal-cell pad"></div>');
      daily.forEach(function (p) {
        var dayNum = Number(p.day.slice(8, 10));
        var level = p.expense <= 0 ? 0 : Math.max(1, Math.ceil((p.expense / max) * 4));
        var cls = "m-cal-cell" + (p.expense > 0 ? " fill has-data l" + level : "");
        if (p.day === today) cls += " today";
        var style = "";
        if (p.expense > 0) {
          var alpha = 0.22 + (p.expense / max) * 0.68;
          style = ' style="background:color-mix(in srgb,var(--m-accent) ' +
            Math.round(alpha * 100) + "%,var(--surface-3))\"" ;
        }
        cells.push(
          '<div class="' + cls + '" title="' + esc(p.day) + " · " +
          fmtRp(p.expense) + '"' + style + ' data-day="' + esc(p.day) + '">' +
          "<span>" + dayNum + "</span>" +
          (p.expense > 0 ? '<span class="amt">' + fmtRp(p.expense, { compact: true }) + "</span>" : "") +
          "</div>"
        );
      });
      host.innerHTML = cells.join("");
    }

    document.getElementById("periodSeg").addEventListener("click", function (ev) {
      var b = ev.target.closest("button[data-period]");
      if (!b) return;
      state.period = b.dataset.period;
      this.querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("active", x === b);
      });
      loadReport();
    });
    document.getElementById("pagerPrev").addEventListener("click", function () {
      if (state.period === "year") state.year = String(Number(state.year) - 1);
      else state.month = shiftMonth(state.month, -1);
      loadReport();
    });
    document.getElementById("pagerNext").addEventListener("click", function () {
      if (state.period === "year") state.year = String(Number(state.year) + 1);
      else state.month = shiftMonth(state.month, 1);
      loadReport();
    });
    document.getElementById("calGrid").addEventListener("click", function (ev) {
      var cell = ev.target.closest(".m-cal-cell.has-data");
      if (!cell) return;
      window.location.href =
        baseUrl() + "/transactions?start=" + cell.dataset.day +
        "&end=" + cell.dataset.day;
    });

    renderReport(window.__REPORT__);
  }

  // ── Inbox ────────────────────────────────────────────────────────
  var inboxList = document.getElementById("inboxList");
  if (inboxList) {
    inboxList.addEventListener("click", function (ev) {
      var item = ev.target.closest(".m-inbox-item");
      if (!item) return;
      var id = item.dataset.id;
      if (ev.target.closest(".dismiss")) {
        post(baseUrl() + "/inbox/" + id + "/dismiss", {})
          .then(function () { item.remove(); Tomo.toast("Dismissed", "ok"); })
          .catch(function (e) { Tomo.toast(e.message || "Failed", "err"); });
      } else if (ev.target.closest(".edit-toggle")) {
        item.querySelector(".m-inbox-edit").classList.toggle("open");
      } else if (ev.target.closest(".confirm")) {
        var edit = item.querySelector(".m-inbox-edit");
        var overrides = {};
        if (edit) {
          edit.querySelectorAll("[name]").forEach(function (el) {
            if (el.value) overrides[el.name] = el.value;
          });
        }
        post(baseUrl() + "/inbox/" + id + "/confirm", overrides)
          .then(function () { item.remove(); Tomo.toast("Added to ledger", "ok"); })
          .catch(function (e) { Tomo.toast(e.message || "Failed", "err"); });
      }
    });
    var confirmAll = document.getElementById("inboxConfirmAll");
    if (confirmAll) {
      confirmAll.addEventListener("click", function () {
        post(baseUrl() + "/inbox/confirm-all", {})
          .then(function (d) {
            Tomo.toast("Confirmed " + d.confirmed + " items", "ok");
            location.reload();
          })
          .catch(function (e) { Tomo.toast(e.message || "Failed", "err"); });
      });
    }
  }

  // ── Imports: drop zones ──────────────────────────────────────────
  document.querySelectorAll(".m-drop").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var url = zone.dataset.url;
    var result = document.getElementById(zone.dataset.result);
    function upload(file) {
      if (!file) return;
      var fd = new FormData();
      fd.append("file", file);
      zone.classList.add("drag");
      if (result) result.innerHTML = '<span class="m-spin"></span> Uploading…';
      fetch(url, { method: "POST", body: fd, credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          zone.classList.remove("drag");
          if (!res.ok) throw new Error(res.j.detail || "Import failed");
          var j = res.j;
          if (result) {
            if (j.imported != null) {
              result.innerHTML =
                '<div class="m-card" style="padding:14px">' +
                "<b>Import complete</b> — " + j.imported + " imported, " +
                j.duplicates + " duplicates skipped, " + j.skipped + " rows skipped." +
                (j.errors && j.errors.length
                  ? '<div class="m-dim" style="margin-top:6px">' + j.errors.map(esc).join("<br>") + "</div>"
                  : "") +
                "</div>";
            } else {
              result.innerHTML =
                '<div class="m-card" style="padding:14px">' +
                (j.parsed
                  ? "Receipt parsed — check the <a href='" + baseUrl() + "/inbox'>Inbox</a> to confirm."
                  : "Receipt saved to <a href='" + baseUrl() + "/inbox'>Inbox</a>; parsing was inconclusive, confirm it manually.") +
                "</div>";
            }
          }
          Tomo.toast("Done", "ok");
        })
        .catch(function (e) {
          zone.classList.remove("drag");
          if (result) result.innerHTML = "";
          Tomo.toast(e.message || "Upload failed", "err");
        });
    }
    zone.addEventListener("click", function () { input.click(); });
    input.addEventListener("change", function () { upload(input.files[0]); });
    zone.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      zone.classList.add("drag");
    });
    zone.addEventListener("dragleave", function () { zone.classList.remove("drag"); });
    zone.addEventListener("drop", function (ev) {
      ev.preventDefault();
      upload(ev.dataTransfer.files[0]);
    });
  });

  // Overview sparkline refresh helper (overview page re-render after edits)
  var refreshOverview = null;
})();
