"""
server.py

Flask + Flask-SocketIO server that bridges eog_controller.py's blink
pipeline to the browser (static/index.html, static/morse.html) over
WebSockets.

Emits (server -> browser):
    eog_signal     {value: int, timestamp: float}   raw EOG sample (eog_controller.py)
    eog_blink      {start_timestamp, end_timestamp, duration_ms}  detected blink (pre-gesture)
    eog_gesture    {label, duration_ms, timestamp}  recognized single/double/long blink
    eog_action     {action: str, gesture: str, timestamp: float}  triggered pyautogui action
    signal_update  {value: int, timestamp: float}   raw sample on the EMG-shaped channel --
                                                     no real producer in this repo anymore
                                                     (see the __main__ block below for the
                                                     one thing that still emits it: a
                                                     synthetic-data dev/demo mode)
    gesture        {label: str, confidence: float}  debounced gesture event on that same
                                                     channel -- morse.html's hard_hold
                                                     (backspace) / single_flex (clear line)
                                                     listen for this; reachable today only
                                                     via that same __main__ demo mode
    calibrating    {progress: float}                baseline calibration progress, same channel
    ai_token       {token: str}                      unimplemented; no producer exists
    ai_done        {}                                 unimplemented; no producer exists

Receives (browser -> server):
    set_mode       {mode: 'emg' | 'eeg'}   Phase 2 scaffold, see handler below.
    focus_mode     {active: bool}          A blink-driven tool page (morse.html)
                                           announces itself; while any is connected,
                                           focus_mode_active is True and
                                           eog_controller.py suppresses its remaining
                                           OS-level pyautogui action (long_blink's
                                           play/pause), so a dash doesn't also yank
                                           audio focus.
"""

import os
import time
import threading
from collections import deque

from flask import Flask, request, send_from_directory
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

load_dotenv()

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = int(os.getenv("SERVER_PORT", "8080"))
GESTURE_HISTORY_SIZE = 20
EOG_ACTION_HISTORY_SIZE = 10


class EMGServer:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.current_mode = "emg"
        self.status = {
            "serial_port": "unknown",
            "baud_rate": None,
            "window_ms": None,
            "hop_ms": None,
            "model_name": "none (untrained)",
        }

        self.app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
        # threading mode uses Python's built-in threading instead of
        # eventlet's monkey-patched greenlets -- no extra dependency, and
        # it doesn't fight with other threads in the process (serial
        # reader, feature extractor, classifier) the way eventlet's
        # cooperative scheduler could, which was causing the socket to
        # drop and reconnect under load.
        self.socketio = SocketIO(self.app, async_mode="threading", cors_allowed_origins="*")

        # Circular buffer of the last N gesture events, so a client that
        # (re)connects mid-session sees recent history immediately instead
        # of an empty panel until the next live gesture fires.
        self._gesture_history = deque(maxlen=GESTURE_HISTORY_SIZE)
        # Circular buffer of the last N EOG actions (eog_controller.py),
        # replayed the same way on (re)connect -- see emit_eog_action().
        self._eog_action_history = deque(maxlen=EOG_ACTION_HISTORY_SIZE)
        self._history_lock = threading.Lock()

        # Session ids of connected blink-driven tool pages (morse.html).
        # While any are present, eog_controller.py suppresses its
        # remaining OS-level pyautogui action (long_blink's play/pause)
        # so a deliberate dash doesn't also yank audio focus -- see
        # focus_mode_active.
        self._focus_clients = set()

        self._server_thread = None

        self._register_routes()
        self._register_handlers()

    # -- setup -----------------------------------------------------------

    def _register_routes(self):
        @self.app.route("/")
        def index():
            return send_from_directory(STATIC_DIR, "index.html")

        @self.app.route("/api/status")
        def api_status():
            # Static-ish startup info for the dashboard's status bar
            # (serial port, baud rate, window size, model name).
            # samples/sec is computed client-side from event rate instead,
            # since that's a live number, not startup config.
            from flask import jsonify
            return jsonify(self.status)

    def _register_handlers(self):
        @self.socketio.on("connect")
        def handle_connect():
            print("[server] dashboard connected")
            with self._history_lock:
                buffered = list(self._gesture_history)
                eog_buffered = list(self._eog_action_history)
            # Replay recent gesture history immediately on connect so a
            # (re)connecting dashboard populates instantly instead of
            # sitting empty until the next live gesture.
            emit("history", {"events": buffered})
            emit("eog_history", {"events": eog_buffered})

        @self.socketio.on("disconnect")
        def handle_disconnect():
            # If this was a focus-mode tool page, dropping it must clear
            # focus mode even when the tab was closed abruptly (no
            # explicit focus_mode {active: false} ever arrives then).
            self._focus_clients.discard(request.sid)
            print("[server] dashboard disconnected")

        @self.socketio.on("focus_mode")
        def handle_focus_mode(data):
            active = bool((data or {}).get("active"))
            if active:
                self._focus_clients.add(request.sid)
            else:
                self._focus_clients.discard(request.sid)
            print(f"[server] focus mode {'ON' if self.focus_mode_active else 'OFF'} "
                  f"({len(self._focus_clients)} focus client(s))")

        @self.socketio.on("set_mode")
        def handle_set_mode(data):
            mode = (data or {}).get("mode", "emg")
            if mode not in ("emg", "eeg"):
                print(f"[server] ignoring unknown mode: {mode!r}")
                return

            self.current_mode = mode
            print(f"[server] mode switched to {mode!r}")

            # TODO(EEG - Phase 2): once an EEG feature pipeline exists,
            # this handler should tell it which feature queue to consume
            # from (EMG vs EEG) instead of just recording the mode
            # string. Currently neither EMG nor EEG has a backing
            # pipeline in this repo, so this is bookkeeping only.

    @property
    def focus_mode_active(self):
        """True while at least one blink-driven tool page (morse.html) is
        connected -- eog_controller.py checks this to suppress its
        remaining OS-level action."""
        return len(self._focus_clients) > 0

    # -- outbound events ---------------------------------------------------

    def emit_signal(self, value, timestamp):
        self.socketio.emit("signal_update", {"value": value, "timestamp": timestamp})

    def emit_gesture(self, label, confidence):
        event = {"label": label, "confidence": confidence, "timestamp": time.time()}
        with self._history_lock:
            self._gesture_history.append(event)
        self.socketio.emit("gesture", event)

    def emit_calibrating(self, progress):
        self.socketio.emit("calibrating", {"progress": progress})

    def emit_eog_signal(self, value, timestamp):
        """Raw EOG sample for the dashboard's EOG waveform canvas."""
        self.socketio.emit("eog_signal", {"value": value, "timestamp": timestamp})

    def emit_eog_blink(self, start_ts, end_ts, duration_ms):
        """A raw detected blink (start/end crossing the rolling-baseline
        stdev thresholds), independent of whether it goes on to become a
        recognized gesture -- drawn as a vertical marker on the waveform."""
        self.socketio.emit("eog_blink", {
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "duration_ms": duration_ms,
        })

    def emit_eog_gesture(self, label, duration_ms, timestamp):
        """A recognized single/double/long blink gesture."""
        self.socketio.emit("eog_gesture", {
            "label": label,
            "duration_ms": duration_ms,
            "timestamp": timestamp,
        })

    def emit_eog_action(self, action, gesture, timestamp):
        """A pyautogui action triggered by a recognized gesture -- kept
        in a rolling history so the dashboard's action log backfills on
        (re)connect, same pattern as emit_gesture()'s history."""
        event = {"action": action, "gesture": gesture, "timestamp": timestamp}
        with self._history_lock:
            self._eog_action_history.append(event)
        self.socketio.emit("eog_action", event)

    def set_status(self, **kwargs):
        """Update the startup info served at GET /api/status (serial port,
        baud rate, window size, model name -- shown in the dashboard's
        status bar)."""
        self.status.update(kwargs)

    def emit_ai_token(self, token):
        """TODO(AI integration): not called anywhere -- no AI client
        exists in this repo. Wire this up to a streaming callback if one
        is added later."""
        self.socketio.emit("ai_token", {"token": token})

    def emit_ai_done(self):
        """TODO(AI integration): see emit_ai_token()."""
        self.socketio.emit("ai_done", {})

    # -- lifecycle ---------------------------------------------------------

    def run(self, blocking=True):
        print(f"[server] serving on http://{self.host}:{self.port}")
        # threading mode falls back to Werkzeug's dev server, which
        # Flask-SocketIO refuses to start without this flag since it's
        # not hardened for production. That's fine here -- this is a
        # single-user local dashboard, not a public deployment.
        run_kwargs = {"host": self.host, "port": self.port, "allow_unsafe_werkzeug": True}
        if blocking:
            self.socketio.run(self.app, **run_kwargs)
        else:
            self._server_thread = threading.Thread(
                target=self.socketio.run,
                args=(self.app,),
                kwargs=run_kwargs,
                daemon=True,
            )
            self._server_thread.start()

    def stop(self):
        self.socketio.stop()


if __name__ == "__main__":
    # Standalone smoke test: serves the dashboard with a synthetic signal
    # and fake gesture events so the UI can be developed/tested without
    # the micro:bit attached.
    import math
    import random
    import time

    server = EMGServer()

    def simulate():
        server.socketio.sleep(1.0)
        t0 = time.time()
        last_gesture = time.time()
        gestures = ["rest", "single_flex", "hard_hold"]
        while True:
            t = time.time() - t0
            value = int(512 + 300 * math.sin(t * 3) + random.uniform(-30, 30))
            value = max(0, min(1023, value))
            server.emit_signal(value, time.time())

            if time.time() - last_gesture > 4.0:
                label = random.choice(gestures)
                server.emit_gesture(label, random.uniform(0.6, 0.99))
                last_gesture = time.time()

            server.socketio.sleep(0.02)

    server.socketio.start_background_task(simulate)
    server.run(blocking=True)
