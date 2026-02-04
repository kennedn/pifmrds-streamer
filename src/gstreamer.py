"""GStreamer radio streaming components."""
# pylint: disable=W0718

import json
import logging
import os
import stat
import re
import subprocess
import threading
import time
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib #pylint: disable=C0413

Gst.init(None)

signal.signal(signal.SIGPIPE, signal.SIG_DFL)


logger = logging.getLogger(__name__)

APP_NAME = "pifmrds-streamer"

CONFIG_DIR = Path.home() / ".config" / APP_NAME
STATE_FILE = CONFIG_DIR / "stations.json"

RDS_CTL_FIFO = f"/tmp/{APP_NAME}_rds_ctl"

DEFAULT_FREQ = "100.0"

DEFAULT_STATION_NAME = "Dance UK (default)"
DEFAULT_STREAM_URL = "http://51.89.148.171:8022/"
DEFAULT_PS = "DanceUK"
DEFAULT_RT = "Streaming"


# Helpers

def set_nice(n: int):
    """Return a function to set process nice value."""
    def _fn():
        try:
            os.nice(n)
        except PermissionError:
            pass
    return _fn


def safe_ps(text: str) -> str:
    """Sanitize text for RDS PS field (max 8 chars, alphanumeric + space)."""
    t = re.sub(r"[^0-9A-Za-z ]+", "", (text or "")).strip()
    return (t or DEFAULT_PS)[:8]


def safe_rt(text: str) -> str:
    """Sanitize text for RDS RT field (max 64 chars)."""
    t = (text or "").strip()
    return (t or " ")[:64]


def load_state_json() -> Tuple[Dict[str, str], Optional[str], Optional[str]]:
    """
    Load stations + last selected station + freq from state file.
    Always includes the default station at position 0.
    """
    stations: Dict[str, str] = {DEFAULT_STATION_NAME: DEFAULT_STREAM_URL}
    last: Optional[str] = DEFAULT_STATION_NAME
    freq: Optional[str] = DEFAULT_FREQ

    if not STATE_FILE.exists():
        return stations, last, freq

    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:  # pylint: disable=broad-except
        logger.exception("Error loading state file %s", STATE_FILE)
        return stations, last, freq

    loaded_last = data.get("last")
    if isinstance(loaded_last, str) and loaded_last in stations:
        last = loaded_last

    loaded_freq = data.get("freq")
    if isinstance(loaded_freq, str):
        freq = loaded_freq

    loaded_stations = data.get("stations", {}) or {}
    loaded_stations.pop(last, None)  # ensure last loaded is placed first
    stations.update(loaded_stations)

    return stations, last, freq


def save_state_json(stations: Dict[str, str], last: Optional[str], freq: Optional[str]) -> None:
    """Save stations and last selected station to state file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stations": stations, "last": last, "freq": freq}, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)


def ensure_fifo(path: str) -> None:
    """Create FIFO (and replace non-FIFO files) if needed."""
    if os.path.exists(path):
        st = os.stat(path)
        if not stat.S_ISFIFO(st.st_mode):
            os.remove(path)
    if not os.path.exists(path):
        os.mkfifo(path)


def open_ctl_fifo_rdwr(path: str) -> int:
    """
    Open FIFO RDWR to avoid blocking if the other end isn't open yet.
    O_CLOEXEC avoids leaking fd into exec'd children where supported.
    """
    return os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))


class RestartReason(Exception):
    """Internal control flow: raised to restart the whole stack."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class _RunHandle:
    """Internal: holds per-run resources so stop() can request a quit."""
    loop: Optional[GLib.MainLoop] = None
    ctl_fd: Optional[int] = None
    pif: Optional[subprocess.Popen] = None
    pipeline: Optional[Gst.Pipeline] = None
    restart_reason: Dict[str, Optional[str]] = field(default_factory=lambda: {"reason": None})


@dataclass
class RadioController:
    """Control FM radio streaming with RDS metadata support.

    Manages station selection, frequency tuning, and the background streaming thread
    that coordinates pifmrds (RDS transmitter) with GStreamer audio pipelines.
    """
    stations: Dict[str, str] = field(default_factory=dict)
    last_station: Optional[str] = None
    freq: Optional[str] = None

    current_name: Optional[str] = None
    current_url: Optional[str] = None
    last_title: str = ""

    stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _run: _RunHandle = field(default_factory=_RunHandle)

    def _ctl_send(self, line: str) -> None:
        """Send a control command to the RDS FIFO."""
        fd = self._run.ctl_fd
        if fd is None:
            return
        try:
            os.write(fd, (line.rstrip() + "\n").encode("utf-8", "replace"))
        except OSError:
            logger.exception("RDS ctl write failed")

    def _request_restart(self, reason: str) -> None:
        """Request the GLib main loop to quit with the given reason."""
        if self._run.restart_reason["reason"] is None:
            self._run.restart_reason["reason"] = reason
        loop = self._run.loop
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass

    def stop(self) -> None:
        """Stop streaming and tear down background thread resources."""
        with self._lock:
            logger.info("Stop requested")
            self.stop_event.set()
            # Ask current GLib loop to quit ASAP (thread-safe via idle_add)
            try:
                GLib.idle_add(self._request_restart, "stop requested")
            except Exception:
                # If idle_add fails (e.g. loop not running), best-effort quit directly
                self._request_restart("stop requested")

        # Join thread outside the lock to avoid deadlocks
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)

        with self._lock:
            self._thread = None
            self.current_name = None
            self.current_url = None
            self.last_title = ""

    def start(self, name: str, url: str) -> None:
        """Start playing a station with the given name and stream URL."""
        # Stop existing run first (outside lock to let joins happen cleanly)
        self.stop()

        with self._lock:
            self.stop_event.clear()
            self.current_name = name
            self.current_url = url
            self.last_station = name
            save_state_json(self.stations, self.last_station, self.freq)

            logger.info("Starting station=%r freq=%r url=%r", name, self.freq, url)

            self._thread = threading.Thread(target=self._run_forever, daemon=True)
            self._thread.start()

    def startup_autoplay(self) -> None:
        """Start playing the last selected station or the default station."""
        if self.last_station and self.last_station in self.stations:
            self.start(self.last_station, self.stations[self.last_station])
        else:
            self.start(DEFAULT_STATION_NAME, DEFAULT_STREAM_URL)

    def _run_forever(self) -> None:
        """Run the streaming loop with exponential backoff on restarts."""
        backoff = 1.0
        max_backoff = 15.0

        while not self.stop_event.is_set():
            try:
                with self._lock:
                    url = self.current_url
                    freq = self.freq or DEFAULT_FREQ
                if not url:
                    raise RestartReason("no current URL set")

                self._run_once(url=url, freq=freq)
                backoff = 1.0
            except RestartReason as rr:
                if self.stop_event.is_set():
                    break
                logger.warning("Restarting because: %s", rr.reason)
            except Exception:
                if self.stop_event.is_set():
                    break
                logger.exception("Unexpected exception, restarting")

            time.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)

        logger.info("Background streamer thread exiting")

    def _run_once(self, url: str, freq: str, stall_seconds: int = 10) -> None:
        """Run one streaming cycle: start pifmrds and GStreamer pipeline.

        Streams until error, audio stall, or stop event. Raises RestartReason on failure.
        """
        ensure_fifo(RDS_CTL_FIFO)

        # Reset per-run handle
        self._run = _RunHandle()

        # Open FIFO and start pifmrds
        ctl_fd = open_ctl_fifo_rdwr(RDS_CTL_FIFO)
        self._run.ctl_fd = ctl_fd

        pifmrds_cmd = [
            "pifmrds",
            "-freq", freq,
            "-ctl", RDS_CTL_FIFO,
            "-audio", "-",
        ]

        # pylint: disable=W1509
        pif = subprocess.Popen(
            pifmrds_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            preexec_fn=set_nice(-20),
        )
        # pylint: enable=W1509
        self._run.pif = pif

        # Send initial PS/RT
        self._ctl_send(f"PS {safe_ps(DEFAULT_PS)}")
        self._ctl_send(f"RT {safe_rt(DEFAULT_RT)}")

        # stderr reader (logging component from the Flask version)
        def read_stderr(name: str, proc: subprocess.Popen) -> None:
            try:
                if proc.stderr:
                    for line in iter(proc.stderr.readline, b""):
                        if not line:
                            break
                        logger.error("[%s] %s", name, line.decode("utf-8", "ignore").rstrip())
            except Exception:
                logger.exception("Error reading %s stderr", name)

        threading.Thread(target=read_stderr, args=("pifmrds", pif), daemon=True).start()

        # Build GStreamer pipeline
        pipeline_str = (
            f'uridecodebin uri="{url}" name=d '
            f'd. ! queue ! audioconvert ! audioresample '
            f'! audio/x-raw,format=S16LE,rate=76000,channels=2 '
            f'! wavenc ! appsink name=out emit-signals=true sync=false max-buffers=200 drop=false'
        )
        pipeline = Gst.parse_launch(pipeline_str)
        self._run.pipeline = pipeline  # type: ignore[assignment]

        appsink = pipeline.get_by_name("out")
        if appsink is None:
            raise RestartReason("appsink element not found")

        loop = GLib.MainLoop()
        self._run.loop = loop

        bus = pipeline.get_bus()
        bus.add_signal_watch()

        last_audio_write_monotonic = time.monotonic()
        last_title = None
        last_org = None

        def request_restart(reason: str) -> None:
            # Record reason once, then quit loop
            if self._run.restart_reason["reason"] is None:
                self._run.restart_reason["reason"] = reason
            try:
                loop.quit()
            except Exception:
                pass

        def on_new_sample(sink):
            nonlocal last_audio_write_monotonic
            sample = sink.emit("pull-sample")
            if sample is None:
                GLib.idle_add(request_restart, "appsink pull-sample returned None")
                return Gst.FlowReturn.EOS

            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK

            try:
                if pif.stdin:
                    pif.stdin.write(mapinfo.data)
                    last_audio_write_monotonic = time.monotonic()
            except BrokenPipeError:
                GLib.idle_add(request_restart, "pifmrds stdin broken pipe")
                return Gst.FlowReturn.EOS
            finally:
                buf.unmap(mapinfo)

            return Gst.FlowReturn.OK

        appsink.connect("new-sample", on_new_sample)

        def on_message(_bus, msg):
            nonlocal last_title, last_org
            t = msg.type

            if t == Gst.MessageType.TAG:
                taglist = msg.parse_tag()

                ok, title = taglist.get_string("title")
                if ok and title and title != last_title:
                    last_title = title
                    with self._lock:
                        self.last_title = title
                    logger.info("RT: %s", title)
                    self._ctl_send(f"RT {safe_rt(title)}")

                ok, org = taglist.get_string("organization")
                if ok and org and org != last_org:
                    last_org = org
                    logger.info("PS: %s", org)
                    self._ctl_send(f"PS {safe_ps(org)}")

            elif t == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                logger.error("GStreamer ERROR: %s", err)
                if dbg:
                    logger.error("GStreamer DEBUG: %s", dbg)
                request_restart("gstreamer error")

            elif t == Gst.MessageType.EOS:
                logger.warning("GStreamer EOS")
                request_restart("gstreamer eos")

        bus.connect("message", on_message)

        # Watchdog: pifmrds exit
        def watch_pifmrds():
            if self.stop_event.is_set():
                request_restart("stop event set")
                return False
            rc = pif.poll()
            if rc is not None:
                request_restart(f"pifmrds exited rc={rc}")
                return False
            return True

        # Watchdog: audio stall
        def watch_stall():
            if self.stop_event.is_set():
                request_restart("stop event set")
                return False
            elapsed = time.monotonic() - last_audio_write_monotonic
            if elapsed >= stall_seconds:
                request_restart(f"audio stalled for {elapsed:.1f}s")
                return False
            return True

        GLib.timeout_add_seconds(1, watch_pifmrds)
        GLib.timeout_add_seconds(1, watch_stall)

        # Start pipeline
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RestartReason("failed to set pipeline to PLAYING")

        try:
            loop.run()
        finally:
            # Cleanup order: stop pipeline, close stdin, terminate pifmrds, close fifo
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

            try:
                if pif.stdin:
                    pif.stdin.close()
            except Exception:
                pass

            try:
                if pif.poll() is None:
                    pif.terminate()
                    try:
                        pif.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pif.kill()
            except Exception:
                pass

            try:
                os.close(ctl_fd)
            except Exception:
                pass

        reason = self._run.restart_reason["reason"]
        if self.stop_event.is_set():
            raise RestartReason("stopped")
        if reason:
            raise RestartReason(reason)
        raise RestartReason("exited without explicit reason")
