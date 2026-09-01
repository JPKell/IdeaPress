// Progressive enhancement for the diff view. The diff is fully rendered server-side without it.
//
// One behaviour only: a toggle that hides unchanged lines. The server already elides long
// unchanged runs, so this is for the remainder — and with JavaScript off the reader simply sees
// every line, which is the correct fallback rather than a degraded one.

(function () {
  "use strict";

  var table = document.querySelector("table.diff");
  var toggle = document.querySelector("[data-diff-toggle]");
  if (!table || !toggle) {
    return;
  }

  toggle.hidden = false;
  toggle.addEventListener("click", function () {
    var hiding = table.classList.toggle("changes-only");
    toggle.setAttribute("aria-pressed", hiding ? "true" : "false");
    toggle.textContent = hiding ? "Show unchanged lines" : "Hide unchanged lines";
  });
})();
