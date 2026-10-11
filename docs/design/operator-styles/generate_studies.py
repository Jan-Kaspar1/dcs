#!/usr/bin/env python3
"""Render five static operator design studies from one real model/snapshot.

This is review artwork, never a second controller or operator application.
The drawing uses the model's node coordinates, bindings and pipe endpoints.
Full captured source documents are embedded in SVG metadata for reproducibility.
No telemetry history, command receipt or plant behavior is synthesized.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET

NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)
SIZES = {
    "pump": (160, 140), "motor": (160, 140), "valve": (160, 140),
    "tank": (150, 195), "measurement": (190, 100), "label": (300, 50),
}
STYLES = {
    "precision": dict(name="Precision", tagline="A focused, compact operator console",
        bg="#f3f5f6", ink="#22303b", muted="#586775", line="#d9e0e5",
        accent="#315d8f", soft="#eaf0f6", radius=2, nav=168, panel=314,
        font="DejaVu Sans, sans-serif", body=13, title=25, value=27, chrome=54),
    "civic": dict(name="Civic", tagline="Calm, approachable water operations",
        bg="#f5f6f2", ink="#263b39", muted="#5d716c", line="#dce5df",
        accent="#186c68", soft="#e8f0eb", radius=13, nav=190, panel=336,
        font="DejaVu Sans, sans-serif", body=14, title=26, value=28, chrome=66),
    "drafting": dict(name="Drafting", tagline="An engineering drawing brought into operation",
        bg="#f6f9fc", ink="#253b51", muted="#5e7286", line="#d9e3ed",
        accent="#345d91", soft="#eaf1f8", radius=0, nav=168, panel=314,
        font="DejaVu Sans Mono, monospace", body=12, title=24, value=25, chrome=54),
    "editorial": dict(name="Editorial", tagline="White space, strong type, clear process hierarchy",
        bg="#ffffff", ink="#252b30", muted="#63717b", line="#e0e4e7",
        accent="#405ecc", soft="#f0f3fc", radius=0, nav=176, panel=326,
        font="DejaVu Sans, sans-serif", body=14, title=29, value=29, chrome=68),
    "instrument": dict(name="Instrument", tagline="A compact deck of precise operating instruments",
        bg="#eef2f6", ink="#27374a", muted="#5c7083", line="#d8e1eb",
        accent="#2e5da8", soft="#e8eff9", radius=6, nav=168, panel=326,
        font="DejaVu Sans, sans-serif", body=13, title=25, value=26, chrome=54),
}


def element(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, f"{{{NS}}}{tag}", {
        key.replace("_", "-"): str(value) for key, value in attrs.items()
        if value is not None
    })
    if text is not None:
        node.text = str(text)
    return node


class Drawing:
    def __init__(self, style, source):
        self.s = style
        self.root = ET.Element(f"{{{NS}}}svg", {
            "width": "1400", "height": "880", "viewBox": "0 0 1400 880",
            "role": "img", "aria-labelledby": "study-title study-description",
        })
        element(self.root, "title", f"{style['name']} — static DCS operator design study", id="study-title")
        element(self.root, "desc", "Read-only visual study using the captured water-area model and "
            "snapshot. Controls are static illustrations. No process action is sent.", id="study-description")
        element(self.root, "metadata", json.dumps(source, ensure_ascii=False, separators=(",", ":")))
        self.g = element(self.root, "g", font_family=style["font"], fill=style["ink"])
        self.defs = element(self.root, "defs")

    def rect(self, x, y, w, h, fill=None, stroke=None, radius=None, parent=None, **attrs):
        return element(parent if parent is not None else self.g, "rect", x=x, y=y, width=w, height=h,
            rx=self.s["radius"] if radius is None else radius,
            fill=fill or "none", stroke=stroke, **attrs)

    def text(self, x, y, value, size=None, fill=None, weight=400, anchor=None, parent=None, **attrs):
        return element(parent if parent is not None else self.g, "text", value, x=x, y=y,
            font_size=size or self.s["body"], fill=fill or self.s["ink"],
            font_weight=weight, text_anchor=anchor, **attrs)

    def line(self, x1, y1, x2, y2, color=None, width=1, parent=None, **attrs):
        return element(parent if parent is not None else self.g, "line", x1=x1, y1=y1, x2=x2, y2=y2,
            stroke=color or self.s["line"], stroke_width=width, **attrs)

    def path(self, value, color=None, width=2, fill="none", parent=None, **attrs):
        return element(parent if parent is not None else self.g, "path", d=value, fill=fill,
            stroke=color or self.s["ink"], stroke_width=width, **attrs)


def raw_value(sample):
    return next(iter(sample.get("value", {}).values()), None) if sample else None


def quality(sample):
    current = sample.get("quality") if sample else None
    if isinstance(current, str):
        return current.capitalize()
    if isinstance(current, dict):
        return " · ".join(str(v) for pair in current.items() for v in pair)
    return "No sample"


def number(value):
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (float, int)):
        return f"{value:.3f}" if isinstance(value, float) else str(value)
    return "—"


def port_position(node, port):
    width, height = SIZES[node["symbol"]]
    if node["symbol"] == "tank":
        return (node["x"] + (width - 18 if port == "e" else 18 if port == "w" else width / 2),
            node["y"] + (20 if port == "n" else 120 if port == "s" else 58))
    if node["symbol"] in ("pump", "motor", "valve"):
        return (node["x"] + (width - 15 if port == "e" else 15 if port == "w" else width / 2),
            node["y"] + (9 if port == "n" else 71 if port == "s" else 40))
    return (node["x"] + (width if port == "e" else 0 if port == "w" else width / 2),
        node["y"] + (0 if port == "n" else height if port == "s" else height / 2))


def route(pipe, nodes):
    a = port_position(nodes[pipe["from"]["node"]], pipe["from"]["port"])
    b = port_position(nodes[pipe["to"]["node"]], pipe["to"]["port"])
    if pipe["from"]["port"] in ("e", "w"):
        middle = int((a[0] + b[0]) / 2 + 0.5)
        points = (a, (middle, a[1]), (middle, b[1]), b)
    else:
        middle = int((a[1] + b[1]) / 2 + 0.5)
        points = (a, (a[0], middle), (b[0], middle), b)
    return " ".join(f"{'M' if n == 0 else 'L'} {x:g} {y:g}" for n, (x, y) in enumerate(points))


class Model:
    def __init__(self, index, snapshot):
        self.index, self.snapshot = index, snapshot
        self.points = {p["point"]: p for p in index["points"]}
        self.samples = {p["point"]: p["sample"] for p in snapshot["points"]}
        self.equipment = {e["id"]: e for e in index["equipment"]}
        components = {c["id"]: c for c in index["components"]}
        descriptors = {d["name"]: d for d in snapshot["descriptors"]}
        self.members = {e["id"]: [descriptors[components[c]["name"]] for c in e["components"]]
            for e in index["equipment"]}
        self.parameters = {p["name"]: p["values"] for p in snapshot["parameters"]}
        self.alarms = []
        for d in snapshot["descriptors"]:
            ports = {p["name"]: p for p in d["ports"]}
            if "alarm" not in ports:
                continue
            flags = {name: raw_value(self.samples.get(port["point"])) is True
                for name, port in ports.items() if port["kind"] == "bool"}
            owner = next((e for e in index["equipment"] if d in self.members[e["id"]]), None)
            self.alarms.append(dict(name=d["name"], point=ports["alarm"]["point"], owner=owner,
                active=flags.get("alarm", False), unacknowledged=flags.get("unacknowledged", False),
                priority=raw_value(dict(value=self.parameters[d["name"]].get("priority", {}))),
                managed=any(flags.get(n) for n in ("shelved", "suppressed", "out_of_service"))))

    def ports(self, equip, kind, name):
        return [p for d in self.members[equip] if d["kind"] == kind for p in d["ports"] if p["name"] == name]

    def sample(self, point):
        return self.samples.get(point)

    def state(self, equip):
        e = self.equipment[equip]
        if e["kind"] in ("analog-measurement", "control-loop", "pumping-station"):
            point = e["points"][0]
            return f"{number(raw_value(self.sample(point)))} {self.points[point]['unit']}", quality(self.sample(point))
        flags = {p["name"]: raw_value(self.sample(p["point"])) for d in self.members[equip]
            for p in d["ports"] if p["name"] in ("tripped", "fault", "discrepancy")}
        if any(flags.values()):
            return ("Protection tripped" if flags.get("tripped") else "Feedback fault"), "Inspect equipment"
        actuator = next(d for d in self.members[equip] if d["kind"] in ("motor", "valve"))
        ports = {p["name"]: p["point"] for p in actuator["ports"]}
        if e["kind"] == "modulating-valve":
            point = ports["fb"]
            text = f"Position {number(raw_value(self.sample(point)))} {self.points[point]['unit']}"
        else:
            running = raw_value(self.sample(ports["run"])) is True
            text = ("Open" if running else "Closed / not open") if e["kind"] == "on-off-valve" else ("Running" if running else "Stopped")
        mode = next((c for c in e["controls"] if c["role"] == "mode"), None)
        tag = (mode["true_label"] if raw_value(self.sample(mode["point"])) else mode["false_label"]) if mode else "Good"
        return text, tag


def gauge(d, parent, node, model, x, y, width, instrument=False):
    bounds = node.get("display") or model.points[node["binding"]["point"]].get("display")
    if not bounds:
        return
    value = raw_value(model.sample(node["binding"]["point"]))
    scale = lambda n: x + width * (n - bounds["min"]) / (bounds["max"] - bounds["min"])
    d.rect(x, y, width, 6 if instrument else 4, d.s["line"], radius=1, parent=parent)
    band = bounds.get("normal")
    if band and band[0] != band[1]:
        d.rect(scale(band[0]), y - 1, scale(band[1]) - scale(band[0]), 8 if instrument else 6,
            "#aab8be", radius=1, parent=parent)
    if isinstance(value, (float, int)):
        px = scale(max(bounds["min"], min(bounds["max"], value)))
        d.line(px, y - 5, px, y + 9, d.s["ink"], 2, parent=parent)
    d.text(x, y + 24, f"{bounds['min']:g}", 12, d.s["muted"], parent=parent)
    d.text(x + width, y + 24, f"{bounds['max']:g}", 12, d.s["muted"], anchor="end", parent=parent)


def schematic(d, key, view, model, x, y, width, height):
    # Preserve the model coordinate plane and exact production port/route rules.
    group = element(d.g, "g", data_view=view["id"])
    scale = min(width / 1230, height / 700)
    origin_x = x + (width - 1230 * scale) / 2
    origin_y = y + (height - 700 * scale) / 2
    scene = element(group, "g", transform=f"translate({origin_x:g} {origin_y:g}) scale({scale:g}) translate(10 -85)")
    nodes = {n["id"]: n for n in view["nodes"]}
    for pipe in view["pipes"]:
        path = d.path(route(pipe, nodes), "#84939f" if key != "drafting" else "#738aa0",
            3 if key in ("civic", "instrument") else 2.4, parent=scene,
            stroke_linejoin="round", stroke_linecap="round",
            data_from=f"{pipe['from']['node']}:{pipe['from']['port']}",
            data_to=f"{pipe['to']['node']}:{pipe['to']['port']}")
        element(path, "title", json.dumps(pipe))
    for node in view["nodes"]:
        symbol, binding = node["symbol"], node.get("binding") or {}
        w, _ = SIZES[symbol]
        cx = w / 2
        g = element(scene, "g", transform=f"translate({node['x']} {node['y']})",
            data_node=node["id"], data_binding=json.dumps(binding, separators=(",", ":")))
        element(g, "title", node["label"] + " | " + json.dumps(binding))
        color = d.s["ink"]
        if node["id"] == "modulating":
            d.rect(0, -4, 160, 145, d.s["soft"], d.s["accent"], parent=g,
                stroke_width=1.5, stroke_dasharray="4 4" if key == "drafting" else None)
            color = d.s["accent"]
        if symbol in ("pump", "motor"):
            d.line(cx - 65, 40, cx + 65, 40, color, 2, parent=g)
            text, tag = model.state(binding["equipment"])
            fill = "#fff" if text == "Running" else d.s["line"]
            element(g, "circle", cx=cx, cy=40, r=31, fill=fill, stroke=color, stroke_width=2)
            if symbol == "pump":
                d.path(f"M {cx-14} 22 L {cx+19} 40 L {cx-14} 58 Z", color, 0, color, parent=g)
            else:
                d.text(cx, 48, "M", 25, color, anchor="middle", parent=g)
        elif symbol == "valve":
            d.line(cx - 65, 40, cx + 65, 40, color, 2, parent=g)
            d.path(f"M {cx-29} 17 L {cx+29} 63 L {cx+29} 17 L {cx-29} 63 Z", color, 2, "#fff", parent=g)
            d.path(f"M {cx} 17 V 5 M {cx-12} 5 H {cx+12}", color, 2, parent=g)
            text, tag = model.state(binding["equipment"])
        elif symbol == "tank":
            if key in ("precision", "drafting"):
                d.path("M 18 20 H 132 V 110 Q 75 130 18 110 Z", color, 2, "#fff", parent=g)
            else:
                d.path("M 18 20 L 18 110 Q 75 130 132 110 L 132 20 M 18 20 Q 75 0 132 20 M 18 20 Q 75 40 132 20", color, 2, "#fff", parent=g)
            text, tag = model.state(binding["equipment"])
        elif symbol == "measurement":
            point = binding["point"]
            sample = model.sample(point)
            unit = model.points[point]["unit"]
            if key == "instrument":
                d.rect(8, -5, w - 16, 97, "#f7f9fc", d.s["line"], parent=g)
            d.text(cx, 17, node["label"], 16, d.s["muted"], anchor="middle", parent=g,
                font_family="DejaVu Sans Mono, monospace" if key == "drafting" else None)
            value = d.text(cx, 48, f"{number(raw_value(sample))} {unit}", d.s["value"], weight=600,
                anchor="middle", parent=g, font_family="DejaVu Sans Mono, monospace" if key == "drafting" else None)
            element(value, "title", f"Point {point}: {json.dumps(sample)}; unit {unit}")
            gauge(d, g, node, model, 24, 62, 142, key == "instrument")
            d.text(cx, 86, quality(sample), 12, d.s["muted"], anchor="middle", parent=g)
            continue
        elif symbol == "label":
            d.text(cx, 40 if node["id"] == "balance-spill-route" else 25,
                node["label"], 17, d.s["muted"], anchor="middle", parent=g)
            continue
        ly = 145 if symbol == "tank" else 92
        d.text(cx, ly, node["label"], 18, weight=600, anchor="middle", parent=g)
        d.text(cx, ly + 24, text, 16, anchor="middle", parent=g)
        d.text(cx, ly + 44, tag, 13, d.s["muted"], anchor="middle", parent=g)


def inspector(d, key, model, x, y, width):
    s = d.s
    equip = model.equipment["lv201"]
    if key == "civic":
        x, y, width = x + 12, y + 16, width - 24
        d.rect(x + 2, y + 4, width, 880 - y - 26, "#dae2dc", radius=15, fill_opacity=.55)
    inset = 24 if key in ("civic", "editorial") else 20
    px, right = x + inset, x + width - inset
    d.rect(x, y, width, 880 - y - 26, "#f8fafb" if key == "precision" else "#fff",
        s["line"], radius=13 if key == "civic" else 0)
    d.text(px, y + 29, "EQUIPMENT", 11, s["muted"], weight=600, letter_spacing=1.1)
    label = equip["label"].split(" · ")
    d.text(px, y + 62, label[0], 32 if key == "editorial" else 22 if key == "precision" else 24,
        weight=700, font_family="DejaVu Serif, serif" if key == "editorial" else None)
    d.text(px, y + 86, label[1], 13, s["muted"])
    state, mode_text = model.state("lv201")
    sy = y + 110
    if key not in ("editorial", "precision"):
        d.rect(px, sy, right - px, 73, s["soft"], radius=s["radius"])
    else:
        d.line(px, sy, right, sy, s["ink"], 2)
    d.text(px + 12, sy + 22, "ACTUAL FEEDBACK", 10, s["muted"], weight=600)
    d.text(px + 12, sy + 49, state, 21 if key == "editorial" else 17 if key == "precision" else 19, weight=600)
    d.text(right - 10, sy + 66, "Good", 11, s["muted"], anchor="end")
    yy = sy + 106
    for control in equip["controls"]:
        point = control["point"]
        sample = model.sample(point)
        d.text(px, yy, control["label"], 12, s["muted"], weight=500)
        if "false_label" in control:
            by = yy + 13
            gap = 4
            cw = (right - px - gap) / 2
            current = raw_value(sample)
            for i, (value, label) in enumerate(((False, control["false_label"]), (True, control["true_label"]))):
                selected = current is value
                d.rect(px + i * (cw + gap), by, cw, 32 if key == "precision" else 38, s["accent"] if selected else "#fff",
                    s["accent"] if selected else s["line"], radius=s["radius"])
                d.text(px + i * (cw + gap) + cw / 2, by + (21 if key == "precision" else 24), label, 12,
                    "#fff" if selected else s["muted"], weight=600, anchor="middle")
            yy += 76 if key == "precision" else 84
        else:
            by = yy + 14
            d.rect(px, by, right - px - 76, 32 if key == "precision" else 40, "#fff", s["line"])
            d.text(px + 12, by + (22 if key == "precision" else 26), f"{number(raw_value(sample))} {model.points[point]['unit']}", 17, weight=600)
            d.rect(right - 68, by, 68, 32 if key == "precision" else 40, s["soft"], s["line"])
            d.text(right - 34, by + (21 if key == "precision" else 25), "Apply", 12, s["muted"], anchor="middle", weight=600)
            bounds = control.get("limits")
            if bounds:
                low = next(iter(bounds["min"].values()))
                high = next(iter(bounds["max"].values()))
                d.text(px, by + 61, f"Declared request limits {low:g}–{high:g} {model.points[point]['unit']}", 11, s["muted"])
            d.text(px, by + 81, "Stored request; used in Manual mode", 11, s["muted"])
            yy += 116 if key == "precision" else 124
    d.line(px, yy - 9, right, yy - 9)
    d.text(px, yy + 16, "OUTPUT & FEEDBACK", 11, s["muted"], weight=600, letter_spacing=.8)
    manual_out = model.ports("lv201", "manual-station", "out")[0]["point"]
    applied = model.ports("lv201", "valve", "out")[0]["point"]
    feedback = model.ports("lv201", "valve", "fb")[0]["point"]
    yy += 43
    for label, point in (("Requested output", manual_out), ("Applied output", applied), ("Actual feedback", feedback)):
        sample = model.sample(point)
        d.text(px, yy, label, 12, s["muted"])
        d.text(right, yy, f"{number(raw_value(sample))} {model.points[point]['unit']}", 15, weight=600, anchor="end")
        d.text(right, yy + 17, quality(sample), 10, s["muted"], anchor="end")
        if key == "instrument":
            bounds = model.points[point].get("display")
            if bounds:
                val = raw_value(sample)
                d.rect(px, yy + 12, 84, 4, s["line"], radius=1)
                d.rect(px, yy + 12, max(0, min(84, 84 * val / bounds["max"])), 4, s["accent"], radius=1)
        d.line(px, yy + 26, right, yy + 26)
        yy += 47
    trip = model.ports("lv201", "interlock", "tripped")[0]["point"]
    tripped = raw_value(model.sample(trip))
    d.text(px, yy + 8, "PROTECTION", 11, s["muted"], weight=600, letter_spacing=.8)
    d.text(px, yy + 31, "Output blocked" if tripped else "No protection trip asserted", 12, weight=500)
    d.text(px, yy + 56, "Static study — no command submitted", 11, s["muted"])
    d.text(px, yy + 76, "Receipts belong to live operator control.", 11, s["muted"])


def study(key, model, source):
    s = STYLES[key]
    d = Drawing(s, source)
    view = model.index["views"][0]
    nav, panel, chrome = s["nav"], s["panel"], s["chrome"]
    rail = 76 if key in ("civic", "editorial") else 66
    top = chrome + rail
    d.rect(0, 0, 1400, 880, s["bg"], radius=0)
    d.rect(0, 0, 1400, chrome, "#fff", radius=0)
    d.line(0, chrome, 1400, chrome)
    d.rect(22, 17 if chrome == 54 else 23, 26, 24, s["soft"], radius=s["radius"])
    d.path(f"M 27 {chrome / 2 + 4:g} V {chrome / 2-5:g} H 43 V {chrome / 2+4:g} Z M 31 {chrome/2-5:g} V {chrome/2-9:g}", s["accent"], 1.5)
    d.text(58, chrome / 2 + 5, "DCS", 19, weight=700)
    d.text(nav + 24, chrome / 2 + 4, "WATER OPERATIONS", 11, s["muted"], weight=500, letter_spacing=1)
    d.text(1376, chrome / 2 + 4, "STATIC DESIGN STUDY · " + s["name"], 11, s["accent"], weight=600, anchor="end")
    d.rect(0, chrome, 1400, rail, "#fafbfc" if key != "civic" else "#f2f5ef", radius=0)
    d.line(0, top, 1400, top)
    active = sum(a["active"] for a in model.alarms)
    unack = sum(a["unacknowledged"] for a in model.alarms)
    d.text(22, chrome + 27, f"{unack} unacknowledged", 13, weight=600)
    d.text(22, chrome + 46, f"{active} active · Alarm management", 11, s["muted"])
    alarms = [a for a in model.alarms if a["unacknowledged"] or a["active"]]
    for i, alarm in enumerate(alarms[:3]):
        ax = 225 + i * 330
        d.rect(ax, chrome + 11, 314, rail - 22, "#fff", s["line"])
        d.rect(ax, chrome + 11, 3, rail - 22, "#906918", radius=0)
        priority = f"P{alarm['priority']}" if alarm["priority"] is not None else "P?"
        d.rect(ax + 12, chrome + 21, 29, 24, "#f7e7b8", radius=3)
        d.text(ax + 26.5, chrome + 37, priority, 11, "#765612", weight=700, anchor="middle")
        owner = alarm["owner"]["label"] if alarm["owner"] else alarm["name"]
        d.text(ax + 53, chrome + 29, owner, 12, weight=600)
        d.text(ax + 53, chrome + 46, "Active" if alarm["active"] else "Returned · unacknowledged", 11, s["muted"])
    if not alarms:
        d.text(225, chrome + 37, "No standing alarms in captured snapshot", 13, s["muted"])
    dark_nav = key == "instrument"
    nav_muted = "#a6b7ce" if dark_nav else s["muted"]
    nav_ink = "#edf3fc" if dark_nav else s["ink"]
    nav_selected = "#304968" if dark_nav else s["soft"]
    d.rect(0, top, nav, 880 - top, "#1c2e46" if dark_nav else "#fff", radius=0)
    d.line(nav, top, nav, 880)
    d.text(22, top + 32, "PLANT AREAS", 10, nav_muted, weight=600, letter_spacing=.9)
    d.rect(12, top + 47, nav - 24, 60 if key == "civic" else 52, nav_selected)
    d.rect(12, top + 47, 3, 60 if key == "civic" else 52, "#97b9ee" if dark_nav else s["accent"], radius=0)
    # Area label is split for its actual declared navigation label.
    parts = view["label"].split(" · ")
    d.text(24, top + 69, parts[0], 12, nav_ink if dark_nav else s["accent"], weight=600)
    d.text(24, top + 87, parts[1], 10, nav_muted)
    d.text(22, top + 145, "WORKSPACE", 10, nav_muted, weight=600, letter_spacing=.9)
    for i, (label, selected) in enumerate((("Schematic", True), ("Alarms", False))):
        yy = top + 160 + i * 42
        if selected:
            d.rect(12, yy, nav - 24, 34, nav_selected)
        nav_color = nav_ink if dark_nav and selected else s["accent"] if selected else nav_muted
        d.text(25, yy + 22, "◇" if selected else "△", 15, nav_color)
        d.text(48, yy + 22, label, 12, nav_color, weight=600 if selected else 400)
    d.line(22, 764, nav - 22, 764, "#3b4f69" if dark_nav else s["line"])
    d.text(22, 791, "SNAPSHOT", 10, nav_muted, weight=600, letter_spacing=1)
    d.text(22, 813, f"Tick {model.snapshot['tick']}", 12, nav_ink)
    d.text(22, 833, "Read-only design study", 10, nav_muted)
    px = nav + 24
    content_width = 1400 - nav - panel - 48
    d.text(px, top + 32, s["name"].upper(), 10, s["muted"], weight=600, letter_spacing=1.2)
    d.text(px, top + 69, "Water station", s["title"], weight=650,
        font_family="DejaVu Serif, serif" if key == "editorial" else None)
    d.text(px, top + 93, "Balance and discharge · " + s["tagline"], 12, s["muted"])
    cy = top + 119
    ch = 880 - cy - 64
    if key != "editorial":
        d.rect(px, cy, content_width, ch, "#fff", s["line"])
    else:
        d.line(px, cy, px + content_width, cy, s["ink"], 1.5)
    if key == "drafting":
        for gx in range(int(px + 12), int(px + content_width), 28):
            d.line(gx, cy + 1, gx, cy + ch - 1, "#eff3f8", .8)
        for gy in range(int(cy + 12), int(cy + ch), 28):
            d.line(px + 1, gy, px + content_width - 1, gy, "#eff3f8", .8)
    instrument_height = 72 if key == "instrument" else 0
    if instrument_height:
        readings = [next(n for n in view["nodes"] if n["id"] == identity)
            for identity in ("balance-reading", "setpoint", "discharge")]
        cell_width = (content_width - 40) / 3
        for i, node in enumerate(readings):
            bx = px + 12 + i * (cell_width + 8)
            point = node["binding"]["point"]
            d.rect(bx, cy + 12, cell_width, 57, s["soft"], s["line"])
            d.text(bx + 12, cy + 30, node["label"], 10, s["muted"], weight=500)
            d.text(bx + 12, cy + 55, f"{number(raw_value(model.sample(point)))} {model.points[point]['unit']}", 20, weight=650)
            d.text(bx + cell_width - 10, cy + 54, quality(model.sample(point)), 10, s["muted"], anchor="end")
    schematic(d, key, view, model, px + 8, cy + 8 + instrument_height,
        content_width - 16, ch - 16 - instrument_height)
    inspector(d, key, model, 1400 - panel, top, panel)
    d.text(px, 837, "Normal band uses declared display bounds · values rounded to 0.001", 10, s["muted"])
    d.text(px, 856, source["captured_at"] + " · snapshot captured, not live telemetry", 10, s["muted"])
    return d.root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://192.168.178.107:9081")
    parser.add_argument("--from-study", type=Path,
        help="replay the complete source metadata embedded in a standalone study SVG")
    parser.add_argument("--index", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--captured-at", help="UTC timestamp when replaying captured source")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "assets")
    args = parser.parse_args()
    if args.from_study and (args.index or args.snapshot or args.captured_at is not None):
        parser.error("--from-study cannot be combined with --index, --snapshot or --captured-at")
    if bool(args.index) != bool(args.snapshot):
        parser.error("--index and --snapshot must be supplied together")
    if args.from_study:
        try:
            metadata = ET.parse(args.from_study).getroot().find(f"{{{NS}}}metadata")
            source = json.loads(metadata.text if metadata is not None else "null")
        except (OSError, ET.ParseError, json.JSONDecodeError) as error:
            parser.error(f"cannot read embedded study metadata: {error}")
        required = ("index", "snapshot", "captured_at", "index_sha256", "snapshot_sha256")
        if not isinstance(source, dict) or not all(field in source for field in required):
            parser.error("--from-study requires a standalone SVG with complete source metadata")
        if not isinstance(source["index"], dict) or not isinstance(source["snapshot"], dict):
            parser.error("embedded index and snapshot must be objects")
        index, snapshot, captured = source["index"], source["snapshot"], source["captured_at"]
        # These retained hashes identify the original capture/replay inputs.
        # Re-encoding the embedded objects would not recover their raw bytes.
    else:
        if args.index:
            if not args.captured_at:
                parser.error("--captured-at is required with replay files")
            index_bytes, snapshot_bytes = args.index.read_bytes(), args.snapshot.read_bytes()
        else:
            with urllib.request.urlopen(args.url.rstrip("/") + "/signals", timeout=10) as response:
                index_bytes = response.read()
            with urllib.request.urlopen(args.url.rstrip("/") + "/snapshot", timeout=10) as response:
                snapshot_bytes = response.read()
        captured = args.captured_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        index, snapshot = json.loads(index_bytes), json.loads(snapshot_bytes)
        source = dict(kind="static-operator-design-study", source_url=args.url,
            captured_at=captured, index_sha256=hashlib.sha256(index_bytes).hexdigest(),
            snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
            rounding="Displayed numeric values round to 0.001; original samples are embedded below.",
            index=index, snapshot=snapshot)
    if not any(e["id"] == "lv201" for e in index.get("equipment", [])):
        parser.error("the source must declare the connected water area and LV-201")
    model = Model(index, snapshot)
    args.output.mkdir(parents=True, exist_ok=True)
    roots = []
    for key in STYLES:
        root = study(key, model, source)
        ET.ElementTree(root).write(args.output / f"{key}.svg", encoding="utf-8", xml_declaration=True)
        roots.append((key, root))
    sheet = ET.Element(f"{{{NS}}}svg", dict(width="2800", height="3020", viewBox="0 0 2800 3020", role="img"))
    element(sheet, "title", "Five original light DCS operator design studies")
    element(sheet, "metadata", json.dumps({k: v for k, v in source.items() if k not in ("index", "snapshot")}, ensure_ascii=False))
    element(sheet, "rect", x=0, y=0, width=2800, height=3020, fill="#edf0f3")
    element(sheet, "text", "Choose a visual direction", x=40, y=49, font_family="DejaVu Sans, sans-serif", font_size=32, font_weight=700, fill="#22303b")
    element(sheet, "text", "Five static studies · one actual water-area model and captured snapshot · production unchanged", x=40, y=82, font_family="DejaVu Sans, sans-serif", font_size=19, fill="#5b6975")
    for i, (key, root) in enumerate(roots):
        x = (i % 2) * 1400
        y = 118 + (i // 2) * 960
        element(sheet, "text", f"{i + 1:02d} / {STYLES[key]['name']}", x=x + 24, y=y + 32, font_family="DejaVu Sans, sans-serif", font_size=24, font_weight=700, fill="#22303b")
        nested = element(sheet, "svg", x=x, y=y + 48, width=1400, height=880, viewBox="0 0 1400 880")
        for child in root:
            if child.tag.rsplit("}", 1)[-1] not in ("metadata", "title", "desc"):
                nested.append(copy.deepcopy(child))
    x, y = 1440, 2150
    for j, line in enumerate(("The comparison is about presentation.", "Every pipe retains its model-declared endpoints.",
            "Readings, units, quality and alarms share one snapshot.", "No trends, command outcomes or telemetry are invented.",
            "Static controls send no plant commands.", "Choose a style before changing production.")):
        element(sheet, "text", line, x=x, y=y + j * 47, font_family="DejaVu Sans, sans-serif", font_size=23 if j == 0 else 18,
            font_weight=700 if j == 0 else 400, fill="#22303b" if j == 0 else "#5b6975")
    ET.ElementTree(sheet).write(args.output / "contact-sheet.svg", encoding="utf-8", xml_declaration=True)
    print(f"Rendered five SVG studies and contact sheet from tick {snapshot['tick']} at {captured}")


if __name__ == "__main__":
    main()
