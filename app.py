#!/usr/bin/env python3
"""Main entry point for pifmrds-streamer."""

import logging
import shutil

from src.flask_app import create_app
from src.gstreamer import (
    APP_NAME,
    RadioController,
    load_state_json,
    save_state_json,
)

logger = logging.getLogger(APP_NAME)

WEB_HOST = "0.0.0.0"
WEB_PORT = 80

REQUIRED_CMDS = ["pifmrds"]


def main() -> None:
    """ entry point for pifmrds-streamer. """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
    )

    logger.info("Starting %s (GStreamer)", APP_NAME)

    for cmd in REQUIRED_CMDS:
        if not shutil.which(cmd):
            raise SystemExit(f"Missing command: {cmd}")

    stations, last, freq_val = load_state_json()

    ctl = RadioController()
    ctl.stations = stations
    ctl.last_station = last
    ctl.freq = freq_val

    save_state_json(stations, last, freq_val)
    ctl.startup_autoplay()

    app = create_app(ctl)
    app.run(WEB_HOST, WEB_PORT)


if __name__ == "__main__":
    main()
