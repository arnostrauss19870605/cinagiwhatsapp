/* The workflow builder.
 *
 * The server owns the workflow. This file draws it with Drawflow, sends one
 * small change at a time (connect, disconnect, move, add, remove), and
 * redraws from what the server sends back. Step settings live in the side
 * panel, which is ordinary server-rendered HTML swapped in by HTMX.
 */
(function () {
  "use strict";

  var root = document.getElementById("wf-builder");
  if (!root || typeof Drawflow === "undefined") return;

  var URLS = JSON.parse(root.dataset.urls);
  var CSRF = document.querySelector('meta[name="csrf-token"]').content;
  var START = "__start";

  var container = document.getElementById("drawflow");
  var editor = new Drawflow(container);
  editor.reroute = false;
  editor.curvature = 0.45;
  editor.zoom_min = 0.25;
  editor.zoom_max = 1.6;
  // Drawflow deletes a selected box on the Delete key. Steps are removed from
  // the side panel instead (with a confirmation); only a selected link may go.
  editor.key = function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target || {}).tagName || "");
    if (!typing && (e.key === "Delete" || e.key === "Backspace") && this.connection_selected) {
      this.removeConnection();
    }
  };
  editor.start();

  var data = null;
  var byStep = {};
  var byNode = {};
  var suppress = false;
  var selected = null;
  var simStep = null;
  var moveTimer = null;
  var moved = {};

  // -- talking to the server ---------------------------------------------

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    }).then(function (r) {
      return r.json().catch(function () { return { error: "The server did not answer properly. Reload the page." }; });
    });
  }

  function op(body, opts) {
    setSaving(true);
    return post(URLS.op, body).then(function (d) {
      setSaving(false);
      if (d.error) toast(d.error, true);
      if (d.nodes) {
        if (opts && opts.redraw) draw(d);
        else { data = d; status(); checks(); }
      }
      return d;
    });
  }

  var firstLoad = true;

  function load() {
    return fetch(URLS.data, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      draw(d);
      // A big imported workflow can sit far from the canvas origin: open on its start.
      if (firstLoad) { firstLoad = false; focusStep(START, true); }
    });
  }

  // -- drawing ------------------------------------------------------------

  function esc(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function stepById(id) {
    for (var i = 0; i < data.nodes.length; i++) if (data.nodes[i].id === id) return data.nodes[i];
    return null;
  }

  function levelFor(id) {
    var level = "";
    data.checks.forEach(function (c) {
      if (c.step === id) level = c.level === "error" ? "error" : (level || "warning");
    });
    return level;
  }

  function cardHtml(n) {
    var level = levelFor(n.id);
    var cls = "wf-card wf-t-" + n.type + (level ? " wf-" + level : "") + (n.id === simStep ? " wf-current" : "");
    var exits = n.exits.map(function (ex) {
      return '<li class="' + (n.next[ex.key] ? "" : "wf-open") + '" title="' + esc(ex.label) + '">' + esc(ex.label) + "</li>";
    }).join("");
    var jump = n.type === "jump" && n.jump_to
      ? '<div class="wf-jump">↩ ' + esc((stepById(n.jump_to) || {}).name || "?") + "</div>" : "";
    return '<div class="' + cls + '" data-step="' + esc(n.id) + '">' +
      '<div class="wf-head"><span class="wf-kind">' + esc(n.label) + "</span>" +
      '<span class="wf-counts" title="Chats in the last ' + data.count_days + ' days">' + n.reached + " reached" +
      (n.ended ? " · " + n.ended + " ended here" : "") + "</span></div>" +
      '<div class="wf-name">' + esc(n.name) + "</div>" +
      (n.summary ? '<div class="wf-summary">' + esc(n.summary) + "</div>" : "") + jump +
      (exits ? '<ul class="wf-exits">' + exits + "</ul>" : "") + "</div>";
  }

  function startHtml() {
    return '<div class="wf-card wf-t-start"><div class="wf-head"><span class="wf-kind">Start</span>' +
      '<span class="wf-counts">' + data.started + " chats</span></div>" +
      '<div class="wf-name">A new chat starts</div><div class="wf-summary">Drag from the dot to the first step.</div>' +
      '<ul class="wf-exits"><li class="' + (data.start ? "" : "wf-open") + '">First step</li></ul></div>';
  }

  function placeOutputs(nodeId) {
    var el = document.getElementById("node-" + nodeId);
    if (!el) return;
    var rows = el.querySelectorAll(".wf-exits li");
    var outs = el.querySelectorAll(".outputs .output");
    for (var i = 0; i < outs.length; i++) {
      // Row offsets are measured from the card, which starts at the node's top edge.
      if (rows[i]) outs[i].style.top = (rows[i].offsetTop + rows[i].offsetHeight / 2 - 8) + "px";
    }
  }

  function draw(d) {
    data = d;
    suppress = true;
    editor.clear();
    byStep = {};
    byNode = {};
    var first = d.start ? stepById(d.start) : null;
    var sp = d.start_pos || (first ? { x: first.x - 320, y: first.y } : { x: 40, y: 40 });
    var startId = editor.addNode("start", 0, 1, sp.x, sp.y, "wf-start-node", { step: START }, startHtml());
    byStep[START] = startId;
    byNode[startId] = START;
    d.nodes.forEach(function (n) {
      var id = editor.addNode(n.type, 1, n.exits.length, n.x, n.y, "wf-node", { step: n.id }, cardHtml(n));
      byStep[n.id] = id;
      byNode[id] = n.id;
    });
    Object.keys(byNode).forEach(placeOutputs);
    if (d.start && byStep[d.start]) editor.addConnection(startId, byStep[d.start], "output_1", "input_1");
    d.nodes.forEach(function (n) {
      n.exits.forEach(function (ex, i) {
        var target = n.next[ex.key];
        if (target && byStep[target]) editor.addConnection(byStep[n.id], byStep[target], "output_" + (i + 1), "input_1");
      });
    });
    Object.keys(byNode).forEach(function (id) { editor.updateConnectionNodes("node-" + id); });
    suppress = false;
    if (selected && byStep[selected]) mark(selected, "selected");
    status();
    checks();
  }

  function redrawCards() {
    data.nodes.forEach(function (n) {
      var el = document.querySelector("#node-" + byStep[n.id] + " .drawflow_content_node");
      if (el) el.innerHTML = cardHtml(n);
    });
  }

  function mark(stepId, cls) {
    var el = document.getElementById("node-" + byStep[stepId]);
    if (el) el.classList.add(cls);
  }

  function focusStep(stepId, atLeft) {
    var node = editor.getNodeFromId(byStep[stepId]);
    if (!node) return;
    var rect = container.getBoundingClientRect();
    editor.canvas_x = (atLeft ? 40 : rect.width / 2 - 130 * editor.zoom) - node.pos_x * editor.zoom;
    editor.canvas_y = rect.height / 3 - node.pos_y * editor.zoom;
    editor.precanvas.style.transform = "translate(" + editor.canvas_x + "px, " + editor.canvas_y + "px) scale(" + editor.zoom + ")";
    var card = document.querySelector("#node-" + byStep[stepId] + " .wf-card");
    if (card && !atLeft) { card.classList.remove("wf-flash"); void card.offsetWidth; card.classList.add("wf-flash"); }
  }

  // -- reacting to the canvas ------------------------------------------------

  function exitFor(nodeId, outputClass) {
    var step = byNode[nodeId];
    if (step === START) return { from: START, exit: "start" };
    var n = stepById(step);
    var index = parseInt(String(outputClass).split("_")[1], 10) - 1;
    return n && n.exits[index] ? { from: step, exit: n.exits[index].key } : null;
  }

  editor.on("connectionCreated", function (c) {
    if (suppress) return;
    var source = exitFor(c.output_id, c.output_class);
    var target = byNode[c.input_id];
    if (!source || !target || target === START) return;
    // One exit leads to one place: drawing a new link replaces the old one.
    var node = editor.getNodeFromId(c.output_id);
    var others = node.outputs[c.output_class].connections.filter(function (x) { return String(x.node) !== String(c.input_id); });
    suppress = true;
    others.forEach(function (x) { editor.removeSingleConnection(c.output_id, x.node, c.output_class, x.output); });
    suppress = false;
    op({ op: "connect", from: source.from, exit: source.exit, to: target }).then(redrawCards);
  });

  editor.on("connectionRemoved", function (c) {
    if (suppress) return;
    var source = exitFor(c.output_id, c.output_class);
    if (source) op({ op: "disconnect", from: source.from, exit: source.exit }).then(redrawCards);
  });

  editor.on("nodeMoved", function (id) {
    var node = editor.getNodeFromId(id);
    if (!node) return;
    moved[byNode[id]] = { x: node.pos_x, y: node.pos_y };
    clearTimeout(moveTimer);
    moveTimer = setTimeout(function () {
      var positions = moved;
      moved = {};
      post(URLS.op, { op: "move", positions: positions });
    }, 500);
  });

  editor.on("nodeSelected", function (id) {
    var step = byNode[id];
    if (step && step !== START) openStep(step);
  });

  // -- the side panel -------------------------------------------------------

  var panel = document.getElementById("wf-panel");
  var panelBody = document.getElementById("wf-panel-body");

  function showTab(name) {
    root.dataset.tab = name;
    document.querySelectorAll("[data-tab-button]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.tabButton === name ? "true" : "false");
    });
    document.querySelectorAll("[data-tab-pane]").forEach(function (p) {
      p.hidden = p.dataset.tabPane !== name;
    });
    panel.hidden = false;
  }

  function openStep(stepId) {
    selected = stepId;
    showTab("step");
    panelBody.innerHTML = '<p class="p-6 text-sm text-slate-500">Loading…</p>';
    htmx.ajax("GET", URLS.panel.replace("STEP", encodeURIComponent(stepId)), { target: "#wf-panel-body", swap: "innerHTML" });
  }

  document.body.addEventListener("workflowChanged", function () { load(); });

  window.WorkflowBuilder = {
    removeStep: function (stepId) {
      if (!confirm("Remove this step? Whatever led to it will lead to the step after it.")) return;
      selected = null;
      panelBody.innerHTML = '<p class="p-6 text-sm text-slate-500">Step removed.</p>';
      op({ op: "delete", id: stepId }, { redraw: true });
    },
    closePanel: function () { panel.hidden = true; selected = null; },
    focus: focusStep,
  };

  document.querySelectorAll("[data-tab-button]").forEach(function (b) {
    b.addEventListener("click", function () {
      if (!panel.hidden && root.dataset.tab === b.dataset.tabButton) panel.hidden = true;
      else showTab(b.dataset.tabButton);
    });
  });

  // -- status and checks ----------------------------------------------------

  function setSaving(on) {
    var el = document.getElementById("wf-saving");
    if (el) el.hidden = !on;
  }

  function toast(text, bad) {
    var el = document.getElementById("wf-toast");
    el.textContent = text;
    el.className = "fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-xl px-4 py-2 text-sm text-white shadow-lg " +
      (bad ? "bg-rose-600" : "bg-slate-800");
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.hidden = true; }, 4000);
  }

  function status() {
    var el = document.getElementById("wf-status");
    var words;
    if (data.editable) {
      words = "Draft version " + data.version + (data.published ? " · version " + data.published + " is live" : " · not published yet");
    } else if (data.version) {
      words = "Version " + data.version + " is live. Any change makes a draft.";
    } else {
      words = "No steps yet";
    }
    el.textContent = words;
    document.getElementById("wf-publish").hidden = !data.editable;
    var errors = data.checks.filter(function (c) { return c.level === "error"; }).length;
    document.getElementById("wf-publish-button").disabled = errors > 0;
    var badge = document.getElementById("wf-check-count");
    badge.textContent = data.checks.length;
    badge.className = "ml-1 rounded-full px-1.5 text-[11px] font-semibold " +
      (errors ? "bg-rose-100 text-rose-700" : data.checks.length ? "bg-amber-100 text-amber-800" : "bg-[#76BD4C]/20 text-[#3d7a1e]");
  }

  function checks() {
    var list = document.getElementById("wf-checks");
    if (!data.checks.length) {
      list.innerHTML = '<li class="text-[#3d7a1e]">Nothing to fix. Ready to publish.</li>';
      return;
    }
    list.innerHTML = data.checks.map(function (c, i) {
      return '<li><button type="button" data-check="' + i + '" class="w-full rounded-lg px-2 py-1.5 text-left hover:bg-slate-50 ' +
        (c.level === "error" ? "text-rose-700" : "text-amber-800") + '">' +
        (c.level === "error" ? "✖ " : "⚠ ") + esc(c.text) + "</button></li>";
    }).join("");
    list.querySelectorAll("[data-check]").forEach(function (b) {
      b.addEventListener("click", function () {
        var c = data.checks[+b.dataset.check];
        if (c.step) { focusStep(c.step); openStep(c.step); }
      });
    });
  }

  // -- toolbar --------------------------------------------------------------

  editor.on("zoom", function (z) {
    document.getElementById("wf-zoom-reset").textContent = Math.round(z * 100) + "%";
  });
  document.getElementById("wf-zoom-in").addEventListener("click", function () { editor.zoom_in(); });
  document.getElementById("wf-zoom-out").addEventListener("click", function () { editor.zoom_out(); });
  document.getElementById("wf-zoom-reset").addEventListener("click", function () { editor.zoom_reset(); });
  document.getElementById("wf-tidy").addEventListener("click", function () {
    if (confirm("Rearrange every step neatly? Your own arrangement is replaced.")) op({ op: "tidy" }, { redraw: true });
  });
  document.getElementById("wf-counts-toggle").addEventListener("change", function (e) {
    root.classList.toggle("wf-show-counts", e.target.checked);
  });

  // -- adding steps from the palette -----------------------------------------

  function canvasPoint(clientX, clientY) {
    var rect = editor.precanvas.getBoundingClientRect();
    return { x: (clientX - rect.left) / editor.zoom, y: (clientY - rect.top) / editor.zoom };
  }

  function addStep(kind, point) {
    op({ op: "add", kind: kind, x: point.x, y: point.y }, { redraw: true }).then(function (d) {
      if (d.added) openStep(d.added);
    });
  }

  document.querySelectorAll("[data-step-kind]").forEach(function (item) {
    item.addEventListener("dragstart", function (e) { e.dataTransfer.setData("text/plain", item.dataset.stepKind); });
    item.addEventListener("click", function () {
      var rect = container.getBoundingClientRect();
      addStep(item.dataset.stepKind, canvasPoint(rect.left + rect.width / 2 - 130, rect.top + 80));
    });
  });
  container.addEventListener("dragover", function (e) { e.preventDefault(); });
  container.addEventListener("drop", function (e) {
    e.preventDefault();
    var kind = e.dataTransfer.getData("text/plain");
    if (kind) addStep(kind, canvasPoint(e.clientX - 20, e.clientY - 20));
  });

  // -- the test chat ----------------------------------------------------------

  var chat = document.getElementById("wf-chat");

  function bubble(m) {
    var li = document.createElement("li");
    if (m.from === "note") {
      li.className = "mx-auto max-w-[90%] text-center text-[11px] italic text-slate-500";
      li.textContent = m.text;
    } else {
      li.className = m.from === "you"
        ? "ml-auto max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-[#dcf8c6] px-3 py-2 text-sm"
        : "mr-auto max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-bl-sm bg-white px-3 py-2 text-sm shadow-sm";
      li.textContent = m.text;
      if (m.file) {
        var f = document.createElement("div");
        f.className = "mb-1 rounded-lg bg-slate-100 px-2 py-1 text-xs font-medium";
        f.textContent = "📄 " + m.file;
        li.insertBefore(f, li.firstChild);
      }
      if (m.options) {
        var box = document.createElement("div");
        box.className = "mt-2 flex flex-col gap-1";
        if (m.style === "list") {
          var hint = document.createElement("div");
          hint.className = "text-[11px] text-slate-500";
          hint.textContent = "Shown as a pick-list:";
          box.appendChild(hint);
        }
        m.options.forEach(function (o) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "rounded-lg border border-slate-200 px-2 py-1 text-xs font-semibold text-[#0065A9] hover:bg-sky-50";
          b.textContent = o.title;
          b.addEventListener("click", function () { simulate({ text: o.title, reply_id: o.id }); });
          box.appendChild(b);
        });
        li.appendChild(box);
      }
    }
    chat.appendChild(li);
  }

  function simulate(body) {
    if (body.action === "start") chat.innerHTML = "";
    body.after_hours = document.getElementById("wf-after-hours").checked;
    post(URLS.simulate, body).then(function (d) {
      (d.messages || []).forEach(bubble);
      chat.scrollTop = chat.scrollHeight;
      simStep = d.waiting ? d.step : null;
      redrawCards();
      document.getElementById("wf-timeout").disabled = !d.waiting;
    });
  }

  document.getElementById("wf-sim-start").addEventListener("click", function () { simulate({ action: "start" }); });
  document.getElementById("wf-timeout").addEventListener("click", function () { simulate({ action: "timeout" }); });
  document.getElementById("wf-sim-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var input = document.getElementById("wf-sim-text");
    if (!input.value.trim()) return;
    simulate({ text: input.value });
    input.value = "";
  });

  // Fill the screen below whatever banners the page shows, never more.
  function fit() {
    root.style.height = Math.max(420, window.innerHeight - root.getBoundingClientRect().top - window.scrollY) + "px";
  }
  window.addEventListener("resize", fit);
  fit();

  load();
})();
