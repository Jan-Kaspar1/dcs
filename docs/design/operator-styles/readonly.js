/* This guard is loaded before any production script, including automatic ack
 * releases. The proxy independently refuses every mutation. Nothing here
 * represents a controller command outcome or changes the production contract. */
(function () {
  "use strict";
  const originalFetch = window.fetch.bind(window);
  window.fetch = function (input, options) {
    const request = input instanceof Request ? input : null;
    const method = String(options?.method || request?.method || "GET").toUpperCase();
    const url = new URL(request ? request.url : String(input), location.href);
    if (method !== "GET" || url.origin !== location.origin) {
      return Promise.reject(new Error("Read-only design preview: request was not sent to the controller"));
    }
    return originalFetch(input, options);
  };
  const writes = "button.write, button.force, button.unforce, button.ack, button.managed, button.tune, button.invoke, button.switch, button.equipment-write, #command-form button, .alarm-management-actions button";
  let initialPanelOpened = false;
  function disableWrites() {
    document.querySelectorAll(writes).forEach(button => {
      if (button.dataset.reviewReadOnly) return;
      button.dataset.reviewReadOnly = "true";
      button.disabled = true;
      button.title = "Read-only design preview; commands are available in the operational simulator";
    });
    const panel = document.getElementById("equipment");
    if (panel && !panel.querySelector(".review-panel-note")) {
      const note = document.createElement("p");
      note.className = "review-panel-note";
      note.textContent = "Design preview · controls are read-only";
      panel.insertBefore(note, panel.firstChild);
    }
    // Only a review starting selection. The model renderer resolves its
    // binding and supplies all standard control/state content.
    if (!initialPanelOpened) {
      const node = document.querySelector('.plant-equipment[data-node="modulating"]');
      if (node) {
        initialPanelOpened = true;
        node.dispatchEvent(new Event("click", { bubbles: true }));
      }
    }
  }
  document.addEventListener("submit", event => { event.preventDefault(); event.stopImmediatePropagation(); }, true);
  document.addEventListener("DOMContentLoaded", () => {
    const brand = document.querySelector(".plant-brand");
    if (brand) { brand.target = "_top"; brand.title = "Back to design gallery"; }
    new MutationObserver(disableWrites).observe(document.body, { childList: true, subtree: true });
    disableWrites();
  });
})();
