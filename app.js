/* ---------------------------------------------------------------------
   EMG·AI dashboard client.
   Canvas waveform renderer + Socket.IO event wiring. Vanilla JS, no
   build step, no framework.
--------------------------------------------------------------------- */

(() => {
  "use strict";

  const WINDOW_SEC = 5;       // how much history the waveform shows
  const SIGNAL_MAX = 1023;    // ADC range
  const MAX_AI_MESSAGES = 8;  // hard cap on retained AI message DOM nodes
  const AI_FADE_AFTER = 5;    // messages beyond this index start fading
  const MAX_GESTURE_HISTORY = 50; // persistent client-side gesture log cap

  // ---- DOM refs ---------------------------------------------------------

  const canvas = document.getElementById("waveform");
  const ctx = canvas.getContext("2d");
  const waveformLabel = document.querySelector(".waveform-panel .panel__label");

  const eogCanvas = document.getElementById("eogWaveform");
  const eogCtx = eogCanvas.getContext("2d");

  const viewTabs = document.getElementById("viewTabs");
  const viewEmg = document.getElementById("viewEmg");
  const viewEog = document.getElementById("viewEog");

  const eogGestureLabelEl = document.getElementById("eogGestureLabel");
  const eogDurationEl = document.getElementById("eogDuration");
  const eogLastTriggeredEl = document.getElementById("eogLastTriggered");
  const eogActionLog = document.getElementById("eogActionLog");

  const connIndicator = document.getElementById("connIndicator");
  const connLabel = document.getElementById("connLabel");

  const modeToggle = document.getElementById("modeToggle");

  const gestureLabelEl = document.getElementById("gestureLabel");
  const confidenceFillEl = document.getElementById("confidenceFill");
  const confidenceValueEl = document.getElementById("confidenceValue");
  const lastTriggeredEl = document.getElementById("lastTriggered");

  const gestureLog = document.getElementById("gestureLog");

  const aiFeed = document.getElementById("aiFeed");
  const aiEmpty = document.getElementById("aiEmpty");

  const statPort = document.getElementById("statPort");
  const statBaud = document.getElementById("statBaud");
  const statRate = document.getElementById("statRate");
  const statWindow = document.getElementById("statWindow");
  const statModel = document.getElementById("statModel");

  // ---- socket -------------------------------------------------------
  // The socket.io client reconnects automatically by default. None of
  // these handlers ever clear waveform samples, gesture history, or AI
  // messages -- a dropped connection should never wipe the dashboard,
  // it should just resume where it left off (see the "history" handler
  // below for how gesture history gets backfilled after a reconnect).

  const socket = io();

  socket.on("connect", () => {
    connIndicator.classList.remove("is-reconnecting");
    connIndicator.classList.add("is-connected");
    connLabel.textContent = "connected";
  });

  socket.on("disconnect", () => {
    // Socket.IO will keep retrying in the background -- reflect that as
    // "reconnecting" rather than a flat "disconnected", and leave every
    // other panel's data exactly as it was.
    connIndicator.classList.remove("is-connected");
    connIndicator.classList.add("is-reconnecting");
    connLabel.textContent = "reconnecting…";
  });

  socket.io.on("reconnect_failed", () => {
    connIndicator.classList.remove("is-connected", "is-reconnecting");
    connLabel.textContent = "disconnected";
  });

  // ---- waveform -------------------------------------------------------
  // `samples` is intentionally never cleared by any connect/disconnect
  // handler -- a reconnect just resumes appending to the same buffer,
  // so the canvas keeps scrolling rather than snapping back to empty.

  let samples = []; // { t: seconds (epoch), v: 0-1023 }
  let samplesThisSecond = 0;
  let rateWindowStart = performance.now();

  socket.on("signal_update", (msg) => {
    samples.push({ t: msg.timestamp, v: msg.value });
    samplesThisSecond += 1;

    const cutoff = msg.timestamp - WINDOW_SEC;
    // Amortized trim: only sweep once the head is stale, not every push.
    while (samples.length && samples[0].t < cutoff) {
      samples.shift();
    }
  });

  // ---- EOG waveform + blink markers ------------------------------------
  // Same buffering/trim pattern as the EMG `samples` array above, plus a
  // parallel `eogBlinkMarkers` array of detected-blink timestamps drawn
  // as vertical lines by drawWaveformOn().

  let eogSamples = []; // { t: seconds (epoch), v: 0-1023 }
  let eogBlinkMarkers = []; // { t: seconds (epoch) }

  socket.on("eog_signal", (msg) => {
    eogSamples.push({ t: msg.timestamp, v: msg.value });

    const cutoff = msg.timestamp - WINDOW_SEC;
    while (eogSamples.length && eogSamples[0].t < cutoff) {
      eogSamples.shift();
    }
  });

  socket.on("eog_blink", (msg) => {
    eogBlinkMarkers.push({ t: msg.start_timestamp });

    const cutoff = msg.start_timestamp - WINDOW_SEC;
    while (eogBlinkMarkers.length && eogBlinkMarkers[0].t < cutoff) {
      eogBlinkMarkers.shift();
    }
  });

  // Generic canvas helpers -- shared by the EMG waveform and the EOG
  // waveform (same rendering rules, different backing sample arrays),
  // so the EOG tab reuses this instead of duplicating the renderer.

  function resizeCanvasEl(canvasEl, ctxEl) {
    const rect = canvasEl.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvasEl.width = Math.round(rect.width * dpr);
    canvasEl.height = Math.round(rect.height * dpr);
    ctxEl.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function resizeCanvas() {
    resizeCanvasEl(canvas, ctx);
    resizeCanvasEl(eogCanvas, eogCtx);
  }

  window.addEventListener("resize", resizeCanvas);

  function drawGridOn(ctxEl, width, height) {
    ctxEl.strokeStyle = "#1c1c1c";
    ctxEl.lineWidth = 1;
    ctxEl.font = "10px 'JetBrains Mono', monospace";
    ctxEl.fillStyle = "#4a4a4a";

    // horizontal gridlines across the 0-1023 ADC range
    const hLines = [0, 256, 512, 768, 1023];
    hLines.forEach((val) => {
      const y = height - (val / SIGNAL_MAX) * height;
      ctxEl.beginPath();
      ctxEl.moveTo(0, y);
      ctxEl.lineTo(width, y);
      ctxEl.stroke();
      ctxEl.fillText(String(val), 4, Math.max(10, y - 3));
    });

    // vertical gridlines, one per second over the trailing window
    for (let s = 0; s <= WINDOW_SEC; s++) {
      const x = width - (s / WINDOW_SEC) * width;
      ctxEl.beginPath();
      ctxEl.moveTo(x, 0);
      ctxEl.lineTo(x, height);
      ctxEl.stroke();
    }
  }

  // `markers` (optional) is an array of { t: epoch-seconds } -- drawn as
  // vertical lines, used by the EOG tab to mark detected blink events.
  function drawWaveformOn(ctxEl, canvasEl, sampleList, markers) {
    const rect = canvasEl.getBoundingClientRect();
    const width = rect.width;
    const height = rect.height;

    ctxEl.clearRect(0, 0, width, height);
    drawGridOn(ctxEl, width, height);

    if (sampleList.length > 1) {
      const now = sampleList[sampleList.length - 1].t;

      if (markers) {
        ctxEl.strokeStyle = "#ff8a00";
        ctxEl.lineWidth = 1;
        markers.forEach((m) => {
          if (now - m.t > WINDOW_SEC) return;
          const x = width - ((now - m.t) / WINDOW_SEC) * width;
          ctxEl.beginPath();
          ctxEl.moveTo(x, 0);
          ctxEl.lineTo(x, height);
          ctxEl.stroke();
        });
      }

      ctxEl.strokeStyle = "#c6ff00";
      ctxEl.lineWidth = 1.5;
      ctxEl.beginPath();

      for (let i = 0; i < sampleList.length; i++) {
        const s = sampleList[i];
        const x = width - ((now - s.t) / WINDOW_SEC) * width;
        const y = height - (s.v / SIGNAL_MAX) * height;
        if (i === 0) ctxEl.moveTo(x, y);
        else ctxEl.lineTo(x, y);
      }
      ctxEl.stroke();
    }
  }

  let lastFrameTime = 0;
  const FRAME_INTERVAL_MS = 1000 / 50; // ~50fps cap

  function renderLoop(now) {
    if (now - lastFrameTime >= FRAME_INTERVAL_MS) {
      drawWaveformOn(ctx, canvas, samples, null);
      drawWaveformOn(eogCtx, eogCanvas, eogSamples, eogBlinkMarkers);
      lastFrameTime = now;
    }
    requestAnimationFrame(renderLoop);
  }

  // samples/sec readout, recomputed once a second from the live event rate
  setInterval(() => {
    statRate.textContent = String(samplesThisSecond);
    samplesThisSecond = 0;
  }, 1000);

  resizeCanvas();
  requestAnimationFrame(renderLoop);

  // ---- calibration progress -------------------------------------------

  socket.on("calibrating", (msg) => {
    const pct = Math.round((msg.progress || 0) * 100);
    if (pct >= 100) {
      waveformLabel.textContent = "SIGNAL — LAST 5s";
    } else {
      waveformLabel.textContent = `CALIBRATING BASELINE… ${pct}%`;
    }
  });

  // ---- gesture readout + persistent history ------------------------

  function formatTime(date) {
    return date.toLocaleTimeString([], { hour12: false });
  }

  // Persists across reconnects for the lifetime of the page load (not
  // just the current socket connection) -- reconnecting never clears
  // this array, it only ever gets merged into.
  let gestureHistory = [];
  const seenGestureKeys = new Set();

  function gestureKey(evt) {
    return `${evt.timestamp}|${evt.label}`;
  }

  function renderGestureLog() {
    gestureLog.textContent = "";

    if (gestureHistory.length === 0) {
      const empty = document.createElement("div");
      empty.className = "gesture-log__empty";
      empty.textContent = "no gestures yet";
      gestureLog.appendChild(empty);
      return;
    }

    // newest first; array itself stays oldest-first so trimming from
    // the front (below) drops the oldest entry, as intended.
    for (let i = gestureHistory.length - 1; i >= 0; i--) {
      const evt = gestureHistory[i];
      const row = document.createElement("div");
      row.className = "gesture-log__row";
      row.setAttribute("data-gesture", evt.label);

      const time = document.createElement("span");
      time.className = "gesture-log__time";
      time.textContent = evt.timestamp ? formatTime(new Date(evt.timestamp * 1000)) : "—";

      const label = document.createElement("span");
      label.className = "gesture-log__label-cell";
      label.textContent = evt.label;

      const conf = document.createElement("span");
      conf.className = "gesture-log__conf";
      conf.textContent = `${Math.round((evt.confidence || 0) * 100)}%`;

      row.append(time, label, conf);
      gestureLog.appendChild(row);
    }
  }

  // Adds an event to the persistent history (deduped by timestamp+label
  // so replayed "history" events on reconnect never create duplicates),
  // trims to MAX_GESTURE_HISTORY dropping the oldest first, and
  // re-renders the log. Safe to call for both live gestures and
  // reconnect-replayed history.
  function addGestureToHistory(evt) {
    const key = gestureKey(evt);
    if (seenGestureKeys.has(key)) return;

    seenGestureKeys.add(key);
    gestureHistory.push(evt);
    gestureHistory.sort((a, b) => a.timestamp - b.timestamp);

    while (gestureHistory.length > MAX_GESTURE_HISTORY) {
      const dropped = gestureHistory.shift();
      seenGestureKeys.delete(gestureKey(dropped));
    }

    renderGestureLog();
  }

  let lastActiveGestureLabel = null;

  socket.on("gesture", (msg) => {
    gestureLabelEl.textContent = msg.label;
    gestureLabelEl.setAttribute("data-gesture", msg.label);

    const pct = Math.max(0, Math.min(1, msg.confidence)) * 100;
    confidenceFillEl.style.width = `${pct}%`;
    confidenceValueEl.textContent = `${Math.round(pct)}%`;

    lastTriggeredEl.textContent = msg.timestamp
      ? formatTime(new Date(msg.timestamp * 1000))
      : formatTime(new Date());

    if (msg.label !== "rest") {
      lastActiveGestureLabel = msg.label;
    }

    addGestureToHistory(msg);
  });

  // Sent once per connection (including reconnects) with the server's
  // last-20 gesture buffer, so the log backfills instantly instead of
  // sitting empty/stale until the next live gesture. Merges via the
  // same dedup path as live events, so it never duplicates or clears
  // what's already on screen.
  socket.on("history", (msg) => {
    (msg.events || []).forEach(addGestureToHistory);
  });

  // ---- AI response panel -------------------------------------------
  // NOTE: no AI client exists in this repo, so ai_token / ai_done are
  // never actually emitted by the backend in this build. The handlers
  // below are wired up and ready for whenever one is added -- they just
  // won't fire until then, which is why aiEmpty stays visible.

  let currentAiMessageEl = null;
  let currentAiTextEl = null;
  let aiMessages = [];

  function pruneAiMessages() {
    aiMessages.forEach((el, idx) => {
      el.classList.toggle("is-fading", idx >= AI_FADE_AFTER);
    });
    while (aiMessages.length > MAX_AI_MESSAGES) {
      const stale = aiMessages.pop();
      stale.remove();
    }
  }

  socket.on("ai_token", (msg) => {
    if (aiEmpty) aiEmpty.remove();

    if (!currentAiMessageEl) {
      const gesture = lastActiveGestureLabel || "gesture";

      currentAiMessageEl = document.createElement("div");
      currentAiMessageEl.className = "ai-message";
      currentAiMessageEl.setAttribute("data-gesture", gesture);

      const tag = document.createElement("div");
      tag.className = "ai-message__tag";
      tag.textContent = `${gesture} · ${formatTime(new Date())}`;

      currentAiTextEl = document.createElement("span");
      currentAiTextEl.className = "ai-message__text";

      const cursor = document.createElement("span");
      cursor.className = "ai-cursor";

      currentAiMessageEl.appendChild(tag);
      const body = document.createElement("div");
      body.appendChild(currentAiTextEl);
      body.appendChild(cursor);
      currentAiMessageEl.appendChild(body);

      aiFeed.insertBefore(currentAiMessageEl, aiFeed.firstChild);
      aiMessages.unshift(currentAiMessageEl);
      pruneAiMessages();
    }

    currentAiTextEl.textContent += msg.token;
  });

  socket.on("ai_done", () => {
    if (currentAiMessageEl) {
      const cursor = currentAiMessageEl.querySelector(".ai-cursor");
      if (cursor) cursor.remove();
    }
    currentAiMessageEl = null;
    currentAiTextEl = null;
  });

  // ---- EOG gesture readout + action log --------------------------------

  socket.on("eog_gesture", (msg) => {
    eogGestureLabelEl.textContent = msg.label;
    eogGestureLabelEl.setAttribute("data-gesture", msg.label);
    eogDurationEl.textContent = `${Math.round(msg.duration_ms)}ms`;
    eogLastTriggeredEl.textContent = msg.timestamp
      ? formatTime(new Date(msg.timestamp * 1000))
      : formatTime(new Date());
  });

  // Persistent (page-lifetime) action history, same append/trim/render
  // pattern as the EMG gesture log above, capped at the last 10 actions.
  let eogActionHistory = [];
  const MAX_EOG_ACTION_HISTORY = 10;

  function renderEogActionLog() {
    eogActionLog.textContent = "";

    if (eogActionHistory.length === 0) {
      const empty = document.createElement("div");
      empty.className = "gesture-log__empty";
      empty.textContent = "no actions yet";
      eogActionLog.appendChild(empty);
      return;
    }

    for (let i = eogActionHistory.length - 1; i >= 0; i--) {
      const evt = eogActionHistory[i];
      const row = document.createElement("div");
      row.className = "gesture-log__row eog-action-row";
      row.setAttribute("data-gesture", evt.gesture);

      const time = document.createElement("span");
      time.className = "gesture-log__time";
      time.textContent = evt.timestamp ? formatTime(new Date(evt.timestamp * 1000)) : "—";

      const label = document.createElement("span");
      label.className = "gesture-log__label-cell";
      label.textContent = `${evt.gesture} → ${evt.action}`;

      row.append(time, label);
      eogActionLog.appendChild(row);
    }
  }

  function addEogAction(evt) {
    eogActionHistory.push(evt);
    while (eogActionHistory.length > MAX_EOG_ACTION_HISTORY) {
      eogActionHistory.shift();
    }
    renderEogActionLog();
  }

  socket.on("eog_action", (msg) => addEogAction(msg));

  // Replayed on (re)connect with the server's last-10 action buffer,
  // same reconnect-backfill pattern as the EMG "history" event.
  socket.on("eog_history", (msg) => {
    (msg.events || []).forEach(addEogAction);
  });

  // ---- view tabs (EMG / EOG) -------------------------------------------

  viewTabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".view-tab");
    if (!btn || !btn.dataset.view) return; // e.g. the MORSE link -- let it navigate

    viewTabs.querySelectorAll(".view-tab").forEach((el) => {
      el.classList.toggle("is-active", el === btn);
    });

    const view = btn.dataset.view;
    viewEmg.style.display = view === "emg" ? "" : "none";
    viewEog.style.display = view === "eog" ? "" : "none";
  });

  // ---- mode toggle (Phase 2 scaffold) --------------------------------

  let currentMode = "emg";

  modeToggle.addEventListener("click", () => {
    currentMode = currentMode === "emg" ? "eeg" : "emg";
    modeToggle.querySelectorAll(".mode-toggle__option").forEach((el) => {
      el.classList.toggle("is-active", el.dataset.mode === currentMode);
    });
    socket.emit("set_mode", { mode: currentMode });
  });

  // ---- status bar (startup config, fetched once) -----------------------

  fetch("/api/status")
    .then((res) => res.json())
    .then((status) => {
      statPort.textContent = status.serial_port ?? "—";
      statBaud.textContent = status.baud_rate ?? "—";
      statModel.textContent = status.model_name ?? "—";
      if (status.window_ms != null && status.hop_ms != null) {
        statWindow.textContent = `${status.window_ms}ms / ${status.hop_ms}ms hop`;
      }
    })
    .catch(() => {
      /* server not reachable yet -- status bar just keeps its placeholders */
    });
})();
