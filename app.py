#!/usr/bin/env python3
"""
pifmrds-streamer
"""

import json
import os
import re
import stat
import shutil
import threading
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

from flask import Flask, request, redirect, render_template_string

# -------------------------
# Configuration
# -------------------------

APP_NAME = "pifmrds-streamer"

CONFIG_DIR = Path.home() / ".config" / APP_NAME
STATE_FILE = CONFIG_DIR / "stations.json"

RDS_CTL_FIFO = f"/tmp/{APP_NAME}_rds_ctl"

FREQ = "100.0"

DEFAULT_STATION_NAME = "Dance UK (default)"
DEFAULT_STREAM_URL = "http://51.89.148.171:8022/"
DEFAULT_PS = "DanceUK"

WEB_HOST = "0.0.0.0"
WEB_PORT = 80

REQUIRED_CMDS = ["pifmrds", "sox"]

STREAMTITLE_RE = re.compile(r"StreamTitle='([^']*)';", re.IGNORECASE)

def safe_ps(text: str) -> str:
    """Sanitize text for RDS PS field (max 8 chars, alphanumeric + space)."""
    t = re.sub(r"[^0-9A-Za-z ]+", "", text).strip()
    return (t or DEFAULT_PS)[:8]

def safe_rt(text: str) -> str:
    """Sanitize text for RDS RT field (max 64 chars)."""
    return (text.strip() or " ")[:64]

def load_state_json() -> Tuple[Dict[str, str], Optional[str]]:
    """Load stations and last selected station from state file."""
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
    """Save stations and last selected station to state file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stations": stations, "last": last}, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)

def enforce_default_station(stations: Dict[str, str]) -> None:
    """Ensure the default station exists in the stations dictionary."""
    stations[DEFAULT_STATION_NAME] = DEFAULT_STREAM_URL

@dataclass
class RadioController:
    """Manages radio streaming, RDS metadata updates, and metadata fetching."""
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
        """Create RDS control FIFO if it doesn't exist."""
        if os.path.exists(RDS_CTL_FIFO):
            if not stat.S_ISFIFO(os.stat(RDS_CTL_FIFO).st_mode):
                os.remove(RDS_CTL_FIFO)
        if not os.path.exists(RDS_CTL_FIFO):
            os.mkfifo(RDS_CTL_FIFO)

    def _open_ctl(self):
        """Open a file descriptor to the RDS control FIFO."""
        if self.ctl_file:
            return
        self._ensure_fifo()
        self.ctl_fd = os.open(RDS_CTL_FIFO, os.O_RDWR | os.O_NONBLOCK)
        self.ctl_file = os.fdopen(self.ctl_fd, "w", buffering=1)

    def _write_ctl(self, line: str):
        """Write a command line to the RDS control FIFO."""
        if not self.ctl_file:
            self._open_ctl()
        self.ctl_file.write(line.rstrip() + "\n")
        self.ctl_file.flush()

    # ---- lifecycle ----

    def stop(self):
        """Stop the currently playing station and terminate all processes."""
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
        """Start playing a station with the given name and stream URL."""
        self.stop()
        self.stop_event.clear()
        self._ensure_fifo()

        # Start pifmrds
        self.pifmrds_proc = subprocess.Popen(
            ["nice", "-10", "pifmrds", "-freq", FREQ, "-ctl", RDS_CTL_FIFO, "-audio", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0
        )

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

    def _metadata_loop(self, url: str):
        """Fetch and update RDS metadata from ICY stream headers."""
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
        """Start playing the last selected station or the default station."""
        if self.last_station and self.last_station in self.stations:
            self.start(self.last_station, self.stations[self.last_station])
        else:
            self.start(DEFAULT_STATION_NAME, DEFAULT_STREAM_URL)

app = Flask(__name__)
ctl = RadioController()

TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <title>pifmrds-streamer</title>

  <!-- Material Symbols -->
  <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined"
        rel="stylesheet">

  <style>
    :root {
      --bg: #f6f7f9;
      --card: #ffffff;
      --border: #e0e0e0;
      --text: #111;
      --muted: #666;
      --accent: #25a49f;
      --danger: #b00020;
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f1115;
        --card: #171a21;
        --border: #2a2e39;
        --text: #f1f1f1;
        --muted: #9aa0aa;
        --accent: #125250;
        --danger: #6f0015;
      }
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg);
      color: var(--text);
    }

    header {
      padding: 16px;
      background: var(--card);
      border-bottom: 1px solid var(--border);
    }

    header h1 {
      margin: 0;
      font-size: 1.25rem;
    }

    main {
      max-width: 900px;
      margin: 0 auto;
      padding: 12px;
    }

    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 12px;
    }

    .row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }

    .grow {
      flex: 1;
      min-width: 0;
    }

    .muted {
      color: var(--muted);
      font-size: 0.9rem;
    }

    h2, h3 {
      margin: 0 0 8px 0;
      font-size: 1rem;
    }

    input[type=text] {
      width: 100%;
      padding: 10px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 1rem;
    }

    button {
      padding: 10px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--text);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }

    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }

    button.danger {
      background: var(--danger);
      color: #fff;
      border-color: var(--danger);
    }

    button:active {
      transform: scale(0.96);
    }

    .material-symbols-outlined {
      font-size: 22px;
      line-height: 1;
    }


    .station-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }

    @media (max-width: 600px) {
      header h1 {
        font-size: 1.1rem;
      }

      button {
        width: 100%;
      }

      .station-actions button {
        flex: 1;
      }
    }
  </style>
</head>

<body>
  <header>
    <h1>pifmrds-streamer</h1>
  </header>

  <main>

    <div class="card">
      <h3>Now playing</h3>
      <div><strong>{{ now_name or "—" }}</strong></div>
      <div class="muted">{{ now_url or "" }}</div>
      <div class="muted">{{ rds or "—" }}</div>

      <div class="station-actions">
        <form method="post" action="/stop">
          <button class="danger" type="submit" aria-label="Stop">
            <span class="material-symbols-outlined">stop</span>
          </button>
        </form>
      </div>
    </div>

    <div class="card">
      <h3>Add / update station</h3>
      <form method="post" action="/add" style="margin-top:10px;">
        <div class="row">
          <div class="grow">
            <input type="text" name="name" placeholder="Station name" required>
          </div>
          <div class="grow">
            <input type="text" name="url" placeholder="Stream URL" required>
          </div>
        </div>
        <div style="margin-top:10px;">
          <button class="primary" type="submit">
            <span class="material-symbols-outlined">save</span>
          </button>
        </div>
      </form>
    </div>

    <h2>Stations</h2>

    {% for name, url in stations.items() %}
      <div class="card">
        <div><strong>{{ name }}</strong></div>
        <div class="muted">{{ url }}</div>

        <div class="station-actions">
          <form method="post" action="/select">
            <input type="hidden" name="name" value="{{ name }}">
            <button class="primary" type="submit" aria-label="Play">
              <span class="material-symbols-outlined">play_arrow</span>
            </button>
          </form>

          {% if name != default %}
            <form method="post" action="/delete"
                  onsubmit="return confirm('Delete station {{ name }}?');">
              <input type="hidden" name="name" value="{{ name }}">
              <button class="danger" type="submit" aria-label="Delete">
                <span class="material-symbols-outlined">delete</span>
              </button>
            </form>
          {% endif %}
        </div>
      </div>
    {% endfor %}

  </main>
</body>
</html>
"""


@app.route("/")
def index():
    """Render the main web UI page."""
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
    """Add or update a station with the provided name and URL."""
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    if name != DEFAULT_STATION_NAME:
        ctl.stations[name] = url
    save_state_json(ctl.stations, ctl.last_station)
    return redirect("/")

@app.post("/delete")
def delete():
    """Delete a station (unless it's the default station)."""
    name = request.form["name"]
    if name != DEFAULT_STATION_NAME:
        ctl.stations.pop(name, None)
    save_state_json(ctl.stations, ctl.last_station)
    return redirect("/")

@app.post("/select")
def select():
    """Select and start playing a station."""
    name = request.form["name"]
    ctl.start(name, ctl.stations[name])
    return redirect("/")

@app.post("/stop")
def stop():
    """Stop the currently playing station."""
    ctl.stop()
    return redirect("/")

def main():
    """Initialize the application and start the Flask web server."""
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

