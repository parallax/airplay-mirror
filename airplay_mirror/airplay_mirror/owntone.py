"""Async client for OwnTone's JSON API and websocket notifications."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class OwnToneError(Exception):
    pass


class OwnToneClient:
    def __init__(self, base_url: str, ws_url: str, session: aiohttp.ClientSession, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with self.session.request(method, url, json=json_body, params=params, timeout=self.timeout) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise OwnToneError(f"{method} {path} -> {resp.status} {text[:200]}")
                if resp.status == 204 or resp.content_length == 0:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    return await resp.json(content_type=None)
                text = await resp.text()
                return json.loads(text) if text.strip() else None
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise OwnToneError(f"{method} {path}: {exc}") from exc

    # ---- readiness ---------------------------------------------------------------------------------

    async def ready(self) -> bool:
        try:
            await self._request("GET", "/api/config")
            return True
        except OwnToneError:
            return False

    async def wait_ready(self, timeout: float, interval: float = 1.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await self.ready():
                return True
            await asyncio.sleep(interval)
        return False

    # ---- outputs -----------------------------------------------------------------------------------

    async def outputs(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/outputs")
        return list((data or {}).get("outputs", []))

    async def set_outputs(self, ids: list[str]) -> None:
        await self._request("PUT", "/api/outputs/set", json_body={"outputs": list(ids)})

    async def update_output(
        self,
        output_id: str,
        *,
        selected: bool | None = None,
        volume: int | None = None,
        pin: str | None = None,
        offset_ms: int | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if selected is not None:
            body["selected"] = selected
        if volume is not None:
            body["volume"] = int(volume)
        if pin is not None:
            body["pin"] = pin
        if offset_ms is not None:
            body["offset_ms"] = int(offset_ms)
        await self._request("PUT", f"/api/outputs/{output_id}", json_body=body)

    # ---- player ------------------------------------------------------------------------------------

    async def player(self) -> dict[str, Any]:
        return dict(await self._request("GET", "/api/player") or {})

    async def play(self) -> None:
        await self._request("PUT", "/api/player/play")

    async def pause(self) -> None:
        await self._request("PUT", "/api/player/pause")

    async def stop(self) -> None:
        await self._request("PUT", "/api/player/stop")

    async def queue_item_path(self, item_id: int | None) -> str | None:
        if not item_id:
            return None
        data = await self._request("GET", "/api/queue", params={"id": str(item_id)})
        for item in (data or {}).get("items", []):
            if int(item.get("id", -1)) == int(item_id):
                return item.get("path")
        return None

    # ---- library -----------------------------------------------------------------------------------

    async def find_track_id(self, path: str) -> int | None:
        data = await self._request(
            "GET", "/api/search", params={"type": "tracks", "expression": "data_kind is pipe", "limit": "200"}
        )
        for item in ((data or {}).get("tracks") or {}).get("items", []):
            if item.get("path") == path:
                return int(item["id"])
        return None

    async def queue_track(self, track_id: int, *, clear: bool = True, playback: bool = True) -> None:
        params = {"uris": f"library:track:{track_id}", "clear": "true" if clear else "false"}
        if playback:
            params["playback"] = "start"
        await self._request("POST", "/api/queue/items/add", params=params)

    async def rescan(self) -> None:
        await self._request("PUT", "/api/update")

    # ---- notifications -----------------------------------------------------------------------------

    async def listen(
        self, on_notify: Callable[[list[str]], Awaitable[None]], stop: asyncio.Event, kinds: tuple[str, ...] = ()
    ) -> None:
        """Keep a websocket open and call ``on_notify`` with the kinds of change OwnTone reports."""
        kinds = kinds or ("outputs", "player", "update")
        backoff = 1.0
        while not stop.is_set():
            try:
                async with self.session.ws_connect(self.ws_url, protocols=("notify",), heartbeat=30) as ws:
                    await ws.send_json({"notify": list(kinds)})
                    backoff = 1.0
                    async for msg in ws:
                        if stop.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            notify = data.get("notify") if isinstance(data, dict) else None
                            if notify:
                                await on_notify([str(k) for k in notify])
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                log.debug("OwnTone websocket: %s", exc)
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 15.0)
