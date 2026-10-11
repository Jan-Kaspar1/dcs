/* Review selection is local presentation only; it never activates a plant. */
(function () {
  "use strict";
  const options = {
    precision: ["01 / Precision", "A crisp operator console: square edges, thin rules, tightly aligned values and a compact inspector.", "Best for scan speed and frequent operation. The most restrained of the five."],
    civic: ["02 / Civic", "A softer utility workspace: warm whites, teal selection, rounded sections and generous controls.", "Best for a calm, approachable product. Uses more space than Precision."],
    drafting: ["03 / Drafting", "An engineering sheet: cool white, fine grid, navy linework and monospaced instrument values.", "Best for a visibly code-first engineering identity. More technical in character."],
    editorial: ["04 / Editorial", "A quiet, typographic workspace: open white space, large headings, prominent values and rule-based panels.", "Best for a distinctive polished product. The least dense of the five."],
    instrument: ["05 / Instrument", "A structured instrument deck: compact chrome, clear readout sections, substantial gauges and a layered inspector.", "Best for comparing process values and actuator feedback. More visual structure than Editorial."]
  };
  const frame = document.getElementById("operator-preview");
  const initial = new URLSearchParams(location.search).get("style");
  let selected = Object.hasOwn(options, initial) ? initial : "precision";
  function choose(style) {
    if (!Object.hasOwn(options, style)) return;
    selected = style;
    document.querySelectorAll("[data-style]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.style === style)));
    const [title, description, tradeoff] = options[style];
    document.getElementById("style-title").textContent = title;
    document.getElementById("style-description").textContent = description;
    document.getElementById("style-tradeoff").textContent = tradeoff;
    document.getElementById("open-preview").href = "/operator?style=" + style;
    frame.title = options[style][0] + " operator design preview";
    if (frame.contentDocument?.body) frame.contentDocument.body.dataset.reviewStyle = style;
    history.replaceState(null, "", "?style=" + style);
  }
  document.querySelectorAll("[data-style]").forEach(button => button.addEventListener("click", () => choose(button.dataset.style)));
  document.querySelectorAll("[data-viewport]").forEach(button => {
    if (button.tagName !== "BUTTON") return;
    button.addEventListener("click", () => {
      const viewport = button.dataset.viewport;
      document.querySelector(".preview-stage").dataset.viewport = viewport;
      document.querySelectorAll("button[data-viewport]").forEach(choice => choice.setAttribute("aria-pressed", String(choice.dataset.viewport === viewport)));
    });
  });
  frame.addEventListener("load", () => choose(selected));
  choose(selected);
})();
