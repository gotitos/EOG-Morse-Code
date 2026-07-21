"""
eog_controller.py

Entry point for the EOG (electrooculography) blink-gesture pipeline --
run this file directly. Uses serial_reader.py for the serial stream and
server.py for the dashboard/WebSocket layer that serves static/morse.html
and static/index.html:

    micro:bit --serial--> SerialReader --> BlinkDetector --> GestureRecognizer
                                                                    |
                                                                    v
                                                     pyautogui action + EMGServer (dashboard)

No ML classifier is involved -- blink detection and gesture
recognition are both pure signal-processing rules:

  - BlinkDetector tracks a rolling 500ms baseline (mean + stdev) of raw
    samples. A blink starts when a sample deviates > BLINK_START_STDEV
    stdev from that baseline, and ends when it returns within
    BLINK_END_STDEV stdev. Every
    completed blink is reported regardless of duration -- the
    dashboard draws each one as a vertical marker on the waveform.
  - GestureRecognizer turns completed blinks into single_blink /
    double_blink / long_blink gestures per the duration/timing rules
    in the module-level constants below, with a 1s cooldown after any
    recognized gesture.

Run with `python3 eog_controller.py`.
"""

import argparse
import os
import queue
import signal
import threading
import time
from collections import deque

from dotenv import load_dotenv

from serial_reader import SerialReader
from server import EMGServer

try:
    import pyautogui
except ImportError:
    pyautogui = None

load_dotenv()

BASELINE_WINDOW_SEC = 1.5       # rolling window used for baseline mean/stdev. Was 0.5 --
                                 # too short a window makes baseline_std (and so the
                                 # detection threshold) swing with every brief noisy patch
                                 # (head movement, momentary bad contact): right after one,
                                 # std stays inflated for up to a window's length even once
                                 # things are quiet again, so a normal blink right then can
                                 # land on the threshold boundary and get missed -- this is
                                 # what "accurate sometimes, not other times" looks like.
                                 # 1.5s absorbs a brief noisy patch without std leaving the
                                 # floor at all. (Tested up to 3.0s -- that overcorrects:
                                 # baseline_mean starts lagging the true rest level instead,
                                 # causing its own misses. 1.5s is the sweet spot.)
BLINK_START_STDEV = 2.5         # blink starts when |value - mean| exceeds this many stdev
BLINK_END_STDEV = 1.5           # blink ends when it returns within this many stdev
MIN_STDEV_FLOOR = 6.0           # guards against a near-flat signal producing a ~0 stdev.
                                 # This has swung both ways: 10.0 (needing a ~30-count
                                 # swing) required a hard/forceful blink to register at
                                 # all; dropping it to 4.0 (~9-count swing) went too far
                                 # the other way and let ambient noise fire spurious raw
                                 # eog_blink events (that event has no backend cooldown of
                                 # its own -- see server.py's docstring). 6.0 (~15 counts)
                                 # is the midpoint.
NOISY_STDEV_WARNING = 12.0      # purely diagnostic (see calibrate() below) -- NOT a clamp.
                                 # A capped ceiling was tried here to keep a noisy session
                                 # (stdev=31.3 counts measured on one run, ~78-count
                                 # threshold, needed a very forceful blink) usable, and it
                                 # backfired badly: real electrode/movement noise is
                                 # temporally correlated, not independent sample-to-sample,
                                 # so a bad-contact burst legitimately holds above threshold
                                 # for several consecutive samples -- exactly what
                                 # BLINK_START_MIN_SAMPLES can't filter, since that only
                                 # screens isolated single-sample spikes. Capping let those
                                 # bursts through constantly (measured ~9 false blinks/sec in
                                 # a simulated noisy session). Left uncapped, a noisy session
                                 # does need a firmer blink -- but that's the correct
                                 # tradeoff: no fixed multiplier is both sensitive and
                                 # noise-proof when the underlying signal-to-noise ratio is
                                 # genuinely poor. Surfacing it as a warning (telling you to
                                 # improve electrode contact) is the real fix, not a clamp.
BLINK_START_MIN_SAMPLES = 3     # consecutive over-threshold samples required to confirm
                                 # a blink start -- see BlinkDetector.process(). Filters
                                 # single-sample ADC/electrode noise spikes without adding
                                 # noticeable latency (at typical serial rates this is a
                                 # few ms), which a bigger MIN_STDEV_FLOOR alone can't do
                                 # without also blocking light real blinks. Started at 2,
                                 # bumped to 3 -- occasional false positives still got
                                 # through, paired with a MIN_BLINK_DURATION_MS floor below
                                 # as a second, duration-based check on top of this one.
MIN_BLINK_DURATION_MS = 15      # second layer: even after clearing the consecutive-sample
                                 # gate above, a completed blink shorter than this is
                                 # dropped as noise rather than reported. A real blink,
                                 # even a light/quick one, still spans tens of ms.
BLINK_REFRACTORY_MS = 100       # minimum quiet gap required right after a blink ends
                                 # before a new one can start. Without this, the tail of a
                                 # single blink -- signal settling/ringing as the eye
                                 # finishes reopening -- could immediately re-cross the
                                 # start threshold and get counted as a SEPARATE blink,
                                 # inflating morse.html's burst counts (reported: 3
                                 # deliberate blinks sometimes counted as 4). 100ms is
                                 # comfortably shorter than the gap between genuinely
                                 # separate deliberate blinks in a burst (typically
                                 # 150ms+), so it shouldn't cost real blinks.

SINGLE_BLINK_MIN_MS = 80
SINGLE_BLINK_MAX_MS = 400
DOUBLE_BLINK_GAP_MS = 600       # max gap after a short blink ends for a second one to pair with it
LONG_BLINK_MIN_MS = 500
GESTURE_COOLDOWN_SEC = 1.0

CALIBRATION_BLINKS = 5
CALIBRATION_TIMEOUT_SEC = 30.0

SERIAL_CONNECT_TIMEOUT_SEC = 5.0
SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))


class BlinkDetector:
    """Rolling-baseline blink start/end detector.

    Maintains a trailing BASELINE_WINDOW_SEC window of raw samples and
    flags a blink whenever the newest sample deviates far enough from
    that window's own mean/stdev. There is no fixed voltage threshold --
    it self-adjusts to a given session's noise floor and electrode
    contact quality, which is why calibrate() below just runs this same
    rule against a few real blinks instead of hand-picking a constant.

    Only confirmed rest-state samples are added to the rolling window --
    samples while in_blink is True are excluded. Two blinks close
    together (e.g. a double_blink's ~150-600ms gap) would otherwise
    leave the first blink's elevated samples sitting in the window
    while the second blink is evaluated, inflating baseline_mean/std
    enough that the second blink's deviation no longer crosses the
    start threshold. Freezing the baseline during a blink keeps it
    representative of actual rest noise regardless of how closely
    blinks are spaced.
    """

    def __init__(self):
        self._window = deque()  # (timestamp, value) rest-state samples, trimmed to BASELINE_WINDOW_SEC
        self.in_blink = False
        self.blink_start_ts = None
        self.baseline_mean = 0.0
        self.baseline_std = MIN_STDEV_FLOOR
        self._start_streak = 0        # consecutive over-threshold samples seen so far
        self._start_candidate_ts = None  # ts of the first sample in that streak
        self._last_blink_end_ts = None   # ts the most recent blink ended, for BLINK_REFRACTORY_MS

    def _is_ready(self, ts):
        if not self._window:
            return False
        span = ts - self._window[0][0]
        return span >= BASELINE_WINDOW_SEC * 0.5 and len(self._window) >= 5

    def _add_rest_sample(self, ts, value):
        self._window.append((ts, value))
        while self._window and (ts - self._window[0][0]) > BASELINE_WINDOW_SEC:
            self._window.popleft()

        values = [v for _, v in self._window]
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        self.baseline_mean = mean
        self.baseline_std = max(variance ** 0.5, MIN_STDEV_FLOOR)

    def process(self, ts, value):
        """Feed one (timestamp, value) sample.

        Returns (start_ts, end_ts, duration_sec) if this sample just
        closed out a blink, else None.
        """
        if not self._is_ready(ts):
            # Not enough history yet (e.g. right at startup) -- treat as
            # a rest sample so the window fills, but don't evaluate a
            # blink transition against a baseline we don't trust yet.
            self._add_rest_sample(ts, value)
            return None

        if not self.in_blink:
            if (
                self._last_blink_end_ts is not None
                and (ts - self._last_blink_end_ts) * 1000.0 < BLINK_REFRACTORY_MS
            ):
                # Still within the refractory window right after the last
                # blink ended -- this sample may still be settling from
                # that blink's tail, so it's excluded from both a new
                # blink-start evaluation AND the rest baseline (same
                # reasoning as excluding in-blink samples: an elevated
                # reading here isn't representative of true rest noise).
                self._start_streak = 0
                return None

            deviation = abs(value - self.baseline_mean)
            if deviation > BLINK_START_STDEV * self.baseline_std:
                # Require BLINK_START_MIN_SAMPLES consecutive over-threshold
                # samples before confirming a start, not just one. A real
                # blink's deviation persists for many samples (tens of ms);
                # a single glitchy ADC reading does not -- this is what
                # actually distinguishes them, since a low enough amplitude
                # threshold to catch a light blink will also let occasional
                # single-sample noise spikes through.
                if self._start_streak == 0:
                    self._start_candidate_ts = ts
                self._start_streak += 1
                if self._start_streak >= BLINK_START_MIN_SAMPLES:
                    self.in_blink = True
                    self.blink_start_ts = self._start_candidate_ts
                    self._start_streak = 0
                    self._start_candidate_ts = None
                return None
            # Streak broken (or never started) -- this sample is rest.
            self._start_streak = 0
            self._add_rest_sample(ts, value)
            return None

        # in_blink: check for a return to baseline. Blink samples
        # themselves are never added to the window (see class docstring).
        deviation = abs(value - self.baseline_mean)
        if deviation <= BLINK_END_STDEV * self.baseline_std:
            self.in_blink = False
            self._last_blink_end_ts = ts
            start_ts = self.blink_start_ts
            self.blink_start_ts = None
            self._add_rest_sample(ts, value)
            duration_sec = ts - start_ts
            if duration_sec * 1000.0 < MIN_BLINK_DURATION_MS:
                # Cleared the consecutive-sample start gate but still too
                # short overall to be a real blink (e.g. correlated noise
                # that happened to persist 2-3 samples) -- state has
                # already been reset above so detection carries on
                # normally, this event alone is just not reported.
                return None
            return (start_ts, ts, duration_sec)

        return None


class GestureRecognizer:
    """Turns completed blink events into single/double/long gestures.

    Rules:
        single_blink : one blink, 80-400ms, no second blink within 600ms
        double_blink : two blinks within 600ms of each other, each 80-400ms
        long_blink   : one blink sustained > 500ms
        cooldown     : 1s after any recognized gesture before the next
                       gesture can fire

    A short (80-400ms) blink is held as `_pending` until either a
    second short blink arrives within DOUBLE_BLINK_GAP_MS (-> fires
    double_blink) or that window elapses with nothing following it
    (-> poll_timeout() fires single_blink). long_blink fires
    immediately on its own blink ending since duration alone already
    settles it.
    """

    def __init__(self, on_gesture):
        self._on_gesture = on_gesture
        self._pending = None  # (start_ts, end_ts, duration_ms) awaiting a possible pair
        self._last_gesture_ts = 0.0

    def _in_cooldown(self, now):
        return now - self._last_gesture_ts < GESTURE_COOLDOWN_SEC

    def _fire(self, label, duration_ms, timestamp):
        self._last_gesture_ts = timestamp
        self._pending = None
        self._on_gesture(label, duration_ms, timestamp)

    def feed_blink(self, start_ts, end_ts, duration_sec):
        if self._in_cooldown(end_ts):
            return

        duration_ms = duration_sec * 1000.0

        if duration_ms > LONG_BLINK_MIN_MS:
            self._fire("long_blink", duration_ms, end_ts)
            return

        if SINGLE_BLINK_MIN_MS <= duration_ms <= SINGLE_BLINK_MAX_MS:
            if self._pending is not None:
                # Report the full first-start -> second-end span rather
                # than just the second blink's own duration, so a
                # double_blink doesn't read like a short single_blink.
                first_start_ts = self._pending[0]
                span_ms = (end_ts - first_start_ts) * 1000.0
                self._fire("double_blink", span_ms, end_ts)
            else:
                self._pending = (start_ts, end_ts, duration_ms)
        # else: too short or in the 400-500ms no-man's-land -- noise, ignored.

    def poll_timeout(self, now):
        """Call once per incoming sample so a pending short blink that
        never got a pair still resolves to single_blink once
        DOUBLE_BLINK_GAP_MS has elapsed with nothing following it."""
        if self._pending is None or self._in_cooldown(now):
            return
        _, pending_end_ts, duration_ms = self._pending
        if (now - pending_end_ts) * 1000.0 >= DOUBLE_BLINK_GAP_MS:
            self._fire("single_blink", duration_ms, pending_end_ts)


def calibrate(raw_queue, target_blinks=CALIBRATION_BLINKS, timeout_sec=CALIBRATION_TIMEOUT_SEC):
    """Blocking calibration pass: counts `target_blinks` real blinks
    using the exact same rolling-baseline rule the live loop uses, then
    prints the resulting detection threshold. Returns the warmed-up
    BlinkDetector so the live loop picks up right where this left off.
    """
    print(f"[eog] blink naturally {target_blinks} times now (relax between blinks)...")
    detector = BlinkDetector()
    detected = 0
    deadline = time.time() + timeout_sec

    while detected < target_blinks and time.time() < deadline:
        try:
            ts, val = raw_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        completed = detector.process(ts, val)
        if completed is not None:
            _, _, duration_sec = completed
            detected += 1
            print(f"[eog]     blink {detected}/{target_blinks} detected "
                  f"(duration={duration_sec * 1000:.0f}ms)")

    threshold_counts = BLINK_START_STDEV * detector.baseline_std
    print(
        f"[eog] calibration complete -- baseline={detector.baseline_mean:.1f} counts, "
        f"stdev={detector.baseline_std:.1f} counts, "
        f"detection threshold=±{threshold_counts:.1f} counts "
        f"({BLINK_START_STDEV}×stdev)"
    )
    if detected < target_blinks:
        print(f"[eog]     WARNING: only saw {detected}/{target_blinks} blinks before timing "
              f"out -- continuing anyway with the threshold above.")
    if detector.baseline_std >= NOISY_STDEV_WARNING:
        print(f"[eog]     WARNING: signal is noisier than usual (stdev={detector.baseline_std:.1f} "
              f"counts) -- the threshold above scales up to compensate, so it'll take a "
              f"firmer blink than normal to register. This is electrode contact/placement, "
              f"not something restarting fixes -- reseating the electrodes usually helps "
              f"more than waiting it out.")

    return detector


# -- actions ---------------------------------------------------------------

def _pyautogui_unavailable(action_desc):
    print(f"[eog]     WARNING: pyautogui not installed -- skipping {action_desc}")
    return f"{action_desc} (skipped: pyautogui unavailable)"


def do_long_blink_action():
    """long_blink -> play/pause media."""
    if pyautogui is None:
        return _pyautogui_unavailable("play/pause")
    try:
        pyautogui.press("playpause")
        return "play/pause (media hotkey)"
    except Exception as exc:
        print(f"[eog]     WARNING: pyautogui key press failed: {exc}")
        return f"play/pause (failed: {exc})"


def wait_for_connection(reader, timeout_sec=SERIAL_CONNECT_TIMEOUT_SEC):
    print("[eog] (1/3) connecting to micro:bit serial port...")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if reader.connected:
            print(f"[eog]     connected on {reader.actual_port} @ {reader.baud_rate} baud")
            return True
        time.sleep(0.1)
    print("[eog]     WARNING: not connected yet -- serial_reader will keep retrying "
          "in the background. Calibration will wait for real samples to arrive.")
    return False


def run(start_server=True, server=None):
    """Entry point for `python3 eog_controller.py`. If `server` is given
    (an already-running EMGServer), it's reused instead of starting a
    second one."""
    port = os.getenv("SERIAL_PORT", "auto")
    reader = SerialReader(port=port)
    reader.start()
    wait_for_connection(reader)

    raw_queue = reader.get_queue()

    print("[eog] (2/3) calibration")
    detector = calibrate(raw_queue)

    owns_server = server is None
    if owns_server:
        if start_server:
            print(f"[eog] (3/3) starting dashboard server on http://{SERVER_HOST}:{SERVER_PORT}")
            server = EMGServer(host=SERVER_HOST, port=SERVER_PORT)
            server.set_status(
                serial_port=reader.actual_port or "disconnected",
                baud_rate=reader.baud_rate,
                window_ms=None,
                hop_ms=None,
                model_name="eog (rule-based, no ML model)",
            )
            server.run(blocking=False)
        else:
            print("[eog] (3/3) dashboard server disabled (--no-server)")
            server = None

    def handle_gesture(label, duration_ms, timestamp):
        print(f"[eog]     gesture: {label:<12} duration={duration_ms:.0f}ms")
        if server is not None:
            server.emit_eog_gesture(label, duration_ms, timestamp)

        # While a blink-driven tool page (morse.html) is connected,
        # gestures drive it over the WebSocket instead -- firing the OS
        # action too would yank audio focus away mid-use.
        if server is not None and server.focus_mode_active:
            print(f"[eog]     action suppressed (focus mode active)")
            return

        # single_blink/double_blink have no OS-level action -- they used
        # to trigger Cmd+Tab / open a new browser tab, which was pulled
        # since those actions fired from a background process, yanking
        # focus/tabs around regardless of what the user was doing.
        if label == "long_blink":
            action = do_long_blink_action()
        else:
            action = None

        if action is not None:
            print(f"[eog]     action: {action}")
            if server is not None:
                server.emit_eog_action(action, label, timestamp)

    recognizer = GestureRecognizer(handle_gesture)

    stop_event = threading.Event()

    def shutdown(*_args):
        print("\n[eog] shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("[eog] running (Ctrl+C to stop)")
    try:
        while not stop_event.is_set():
            try:
                ts, val = raw_queue.get(timeout=0.2)
            except queue.Empty:
                recognizer.poll_timeout(time.time())
                continue

            if server is not None:
                server.emit_eog_signal(val, ts)

            completed = detector.process(ts, val)
            if completed is not None:
                start_ts, end_ts, duration_sec = completed
                if server is not None:
                    server.emit_eog_blink(start_ts, end_ts, duration_sec * 1000.0)
                recognizer.feed_blink(start_ts, end_ts, duration_sec)

            recognizer.poll_timeout(ts)
    finally:
        reader.stop()
        if owns_server and server is not None:
            server.stop()
        print("[eog] stopped.")


def main():
    parser = argparse.ArgumentParser(description="Standalone EOG blink-gesture controller")
    parser.add_argument("--no-server", action="store_true",
                         help="skip starting the dashboard server (terminal output only)")
    args = parser.parse_args()
    run(start_server=not args.no_server)


if __name__ == "__main__":
    main()
