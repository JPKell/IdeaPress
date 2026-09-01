// Progressive enhancement for the workspace. The page is complete without this file.
//
// ADR-0020: server-rendered HTML with progressive enhancement, no SPA. Everything this script does
// is something the page already does with a reload — it just does it without one. If the script
// fails to load, is blocked, or throws, the workspace still renders, the navigator still
// navigates, and every form still submits. Nothing here is required for a read-only view, which is
// the property UI/UX Standards §13 asks for and `tests/accessibility/` asserts.
//
// It attaches only when the page says a stage is running, and it disconnects on completion so a
// finished project holds no open connection.

(function () {
  "use strict";

  var live = document.querySelector("[data-stage-stream]");
  if (!live || typeof EventSource === "undefined") {
    return; // No running stage, or no EventSource: the page is already correct as rendered.
  }

  var url = live.getAttribute("data-stage-stream");
  var log = live.querySelector("[data-stage-log]");
  var status = live.querySelector("[data-stage-status]");
  if (!url || !log) {
    return;
  }

  // The server replays from the beginning for a connection with no Last-Event-ID, so a refresh
  // mid-stage rebuilds the whole log rather than resuming from a gap. That is the stream contract
  // the SSE handler already implements; this only has to not fight it.
  var source = new EventSource(url);

  function append(text, kind) {
    var line = document.createElement("li");
    line.textContent = text; // textContent, never innerHTML: this is model output (risk S1).
    if (kind) {
      line.className = "event-" + kind;
    }
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  source.addEventListener("stage.event", function (event) {
    var payload;
    try {
      payload = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    var body = payload && payload.payload ? payload.payload : payload;
    append((body.event_type || "event") + " — " + (body.message || ""), body.event_type);
  });

  function finish(label) {
    if (status) {
      status.textContent = label;
    }
    source.close();
    // The rendered page is the source of truth for everything but the log, so reload once the
    // stage ends rather than trying to patch coverage, findings and provenance in place.
    window.setTimeout(function () {
      window.location.reload();
    }, 750);
  }

  source.addEventListener("stage.completed", function () {
    finish("Finished. Reloading…");
  });
  source.addEventListener("stage.failed", function () {
    finish("Failed. Reloading…");
  });
  source.onerror = function () {
    if (status) {
      status.textContent = "Live updates disconnected — reload to see the current state.";
    }
  };
})();
