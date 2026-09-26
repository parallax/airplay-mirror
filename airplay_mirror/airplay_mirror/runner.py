"""Glue between the engine and the outside world: processes, OwnTone's API and the group store."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .config import Settings
from .engine import Action, Engine, PlayPipe, Rescan, RestartReceiver, SelectOutputs, SetOffsets, SetVolume, StopPlayer
from .groups import Group, GroupStore
from .metadata import MetadataRelay, TrackInfo
from .owntone import OwnToneClient, OwnToneError
from .procs import ProcSpec, Supervisor
from .templates import build_config_set, meta_path, pipe_path, shairport_conf_path, write_config_set

log = logging.getLogger(__name__)

OWNTONE = "owntone"


def receiver_name(group_id: str) -> str:
    return f"sps:{group_id}"


class Runner:
    def __init__(
        self, settings: Settings, engine: Engine, store: GroupStore, supervisor: Supervisor, client: OwnToneClient
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.store = store
        self.supervisor = supervisor
        self.client = client
        self.lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._known_outputs: set[str] | None = None
        self._relays: dict[str, tuple[MetadataRelay, asyncio.Task]] = {}

    # ---- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        groups = self.store.load()
        self.engine.set_groups(groups)
        write_config_set(self.settings, build_config_set(self.settings, groups))
        log.info("%d group(s) configured", len(groups))
        for group in groups:
            self._start_relay(group)

        if self.settings.supervise:
            await self.supervisor.start(self._owntone_spec())
            ready = await self.client.wait_ready(self.settings.owntone_ready_timeout)
            if not ready:
                log.error("OwnTone did not become ready in %ss; carrying on", self.settings.owntone_ready_timeout)
            else:
                log.info("OwnTone is ready on %s", self.settings.owntone_url)
            await self.execute(self.engine.on_owntone_state(ready))
            await self.refresh_outputs()
            if not groups:
                log.info("No groups defined yet: open the add-on panel and add one")
            for group in groups:
                await self.supervisor.start(self._receiver_spec(group))
                await asyncio.sleep(0.3)
        else:
            log.warning("Process supervision disabled (AM_SUPERVISE=false): web UI and config generation only")
            ready = await self.client.ready()
            await self.execute(self.engine.on_owntone_state(ready))
            await self.refresh_outputs()

        self._tasks = [
            asyncio.create_task(self.client.listen(self._on_notify, self._stop), name="owntone-ws"),
            asyncio.create_task(self._ticker(), name="ticker"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for group_id in list(self._relays):
            await self._stop_relay(group_id)
        receivers = [receiver_name(g.id) for g in self.store.all()]
        await self.supervisor.stop_all(order=[*receivers, OWNTONE])

    # ---- specs -------------------------------------------------------------------------------------

    def _owntone_spec(self) -> ProcSpec:
        return ProcSpec(name=OWNTONE, argv=[self.settings.owntone_bin, "-f", "-c", self.settings.owntone_conf])

    def _receiver_spec(self, group: Group) -> ProcSpec:
        return ProcSpec(
            name=receiver_name(group.id),
            argv=[self.settings.shairport_bin, "-u", "-c", shairport_conf_path(self.settings, group.id)],
            env={"AM_WEB_PORT": str(self.settings.web_port)},
            stop_timeout=5.0,
        )

    # ---- metadata relays ---------------------------------------------------------------------------

    def _start_relay(self, group: Group) -> None:
        if group.id in self._relays:
            return
        relay = MetadataRelay(
            group.id,
            meta_path(self.settings, group),
            f"{pipe_path(self.settings, group)}.metadata",
            on_track=self._on_track,
        )
        task = asyncio.create_task(relay.run(self._stop), name=f"meta:{group.id}")
        self._relays[group.id] = (relay, task)

    async def _stop_relay(self, group_id: str) -> None:
        entry = self._relays.pop(group_id, None)
        if entry is None:
            return
        _, task = entry
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _on_track(self, group_id: str, info: TrackInfo) -> None:
        self.engine.on_track(group_id, info.as_dict())

    # ---- groups ------------------------------------------------------------------------------------

    async def apply_groups(self) -> None:
        """Bring processes and config files in line with the store after a change."""
        async with self.lock:
            groups = self.store.all()
            await self.execute(self.engine.set_groups(groups))
            diff = write_config_set(self.settings, build_config_set(self.settings, groups))
            log.info(
                "Config applied: owntone_changed=%s changed=%s removed=%s new_pipes=%s",
                diff.owntone_changed,
                sorted(diff.changed_groups),
                sorted(diff.removed_groups),
                sorted(diff.new_pipes),
            )
            for group_id in diff.removed_groups:
                await self._stop_relay(group_id)
            for group in groups:
                self._start_relay(group)
            if not self.settings.supervise:
                return
            for group_id in diff.removed_groups:
                await self.supervisor.stop(receiver_name(group_id))
            for group in groups:
                name = receiver_name(group.id)
                if group.id in diff.changed_groups and self.supervisor.running(name):
                    await self.supervisor.restart(name)
                else:
                    await self.supervisor.start(self._receiver_spec(group))
            if diff.new_pipes and self.engine.owntone_up:
                await self._safe(self.client.rescan())
            if diff.owntone_changed:
                if self.engine.session is None:
                    await self._restart_owntone()
                else:
                    self.engine.pending_owntone_restart = True
                    self.engine.event("OwnTone will restart to pick up speaker changes once playback stops", "warning")

    async def _restart_owntone(self) -> None:
        self.engine.pending_owntone_restart = False
        self.engine.event("Restarting OwnTone to apply configuration")
        await self.execute(self.engine.on_owntone_state(False))
        await self.supervisor.restart(OWNTONE)
        ready = await self.client.wait_ready(self.settings.owntone_ready_timeout)
        await self.execute(self.engine.on_owntone_state(ready))
        await self.refresh_outputs()

    # ---- session events ----------------------------------------------------------------------------

    async def hook(self, group_id: str, kind: str, value: str | None = None) -> None:
        async with self.lock:
            if kind == "start":
                actions = self.engine.on_hook_start(group_id)
            elif kind == "volume":
                actions = self.engine.on_volume(group_id, value)
            else:
                actions = self.engine.on_hook_stop(group_id)
            await self.execute(actions)

    async def stop_session(self) -> None:
        async with self.lock:
            session = self.engine.session
            if session is None:
                return
            await self.execute(self.engine.on_hook_stop(session.group_id))
            if self.settings.supervise:
                await self.supervisor.restart(receiver_name(session.group_id))

    # ---- outputs -----------------------------------------------------------------------------------

    async def refresh_outputs(self) -> None:
        try:
            outputs = await self.client.outputs()
        except OwnToneError as exc:
            log.debug("outputs: %s", exc)
            return
        await self.execute(self.engine.on_outputs(outputs))
        names = {o.name for o in self.engine.outputs.values()}
        if names != self._known_outputs:
            self._known_outputs = names
            log.info("AirPlay speakers seen by OwnTone: %s", ", ".join(sorted(names)) or "none yet")
            if self.engine.hidden_outputs:
                log.info("Ignoring non-AirPlay outputs: %s", ", ".join(self.engine.hidden_outputs))

    async def pair(self, output_id: str, pin: str) -> None:
        await self.client.update_output(output_id, pin=pin)
        await self.refresh_outputs()

    async def select_output(self, output_id: str, selected: bool) -> None:
        await self.client.update_output(output_id, selected=selected)
        await self.refresh_outputs()

    async def preview_volume(self, levels: dict[str, int], offsets: dict[str, int] | None = None) -> None:
        await self.execute(self.engine.preview_volume(levels, offsets))

    async def reapply(self) -> None:
        async with self.lock:
            await self.execute(self.engine.reapply())

    async def rescan(self) -> None:
        await self.client.rescan()

    # ---- actions -----------------------------------------------------------------------------------

    async def execute(self, actions: list[Action]) -> None:
        for action in actions:
            try:
                await self._execute_one(action)
            except OwnToneError as exc:
                log.warning("%s failed: %s", type(action).__name__, exc)
                self.engine.event(f"OwnTone request failed: {exc}", "error")

    async def _execute_one(self, action: Action) -> None:
        if isinstance(action, SelectOutputs):
            ids = [i for i, _ in action.items]
            await self.client.set_outputs(ids)
            for output_id, volume in action.items:
                await self.client.update_output(output_id, selected=True, volume=volume)
            log.info("Selected outputs %s", ids)
        elif isinstance(action, SetVolume):
            for output_id, volume in action.items:
                await self.client.update_output(output_id, volume=volume)
            log.debug("Volume set: %s", action.items)
        elif isinstance(action, SetOffsets):
            for output_id, ms in action.items:
                await self.client.update_output(output_id, offset_ms=ms)
            log.info("Sync offsets set: %s", action.items)
        elif isinstance(action, PlayPipe):
            group = self.engine.groups.get(action.group_id)
            if group is None:
                return
            path = pipe_path(self.settings, group)
            for attempt in range(3):
                track_id = await self.client.find_track_id(path)
                if track_id is not None:
                    await self.client.queue_track(track_id)
                    log.info("Queued pipe %s as track %s", path, track_id)
                    return
                log.info("Pipe %s not in OwnTone's library yet (attempt %d); rescanning", path, attempt + 1)
                await self.client.rescan()
                await asyncio.sleep(1.5)
            self.engine.event("OwnTone has not indexed the pipe; try Rescan", "error", action.group_id)
        elif isinstance(action, StopPlayer):
            await self.client.stop()
        elif isinstance(action, RestartReceiver):
            if self.settings.supervise:
                await self.supervisor.restart(receiver_name(action.group_id))
        elif isinstance(action, Rescan):
            await self.client.rescan()

    async def _safe(self, coro) -> None:
        try:
            await coro
        except OwnToneError as exc:
            log.warning("%s", exc)

    # ---- background --------------------------------------------------------------------------------

    async def _on_notify(self, kinds: list[str]) -> None:
        if any(k in ("outputs", "update", "database") for k in kinds):
            await self.refresh_outputs()
        if "player" in kinds:
            await self._poll_player()

    async def _poll_player(self) -> None:
        try:
            player = await self.client.player()
            state = str(player.get("state", "stop"))
            path = None
            if state == "play":
                with contextlib.suppress(OwnToneError):
                    path = await self.client.queue_item_path(player.get("item_id"))
        except OwnToneError as exc:
            log.debug("player: %s", exc)
            return
        await self.execute(self.engine.on_player(state, path))

    async def _ticker(self) -> None:
        n = 0
        while not self._stop.is_set():
            await asyncio.sleep(1)
            n += 1
            try:
                async with self.lock:
                    await self.execute(self.engine.tick())
                    if self.engine.pending_owntone_restart and self.engine.session is None and self.settings.supervise:
                        await self._restart_owntone()
                if n % 5 == 0:
                    if not self.engine.owntone_up:
                        if await self.client.ready():
                            async with self.lock:
                                await self.execute(self.engine.on_owntone_state(True))
                            await self.refresh_outputs()
                    else:
                        await self._poll_player()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("ticker")

    def on_proc_exit(self, name: str, code: int | None, expected: bool) -> None:
        if self._loop is None or self._stop.is_set():
            return
        if name == OWNTONE:
            if not expected:
                self._loop.create_task(self.execute(self.engine.on_owntone_state(False)))
        elif name.startswith("sps:"):
            group_id = name[len("sps:") :]
            self._loop.create_task(self.execute(self.engine.on_receiver_exit(group_id, expected)))

    # ---- snapshot ----------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        data = self.engine.snapshot()
        data["supervise"] = self.settings.supervise
        data["owntone"] = self.supervisor.status(OWNTONE)
        for group in data["groups"]:
            group["receiver"] = self.supervisor.status(receiver_name(group["id"]))
        return data
