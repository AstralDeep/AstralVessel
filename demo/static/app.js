// Demo page logic: personas, conversations, streaming a turn, the live timeline, A2A frames and the isolation test.
// Everything shown comes from the Vessel's own A2A stream and conversation API, relayed by the demo app.
(function () {
  "use strict";

  var state = { personas: {}, persona: "alice", conversation: null, task: null, busy: false, controller: null,
                manifest: null, examples: [], frames: [], turnStarted: 0, clock: null };
  var $ = function (id) { return document.getElementById(id); };

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = attrs[key];
      else if (key === "class") node.className = attrs[key];
      else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), attrs[key]);
      else node.setAttribute(key, attrs[key]);
    });
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function markdown(text) {
    if (window.marked && window.DOMPurify) {
      return DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: false }),
                                { ADD_ATTR: ["target"], FORBID_TAGS: ["style", "form", "input"] });
    }
    var div = document.createElement("div"); div.textContent = text || ""; return div.innerHTML;
  }

  function api(path, options) {
    return fetch(path, options).then(function (r) { return r.json().then(function (body) { return { status: r.status, body: body }; }); });
  }

  function fmtMs(ms) { return ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : ms + " ms"; }
  function fmtNum(n) { return (n || 0).toLocaleString(); }
  function short(value, limit) {
    var text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }

  // Pipeline strip
  var pulseTimers = {};
  function pulse(names, ms) {
    names.forEach(function (name) {
      var node = document.querySelector('[data-node="' + name + '"], [data-wire="' + name + '"]');
      if (!node) return;
      node.classList.add("is-active");
      clearTimeout(pulseTimers[name]);
      if (ms) pulseTimers[name] = setTimeout(function () { node.classList.remove("is-active"); }, ms);
    });
  }
  function settle(names) {
    names.forEach(function (name) {
      var node = document.querySelector('[data-node="' + name + '"], [data-wire="' + name + '"]');
      if (node) node.classList.remove("is-active");
    });
  }
  function allOff() { settle(["app", "a2a", "vessel", "tools", "sandbox", "model", "egress", "llm"]); }

  // Tabs
  document.querySelectorAll(".tabs__tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".tabs__tab").forEach(function (t) { t.setAttribute("aria-selected", String(t === tab)); });
      document.querySelectorAll(".tabs__panel").forEach(function (p) { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    });
  });

  // Personas and conversations
  function renderPersonas() {
    var box = $("personas"); box.textContent = "";
    Object.keys(state.personas).forEach(function (key) {
      var p = state.personas[key];
      box.appendChild(el("button", { class: "persona", type: "button", role: "radio", "aria-checked": String(key === state.persona),
        onclick: function () { if (state.busy) return; state.persona = key; state.conversation = null; renderPersonas(); resetTranscript(); loadConversations(); } },
        [el("span", { class: "persona__name", text: p.name }), el("span", { class: "persona__role", text: p.role + " · " + p.id })]));
    });
  }

  function loadConversations() {
    return api("/api/conversations?persona=" + state.persona).then(function (r) {
      var list = $("conversations"); list.textContent = "";
      var items = (r.body && r.body.conversations) || [];
      if (!items.length) { list.appendChild(el("li", { class: "conv-list__empty", text: "None yet." })); return; }
      items.forEach(function (c) {
        list.appendChild(el("li", {}, [el("button", { type: "button", title: c.title, "aria-current": String(c.id === state.conversation),
          onclick: function () { if (!state.busy) openConversation(c.id); }, text: c.title })]));
      });
    });
  }

  function resetTranscript() {
    var t = $("transcript"); t.textContent = "";
    t.appendChild(emptyState());
    $("timeline").textContent = "";
    $("timeline").appendChild(el("li", { class: "timeline__empty text text--muted", text: "Events from the current turn appear here as they stream." }));
    $("turn-stats").textContent = ""; $("frames").textContent = ""; $("isolation-result").textContent = "";
  }

  function emptyState() {
    var box = el("div", { class: "empty", id: "empty" }, [
      el("p", { class: "heading heading--3", text: "Ask about UK research" }),
      el("p", { class: "text text--muted", text: "Try one of these, or ask your own." })]);
    var ex = el("div", { class: "examples" });
    state.examples.forEach(function (q) { ex.appendChild(el("button", { class: "example", type: "button", text: q, onclick: function () { ask(q); } })); });
    box.appendChild(ex);
    return box;
  }

  function openConversation(id) {
    api("/api/conversations/" + id + "?persona=" + state.persona).then(function (r) {
      if (r.status !== 200) return;
      state.conversation = id;
      var t = $("transcript"); t.textContent = "";
      r.body.turns.forEach(function (turn) {
        t.appendChild(userMessage(turn.user_text));
        var agent = agentMessage();
        t.appendChild(agent.node);
        finishAgent(agent, turn.result.text, turn.result.components || [], turn.result);
      });
      var last = r.body.turns[r.body.turns.length - 1];
      if (last) replayTimeline(last.events, last.result);
      loadConversations();
    });
  }

  // Messages
  function userMessage(text) {
    return el("div", { class: "msg msg--user" }, [el("span", { class: "msg__who", text: state.personas[state.persona].name }),
      el("div", { class: "msg__body", text: text })]);
  }

  function agentMessage() {
    var narration = el("div", { class: "msg__narration" });
    var working = el("div", { class: "msg__working", text: "Starting" });
    var body = el("div", { class: "prose" });
    var meta = el("div", { class: "msg__meta" });
    var extra = el("div", {});
    var node = el("div", { class: "msg msg--agent" }, [el("span", { class: "msg__who", text: "Nexus · AstralVessel" }), narration, working, body, extra, meta]);
    return { node: node, narration: narration, working: working, body: body, meta: meta, extra: extra, live: "" };
  }

  function statusTag(status) {
    var cls = status === "ok" ? "tag" : "tag tag--muted";
    return el("span", { class: cls, text: status.replace("_", " ") });
  }

  function finishAgent(agent, text, components, result) {
    agent.working.remove();
    agent.body.innerHTML = markdown(text || (result && result.error) || "No answer was produced.");
    agent.body.querySelectorAll("a").forEach(function (a) { a.setAttribute("target", "_blank"); a.setAttribute("rel", "noopener"); });
    if (components && components.length) agent.extra.appendChild(astralPreview(components));
    if (result) {
      agent.meta.textContent = "";
      agent.meta.appendChild(statusTag(result.status));
      agent.meta.appendChild(el("span", { class: "tag tag--muted", text: fmtNum((result.tokens || {}).input + (result.tokens || {}).output) + " tokens" }));
      agent.meta.appendChild(el("span", { class: "tag tag--muted", text: result.steps + " steps" }));
      agent.meta.appendChild(el("span", { class: "tag tag--muted", text: fmtMs(result.elapsed_ms || 0) }));
    }
  }

  // Astral preview: Projection's HTML inside a sandboxed frame with Astral's own stylesheet
  var ASTRAL_HEAD = '<!doctype html><html><head><meta charset="utf-8">' +
    '<link rel="stylesheet" href="/astral/astral.css"><script src="/astral/vendor/tailwind.js"><\/script>' +
    "<script>tailwind.config={theme:{extend:{colors:{'astral-bg':'rgb(var(--astral-bg) / <alpha-value>)','astral-surface':'rgb(var(--astral-surface) / <alpha-value>)','astral-primary':'rgb(var(--astral-primary) / <alpha-value>)','astral-secondary':'rgb(var(--astral-secondary) / <alpha-value>)','astral-text':'rgb(var(--astral-text) / <alpha-value>)','astral-muted':'rgb(var(--astral-muted) / <alpha-value>)','astral-accent':'rgb(var(--astral-accent) / <alpha-value>)'}}}};<\/script>" +
    '<script src="/astral/vendor/plotly.min.js"><\/script><style>body{margin:0;padding:16px}<\/style></head><body class="bg-astral-bg text-astral-text">';

  function astralPreview(components) {
    var frame = el("iframe", { class: "astral-preview__frame", sandbox: "allow-scripts", title: "AstralProjection rendering", loading: "lazy" });
    var box = el("div", { class: "astral-preview" }, [
      el("div", { class: "cluster cluster--between" }, [el("span", { class: "eyebrow", text: "Rendered by AstralProjection" }),
        el("span", { class: "tag", text: components.length + " primitives" })]),
      frame,
      el("details", {}, [el("summary", { text: "AstralPrimitives JSON (application/vnd.astral.primitives+json)" }),
        el("pre", { text: JSON.stringify(components, null, 2) })])]);
    api("/api/render", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ components: components }) })
      .then(function (r) {
        if (r.status !== 200) { frame.replaceWith(el("p", { class: "text text--small text--muted", text: "AstralProjection is not installed in this demo image." })); return; }
        frame.srcdoc = ASTRAL_HEAD + r.body.html + '<script src="/static/astral-preview.js"><\/script></body></html>';
      });
    return box;
  }

  window.addEventListener("message", function (event) {
    if (!event.data || typeof event.data.astralPreviewHeight !== "number") return;
    document.querySelectorAll(".astral-preview__frame").forEach(function (f) {
      if (f.contentWindow === event.source) f.style.height = Math.min(event.data.astralPreviewHeight + 4, 2400) + "px";
    });
  });

  // Timeline
  var toolItems = {};
  function timelineReset() { $("timeline").textContent = ""; toolItems = {}; }
  function timelineAdd(item) { $("timeline").appendChild(item); item.scrollIntoView({ block: "nearest" }); }
  function since() { return state.turnStarted ? fmtMs(Date.now() - state.turnStarted) : ""; }

  function toolStarted(e) {
    var head = el("div", { class: "timeline__head" }, [el("span", { class: "timeline__name", text: e.name }), el("span", { class: "timeline__time", text: since() })]);
    var args = el("div", { class: "timeline__args", text: short(e.arguments, 220) });
    var item = el("li", { class: "timeline__item timeline__item--tool" }, [head, args, el("div", { class: "msg__working", text: "running" })]);
    toolItems[e.call_id] = item;
    timelineAdd(item);
  }

  function toolFinished(e) {
    var item = toolItems[e.call_id];
    if (!item) { toolStarted(e); item = toolItems[e.call_id]; }
    var working = item.querySelector(".msg__working"); if (working) working.remove();
    item.querySelector(".timeline__time").textContent = fmtMs(e.elapsed_ms) + (e.truncated ? " · truncated" : "");
    if (e.error) item.classList.add("timeline__item--error");
    item.appendChild(el("details", {}, [el("summary", { text: e.error ? "error output" : "output" }), el("pre", { text: e.output || "" })]));
  }

  function stepFinished(e) {
    var tokens = e.tokens || {};
    timelineAdd(el("li", { class: "timeline__item" }, [el("div", { class: "timeline__head" }, [
      el("span", { class: "timeline__name", text: "model step " + e.step + (e.tool_calls ? " → " + e.tool_calls + " tool call" + (e.tool_calls > 1 ? "s" : "") : " → answer") }),
      el("span", { class: "timeline__time", text: fmtNum(tokens.input) + " in · " + fmtNum(tokens.output) + " out" })])]));
  }

  function turnStats(result) {
    var box = $("turn-stats"); box.textContent = "";
    [["status", result.status.replace("_", " ")], ["steps", result.steps], ["tokens", fmtNum((result.tokens || {}).input + (result.tokens || {}).output)], ["time", fmtMs(result.elapsed_ms || 0)]]
      .forEach(function (pair) { box.appendChild(el("div", { class: "stat" }, [el("span", { class: "stat__value", text: String(pair[1]) }), el("span", { class: "stat__label", text: pair[0] })])); });
  }

  function replayTimeline(events, result) {
    timelineReset(); state.turnStarted = 0;
    (events || []).forEach(function (e) {
      if (e.type === "tool.started") toolStarted(e);
      else if (e.type === "tool.finished") toolFinished(e);
      else if (e.type === "step.finished") stepFinished(e);
    });
    if (result) { turnStats(result); timelineAdd(el("li", { class: "timeline__item timeline__item--done", text: "turn finished · " + result.status })); }
  }

  // Frames
  function frameAdd(e) {
    if (!e.raw) return;
    var kind = e.type;
    var list = $("frames");
    var last = list.lastElementChild;
    if ((kind === "text.delta" || kind === "reasoning.delta") && last && last.dataset.kind === kind) {
      var n = Number(last.dataset.count) + 1; last.dataset.count = String(n);
      last.querySelector(".frames__count").textContent = " ×" + n;
      return;
    }
    var item = el("li", {}, [el("details", {}, [el("summary", {}, [el("span", { class: "frames__kind", text: kind }), el("span", { class: "frames__count", text: "" })]),
      el("pre", { text: JSON.stringify(e.raw, null, 1) })])]);
    item.dataset.kind = kind; item.dataset.count = "1";
    list.appendChild(item);
  }

  // Asking
  function setBusy(busy) {
    state.busy = busy;
    $("send").disabled = busy; $("stop").hidden = !busy; $("question").disabled = busy;
  }

  function ask(text) {
    text = (text || "").trim();
    if (!text || state.busy) return;
    var empty = $("empty"); if (empty) empty.remove();
    var t = $("transcript");
    t.appendChild(userMessage(text));
    var agent = agentMessage(); t.appendChild(agent.node);
    agent.node.scrollIntoView({ block: "end" });
    $("question").value = "";
    timelineReset(); $("frames").textContent = ""; $("turn-stats").textContent = "";
    setBusy(true); state.task = null; state.turnStarted = Date.now();
    pulse(["app", "a2a", "vessel"], 0);
    var finalText = null, finalComponents = [], finalResult = null, stepText = "";
    var controller = new AbortController(); state.controller = controller;
    clearInterval(state.clock);
    state.clock = setInterval(function () { if (agent.working.isConnected && !agent.live) agent.working.textContent = "Working · " + since(); }, 500);

    function handle(e) {
      frameAdd(e);
      switch (e.type) {
        case "task":
          state.task = e.task_id;
          if (!state.conversation) state.conversation = e.conversation_id;
          break;
        case "turn.started":
          pulse(["egress", "model", "llm"], 0);
          break;
        case "reasoning.delta":
          pulse(["model", "llm", "egress"], 900);
          if (!stepText) agent.working.textContent = "Thinking · " + since();
          break;
        case "text.delta":
          stepText += e.content || "";
          agent.live = stepText;
          agent.working.textContent = "";
          agent.body.innerHTML = markdown(stepText);
          break;
        case "step.finished":
          stepFinished(e);
          if (e.tool_calls && stepText.trim()) {
            agent.narration.appendChild(el("p", { text: stepText.trim() }));
          }
          if (e.tool_calls) { agent.body.textContent = ""; agent.live = ""; agent.working.textContent = "Working · " + since(); }
          stepText = "";
          settle(["llm"]);
          break;
        case "tool.started":
          toolStarted(e);
          if (e.name === "webfetch" || e.name === "websearch") pulse(["egress", "llm"], 2500);
          else if (e.name === "present") pulse(["vessel"], 1500);
          else pulse(["tools", "sandbox"], 2500);
          break;
        case "tool.finished":
          toolFinished(e);
          break;
        case "answer":
          finalText = e.text; finalComponents = e.components || [];
          break;
        case "turn.finished":
          finalResult = e;
          break;
        case "error":
          agent.extra.appendChild(el("div", { class: "callout callout--danger" }, [el("p", { class: "callout__title", text: "The turn could not run" }), el("p", { text: e.detail || "error" })]));
          break;
        case "status":
          if (e.text) agent.extra.appendChild(el("p", { class: "text text--small text--muted", text: e.text }));
          break;
      }
    }

    fetch("/api/chat", { method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persona: state.persona, text: text, conversation_id: state.conversation }) })
      .then(function (response) {
        var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = "";
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) return;
            buffer += decoder.decode(chunk.value, { stream: true });
            var parts = buffer.split("\n\n"); buffer = parts.pop();
            parts.forEach(function (block) {
              block.split("\n").forEach(function (line) {
                if (line.indexOf("data:") !== 0) return;
                try { handle(JSON.parse(line.slice(5))); } catch (err) { /* ignore a malformed frame */ }
              });
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function () { /* aborted or network error; the result below says what happened */ })
      .then(function () {
        clearInterval(state.clock);
        var result = finalResult || { status: "cancelled", steps: 0, tokens: {}, elapsed_ms: Date.now() - state.turnStarted };
        finishAgent(agent, finalText !== null ? finalText : agent.live, finalComponents, result);
        turnStats(result);
        timelineAdd(el("li", { class: "timeline__item timeline__item--done", text: "turn finished · " + result.status }));
        allOff(); setBusy(false);
        loadConversations();
      });
  }

  $("composer").addEventListener("submit", function (event) { event.preventDefault(); ask($("question").value); });
  $("question").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask($("question").value); }
  });
  $("stop").addEventListener("click", function () {
    if (!state.task) { if (state.controller) state.controller.abort(); return; }
    $("stop").disabled = true;
    api("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ persona: state.persona, task_id: state.task }) })
      .then(function () { $("stop").disabled = false; });
  });
  $("new-chat").addEventListener("click", function () { if (state.busy) return; state.conversation = null; resetTranscript(); loadConversations(); });

  $("isolation-test").addEventListener("click", function () {
    var out = $("isolation-result"); out.textContent = "";
    if (!state.conversation) { out.appendChild(el("p", { class: "text text--muted", text: "Open or start a conversation first." })); return; }
    var other = Object.keys(state.personas).filter(function (k) { return k !== state.persona; })[0];
    var who = state.personas[other];
    api("/api/conversations/" + state.conversation + "?persona=" + other).then(function (r) {
      var held = r.status === 404;
      out.appendChild(el("div", { class: held ? "callout callout--success" : "callout callout--danger" }, [
        el("p", { class: "callout__title", text: held ? "Owner binding held" : "Owner binding failed" }),
        el("p", { text: who.name + " (" + who.id + ") asked for conversation " + state.conversation.slice(0, 8) + "… and got HTTP " + r.status + (held ? ": not found, exactly as if it did not exist." : ".") }),
        el("pre", { text: JSON.stringify(r.body) })]));
    });
  });

  function renderManifest(meta) {
    var box = $("manifest"); box.textContent = "";
    var m = meta.manifest || {}, card = meta.card || {};
    box.appendChild(el("p", { class: "text", text: (m.agent || {}).description || card.description || "" }));
    var dl = el("dl", { class: "kv" });
    [["agent", (m.agent || {}).name + " " + (m.agent || {}).version], ["model", (m.model || {}).model], ["step limit", (m.model || {}).max_steps],
     ["deadline", ((m.sandbox || {}).deadline_seconds || "") + " s"], ["mounts", ((m.sandbox || {}).mounts || []).map(function (x) { return x.target + (x.read_only ? " (read-only)" : ""); }).join(", ")],
     ["primitives", (m.output || {}).primitives ? "on · astralprims " + m.primitives_schema : "off"],
     ["protocol", ((card.supportedInterfaces || [])[0] || {}).protocolBinding + " · A2A " + ((card.supportedInterfaces || [])[0] || {}).protocolVersion]]
      .forEach(function (pair) { dl.appendChild(el("dt", { text: pair[0] })); dl.appendChild(el("dd", { text: String(pair[1]) })); });
    box.appendChild(dl);
    box.appendChild(el("span", { class: "eyebrow", text: "Tools this agent has" }));
    var row = el("div", { class: "tag-row" });
    (m.tools || []).forEach(function (t) { row.appendChild(el("span", { class: "tag", text: t })); });
    box.appendChild(row);
    box.appendChild(el("p", { class: "text text--small text--muted", text: "A tool not listed in vessel.toml does not exist for the agent." }));
    box.appendChild(el("details", {}, [el("summary", { class: "text text--small", text: "A2A agent card" }), el("pre", { class: "mono text--small", text: JSON.stringify(card, null, 1) })]));
  }

  api("/api/meta").then(function (r) {
    var meta = r.body || {};
    state.personas = meta.personas || {};
    state.examples = ((meta.manifest || {}).agent || {}).examples || [];
    var model = ((meta.manifest || {}).model || {}).model || "";
    if (model) $("model-name").textContent = model.split("/").pop().replace(/-/g, " ");
    renderPersonas(); renderManifest(meta); resetTranscript(); loadConversations();
  }).catch(function () {
    $("transcript").appendChild(el("div", { class: "callout callout--danger" }, [el("p", { class: "callout__title", text: "The Vessel is not reachable" })]));
  });
})();
