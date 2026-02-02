#!/usr/bin/env python3
import sys
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib

Gst.init(None)

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def on_message(bus, msg, loop):
    t = msg.type

    if t == Gst.MessageType.TAG:
        taglist = msg.parse_tag()

        # Print all tags that arrive (StreamTitle usually maps to "title")
        tags = {}
        n = taglist.n_tags()
        for i in range(n):
            name = taglist.nth_tag_name(i)
            # Get the first value for that tag
            ok, val = taglist.get_string(name)
            if ok:
                tags[name] = val
            else:
                # fall back for non-string tags
                try:
                    val = taglist.get_value_index(name, 0)
                    tags[name] = val
                except Exception:
                    pass

        # Print in a stable, readable way
        if tags:
            # Common “now playing” fields first if present
            for k in ("title", "artist", "album", "organization", "genre"):
                if k in tags:
                    eprint(f"{k}: {tags[k]}")
            # Then print everything (including icy-ish fields if exposed)
            eprint("tags:", tags)

    elif t == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        eprint("ERROR:", err)
        if dbg:
            eprint("DEBUG:", dbg)
        loop.quit()

    elif t == Gst.MessageType.EOS:
        eprint("EOS")
        loop.quit()

def main():
    if len(sys.argv) != 2:
        eprint(f"Usage: {sys.argv[0]} <stream_url>")
        return 2

    url = sys.argv[1]

    # Pipeline:
    # - uridecodebin auto-detects container/codec
    # - audioconvert/audioresample normalize audio
    # - caps force raw PCM S16LE, 44.1kHz, stereo
    # - fdsink writes binary PCM to stdout (fd=1)
    #
    # Important: do NOT print anything to stdout from Python.
    pipeline_str = (
        f'uridecodebin uri="{url}" name=d '
        f'd. ! queue ! audioconvert ! audioresample '
        f'! audio/x-raw,format=S16LE,rate=44100,channels=2 '
        f'! fdsink fd=1 sync=false'
    )

    pipeline = Gst.parse_launch(pipeline_str)

    bus = pipeline.get_bus()
    bus.add_signal_watch()

    loop = GLib.MainLoop()
    bus.connect("message", on_message, loop)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        eprint("Failed to start pipeline")
        pipeline.set_state(Gst.State.NULL)
        return 1

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    return 0

if __name__ == "__main__":
    sys.exit(main())

