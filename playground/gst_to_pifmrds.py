#!/usr/bin/env python3
import os
import sys
import time
import signal
import subprocess
import gi

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib

Gst.init(None)

def eprint(*a, **k):
    print(*a, file=sys.stderr, **k)

def ps_sanitize(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "RADIO"
    return s[:8]

def rt_sanitize(s: str) -> str:
    s = (s or "").strip()
    return s[:64] if s else ""

class RestartReason(Exception):
    """Internal control flow: raised to restart the whole stack."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason

def ensure_fifo(path: str):
    try:
        if not os.path.exists(path):
            os.mkfifo(path)
    except FileExistsError:
        pass

def open_ctl_fifo_rdwr(path: str) -> int:
    # O_RDWR avoids blocking if the other end isn't open yet.
    # O_CLOEXEC avoids leaking fd into exec'd children.
    return os.open(path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))

def run_once(url: str, freq: str, ps_default: str, rt_default: str,
             ctl_fifo: str = "/tmp/rds_ctl",
             stall_seconds: int = 10):
    """
    Run one lifecycle: start pifmrds + pipeline, stream until error/stall/exit.
    On failure, raise RestartReason.
    """

    ensure_fifo(ctl_fifo)
    ctl_fd = open_ctl_fifo_rdwr(ctl_fifo)

    def ctl_send(line: str):
        try:
            os.write(ctl_fd, (line + "\n").encode("utf-8", "replace"))
        except OSError as ex:
            eprint("RDS ctl write failed:", ex)

    def pifmrds_preexec():
        # Requires root/CAP_SYS_NICE to succeed.
        try:
            os.nice(-20)
        except PermissionError:
            pass

    # Start pifmrds (assumes this script is run under sudo so it can access HW + nice)
    pifmrds_cmd = [
        "pifmrds",
        "-freq", freq,
        "-ps", ps_default,
        "-rt", rt_default,
        "-ctl", ctl_fifo,
        "-audio", "-"
    ]

    pif = subprocess.Popen(
        pifmrds_cmd,
        stdin=subprocess.PIPE,
        stderr=sys.stderr,
        preexec_fn=pifmrds_preexec,
    )

    # Initial PS/RT
    ctl_send(f"PS {ps_sanitize(ps_default)}")
    ctl_send(f"RT {rt_sanitize(rt_default)}")

    # GStreamer pipeline: URL -> decode -> S16LE -> WAV -> appsink
    pipeline_str = (
        f'uridecodebin uri="{url}" name=d '
        f'd. ! queue ! audioconvert ! audioresample '
        f'! audio/x-raw,format=S16LE,rate=44100,channels=2 '
        f'! wavenc ! appsink name=out emit-signals=true sync=false max-buffers=200 drop=false'
    )
    pipeline = Gst.parse_launch(pipeline_str)
    appsink = pipeline.get_by_name("out")

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    # Track liveness
    last_audio_write_monotonic = time.monotonic()
    last_title = None
    last_org = None
    last_genre = None

    # We’ll set this when we want to restart, then quit the loop.
    restart_reason = {"reason": None}

    def request_restart(reason: str):
        if restart_reason["reason"] is None:
            restart_reason["reason"] = reason
        # Quit the loop from the main thread.
        try:
            loop.quit()
        except Exception:
            pass

    def on_new_sample(sink):
        nonlocal last_audio_write_monotonic
        sample = sink.emit("pull-sample")
        if sample is None:
            # Treat as end-of-stream-ish
            request_restart("appsink pull-sample returned None")
            return Gst.FlowReturn.EOS

        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK

        try:
            if pif.stdin:
                pif.stdin.write(mapinfo.data)
                # Flushing every sample keeps latency down; if CPU is tight,
                # remove flush() and rely on OS buffering.
                pif.stdin.flush()
                last_audio_write_monotonic = time.monotonic()
        except BrokenPipeError:
            # pifmrds is gone
            # Don't rely on GStreamer propagating EOS; explicitly restart.
            GLib.idle_add(request_restart, "pifmrds stdin broken pipe")
            return Gst.FlowReturn.EOS
        finally:
            buf.unmap(mapinfo)

        return Gst.FlowReturn.OK

    appsink.connect("new-sample", on_new_sample)

    def on_message(bus, msg):
        nonlocal last_title, last_org, last_genre
        t = msg.type

        if t == Gst.MessageType.TAG:
            taglist = msg.parse_tag()

            ok, title = taglist.get_string("title")
            if ok and title and title != last_title:
                last_title = title
                eprint("title:", title)
                ctl_send(f"RT {rt_sanitize(title)}")

            ok, org = taglist.get_string("organization")
            if ok and org and org != last_org:
                last_org = org
                eprint("organization:", org)
                ctl_send(f"PS {ps_sanitize(org)}")

            ok, genre = taglist.get_string("genre")
            if ok and genre and genre != last_genre:
                last_genre = genre
                eprint("genre:", genre)

        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            eprint("GStreamer ERROR:", err)
            if dbg:
                eprint("DEBUG:", dbg)
            request_restart("gstreamer error")

        elif t == Gst.MessageType.EOS:
            eprint("GStreamer EOS")
            request_restart("gstreamer eos")

    bus.connect("message", on_message)

    # Watchdog: pifmrds exit
    def watch_pifmrds():
        rc = pif.poll()
        if rc is not None:
            request_restart(f"pifmrds exited rc={rc}")
            return False
        return True

    # Watchdog: audio stall (no bytes written)
    def watch_stall():
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
        # Cleanup in a safe order
        try:
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass

        try:
            if pif.stdin:
                pif.stdin.close()
        except Exception:
            pass

        # Give pifmrds a moment to exit cleanly; then kill if needed
        try:
            rc = pif.poll()
            if rc is None:
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

    reason = restart_reason["reason"]
    if reason:
        raise RestartReason(reason)

    # If we exited without a reason, still restart (unexpected)
    raise RestartReason("exited without explicit reason")

def main():
    if len(sys.argv) < 2:
        eprint(f"Usage: {sys.argv[0]} <stream_url> [freq_MHz] [ps_default] [rt_default]")
        return 2

    url = sys.argv[1]
    freq = sys.argv[2] if len(sys.argv) >= 3 else "100.0"
    ps_default = sys.argv[3] if len(sys.argv) >= 4 else "PiRadio"
    rt_default = sys.argv[4] if len(sys.argv) >= 5 else "Streaming"

    # Backoff between restarts to avoid tight crash loops
    backoff = 1.0
    max_backoff = 15.0

    while True:
        try:
            eprint(f"Starting: freq={freq} ps={ps_default!r} rt={rt_default!r} url={url!r}")
            run_once(url, freq, ps_default, rt_default, stall_seconds=10)
            backoff = 1.0
        except RestartReason as rr:
            eprint(f"Restarting because: {rr.reason}")
        except KeyboardInterrupt:
            eprint("Interrupted, exiting.")
            return 0
        except Exception as ex:
            eprint("Unexpected exception, restarting:", repr(ex))

        time.sleep(backoff)
        backoff = min(max_backoff, backoff * 2)

if __name__ == "__main__":
    sys.exit(main())

