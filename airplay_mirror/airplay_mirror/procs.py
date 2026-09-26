"""A small async process supervisor for OwnTone and the shairport-sync receivers.

Each child is started with its output piped into our log, restarted with backoff if it dies, and
stopped with SIGTERM (then SIGKILL) on request.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ExitCallback = Callable[[str, int | None, bool], None]  # (name, returncode, expected)


@dataclass
class ProcSpec:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stop_timeout: float = 8.0
    restart: bool = True


@dataclass
class _Proc:
    spec: ProcSpec
    task: asyncio.Task | None = None
    process: asyncio.subprocess.Process | None = None
    stopping: bool = False
    restarts: int = 0
    last_exit: int | None = None
    since: float | None = None
    backoff: float = 1.0


class Supervisor:
    def __init__(self, on_exit: ExitCallback | None = None) -> None:
        self._procs: dict[str, _Proc] = {}
        self.on_exit = on_exit

    # ---- public ------------------------------------------------------------------------------------

    async def start(self, spec: ProcSpec) -> None:
        existing = self._procs.get(spec.name)
        if existing and existing.task and not existing.task.done():
            if existing.spec.argv == spec.argv and existing.spec.env == spec.env:
                return
            await self.stop(spec.name)
        proc = _Proc(spec=spec)
        self._procs[spec.name] = proc
        proc.task = asyncio.create_task(self._run(proc), name=f"proc:{spec.name}")

    async def stop(self, name: str) -> None:
        proc = self._procs.get(name)
        if proc is None:
            return
        proc.stopping = True
        if proc.process and proc.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.process.terminate()
            try:
                await asyncio.wait_for(proc.process.wait(), timeout=proc.spec.stop_timeout)
            except TimeoutError:
                log.warning("[%s] did not exit after SIGTERM; killing", name)
                with contextlib.suppress(ProcessLookupError):
                    proc.process.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.process.wait(), timeout=3)
        if proc.task:
            proc.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await proc.task
        self._procs.pop(name, None)

    async def restart(self, name: str) -> None:
        proc = self._procs.get(name)
        if proc is None:
            return
        spec = proc.spec
        await self.stop(name)
        await self.start(spec)

    async def stop_all(self, order: list[str] | None = None) -> None:
        names = list(order or []) + [n for n in self._procs if n not in (order or [])]
        for name in names:
            await self.stop(name)

    def running(self, name: str) -> bool:
        proc = self._procs.get(name)
        return bool(proc and proc.process and proc.process.returncode is None)

    def status(self, name: str) -> dict:
        proc = self._procs.get(name)
        if proc is None:
            return {"running": False, "pid": None, "restarts": 0, "last_exit": None, "since": None}
        running = bool(proc.process and proc.process.returncode is None)
        return {
            "running": running,
            "pid": proc.process.pid if running and proc.process else None,
            "restarts": proc.restarts,
            "last_exit": proc.last_exit,
            "since": proc.since,
        }

    def statuses(self) -> dict[str, dict]:
        return {name: self.status(name) for name in self._procs}

    # ---- internals ---------------------------------------------------------------------------------

    async def _run(self, proc: _Proc) -> None:
        spec = proc.spec
        while not proc.stopping:
            started = time.monotonic()
            try:
                proc.process = await asyncio.create_subprocess_exec(
                    *spec.argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, **spec.env},
                )
            except (OSError, ValueError) as exc:
                log.error("[%s] could not start %s: %s", spec.name, spec.argv[0], exc)
                proc.last_exit = -1
                self._notify_exit(spec.name, -1, proc.stopping)
                if not spec.restart:
                    return
                await asyncio.sleep(proc.backoff)
                proc.backoff = min(proc.backoff * 2, 30.0)
                continue
            proc.since = time.time()
            log.info("[%s] started pid %s", spec.name, proc.process.pid)
            assert proc.process.stdout is not None
            await self._pump(spec.name, proc.process.stdout)
            code = await proc.process.wait()
            proc.last_exit = code
            expected = proc.stopping
            (log.info if expected else log.warning)("[%s] exited with code %s", spec.name, code)
            self._notify_exit(spec.name, code, expected)
            if expected or not spec.restart:
                return
            if time.monotonic() - started > 60:
                proc.backoff = 1.0
            proc.restarts += 1
            await asyncio.sleep(proc.backoff)
            proc.backoff = min(proc.backoff * 2, 30.0)

    async def _pump(self, name: str, stream: asyncio.StreamReader) -> None:
        plog = logging.getLogger(f"proc.{name}")
        while True:
            try:
                line = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                return
            plog.info("%s", line.decode(errors="replace").rstrip())

    def _notify_exit(self, name: str, code: int | None, expected: bool) -> None:
        if self.on_exit:
            try:
                self.on_exit(name, code, expected)
            except Exception:  # noqa: BLE001
                log.exception("exit callback failed")
