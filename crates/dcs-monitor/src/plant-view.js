/* Reusable operator symbols consume code-authored views from the plant model.
 * Drawings carry geometry and model references; live state and commands remain
 * on the controller's existing telemetry and receipted command contracts. */
(function () {
  "use strict";
  const WIDTH = 1200, HEIGHT = 760;
  const SIZES = { pump: [160, 140], motor: [160, 140], valve: [160, 140],
    tank: [150, 195], measurement: [190, 100], label: [300, 50] };
  const state = { roots: null, context: null, model: null, views: [], signature: null,
    view: null, selected: null, elements: new Map() };

  function element(tag, attributes, text, namespace) {
    const node = namespace ? document.createElementNS(namespace, tag) : document.createElement(tag);
    Object.entries(attributes || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text != null) node.textContent = text;
    return node;
  }
  function svg(tag, attributes, text) {
    return element(tag, attributes, text, "http://www.w3.org/2000/svg");
  }
  function note(text) { if (state.roots) state.roots.note.textContent = text; }
  function modelMap(context) {
    const index = context.index;
    const descriptors = context.descriptors instanceof Map ? Array.from(context.descriptors.values())
      : context.descriptors || (context.snapshot || {}).descriptors || [];
    return {
      points: new Map((index.points || []).map(point => [point.point, point])),
      components: new Map((index.components || []).filter(component => component.id != null).map(component => [component.id, component])),
      equipment: new Map((index.equipment || []).map(equipment => [equipment.id, equipment])),
      descriptors: new Map(descriptors.map(descriptor => [descriptor.name, descriptor])),
      telemetry: context.telemetry instanceof Map ? context.telemetry
        : new Map(((context.snapshot || {}).points || []).map(point => [point.point, point]))
    };
  }
  function typeFor(kind) {
    if (/valve/.test(kind || "")) return "valve";
    if (/pump/.test(kind || "")) return "pump";
    if (/motor|blower/.test(kind || "")) return "motor";
    return "tank";
  }
  function fallbackViews() {
    const equipment = Array.from(state.model.equipment.values());
    const members = equipment.length ? equipment : Array.from(state.model.components.values())
      .filter(component => ["motor", "valve", "pid", "pump-group", "blower-group"].includes(component.kind));
    const views = [];
    // Compatibility for models without authored views: show every declared
    // equipment member in small overview pages. Membership never implies pipes.
    for (let start = 0; start < members.length; start += 12) {
      const subset = members.slice(start, start + 12);
      const columns = Math.min(4, subset.length), rows = Math.ceil(subset.length / columns);
      views.push({ id: start === 0 ? "overview" : "overview-" + start,
        label: members.length <= 12 ? "Plant overview" : "Equipment " + (start + 1) + "–" + (start + subset.length),
        parent: null, pipes: [], nodes: subset.map((member, number) => ({
          id: "equipment-" + (start + number), symbol: typeFor(member.kind), label: member.label || member.name,
          x: (WIDTH - ((columns - 1) * 280 + 160)) / 2 + (number % columns) * 280,
          y: Math.max(100, (HEIGHT - ((rows - 1) * 190 + 195)) / 2) + Math.floor(number / columns) * 190,
          binding: equipment.length ? { equipment: member.id } : { component: member.id }
        })) });
    }
    if (!views.length) views.push({ id: "overview", label: "Plant overview", parent: null,
      nodes: [{ id: "empty", symbol: "label", label: "No plant drawing is declared", x: 450, y: 320, binding: null }], pipes: [] });
    return views;
  }
  function initRoots() {
    const roots = { nav: document.getElementById("plant-area-list"),
      title: document.getElementById("plant-view-title"), canvas: document.getElementById("plant-canvas"),
      note: document.getElementById("plant-view-note") };
    if (Object.values(roots).some(root => !root)) return false;
    state.roots = roots;
    roots.canvas.setAttribute("viewBox", "0 0 " + WIDTH + " " + HEIGHT);
    roots.canvas.setAttribute("role", "group");
    roots.canvas.setAttribute("aria-label", "Plant schematic; select equipment to open its controls");
    return true;
  }
  function renderNavigation() {
    const nav = state.roots.nav;
    nav.replaceChildren();
    const visited = new Set();
    function visit(view, depth) {
      if (visited.has(view.id)) return;
      visited.add(view.id);
      const button = element("button", { type: "button", class: "plant-area" + (view.id === state.view ? " is-current" : ""),
        "aria-current": view.id === state.view ? "page" : "false", "data-workspace": "plant" });
      button.style.setProperty("--area-depth", String(depth));
      button.append(element("span", { class: "plant-area-name" }, view.label));
      button.append(element("span", { class: "plant-area-count", "aria-label": "symbols" }, String(view.nodes.length)));
      button.addEventListener("click", () => selectArea(view.id));
      nav.append(button);
      state.views.filter(child => child.parent === view.id).forEach(child => visit(child, depth + 1));
    }
    state.views.filter(view => view.parent == null).forEach(view => visit(view, 0));
  }
  function currentView() { return state.views.find(view => view.id === state.view); }
  function selectArea(id) {
    const view = state.views.find(item => item.id === id) || (id === "overview" ? state.views.find(item => item.parent == null) : null);
    if (!view) return;
    state.view = view.id;
    state.selected = null;
    renderNavigation(); renderCanvas();
  }
  function drawSymbol(node, group) {
    const [width] = SIZES[node.symbol], x = width / 2, y = 40;
    if (node.symbol === "pump" || node.symbol === "motor") {
      group.append(svg("line", { x1: x - 65, y1: y, x2: x - 32, y2: y, class: "plant-symbol-line" }));
      group.append(svg("line", { x1: x + 32, y1: y, x2: x + 65, y2: y, class: "plant-symbol-line" }));
      group.append(svg("circle", { cx: x, cy: y, r: 31, class: "plant-symbol-body" }));
      if (node.symbol === "pump") group.append(svg("path", { d: "M " + (x - 14) + " " + (y - 18) + " L " + (x + 19) + " " + y + " L " + (x - 14) + " " + (y + 18) + " Z", class: "plant-pump-impeller" }));
      else group.append(svg("text", { x, y: y + 8, "text-anchor": "middle", class: "plant-motor-letter" }, "M"));
    } else if (node.symbol === "valve") {
      group.append(svg("line", { x1: x - 65, y1: y, x2: x + 65, y2: y, class: "plant-symbol-line" }));
      group.append(svg("path", { d: "M " + (x - 29) + " " + (y - 23) + " L " + (x + 29) + " " + (y + 23) + " L " + (x + 29) + " " + (y - 23) + " L " + (x - 29) + " " + (y + 23) + " Z", class: "plant-symbol-body" }));
      group.append(svg("path", { d: "M " + x + " " + (y - 23) + " V " + (y - 35) + " M " + (x - 12) + " " + (y - 35) + " H " + (x + 12), class: "plant-symbol-line" }));
    } else if (node.symbol === "tank") {
      group.append(svg("path", { d: "M 18 20 L 18 110 Q " + x + " 130 " + (width - 18) + " 110 L " + (width - 18) + " 20 M 18 20 Q " + x + " 0 " + (width - 18) + " 20 M 18 20 Q " + x + " 40 " + (width - 18) + " 20", class: "plant-symbol-body plant-tank" }));
    } else if (node.symbol === "measurement") group.append(svg("rect", { x: 6, y: 4, width: width - 12, height: 62, rx: 2, class: "plant-measurement-body" }));
    const labelY = node.symbol === "label" ? 26 : node.symbol === "tank" ? 143 : node.symbol === "measurement" ? 25 : 94;
    group.append(svg("text", { x, y: labelY, "text-anchor": "middle", class: "plant-label" }, node.label));
    if (node.symbol !== "label") {
      group.append(svg("text", { x, y: node.symbol === "tank" ? 162 : node.symbol === "measurement" ? 50 : 117,
        "text-anchor": "middle", class: node.symbol === "measurement" ? "plant-value" : "plant-status" }, ""));
      group.append(svg("text", { x, y: node.symbol === "measurement" ? 83 : node.symbol === "tank" ? 183 : 137,
        "text-anchor": "middle", class: "plant-tag" }, ""));
    }
  }
  function portPosition(node, port) {
    const [width, height] = SIZES[node.symbol];
    if (node.symbol === "tank") return { x: node.x + (port === "e" ? width - 18 : port === "w" ? 18 : width / 2),
      y: node.y + (port === "n" ? 20 : port === "s" ? 120 : 58) };
    if (["pump", "motor", "valve"].includes(node.symbol)) return { x: node.x + (port === "e" ? width - 15 : port === "w" ? 15 : width / 2),
      y: node.y + (port === "n" ? 9 : port === "s" ? 71 : 40) };
    return { x: node.x + (port === "e" ? width : port === "w" ? 0 : width / 2),
      y: node.y + (port === "n" ? 0 : port === "s" ? height : height / 2) };
  }
  function pipePath(pipe, nodes) {
    const fromNode = nodes.get(pipe.from.node), toNode = nodes.get(pipe.to.node);
    if (!fromNode || !toNode) return null;
    const from = portPosition(fromNode, pipe.from.port), to = portPosition(toNode, pipe.to.port);
    if (pipe.from.port === "e" || pipe.from.port === "w") {
      const middle = Math.round((from.x + to.x) / 2);
      return "M " + from.x + " " + from.y + " H " + middle + " V " + to.y + " H " + to.x;
    }
    const middle = Math.round((from.y + to.y) / 2);
    return "M " + from.x + " " + from.y + " V " + middle + " H " + to.x + " V " + to.y;
  }
  function renderCanvas() {
    const view = currentView(), canvas = state.roots.canvas;
    if (!view) return;
    state.roots.title.textContent = view.label;
    canvas.replaceChildren(); state.elements.clear();
    const defs = svg("defs"); canvas.append(defs);
    const nodes = new Map(view.nodes.filter(node => SIZES[node.symbol]).map(node => [node.id, node]));
    (view.pipes || []).forEach(pipe => {
      const d = pipePath(pipe, nodes);
      if (d) canvas.append(svg("path", { d, class: "plant-pipe", "aria-hidden": "true" }));
    });
    nodes.forEach(node => {
      const [width, height] = SIZES[node.symbol];
      const interactive = Boolean(node.binding && (node.binding.equipment != null || node.binding.component != null));
      const group = svg("g", { transform: "translate(" + node.x + " " + node.y + ")",
        class: "plant-node" + (interactive ? " plant-equipment" : "") + " symbol-" + node.symbol,
        "data-node": node.id, role: interactive ? "button" : "img", "aria-label": node.label });
      if (interactive) group.setAttribute("tabindex", "0");
      group.append(svg("title", {}, node.label));
      group.append(svg("rect", { width, height, rx: 3, class: "plant-node-hit" }));
      const clipId = "plant-clip-" + state.elements.size;
      const clip = svg("clipPath", { id: clipId }); clip.append(svg("rect", { width, height })); defs.append(clip);
      const drawing = svg("g", { "clip-path": "url(#" + clipId + ")" }); drawSymbol(node, drawing); group.append(drawing);
      if (interactive) {
        group.addEventListener("click", () => openNode(node));
        group.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openNode(node); }
        });
      }
      state.elements.set(node.id, group); canvas.append(group);
    });
    updateLive();
    note("Select a pump, motor, or valve to view its feedback, alarms, and controls.");
  }
  function openNode(node) {
    const binding = node.binding;
    if (!binding) return;
    const component = binding.component != null ? state.model.components.get(binding.component) : null;
    const selection = binding.equipment != null ? "equipment:" + binding.equipment : component ? component.name : null;
    if (!selection) { note("The selected equipment is unavailable in the current model."); return; }
    state.selected = node.id;
    state.elements.forEach((element, id) => element.classList.toggle("is-selected", id === node.id));
    if (typeof state.context.selectEquipment === "function") state.context.selectEquipment(selection);
  }
  function sample(point) { return (state.model.telemetry.get(point) || {}).sample || null; }
  function value(reading) {
    if (!reading || !reading.value) return null;
    const entries = Object.entries(reading.value); return entries.length ? entries[0][1] : null;
  }
  function good(reading) { return Boolean(reading && reading.quality === "good"); }
  function quality(reading) {
    if (!reading) return "No sample";
    if (reading.quality === "good") return "Good";
    if (typeof reading.quality === "string") return reading.quality;
    const entry = Object.entries(reading.quality || {})[0];
    return entry ? entry[0] + " · " + String(entry[1]) : "Unknown quality";
  }
  function descriptorsFor(node) {
    const binding = node.binding;
    if (!binding) return [];
    const ids = binding.equipment != null ? (state.model.equipment.get(binding.equipment) || {}).components || []
      : binding.component != null ? [binding.component] : [];
    return ids.map(id => state.model.components.get(id)).filter(Boolean)
      .map(component => state.model.descriptors.get(component.name)).filter(Boolean);
  }
  function liveState(node) {
    const binding = node.binding;
    if (!binding) return { text: "", tag: "", running: false, fault: false, degraded: false };
    if (state.context.stale) return { text: "Data stale", tag: "Connection lost", running: false, fault: false, degraded: true };
    if (binding.point != null) {
      const reading = sample(binding.point), meta = state.model.points.get(binding.point);
      const current = value(reading);
      const text = typeof current === "number" && Number.isFinite(current)
        ? current.toLocaleString(undefined, { maximumFractionDigits: 3 }) + (meta && meta.unit ? " " + meta.unit : "") : "—";
      return { text, tag: good(reading) ? "" : quality(reading), running: false, fault: false, degraded: !good(reading) };
    }
    const descriptors = descriptorsFor(node), important = [];
    const actuators = descriptors.filter(descriptor => descriptor.kind === "motor" || /valve/.test(descriptor.kind));
    let running = null, command = null, fault = false, alarm = false, tripped = false, position = null, positionUnit = "";
    descriptors.forEach(descriptor => (descriptor.ports || []).forEach(port => {
      const reading = sample(port.point), current = value(reading), actuator = actuators.includes(descriptor);
      if (actuator && ["run", "out", "cmd", "fault", "position", "fb", "discrepancy"].includes(port.name)) important.push(reading);
      if (actuator && port.name === "run" && typeof current === "boolean") running = good(reading) ? current : null;
      if (actuator && port.name === "out" && typeof current === "boolean") command = good(reading) ? current : null;
      if (actuator && ["position", "fb"].includes(port.name) && typeof current === "number") {
        position = good(reading) ? current : null;
        positionUnit = (state.model.points.get(port.point) || {}).unit || "";
      }
      if (port.name === "fault" || port.name === "discrepancy") fault = fault || current === true;
      if (port.name === "tripped" && port.direction === "out") { tripped = tripped || current === true; important.push(reading); }
      if (port.name === "alarm" && port.direction === "out" && typeof current === "boolean") { alarm = alarm || current; important.push(reading); }
    }));
    const degraded = !descriptors.length || important.length === 0 || important.some(reading => !good(reading));
    let text = tripped ? "Protection tripped" : fault ? "Feedback fault" : alarm ? "Alarm" : degraded ? "Data degraded"
      : running === true ? "Running" : running === false ? "Stopped" : position != null ? "Position " + position.toLocaleString(undefined, { maximumFractionDigits: 1 }) + (positionUnit ? " " + positionUnit : "")
      : command === true ? "Output on" : command === false ? "Output off" : "Live component";
    if (!fault && !tripped && !alarm && !degraded) {
      if (running === true && command === false) text = "Stop requested";
      if (running === false && command === true) text = "Start requested";
    }
    let tag = degraded && (fault || alarm || tripped) ? "Data degraded" : "";
    if (binding.equipment != null) {
      const equipment = state.model.equipment.get(binding.equipment);
      const mode = equipment && (equipment.controls || []).find(control => /mode/i.test(control.label || ""));
      if (mode) {
        const reading = sample(mode.point);
        if (good(reading) && typeof value(reading) === "boolean") tag += (tag ? " · " : "") + (value(reading) ? mode.true_label || "On" : mode.false_label || "Off");
      }
    }
    return { text, tag, running: running === true && !degraded, fault: fault || alarm || tripped, degraded };
  }
  function updateLive() {
    const view = currentView(); if (!view) return;
    view.nodes.forEach(node => {
      const element = state.elements.get(node.id); if (!element) return;
      const live = liveState(node);
      element.classList.toggle("is-running", live.running);
      element.classList.toggle("is-stopped", !live.running && !live.degraded && Boolean(node.binding));
      element.classList.toggle("is-fault", live.fault);
      element.classList.toggle("is-degraded", live.degraded);
      element.classList.toggle("is-unbound", !node.binding);
      element.classList.toggle("is-selected", state.selected === node.id);
      const status = element.querySelector(".plant-status, .plant-value"), tag = element.querySelector(".plant-tag");
      if (status) status.textContent = live.text;
      if (tag) tag.textContent = live.tag;
      element.setAttribute("aria-label", node.label + (live.text ? " · " + live.text : "") + (live.tag ? " · " + live.tag : ""));
    });
  }
  function update(context) {
    if (!context || !context.index || (!state.roots && !initRoots())) return;
    state.context = context; state.model = modelMap(context);
    const views = context.index.views && context.index.views.length ? context.index.views : fallbackViews();
    const signature = JSON.stringify(views);
    if (signature !== state.signature) {
      state.signature = signature; state.views = views;
      if (!state.views.some(view => view.id === state.view)) state.view = (state.views.find(view => view.parent == null) || state.views[0]).id;
      state.selected = null; renderNavigation(); renderCanvas();
    } else updateLive();
  }
  window.DcsPlantViews = { update, selectArea, selectView: selectArea };
})();
