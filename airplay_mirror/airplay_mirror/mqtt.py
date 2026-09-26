"""MQTT: publish what's playing and accept a few controls, with Home Assistant discovery.

Topics (``status_topic`` defaults to ``airplay-mirror``):

- ``airplay-mirror/state`` (retained JSON): status, active group, track, phone volume, speakers.
- ``airplay-mirror/artwork`` (retained bytes): the current cover art, or empty when there is none.
- ``airplay-mirror/availability``: ``online`` / ``offline``.
- ``airplay-mirror/command``: publish ``stop``, ``rescan`` or ``reapply``.
- ``airplay-mirror/volume/set``: publish 0-100 to set the volume of everything that is playing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import aiomqtt

from . import __version__
from .config import Settings

if TYPE_CHECKING:
    from .runner import Runner

log = logging.getLogger(__name__)

DEVICE = {
    "identifiers": ["airplay_mirror"],
    "name": "AirPlay Mirror",
    "manufacturer": "Parallax",
    "model": "Virtual AirPlay speaker groups",
    "sw_version": __version__,
}


@dataclass
class Publish:
    topic: str
    payload: str | bytes
    retain: bool = False


async def supervisor_mqtt(settings: Settings) -> bool:
    """Fill in MQTT settings from the Home Assistant Supervisor's MQTT service (the Mosquitto add-on)."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.info("No MQTT details from the Supervisor (%s); MQTT stays off", exc)
        return False
    data = body.get("data") or {}
    if not data.get("host"):
        log.info("The Supervisor has no MQTT service; MQTT stays off")
        return False
    settings.mqtt_host = data["host"]
    settings.mqtt_port = int(data.get("port", 1883))
    settings.mqtt_username = data.get("username") or ""
    settings.mqtt_password = data.get("password") or ""
    settings.mqtt_tls = bool(data.get("ssl"))
    log.info("Using the Home Assistant MQTT broker at %s:%s", settings.mqtt_host, settings.mqtt_port)
    return True


# ---- payloads ----------------------------------------------------------------------------------------


def state_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The retained state message, built from the runner's snapshot."""
    session = snapshot.get("session") or {}
    track = session.get("track") or {}
    return {
        "status": snapshot.get("status"),
        "owntone_up": bool(snapshot.get("owntone_up")),
        "group_id": session.get("group_id"),
        "group": session.get("group_name"),
        "state": session.get("state"),
        "started_at": session.get("started_at"),
        "title": track.get("title") or "",
        "artist": track.get("artist") or "",
        "album": track.get("album") or "",
        "has_artwork": bool(track.get("has_artwork")),
        "artwork_id": track.get("artwork_id") or "",
        "volume": session.get("volume_pct", 100) if session else None,
        "speakers": [o.get("name") for o in session.get("outputs", [])],
        "missing": list(session.get("missing", [])),
        "groups": [g.get("name") for g in snapshot.get("groups", [])],
    }


def discovery_messages(prefix: str, status_topic: str, groups: list[dict[str, Any]]) -> list[Publish]:
    """Home Assistant MQTT discovery configs for the add-on's entities."""
    state = f"{status_topic}/state"
    availability = f"{status_topic}/availability"
    common: dict[str, Any] = {"device": DEVICE, "availability_topic": availability}

    def entity(component: str, object_id: str, extra: dict[str, Any]) -> Publish:
        payload = {**common, "unique_id": f"airplay_mirror_{object_id}", **extra}
        if component not in ("button", "image"):
            payload.setdefault("state_topic", state)
        return Publish(f"{prefix}/{component}/airplay_mirror/{object_id}/config", json.dumps(payload), retain=True)

    messages = [
        entity(
            "sensor",
            "status",
            {
                "name": "Status",
                "icon": "mdi:speaker-multiple",
                "value_template": "{{ value_json.status }}",
                "json_attributes_topic": state,
            },
        ),
        entity(
            "sensor",
            "group",
            {
                "name": "Playing group",
                "icon": "mdi:speaker-wireless",
                "value_template": "{{ value_json.group or 'none' }}",
            },
        ),
        entity(
            "sensor", "title", {"name": "Title", "icon": "mdi:music-note", "value_template": "{{ value_json.title }}"}
        ),
        entity(
            "sensor",
            "artist",
            {"name": "Artist", "icon": "mdi:account-music", "value_template": "{{ value_json.artist }}"},
        ),
        entity("sensor", "album", {"name": "Album", "icon": "mdi:album", "value_template": "{{ value_json.album }}"}),
        entity(
            "image",
            "artwork",
            {"name": "Artwork", "image_topic": f"{status_topic}/artwork", "content_type": "image/jpeg"},
        ),
        entity(
            "number",
            "volume",
            {
                "name": "Volume",
                "icon": "mdi:volume-high",
                "min": 0,
                "max": 100,
                "step": 1,
                "mode": "slider",
                "unit_of_measurement": "%",
                "command_topic": f"{status_topic}/volume/set",
                "value_template": "{{ value_json.volume if value_json.volume is not none else 100 }}",
            },
        ),
        entity(
            "button",
            "stop",
            {
                "name": "Stop playback",
                "icon": "mdi:stop-circle",
                "command_topic": f"{status_topic}/command",
                "payload_press": "stop",
            },
        ),
        entity(
            "button",
            "rescan",
            {
                "name": "Rescan speakers",
                "icon": "mdi:refresh",
                "command_topic": f"{status_topic}/command",
                "payload_press": "rescan",
            },
        ),
    ]
    for g in groups:
        messages.append(
            entity(
                "binary_sensor",
                f"group_{g['id']}",
                {
                    "name": f"{g['name']} playing",
                    "icon": "mdi:speaker-play",
                    "device_class": "running",
                    "value_template": f"{{{{ 'ON' if value_json.group_id == {json.dumps(g['id'])} else 'OFF' }}}}",
                },
            )
        )
    return messages


def removal_message(prefix: str, group_id: str) -> Publish:
    """An empty retained config removes a deleted group's entity from Home Assistant."""
    return Publish(f"{prefix}/binary_sensor/airplay_mirror/group_{group_id}/config", "", retain=True)


# ---- bridge --------------------------------------------------------------------------------------------


class MqttBridge:
    def __init__(self, settings: Settings, runner: Runner) -> None:
        self.settings = settings
        self.runner = runner
        self.client: aiomqtt.Client | None = None
        self.connected = False
        self._stopping = False
        self._refresh = asyncio.Event()
        self._removed: set[str] = set()
        self._last_state: str | None = None
        self._last_art: str | None = None

    # ---- lifecycle ---------------------------------------------------------------------------------

    async def run(self) -> None:
        backoff = 2
        while not self._stopping:
            started = time.monotonic()
            try:
                await self._session()
                backoff = 2
            except aiomqtt.MqttError as exc:
                self.connected = False
                self.client = None
                if self._stopping:
                    break
                if time.monotonic() - started > 60:
                    backoff = 2
                log.error("MQTT connection lost: %s. Reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        self._stopping = True
        self._refresh.set()

    def groups_changed(self, removed: set[str] | None = None) -> None:
        """Called by the runner after groups change: republish discovery and drop deleted groups."""
        if removed:
            self._removed |= set(removed)
        self._refresh.set()

    # ---- session -----------------------------------------------------------------------------------

    async def _session(self) -> None:
        s = self.settings
        status = s.status_topic
        tls_context = ssl.create_default_context() if s.mqtt_tls else None
        async with aiomqtt.Client(
            s.mqtt_host,
            port=s.mqtt_port,
            username=s.mqtt_username or None,
            password=s.mqtt_password or None,
            tls_context=tls_context,
            identifier=f"airplay-mirror-{uuid.uuid4().hex[:8]}",
            will=aiomqtt.Will(f"{status}/availability", "offline", retain=True),
            keepalive=60,
        ) as client:
            self.client = client
            self.connected = True
            log.info("Connected to MQTT broker %s:%s", s.mqtt_host, s.mqtt_port)
            await client.subscribe(f"{status}/command")
            await client.subscribe(f"{status}/volume/set")
            await client.subscribe(f"{s.ha_discovery_prefix}/status")
            await self._publish_discovery()
            await client.publish(f"{status}/availability", "online", retain=True)
            self._last_state = None
            self._last_art = None
            ticker = asyncio.create_task(self._ticker())
            try:
                async for message in client.messages:
                    await self._handle(message)
                    if self._stopping:
                        break
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ticker
                self.connected = False
                if self._stopping:
                    with contextlib.suppress(aiomqtt.MqttError):
                        await client.publish(f"{status}/availability", "offline", retain=True)

    async def _publish_discovery(self) -> None:
        if not self.settings.ha_discovery or self.client is None:
            return
        groups = [{"id": g.id, "name": g.name} for g in self.runner.store.all()]
        for msg in discovery_messages(self.settings.ha_discovery_prefix, self.settings.status_topic, groups):
            await self.client.publish(msg.topic, msg.payload, retain=msg.retain)
        for group_id in list(self._removed):
            msg = removal_message(self.settings.ha_discovery_prefix, group_id)
            await self.client.publish(msg.topic, msg.payload, retain=msg.retain)
            self._removed.discard(group_id)

    async def _ticker(self) -> None:
        while not self._stopping and self.client is not None:
            try:
                await self._publish_state()
                if self._refresh.is_set():
                    self._refresh.clear()
                    await self._publish_discovery()
                    self._last_state = None
            except aiomqtt.MqttError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("mqtt publish")
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(self._refresh.wait(), timeout=1.0)

    async def _publish_state(self) -> None:
        assert self.client is not None
        topic = self.settings.status_topic
        payload = json.dumps(state_payload(self.runner.snapshot()), sort_keys=True)
        if payload != self._last_state:
            self._last_state = payload
            await self.client.publish(f"{topic}/state", payload, retain=True)
        art = self.runner.artwork()
        art_id = art[2] if art else ""
        if art_id != self._last_art:
            self._last_art = art_id
            await self.client.publish(f"{topic}/artwork", art[0] if art else b"", retain=True)

    # ---- commands ----------------------------------------------------------------------------------

    async def _handle(self, message: aiomqtt.Message) -> None:
        topic = str(message.topic)
        raw = message.payload if isinstance(message.payload, bytes | bytearray) else b""
        text = raw.decode(errors="replace").strip()
        s = self.settings
        if topic == f"{s.ha_discovery_prefix}/status":
            if text == "online":
                self._refresh.set()
            return
        if topic == f"{s.status_topic}/command":
            log.info("MQTT command: %s", text)
            if text == "stop":
                await self.runner.stop_session()
            elif text == "rescan":
                await self.runner.rescan()
            elif text == "reapply":
                await self.runner.reapply()
            else:
                log.warning("Unknown MQTT command %r", text)
            self._refresh.set()
        elif topic == f"{s.status_topic}/volume/set":
            try:
                pct = int(float(text))
            except ValueError:
                log.warning("Bad volume %r", text)
                return
            await self.runner.set_volume(pct)
            self._refresh.set()
