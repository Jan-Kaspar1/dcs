/* Reusable operator symbols consume code-authored views from the plant model.
 * Drawings carry geometry and model references; live state and commands remain
 * on the controller's existing telemetry and receipted command contracts. */
(function () {
  "use strict";
  const WIDTH = 1200, HEIGHT = 760;
  const SIZES = { pump: [160, 140], motor: [160, 140], valve: [160, 140],
    tank: [150, 195], measurement: [190, 100], label: [300, 50] };
  const state = { roots: null, context: null, model: null, views: [], signature: null,
    view: null, selected: null, bounds: null, elements: new Map() };

  function element(tag, attributes, text, namespace) {
    const node = namespace ? document.createElementNS(namespace, tag) : document.createElement(tag);
    Object.entries(attributes || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text != null) node.textContent = text;
    return node;
  }
  function svg(tag, attributes, text) {
    return element(tag, attributes, text, "http://www.w3.org/2000/svg");
  }
  function note(text) {
    if (!state.roots) return;
    state.roots.note.textContent = text;
    state.roots.note.hidden = !text;
  }
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
    window.addEventListener("resize", updateViewport);
    if (window.ResizeObserver) new window.ResizeObserver(updateViewport).observe(roots.canvas.parentElement);
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
      button.append(element("span", { class: "plant-area-count" }, view.nodes.length + " symbols"));
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
    } else if (node.symbol === "measurement") group.append(svg("path", { d: "M 30 66 H " + (width - 30), class: "plant-measurement-body" }));
    const labelY = node.symbol === "label" ? 26 : node.symbol === "tank" ? 143 : node.symbol === "measurement" ? 20 : 91;
    group.append(svg("text", { x, y: labelY, "text-anchor": "middle", class: "plant-label" }, node.label));
    if (node.symbol !== "label") {
      group.append(svg("text", { x, y: node.symbol === "tank" ? 162 : node.symbol === "measurement" ? 52 : 111,
        "text-anchor": "middle", class: node.symbol === "measurement" ? "plant-value" : "plant-status" }, ""));
      group.append(svg("text", { x, y: node.symbol === "measurement" ? 83 : node.symbol === "tank" ? 183 : 129,
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
  function pipePoints(pipe, nodes) {
    const fromNode = nodes.get(pipe.from.node), toNode = nodes.get(pipe.to.node);
    if (!fromNode || !toNode) return null;
    const from = portPosition(fromNode, pipe.from.port), to = portPosition(toNode, pipe.to.port);
    if (pipe.from.port === "e" || pipe.from.port === "w") {
      const middle = Math.round((from.x + to.x) / 2);
      return [from, { x: middle, y: from.y }, { x: middle, y: to.y }, to];
    }
    const middle = Math.round((from.y + to.y) / 2);
    return [from, { x: from.x, y: middle }, { x: to.x, y: middle }, to];
  }
  function pipePath(pipe, nodes) {
    const points = pipePoints(pipe, nodes);
    return points && points.map((point, index) => (index ? "L " : "M ") + point.x + " " + point.y).join(" ");
  }
  function viewBounds(view, nodes) {
    if (!nodes.size) return null;
    let left = WIDTH, top = HEIGHT, right = 0, bottom = 0;
    function include(x, y) { left = Math.min(left, x); top = Math.min(top, y); right = Math.max(right, x); bottom = Math.max(bottom, y); }
    nodes.forEach(node => {
      const [width, height] = SIZES[node.symbol];
      include(node.x, node.y); include(node.x + width, node.y + height);
    });
    (view.pipes || []).forEach(pipe => (pipePoints(pipe, nodes) || []).forEach(point => include(point.x, point.y)));
    // The drawing's geometry stays on the declared coordinate plane. Cropping
    // empty margins is a viewport choice and includes full symbols and pipes.
    const padding = 24;
    return { x: left - padding, y: top - padding, width: right - left + padding * 2, height: bottom - top + padding * 2 };
  }
  function updateViewport() {
    if (!state.roots) return;
    const canvas = state.roots.canvas, bounds = state.bounds;
    if (window.matchMedia("(max-width: 700px)").matches && bounds) {
      canvas.setAttribute("viewBox", [bounds.x, bounds.y, bounds.width, bounds.height].join(" "));
      // Preserve readable SVG text on narrow displays. Wider drawings pan in
      // the existing scroll container; individual area views still fit.
      canvas.style.minWidth = bounds.width + "px";
      canvas.style.minHeight = bounds.height + "px";
    } else {
      canvas.style.removeProperty("min-width");
      canvas.style.removeProperty("min-height");
      if (bounds) {
        // Crop unused drawing margins while keeping library glyphs at most
        // their authored size. A narrower workspace pans rather than making
        // equipment labels illegible when its faceplate opens.
        const width = Math.max(bounds.width, canvas.clientWidth || WIDTH);
        const height = Math.max(bounds.height, canvas.clientHeight || HEIGHT);
        canvas.setAttribute("viewBox", [bounds.x - (width - bounds.width) / 2,
          bounds.y - (height - bounds.height) / 2, width, height].join(" "));
        canvas.style.minWidth = bounds.width + "px";
        canvas.style.minHeight = bounds.height + "px";
      } else canvas.setAttribute("viewBox", "0 0 " + WIDTH + " " + HEIGHT);
    }
  }
  function renderCanvas() {
    const view = currentView(), canvas = state.roots.canvas;
    if (!view) return;
    state.roots.title.textContent = view.label;
    if (document.body.classList.contains("workspace-plant")) document.getElementById("plant-location").textContent = view.label;
    canvas.replaceChildren(); state.elements.clear();
    const defs = svg("defs"); canvas.append(defs);
    const nodes = new Map(view.nodes.filter(node => SIZES[node.symbol]).map(node => [node.id, node]));
    state.bounds = viewBounds(view, nodes);
    updateViewport();
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
      group.append(svg("rect", { width, height, rx: 8, class: "plant-node-hit" }));
      const clipId = "plant-clip-" + state.elements.size;
      const clip = svg("clipPath", { id: clipId }); clip.append(svg("rect", { width, height })); defs.append(clip);
      const drawing = svg("g", { "clip-path": "url(#" + clipId + ")" }); drawSymbol(node, drawing); group.append(drawing);
      if (interactive) {
        const indicator = svg("g", { transform: "translate(" + (width - 46) + " 2)", class: "plant-alarm-indicator", hidden: "" });
        indicator.append(svg("title", {}));
        indicator.append(svg("rect", { x: 0, y: 0, width: 44, height: 34, rx: 5, class: "plant-alarm-badge" }));
        indicator.append(svg("path", { class: "plant-alarm-priority-mark", "aria-hidden": "true" }));
        indicator.append(svg("text", { x: 28, y: 14, "text-anchor": "middle", class: "plant-alarm-badge-label" }));
        indicator.append(svg("text", { x: 22, y: 28, "text-anchor": "middle", class: "plant-alarm-badge-priority" }));
        group.append(indicator);
      }
      if (interactive) {
        group.addEventListener("click", () => openNode(node));
        group.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openNode(node); }
        });
      }
      state.elements.set(node.id, group); canvas.append(group);
    });
    updateLive();
    note("");
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
  function descriptorsFor(node, model = state.model) {
    const binding = node.binding;
    if (!binding) return [];
    const ids = binding.equipment != null ? (model.equipment.get(binding.equipment) || {}).components || []
      : binding.component != null ? [binding.component] : [];
    return ids.map(id => model.components.get(id)).filter(Boolean)
      .map(component => model.descriptors.get(component.name)).filter(Boolean);
  }
  function liveState(node, model = state.model, context = state.context) {
    const sample = point => (model.telemetry.get(point) || {}).sample || null;
    const binding = node.binding;
    if (!binding) return { text: "", tag: "", running: false, fault: false, degraded: false };
    if (context.stale) return { text: "Data stale", tag: "Offline", description: "Connection lost; current data is stale",
      running: false, fault: false, degraded: true };
    if (binding.point != null) {
      const reading = sample(binding.point), meta = model.points.get(binding.point);
      const current = value(reading);
      const text = typeof current === "number" && Number.isFinite(current)
        ? current.toLocaleString(undefined, { maximumFractionDigits: 3 }) : "—";
      const unit = text !== "—" && meta ? meta.unit || "" : "";
      return { text, unit, tag: good(reading) ? "" : quality(reading), running: false, fault: false, degraded: !good(reading) };
    }
    const descriptors = descriptorsFor(node, model), important = [];
    const actuators = descriptors.filter(descriptor => descriptor.kind === "motor" || /valve/.test(descriptor.kind));
    let running = null, command = null, fault = false, tripped = false, position = null, positionUnit = "";
    descriptors.forEach(descriptor => (descriptor.ports || []).forEach(port => {
      const reading = sample(port.point), current = value(reading), actuator = actuators.includes(descriptor);
      if (actuator && ["run", "out", "cmd", "fault", "position", "fb", "discrepancy"].includes(port.name)) important.push(reading);
      if (actuator && port.name === "run" && typeof current === "boolean") running = good(reading) ? current : null;
      if (actuator && port.name === "out" && typeof current === "boolean") command = good(reading) ? current : null;
      if (actuator && ["position", "fb"].includes(port.name) && typeof current === "number") {
        position = good(reading) ? current : null;
        positionUnit = (model.points.get(port.point) || {}).unit || "";
      }
      if (port.name === "fault" || port.name === "discrepancy") fault = fault || current === true;
      if (port.name === "tripped" && port.direction === "out") { tripped = tripped || current === true; important.push(reading); }
    }));
    const degraded = !descriptors.length || important.length === 0 || important.some(reading => !good(reading));
    let text = tripped ? "Tripped" : fault ? "Feedback fault" : degraded ? "Data uncertain"
      : running === true ? "Running" : running === false ? "Stopped" : position != null ? "Position " + position.toLocaleString(undefined, { maximumFractionDigits: 1 }) + (positionUnit ? " " + positionUnit : "")
      : command === true ? "On" : command === false ? "Off" : "Live";
    if (!fault && !tripped && !degraded) {
      if (running === true && command === false) text = "Stop requested";
      if (running === false && command === true) text = "Start requested";
    }
    let tag = degraded && (fault || tripped) ? "Data uncertain" : "";
    if (binding.equipment != null) {
      const equipment = model.equipment.get(binding.equipment);
      const mode = equipment && (equipment.controls || []).find(control => /mode/i.test(control.label || ""));
      if (mode) {
        const reading = sample(mode.point);
        if (good(reading) && typeof value(reading) === "boolean") tag += (tag ? " · " : "") + (value(reading) ? mode.true_label || "On" : mode.false_label || "Off");
      }
    }
    const description = tripped ? "Protection tripped" : fault ? "Actuator feedback fault" : text;
    return { text, tag, description, running: running === true && !degraded, fault: fault || tripped, degraded };
  }
  function alarmSummary(node) {
    const binding = node.binding;
    if (!binding || (binding.equipment == null && binding.component == null)) return null;
    const ids = new Set();
    if (binding.equipment != null) {
      ((state.model.equipment.get(binding.equipment) || {}).components || []).forEach(id => ids.add(id));
    } else {
      ids.add(binding.component);
      // A primitive glyph can be part of declared higher-level equipment.
      // Its related alarms follow that membership, never a guessed tag name.
      state.model.equipment.forEach(equipment => {
        if ((equipment.components || []).includes(binding.component)) equipment.components.forEach(id => ids.add(id));
      });
    }
    const names = new Set(Array.from(ids).map(id => (state.model.components.get(id) || {}).name).filter(Boolean));
    const owned = Array.from(new Map((state.context.alarmStates || [])
      .filter(alarm => names.has(alarm.name)).map(alarm => [alarm.name, alarm])).values());
    if (!owned.length) return null;
    const standing = owned.filter(alarm => alarm.active || alarm.unacknowledged);
    const active = standing.filter(alarm => alarm.active).length;
    const unacknowledged = standing.filter(alarm => alarm.unacknowledged).length;
    const returned = standing.filter(alarm => alarm.unacknowledged && !alarm.active).length;
    const managed = standing.filter(alarm => alarm.managed).length;
    const attention = standing.filter(alarm => alarm.unacknowledged && !alarm.managed).length;
    const fullyManaged = standing.length > 0 && managed === standing.length;
    const rank = alarm => Number.isInteger(alarm.priority) && alarm.priority >= 0 ? alarm.priority : Infinity;
    const highest = standing.slice().sort((a, b) => rank(a) - rank(b) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))[0];
    const priority = highest && rank(highest) !== Infinity ? highest.priority : null;
    const priorityLabel = priority == null ? "P?" : "P" + priority;
    const priorityClass = [1, 2, 3].includes(priority) ? "p" + priority : "other";
    const degraded = owned.some(alarm => alarm.degraded);
    const stale = Boolean(state.context.stale);
    // A primitive alarm may have no acknowledgment state. Only the declared
    // unacknowledged port establishes that an active alarm was acknowledged.
    const acknowledged = standing.length > 0 && unacknowledged === 0 && standing.every(alarm => {
      const descriptor = state.model.descriptors.get(alarm.name);
      return descriptor && (descriptor.ports || []).some(port => port.name === "unacknowledged" && port.direction === "out");
    });
    const label = priorityLabel + (standing.length ? "×" + standing.length : "");
    let detail = fullyManaged ? "MGD" : returned === standing.length && returned > 0 ? "RET/U"
      : unacknowledged ? "UNACK" : acknowledged ? "ACK" : active ? "ACTIVE" : "ALARM ?";
    if (stale) detail = "STALE";
    else if (degraded) detail = "DATA ?";
    let description = standing.length
      ? standing.length + (standing.length === 1 ? " alarm" : " alarms") + ": " + active + " active, " + unacknowledged + " unacknowledged"
        + (returned ? ", " + returned + " returned" : "") + (managed ? ", " + managed + " managed" : "")
        + "; highest priority " + priorityLabel
      : "No alarm asserted in the last readings";
    if (stale) description = "Last known alarm state; connection stale. " + description;
    else if (degraded) description += "; alarm data degraded, current state is uncertain";
    return { count: standing.length, active, unacknowledged, returned, managed, attention, fullyManaged, acknowledged, priority, priorityClass,
      degraded, stale, visible: standing.length > 0 || degraded || stale, label, detail, description };
  }
  function updateAlarmIndicator(node, element) {
    const indicator = element.querySelector(".plant-alarm-indicator");
    if (!indicator) return "";
    const alarms = alarmSummary(node);
    const visible = Boolean(alarms && alarms.visible);
    indicator.toggleAttribute("hidden", !visible);
    element.classList.toggle("has-alarm", Boolean(alarms && alarms.count));
    element.classList.toggle("has-active-alarm", Boolean(alarms && alarms.active));
    element.classList.toggle("has-unacknowledged-alarm", Boolean(alarms && alarms.unacknowledged));
    element.classList.toggle("is-unacknowledged", Boolean(alarms && alarms.unacknowledged));
    element.classList.toggle("is-acknowledged", Boolean(alarms && alarms.acknowledged));
    element.classList.toggle("is-managed-alarm", Boolean(alarms && alarms.fullyManaged));
    element.classList.toggle("has-attention-alarm", Boolean(alarms && alarms.attention));
    element.classList.toggle("has-degraded-alarm", Boolean(alarms && alarms.degraded));
    element.classList.toggle("has-stale-alarm", Boolean(alarms && alarms.stale));
    ["p1", "p2", "p3", "other"].forEach(priority => {
      const selected = Boolean(visible && alarms.priorityClass === priority);
      element.classList.toggle("alarm-" + priority, selected);
      indicator.classList.toggle(priority, selected);
      indicator.querySelector(".plant-alarm-badge").classList.toggle(priority, selected);
    });
    if (!visible) return "";
    // Priority remains distinguishable without color. Shapes are redundant
    // with the explicit P-code; lifecycle is kept on a separate text line.
    const shapes = { p1: "M 5 13 L 10 4 L 15 13 Z", p2: "M 10 3 L 15 8 L 10 13 L 5 8 Z",
      p3: "M 5 4 H 15 V 14 H 5 Z", other: "M 10 4 A 5 5 0 1 0 10 14 A 5 5 0 1 0 10 4" };
    indicator.querySelector(".plant-alarm-priority-mark").setAttribute("d", shapes[alarms.priorityClass]);
    indicator.querySelector(".plant-alarm-badge-label").textContent = alarms.label;
    indicator.querySelector(".plant-alarm-badge-priority").textContent = alarms.detail;
    indicator.querySelector("title").textContent = alarms.description;
    indicator.setAttribute("aria-label", alarms.description);
    return alarms.description;
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
      if (status) {
        status.textContent = live.text;
        if (live.unit) status.append(svg("tspan", { class: "plant-unit", dx: 5 }, live.unit));
      }
      if (tag) tag.textContent = live.tag;
      const alarmDescription = updateAlarmIndicator(node, element);
      const description = node.label + (live.description || live.text ? " · " + (live.description || live.text) : "") + (live.unit ? " " + live.unit : "") + (live.tag ? " · " + live.tag : "")
        + (alarmDescription ? " · " + alarmDescription : "");
      element.setAttribute("aria-label", description);
      element.querySelector("title").textContent = description;
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
  window.DcsPlantViews = { update, selectArea, selectView: selectArea,
    equipmentState(context, id) {
      return liveState({ binding: { equipment: id } }, modelMap(context), context);
    }
  };
})();
