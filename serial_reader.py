"""
serial_reader.py

Reads the raw analog stream (EOG or EMG, depending on what's wired to
the micro:bit's analog pin) over USB serial and pushes (timestamp,
value) samples into a thread-safe queue for downstream consumers --
eog_controller.py's BlinkDetector.

The micro:bit firmware prints one integer per line (0-1023, the raw
ADC reading) at 115200 baud.
"""

import os
import time
import queue
import threading

import serial
from serial.tools import list_ports
from dotenv import load_dotenv

load_dotenv()

BAUD_RATE = 115200

# BBC micro:bit's CMSIS-DAP USB interface. Used to auto-detect the
# right port when several USB-serial devices are plugged in.
MICROBIT_VID = 0x0D28
MICROBIT_PID = 0x0204

RECONNECT_DELAY_SEC = 2.0


def find_microbit_port():
    """Scan connected serial devices for a micro:bit's VID/PID.

    Falls back to the first available port if no exact match is
    found, since some OS/driver combos report different PID values.
    """
    ports = list(list_ports.comports())

    for port in ports:
        if port.vid == MICROBIT_VID and port.pid == MICROBIT_PID:
            return port.device

    # Fallback: some driver stacks only expose the VID reliably.
    for port in ports:
        if port.vid == MICROBIT_VID:
            return port.device

    if ports:
        return ports[0].device

    return None


class SerialReader:
    """Background-threaded reader for the micro:bit EMG serial stream."""

    def __init__(self, port=None, baud_rate=BAUD_RATE, max_queue_size=0):
        # port=None / "auto" triggers autodetection at start().
        self._configured_port = port
        self.baud_rate = baud_rate
        self.actual_port = None

        self._queue = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._thread = None
        self._serial = None
        self.connected = False

    def get_queue(self):
        """Return the thread-safe queue of (timestamp, value) samples."""
        return self._queue

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._close_serial()

    def _resolve_port(self):
        if self._configured_port and self._configured_port.lower() != "auto":
            return self._configured_port
        return find_microbit_port()

    def _open_serial(self):
        port = self._resolve_port()
        if port is None:
            raise serial.SerialException("No serial ports found")

        self.actual_port = port
        self._serial = serial.Serial(port, self.baud_rate, timeout=1.0)
        self.connected = True
        return port

    def _close_serial(self):
        self.connected = False
        if self._serial is not None:
            try:
                self._serial.close()
            except serial.SerialException:
                pass
            self._serial = None

    def _run(self):
        """Main loop: (re)connect and read lines until stop() is called."""
        while not self._stop_event.is_set():
            try:
                port = self._open_serial()
                print(f"[serial_reader] connected on {port} @ {self.baud_rate} baud")
                self._read_loop()
            except serial.SerialException as exc:
                print(f"[serial_reader] connection lost/failed: {exc}")
                self._close_serial()
                if self._stop_event.is_set():
                    break
                print(f"[serial_reader] retrying in {RECONNECT_DELAY_SEC}s...")
                self._stop_event.wait(RECONNECT_DELAY_SEC)

        self._close_serial()

    def _read_loop(self):
        while not self._stop_event.is_set():
            raw_line = self._serial.readline()
            if not raw_line:
                # Timeout with no data -- keep polling, port is still open.
                continue

            try:
                text = raw_line.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                value = int(text)
            except ValueError:
                # Garbled line (e.g. mid-write on connect) -- skip it.
                continue

            # Clamp to the ADC's documented range in case of noise spikes.
            value = max(0, min(1023, value))
            sample = (time.time(), value)

            try:
                self._queue.put_nowait(sample)
            except queue.Full:
                # Downstream is falling behind; drop the oldest sample
                # rather than blocking the reader thread.
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(sample)


if __name__ == "__main__":
    port_arg = os.getenv("SERIAL_PORT", "auto")
    print(f"[serial_reader] starting standalone test (configured port={port_arg!r})")

    reader = SerialReader(port=port_arg)
    reader.start()

    try:
        q = reader.get_queue()
        while True:
            ts, val = q.get()
            print(f"{ts:.3f}  {val:4d}  {'#' * (val // 20)}")
    except KeyboardInterrupt:
        print("\n[serial_reader] stopping...")
    finally:
        reader.stop()
