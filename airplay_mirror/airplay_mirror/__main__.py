"""Entry point: ``python -m airplay_mirror``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__
from .config import load_settings
from .engine import Engine
from .groups import GroupStore
from .mqtt import MqttBridge, supervisor_mqtt
from .owntone import OwnToneClient
from .procs import Supervisor
from .runner import Runner
from .web import build_app


async def main_async(options_path: str | None) -> int:
    settings = load_settings(options_path)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    log = logging.getLogger("airplay_mirror")
    log.info("AirPlay Mirror %s starting (data in %s)", __version__, settings.data_dir)
    for d in (settings.pipes_dir, settings.meta_dir, settings.owntone_dir, settings.shairport_dir):
        Path(d).mkdir(parents=True, exist_ok=True)

    engine = Engine(settings)
    store = GroupStore(settings.groups_file, settings.max_groups)
    supervisor = Supervisor()
    async with aiohttp.ClientSession() as http:
        client = OwnToneClient(settings.owntone_url, settings.owntone_ws_url, http)
        runner = Runner(settings, engine, store, supervisor, client)
        supervisor.on_exit = runner.on_proc_exit

        app = build_app(runner)
        web_runner = web.AppRunner(app, access_log=None)
        await web_runner.setup()
        site = web.TCPSite(web_runner, "0.0.0.0", settings.web_port)
        await site.start()
        log.info("Web UI listening on port %s", settings.web_port)

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        try:
            await runner.start()
        except Exception:  # noqa: BLE001
            log.exception("startup failed")

        mqtt_task = None
        if settings.mqtt_enabled and (settings.mqtt_host or await supervisor_mqtt(settings)):
            bridge = MqttBridge(settings, runner)
            runner.bridge = bridge
            mqtt_task = asyncio.create_task(bridge.run(), name="mqtt")
        elif settings.mqtt_enabled:
            log.info("MQTT not configured: set mqtt_host or install the Mosquitto add-on to enable it")

        await stop.wait()
        log.info("Shutting down")
        if mqtt_task is not None:
            runner.bridge.stop()
            mqtt_task.cancel()
            try:
                await mqtt_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await runner.stop()
        await web_runner.cleanup()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Virtual AirPlay speakers that relay to groups of real speakers")
    parser.add_argument("--options", help="path to a JSON options file (default: /data/options.json)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.options)))


if __name__ == "__main__":
    main()
