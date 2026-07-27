# EMG·MORSE

An accessibility-focused Morse code typer controlled entirely by eye
blinks. Reads a single-channel EOG (electrooculography) stream off a
micro:bit over USB serial, detects blinks with an adaptive rolling
baseline, classifies them by how many happen in a row (not how long
they last), decodes the resulting dot/dash sequence as Morse code in
real time, and streams everything to a live dashboard over WebSockets.

Built for anyone with limited or no hand/arm mobility (e.g. ALS, spinal
cord injury, or other motor-control conditions), where a blink can
remain one of the few reliably repeatable voluntary movements
available.

## Hardware setup

```
   micro:bit --serial (115200 baud)--> this repo (laptop)
```

EOG electrodes are placed around one eye to pick up the corneo-retinal
potential shift a blink produces; the micro:bit samples that signal on
an analog pin and prints one raw integer (0-1023) per line over USB
serial. `serial_reader.py` auto-detects the micro:bit by USB VID/PID,
falling back to the first available serial port.

## Run instructions

### 1. Install dependencies

```bash
cd emg-ai
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

On macOS, `pyautogui`'s synthetic key events (used for `long_blink` ->
play/pause) need Accessibility permission: System Settings > Privacy &
Security > Accessibility.

Copy/edit `.env` if your setup needs a specific serial port or a
different dashboard port:

```
SERIAL_PORT=auto   # or e.g. /dev/tty.usbmodemXXXX, COM3
SERVER_PORT=8080   # defaults to 8080 since macOS AirPlay Receiver
                    # often claims 5000 (Control Center) -- change
                    # freely if 8080 is taken on your machine too.
```

### 2. Run it

```bash
python eog_controller.py
```

This connects the serial reader, runs a blocking calibration pass
(blink naturally 5 times when prompted), then serves the dashboard at
**http://localhost:8080**. Open that, click **MORSE ▸** in the nav bar,
and start blinking.

`python server.py` also runs standalone with a synthetic signal and
fake gesture events, for developing/testing the dashboard UI without
the hardware connected.

## Blink vocabulary (static/morse.html)

Classified by **how many blinks happen in a row** (each within
900ms of the last), not by how long any single blink lasts -- EOG
electrodes pick up the *movement* of blinking, not the *state* of eyes
being closed, so a sustained hold doesn't produce a sustained reading.

| Blinks in a row | Action |
|:---:|---|
| 1 | ignored (a normal/involuntary blink) |
| 2 | dot (`.`) |
| 3 | dash (`-`) |
| 4 | clear the in-progress letter, or backspace if nothing's pending |
| 5+ | dash (same bucket as 3) |

A 3-second pause after the last blink decodes the built-up sequence
into a letter; a 6-second pause appends a word space.

`eog_controller.py` separately classifies whole blinks by **duration**
(`single_blink` / `double_blink` / `long_blink`) for its own
OS-level actions (`long_blink` -> play/pause) and for the dashboard's
EOG tab -- that classification is independent of morse.html's own
burst-counting, which reads the raw per-blink stream directly.

`hard_hold` (EMG, via the `gesture` event) also backspaces and
`single_flex` clears the whole line, but this repo has no EMG
classifier pipeline of its own -- those two only fire from `python3
server.py`'s standalone demo mode, or a future EMG pipeline emitting
the same event shape.

## Project structure

```
emg-ai/
├── eog_controller.py   # entry point: blink detection + gesture recognition + OS actions
├── serial_reader.py    # micro:bit serial -> queue
├── server.py           # Flask + Flask-SocketIO dashboard server
└── static/
    ├── index.html      # dashboard shell (EMG/EOG waveform tabs, nav to morse.html)
    ├── morse.html       # the blink-to-Morse-code typer
    ├── app.js           # dashboard client (waveform rendering, socket wiring)
    └── style.css         # shared dark/lime theme
```

## Phase 2 (scaffolded, not implemented)

EEG mode is stubbed in `server.py`'s `set_mode` handler and
`static/index.html`'s EMG/EEG toggle, but has no backing pipeline --
switching to it just records the mode string.
