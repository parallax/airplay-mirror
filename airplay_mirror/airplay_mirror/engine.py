"""The decision-making core, free of I/O.

The engine receives facts (groups changed, OwnTone reported these outputs, a receiver's session
started) and returns *actions* for the runner to carry out. Keeping it pure means every rule about
takeover, late-joining speakers and playback fallbacks is covered by fast unit tests.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .groups import Group, Speaker
from .templates import pipe_path

# ---- actions -------------------------------------------------------------------------------------


@dataclass
class SelectOutputs:
    items: list[tuple[str, int]]  # (output id, volume 0-100); everything else gets deselected


@dataclass
class SetVolume:
    items: list[tuple[str, int]]  # (output id, volume 0-100)


@dataclass
class SetOffsets:
    items: list[tuple[str, int]]  # (output id, offset in ms)


@dataclass
class PlayPipe:
    group_id: str


@dataclass
class StopPlayer:
    pass


@dataclass
class RestartReceiver:
    group_id: str


@dataclass
class Rescan:
    pass


Action = SelectOutputs | SetVolume | SetOffsets | PlayPipe | StopPlayer | RestartReceiver | Rescan


# ---- state ---------------------------------------------------------------------------------------


@dataclass
class Output:
    id: str
    name: str
    type: str = ""
    selected: bool = False
    volume: int = 0
    requires_auth: bool = False
    needs_auth_key: bool = False
    offset_ms: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Output:
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            type=str(raw.get("type", "")),
            selected=bool(raw.get("selected", False)),
            volume=int(raw.get("volume", 0) or 0),
            requires_auth=bool(raw.get("requires_auth", False)),
            needs_auth_key=bool(raw.get("needs_auth_key", False)),
            offset_ms=int(raw.get("offset_ms", 0) or 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "selected": self.selected,
            "volume": self.volume,
            "requires_auth": self.requires_auth,
            "needs_auth_key": self.needs_auth_key,
            "offset_ms": self.offset_ms,
        }


@dataclass
class Session:
    group_id: str
    started_at: float
    state: str = "starting"  # pending_owntone | starting | playing
    output_ids: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    verify_at: float | None = None
    queued_explicitly: bool = False
    warned: bool = False
    volume_pct: int = 100  # the phone's volume, 0-100; speaker levels are scaled by it


def scale_levels(items: list[tuple[str, int]], pct: int) -> list[tuple[str, int]]:
    """Turn group levels into absolute OwnTone volumes for the phone's volume ``pct``.

    Levels are relative to the loudest speaker in the group: that one plays at exactly ``pct`` and
    the others keep their ratio to it. This matches OwnTone's own master/relative volume model, which
    it also applies from the phone's volume in the metadata pipe, so the two never disagree.
    """
    top = max((level for _, level in items), default=0)
    if top <= 0:
        return [(i, 0) for i, _ in items]
    return [(i, round(pct * level / top)) for i, level in items]


def airplay_db_to_pct(value: float | str | None) -> int | None:
    """Map shairport-sync's AirPlay volume (0.0 .. -30.0 dB, -144.0 = mute) to 0-100."""
    try:
        db = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if db <= -144.0:
        return 0
    return max(0, min(100, round((db + 30.0) / 30.0 * 100)))


class Engine:
    def __init__(self, settings: Settings, now: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.now = now
        self.groups: dict[str, Group] = {}
        self.outputs: dict[str, Output] = {}
        self.session: Session | None = None
        self.owntone_up = False
        self.pending_owntone_restart = False
        self.player_state = "stop"
        self.events: deque[dict[str, Any]] = deque(maxlen=200)

    # ---- helpers ---------------------------------------------------------------------------------

    def event(self, message: str, level: str = "info", group: str | None = None) -> None:
        self.events.appendleft({"ts": self.now(), "level": level, "message": message, "group": group})

    def own_names(self) -> set[str]:
        return {g.name.lower() for g in self.groups.values()}

    def resolve(self, group: Group) -> tuple[list[tuple[Output, int]], list[str]]:
        """Map a group's speaker names to OwnTone outputs. Exact match first, then case-insensitive."""
        found = [(out, sp.volume) for out, sp in self._resolve_speakers(group)[0]]
        return found, self._resolve_speakers(group)[1]

    def _resolve_speakers(self, group: Group) -> tuple[list[tuple[Output, Speaker]], list[str]]:
        by_name = {o.name: o for o in self.outputs.values()}
        by_lower = {o.name.lower(): o for o in self.outputs.values()}
        found: list[tuple[Output, Speaker]] = []
        missing: list[str] = []
        for sp in group.speakers:
            out = by_name.get(sp.name) or by_lower.get(sp.name.lower())
            if out is None:
                missing.append(sp.name)
            else:
                found.append((out, sp))
        return found, missing

    def _offset_actions(self, pairs: list[tuple[Output, int]]) -> list[Action]:
        """SetOffsets for outputs whose OwnTone offset differs from what the group wants."""
        items = [(o.id, ms) for o, ms in pairs if o.offset_ms != ms]
        for o, ms in pairs:
            o.offset_ms = ms  # assume applied; refreshed from OwnTone on the next outputs update anyway
        return [SetOffsets(items)] if items else []

    def _select_actions(self, group: Group) -> list[Action]:
        assert self.session is not None
        found, missing = self.resolve(group)
        self.session.output_ids = [o.id for o, _ in found]
        self.session.missing = missing
        if missing:
            self.event(f"Not available right now: {', '.join(missing)}", "warning", group.id)
        if not found:
            self.event("None of the group's speakers are online; audio will be discarded", "error", group.id)
        pairs = [(o, sp.offset_ms) for o, sp in self._resolve_speakers(group)[0]]
        return [SelectOutputs(self._scaled(found)), *self._offset_actions(pairs)]

    def _scaled(self, found: list[tuple[Output, int]]) -> list[tuple[str, int]]:
        pct = self.session.volume_pct if self.session else 100
        return scale_levels([(o.id, level) for o, level in found], pct)

    def status(self) -> str:
        if not self.owntone_up:
            return "owntone_down"
        if self.session is None:
            return "idle"
        return "playing" if self.session.state == "playing" else "starting"

    # ---- inputs ----------------------------------------------------------------------------------

    def set_groups(self, groups: list[Group]) -> list[Action]:
        self.groups = {g.id: g for g in groups}
        # Our own virtual speakers must never be treated as outputs.
        self.outputs = {k: o for k, o in self.outputs.items() if o.name.lower() not in self.own_names()}
        if self.session and self.session.group_id not in self.groups:
            self.event("Group removed while playing; stopping", "warning", self.session.group_id)
            self.session = None
            return [StopPlayer()]
        # The playing group may have been edited: apply its new speakers/levels right away.
        return self.reapply()

    def reapply(self) -> list[Action]:
        """Re-select the active group's speakers with the stored levels (and the phone's volume)."""
        if self.session is None or self.session.state == "pending_owntone":
            return []
        group = self.groups.get(self.session.group_id)
        if group is None:
            return []
        return self._select_actions(group)

    def preview_volume(self, levels: dict[str, int], offsets: dict[str, int] | None = None) -> list[Action]:
        """Live preview from the group editor: ``levels`` maps speaker names to their draft levels,
        ``offsets`` to their draft sync offsets in ms.

        The whole set is applied at once because levels are relative to the loudest speaker, so one
        slider can change everyone's absolute volume.
        """
        pct = self.session.volume_pct if self.session else 100
        by_name = {o.name: o for o in self.outputs.values()}
        by_lower = {o.name.lower(): o for o in self.outputs.values()}
        items = []
        pairs = []
        for name, level in levels.items():
            out = by_name.get(name) or by_lower.get(str(name).lower())
            if out is not None:
                items.append((out.id, max(0, min(100, int(level)))))
                if offsets and name in offsets:
                    pairs.append((out, max(-2000, min(2000, int(offsets[name])))))
        actions: list[Action] = [SetVolume(scale_levels(items, pct))] if items else []
        actions.extend(self._offset_actions(pairs))
        return actions

    def on_outputs(self, raw: list[dict[str, Any]]) -> list[Action]:
        own = self.own_names()
        outputs = [Output.from_api(r) for r in raw]
        self.outputs = {o.id: o for o in outputs if o.id and o.name.lower() not in own}
        if not self.session or self.session.state == "pending_owntone":
            return []
        group = self.groups.get(self.session.group_id)
        if group is None or not self.session.missing:
            return []
        found, missing = self.resolve(group)
        if len(missing) < len(self.session.missing):
            joined = sorted(set(self.session.missing) - set(missing))
            self.event(f"Back online, joining: {', '.join(joined)}", "success", group.id)
            self.session.output_ids = [o.id for o, _ in found]
            self.session.missing = missing
            pairs = [(o, sp.offset_ms) for o, sp in self._resolve_speakers(group)[0]]
            return [SelectOutputs(self._scaled(found)), *self._offset_actions(pairs)]
        return []

    def on_volume(self, group_id: str, value: float | str | None) -> list[Action]:
        """The phone moved its volume slider: push the new level to the group's speakers right away."""
        pct = airplay_db_to_pct(value)
        if pct is None or self.session is None or self.session.group_id != group_id:
            return []
        self.session.volume_pct = pct
        group = self.groups.get(group_id)
        if group is None or self.session.state == "pending_owntone":
            return []
        found, _ = self.resolve(group)
        items = self._scaled(found)
        return [SetVolume(items)] if items else []

    def on_player(self, state: str, item_path: str | None = None) -> list[Action]:
        self.player_state = state
        if self.session is None or self.session.state == "pending_owntone":
            return []
        group = self.groups.get(self.session.group_id)
        expected = pipe_path(self.settings, group) if group else None
        if state == "play" and (item_path is None or expected is None or item_path == expected):
            if self.session.state != "playing":
                self.session.state = "playing"
                self.event("Playing", "success", self.session.group_id)
        return []

    def on_owntone_state(self, up: bool) -> list[Action]:
        was_up = self.owntone_up
        self.owntone_up = up
        if up and not was_up:
            self.event("OwnTone is ready")
        if not up and was_up:
            self.event("OwnTone is not responding", "error")
        if self.session is None:
            return []
        if not up:
            self.session.state = "pending_owntone"
            return []
        group = self.groups.get(self.session.group_id)
        if group is None:
            self.session = None
            return []
        # (Re)apply the active session now that OwnTone is (back) up.
        self.session.state = "starting"
        self.session.verify_at = self.now() + self.settings.play_verify_seconds
        self.session.queued_explicitly = False
        self.session.warned = False
        return self._select_actions(group)

    def on_hook_start(self, group_id: str) -> list[Action]:
        group = self.groups.get(group_id)
        if group is None:
            self.event(f"Session started for unknown group {group_id!r}; ignoring", "warning")
            return []
        actions: list[Action] = []
        if self.session and self.session.group_id != group_id:
            prev = self.session.group_id
            self.event(f"Taken over by {group.name}", "warning", prev)
            actions.append(RestartReceiver(prev))
        self.event(f"AirPlay session started for {group.name}", "info", group_id)
        self.session = Session(group_id=group_id, started_at=self.now())
        if not self.owntone_up:
            self.session.state = "pending_owntone"
            self.event("OwnTone is not up yet; will start playback when it is", "warning", group_id)
            return actions
        self.session.verify_at = self.now() + self.settings.play_verify_seconds
        actions.extend(self._select_actions(group))
        return actions

    def on_hook_stop(self, group_id: str) -> list[Action]:
        if self.session is None or self.session.group_id != group_id:
            return []
        self.event("AirPlay session ended", "info", group_id)
        self.session = None
        return [StopPlayer()]

    def on_receiver_exit(self, group_id: str, expected: bool) -> list[Action]:
        if expected:
            return []
        self.event("Receiver exited unexpectedly; restarting it", "error", group_id)
        if self.session and self.session.group_id == group_id:
            self.session = None
            return [StopPlayer()]
        return []

    def tick(self) -> list[Action]:
        s = self.session
        if s is None or s.state != "starting" or not self.owntone_up or s.verify_at is None:
            return []
        if self.now() < s.verify_at:
            return []
        if not s.queued_explicitly:
            s.queued_explicitly = True
            s.verify_at = self.now() + self.settings.play_verify_seconds
            self.event("Playback did not autostart; queueing the pipe explicitly", "warning", s.group_id)
            return [PlayPipe(s.group_id)]
        if not s.warned:
            s.warned = True
            self.event("OwnTone did not start playing; check its log and the speakers", "error", s.group_id)
        return []

    # ---- output ----------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        session = None
        if self.session:
            group = self.groups.get(self.session.group_id)
            session = {
                "group_id": self.session.group_id,
                "group_name": group.name if group else self.session.group_id,
                "state": self.session.state,
                "started_at": self.session.started_at,
                "volume_pct": self.session.volume_pct,
                "outputs": [self.outputs[i].as_dict() for i in self.session.output_ids if i in self.outputs],
                "missing": list(self.session.missing),
            }
        groups = []
        for g in self.groups.values():
            found, missing = self.resolve(g)
            online = {o.name.lower() for o, _ in found}
            groups.append(
                {
                    **g.as_dict(),
                    "port": self.settings.port_base + g.slot,
                    "speakers": [
                        {**s.as_dict(), "online": s.name.lower() in online or s.name in {o.name for o, _ in found}}
                        for s in g.speakers
                    ],
                    "missing": missing,
                    "active": bool(self.session and self.session.group_id == g.id),
                }
            )
        return {
            "now": self.now(),
            "status": self.status(),
            "owntone_up": self.owntone_up,
            "player_state": self.player_state,
            "pending_owntone_restart": self.pending_owntone_restart,
            "session": session,
            "groups": groups,
            "outputs": [o.as_dict() for o in sorted(self.outputs.values(), key=lambda o: o.name.lower())],
            "events": list(self.events),
        }
