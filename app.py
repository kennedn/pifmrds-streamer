#!/usr/bin/env python3
"""
pifmrds-streamer

- Web UI to add/select/delete stations (Flask)
- Stores stations + last selected station as JSON in:
    /root/.config/pifmrds-streamer/stations.json
  (runs as root)
- Autoplays last station on boot; falls back to the default station "Dance UK"
- Default station "Dance UK" is protected: cannot be deleted or overwritten
- Streams audio via sox -> pifmrds stdin
- Updates RDS PS/RT at runtime via pifmrds -ctl FIFO, using ICY StreamTitle where available
- Metadata fetching/parsing is pure Python (no curl, no ffmpeg)
"""

import json
import os
import re
import time
import stat
import shutil
import socket
import threading
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

from flask import Flask, request, redirect, url_for, render_template_string

# -------------------------
# Configuration
# -------------------------

APP_NAME = "pifmrds-streamer"

CONFIG_DIR = Path.home() / ".config" / APP_NAME
STATE_FILE = CONFIG_DIR / "stations.json"

RDS_CTL_FIFO = f"/tmp/{APP_NAME}_rds_ctl"

FREQ = "100.0"

DEFAULT_STATION_NAME = "Dance UK"
DEFAULT_STREAM_URL = "http://51.89.148.171:8022/"
DEFAULT_PS = "DanceUK"  # <= 8 chars

WEB_HOST = "0.0.0.0"
WEB_PORT = 80

REQUIRED_CMDS = ["pifmrds", "sox"]

def set_nice(n: int):
    def _fn():
        os.nice(n)
    return _fn

# -------------------------
# ICY metadata
# -------------------------

STREAMTITLE_RE = re.compile(r"StreamTitle='([^']*)';", re.IGNORECASE)

def safe_ps(text: str) -> str:
    t = re.sub(r"[^0-9A-Za-z ]+", "", text).strip()
    return (t or DEFAULT_PS)[:8]

def safe_rt(text: str) -> str:
    return (text.strip() or " ")[:64]

# -------------------------
# Persistence
# -------------------------

def load_state_json() -> Tuple[Dict[str, str], Optional[str]]:
    if not STATE_FILE.exists():
        return {}, None
    try:
        data = json.loads(STATE_FILE.read_text())
    except Exception:
        return {}, None
    stations = data.get("stations", {})
    last = data.get("last")
    return dict(stations), last if isinstance(last, str) else None

def save_state_json(stations: Dict[str, str], last: Optional[str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stations": stations, "last": last}, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)

def enforce_default_station(stations: Dict[str, str]) -> None:
    stations[DEFAULT_STATION_NAME] = DEFAULT_STREAM_URL

# -------------------------
# Controller
# -------------------------

@dataclass
class RadioController:
    stations: Dict[str, str] = field(default_factory=dict)
    last_station: Optional[str] = None

    current_name: Optional[str] = None
    current_url: Optional[str] = None
    last_title: str = ""

    pifmrds_proc: Optional[subprocess.Popen] = None
    sox_proc: Optional[subprocess.Popen] = None

    ctl_fd: Optional[int] = None
    ctl_file = None  # type: ignore

    stop_event: threading.Event = field(default_factory=threading.Event)

    # ---- FIFO handling ----

    def _ensure_fifo(self):
        if os.path.exists(RDS_CTL_FIFO):
            if not stat.S_ISFIFO(os.stat(RDS_CTL_FIFO).st_mode):
                os.remove(RDS_CTL_FIFO)
        if not os.path.exists(RDS_CTL_FIFO):
            os.mkfifo(RDS_CTL_FIFO)

    def _open_ctl(self):
        if self.ctl_file:
            return
        self._ensure_fifo()
        self.ctl_fd = os.open(RDS_CTL_FIFO, os.O_RDWR | os.O_NONBLOCK)
        self.ctl_file = os.fdopen(self.ctl_fd, "w", buffering=1)

    def _write_ctl(self, line: str):
        if not self.ctl_file:
            self._open_ctl()
        self.ctl_file.write(line.rstrip() + "\n")
        self.ctl_file.flush()

    # ---- lifecycle ----

    def stop(self):
        self.stop_event.set()
        for p in (self.sox_proc, self.pifmrds_proc):
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        self.sox_proc = None
        self.pifmrds_proc = None
        self.current_name = None
        self.current_url = None
        self.last_title = ""

    def start(self, name: str, url: str):
        self.stop()
        self.stop_event.clear()
        self._ensure_fifo()

        # Start pifmrds
        self.pifmrds_proc = subprocess.Popen(
            ["pifmrds", "-freq", FREQ, "-ctl", RDS_CTL_FIFO, "-audio", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            preexec_fn=set_nice(-10),
        )

        time.sleep(0.2)
        if self.pifmrds_proc.poll() is not None:
            raise SystemExit("pifmrds failed to start")

        self._open_ctl()
        self._write_ctl(f"PS {DEFAULT_PS}")
        self._write_ctl("RT Starting...")

        # Start sox (exact original command)
        self.sox_proc = subprocess.Popen(
            ["sox", "-t", "mp3", url, "-t", "wav", "-"],
            stdout=self.pifmrds_proc.stdin,
            stderr=subprocess.PIPE,
        )

        time.sleep(0.2)
        if self.sox_proc.poll() is not None:
            raise SystemExit("sox failed to start")

        self.current_name = name
        self.current_url = url
        self.last_station = name
        save_state_json(self.stations, self.last_station)

        threading.Thread(
            target=self._metadata_loop,
            args=(url,),
            daemon=True
        ).start()

    # ---- metadata ----

    def _metadata_loop(self, url: str):
        try:
            req = urllib.request.Request(
                url,
                headers={"Icy-MetaData": "1", "User-Agent": APP_NAME},
            )
            resp = urllib.request.urlopen(req, timeout=15)
        except Exception:
            return

        # Set PS from stream metadata (station name) once
        station_name = resp.headers.get("icy-name")
        if station_name:
            self._write_ctl(f"PS {safe_ps(station_name)}")

        metaint = resp.headers.get("icy-metaint")
        if not metaint:
            return
        metaint = int(metaint)
        read = resp.read
        last = None

        try:
            while not self.stop_event.is_set():
                read(metaint)
                lb = read(1)
                if not lb:
                    break
                size = lb[0] * 16
                if not size:
                    continue
                meta = read(size).decode("utf-8", "ignore")
                m = STREAMTITLE_RE.search(meta)
                if m:
                    title = m.group(1).strip()
                    if title and title != last:
                        last = title
                        self.last_title = title
                        self._write_ctl(f"RT {safe_rt(title)}")
        finally:
            resp.close()

    def startup_autoplay(self):
        if self.last_station and self.last_station in self.stations:
            self.start(self.last_station, self.stations[self.last_station])
        else:
            self.start(DEFAULT_STATION_NAME, DEFAULT_STREAM_URL)

# -------------------------
# Web UI
# -------------------------

app = Flask(__name__)
ctl = RadioController()

TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>pifmrds-streamer</title>

  <style>
    body {
      font-family: sans-serif;
      max-width: 900px;
      margin: 20px auto;
      padding: 0 12px;
    }
    .card {
      border: 1px solid #ddd;
      border-radius: 10px;
      padding: 12px;
      margin: 10px 0;
    }
    .row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    input[type=text] {
      width: 100%;
      padding: 8px;
      box-sizing: border-box;
    }
    button {
      padding: 8px 12px;
      cursor: pointer;
    }
    .muted {
      color: #666;
      font-size: 0.9em;
    }
    .now {
      background: #f7f7f7;
    }
  </style>
</head>

<body>
  <h1>pifmrds-streamer</h1>

  <div class="card now">
    <div><strong>Now playing:</strong> {{ now_name or "—" }}</div>
    <div class="muted">{{ now_url or "" }}</div>
    <div class="muted">{{ rds or "—" }}</div>
    <div class="row" style="margin-top:10px;">
      <form method="post" action="/stop">
        <button type="submit">Stop</button>
      </form>
    </div>
  </div>

  <div class="card">
    <h3>Add / update station</h3>
    <form method="post" action="/add" style="margin-top:10px;">
      <div class="row">
        <div style="flex:1;">
          <div class="muted">Name</div>
          <input type="text" name="name" placeholder="Station name" required>
        </div>
        <div style="flex:3;">
          <div class="muted">Stream URL</div>
          <input type="text" name="url" placeholder="http(s)://..." required>
        </div>
      </div>
      <div style="margin-top:10px;">
        <button type="submit">Save</button>
      </div>
    </form>
  </div>

  <h2>Stations</h2>

  {% for name, url in stations.items() %}
    <div class="card">
      <div><strong>{{ name }}</strong></div>
      <div class="muted">{{ url }}</div>
      <div class="row" style="margin-top:10px;">
        <form method="post" action="/select">
          <input type="hidden" name="name" value="{{ name }}">
          <button type="submit">Play</button>
        </form>

        {% if name != default %}
          <form method="post" action="/delete"
                onsubmit="return confirm('Delete station {{ name }}?');">
            <input type="hidden" name="name" value="{{ name }}">
            <button type="submit">Delete</button>
          </form>
        {% endif %}
      </div>
    </div>
  {% endfor %}

</body>
</html>
"""


@app.route("/")
def index():
    enforce_default_station(ctl.stations)
    save_state_json(ctl.stations, ctl.last_station)
    return render_template_string(
        TEMPLATE,
        stations=ctl.stations,
        now_name=ctl.current_name,
        now_url=ctl.current_url,
        rds=ctl.last_title,
        default=DEFAULT_STATION_NAME,
    )

@app.post("/add")
def add():
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    if name != DEFAULT_STATION_NAME:
        ctl.stations[name] = url
    save_state_json(ctl.stations, ctl.last_station)
    return redirect("/")

@app.post("/delete")
def delete():
    name = request.form["name"]
    if name != DEFAULT_STATION_NAME:
        ctl.stations.pop(name, None)
    save_state_json(ctl.stations, ctl.last_station)
    return redirect("/")

@app.post("/select")
def select():
    name = request.form["name"]
    ctl.start(name, ctl.stations[name])
    return redirect("/")

@app.post("/stop")
def stop():
    ctl.stop()
    return redirect("/")

# -------------------------
# Main
# -------------------------

def main():
    for cmd in REQUIRED_CMDS:
        if not shutil.which(cmd):
            raise SystemExit(f"Missing command: {cmd}")

    stations, last = load_state_json()
    enforce_default_station(stations)

    ctl.stations = stations
    ctl.last_station = last

    save_state_json(stations, last)
    ctl.startup_autoplay()

    app.run(WEB_HOST, WEB_PORT)

if __name__ == "__main__":
    main()

