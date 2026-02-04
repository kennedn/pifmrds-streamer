"""Flask web application for pifmrds-streamer."""
import os
from flask import Flask, redirect, render_template, request

from src.gstreamer import (
    DEFAULT_STATION_NAME,
    DEFAULT_STREAM_URL,
    RadioController,
    save_state_json,
)


def create_app(ctl: RadioController) -> Flask:
    """Create and configure the Flask application."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    templates_dir = os.path.join(project_root, "templates")
    static_dir = os.path.join(project_root, "static")
    app = Flask(__name__, template_folder=templates_dir, static_folder=static_dir)

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            stations=ctl.stations,
            freq=ctl.freq,
            now_name=ctl.current_name,
            now_url=ctl.current_url,
            rds=ctl.last_title,
            is_playing=(not ctl.stop_event.is_set() and ctl.current_url is not None),
            default=DEFAULT_STATION_NAME,
        )

    @app.post("/add")
    def add():
        name = request.form["name"].strip()
        url = request.form["url"].strip()
        if name != DEFAULT_STATION_NAME:
            ctl.stations[name] = url
        save_state_json(ctl.stations, ctl.last_station, ctl.freq)
        return redirect("/")

    @app.post("/delete")
    def delete():
        name = request.form["name"]
        if name != DEFAULT_STATION_NAME:
            ctl.stations.pop(name, None)
        save_state_json(ctl.stations, ctl.last_station, ctl.freq)
        return redirect("/")

    @app.post("/freq")
    def freq():
        ctl.freq = request.form["freq"].strip()
        save_state_json(ctl.stations, ctl.last_station, ctl.freq)
        # Restart current station (or default) on frequency change
        name = ctl.last_station or DEFAULT_STATION_NAME
        url = ctl.stations.get(name, DEFAULT_STREAM_URL)
        ctl.start(name, url)
        return redirect("/")

    @app.post("/select")
    def select():
        save_state_json(ctl.stations, ctl.last_station, ctl.freq)
        name = request.form["name"]
        ctl.start(name, ctl.stations[name])
        return redirect("/")

    @app.post("/stop")
    def stop():
        ctl.stop()
        return redirect("/")

    return app
