"""Ingress web UI and JSON API."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__
from .groups import GroupError, parse_speakers
from .owntone import OwnToneError
from .runner import Runner

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
RUNNER = web.AppKey("runner", Runner)


def build_app(runner: Runner) -> web.Application:
    app = web.Application(middlewares=[errors_middleware])
    app[RUNNER] = runner
    app.add_routes(
        [
            web.get("/", index),
            web.get("/index.html", index),
            web.get("/api/health", health),
            web.get("/api/state", state),
            web.get("/api/outputs", outputs),
            web.get("/api/groups", groups_list),
            web.post("/api/groups", groups_create),
            web.put("/api/groups/{id}", groups_update),
            web.delete("/api/groups/{id}", groups_delete),
            web.post("/api/outputs/{id}/select", output_select),
            web.post("/api/outputs/{id}/pair", output_pair),
            web.post("/api/session/stop", session_stop),
            web.post("/api/rescan", rescan),
            web.post("/api/hook/{group_id}/{kind}", hook),
        ]
    )
    return app


@web.middleware
async def errors_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except GroupError as exc:
        return web.json_response({"ok": False, "errors": exc.errors}, status=400)
    except OwnToneError as exc:
        return web.json_response({"ok": False, "errors": [str(exc)]}, status=502)


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text="expected a JSON body") from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return body


async def index(request: web.Request) -> web.Response:
    html = (STATIC / "index.html").read_text()
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": __version__})


async def state(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    data = runner.snapshot()
    data["version"] = __version__
    data["settings"] = {
        "port_base": runner.settings.port_base,
        "owntone_port": runner.settings.owntone_port,
        "max_groups": runner.settings.max_groups,
    }
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def outputs(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    await runner.refresh_outputs()
    return web.json_response({"outputs": runner.engine.snapshot()["outputs"]}, headers={"Cache-Control": "no-store"})


def _speaker_names(runner: Runner) -> set[str]:
    return {o.name.lower() for o in runner.engine.outputs.values()}


async def groups_list(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    return web.json_response({"groups": [g.as_dict() for g in runner.store.all()]})


async def groups_create(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    body = await _json(request)
    group = runner.store.create(
        str(body.get("name", "")), parse_speakers(body.get("speakers", [])), reserved_names=_speaker_names(runner)
    )
    await runner.apply_groups()
    return web.json_response({"ok": True, "group": group.as_dict()}, status=201)


async def groups_update(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    body = await _json(request)
    try:
        group = runner.store.update(
            request.match_info["id"],
            str(body.get("name", "")),
            parse_speakers(body.get("speakers", [])),
            reserved_names=_speaker_names(runner),
        )
    except KeyError:
        raise web.HTTPNotFound(text="unknown group") from None
    await runner.apply_groups()
    return web.json_response({"ok": True, "group": group.as_dict()})


async def groups_delete(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    try:
        runner.store.delete(request.match_info["id"])
    except KeyError:
        raise web.HTTPNotFound(text="unknown group") from None
    await runner.apply_groups()
    return web.json_response({"ok": True})


async def output_select(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    body = await _json(request)
    await runner.select_output(request.match_info["id"], bool(body.get("selected", True)))
    return web.json_response({"ok": True})


async def output_pair(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    body = await _json(request)
    pin = str(body.get("pin", "")).strip()
    if not pin:
        raise web.HTTPBadRequest(text="pin is required")
    await runner.pair(request.match_info["id"], pin)
    return web.json_response({"ok": True})


async def session_stop(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    await runner.stop_session()
    return web.json_response({"ok": True})


async def rescan(request: web.Request) -> web.Response:
    runner: Runner = request.app[RUNNER]
    await runner.rescan()
    return web.json_response({"ok": True})


async def hook(request: web.Request) -> web.Response:
    """Called by the shairport-sync session hooks on this host only."""
    runner: Runner = request.app[RUNNER]
    if request.remote not in LOOPBACK:
        raise web.HTTPForbidden(text="hooks are accepted from localhost only")
    kind = request.match_info["kind"]
    if kind not in ("start", "stop"):
        raise web.HTTPBadRequest(text="kind must be start or stop")
    group_id = request.match_info["group_id"]
    task = asyncio.ensure_future(runner.hook(group_id, kind))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=runner.settings.hook_timeout_seconds)
    except TimeoutError:
        log.warning(
            "hook %s %s still running after %ss; letting the receiver continue",
            kind,
            group_id,
            runner.settings.hook_timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        log.exception("hook %s %s failed", kind, group_id)
    return web.json_response({"ok": True})
