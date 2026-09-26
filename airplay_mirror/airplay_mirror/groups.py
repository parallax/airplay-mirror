"""Speaker groups: the data model, validation and the JSON store in ``/data/groups.json``.

A group is advertised as one virtual AirPlay speaker. Each group owns a *slot*, a small integer
that determines its receiver's ports; slots are allocated once and never renumbered so editing one
group never disturbs the others.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from . import persist

MAX_NAME_BYTES = 63  # DNS-SD instance name limit


class GroupError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Speaker:
    name: str  # OwnTone output name, exactly as the speaker advertises itself
    volume: int = 50  # 0-100, applied when the group starts playing
    airplay2: bool = True  # False = force AirPlay 1 (RAOP) for this speaker

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "volume": self.volume, "airplay2": self.airplay2}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Speaker:
        return cls(
            name=str(d.get("name", "")).strip(),
            volume=int(d.get("volume", 50)),
            airplay2=bool(d.get("airplay2", True)),
        )


@dataclass
class Group:
    id: str
    name: str
    slot: int
    speakers: list[Speaker] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "slot": self.slot, "speakers": [s.as_dict() for s in self.speakers]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Group:
        return cls(
            id=str(d["id"]),
            name=str(d["name"]),
            slot=int(d["slot"]),
            speakers=[Speaker.from_dict(s) for s in d.get("speakers", [])],
        )


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "group"


def unique_slug(name: str, taken: set[str]) -> str:
    base = slugify(name)
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def parse_speakers(raw: Any) -> list[Speaker]:
    if not isinstance(raw, list):
        raise GroupError(["speakers must be a list"])
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append(Speaker(name=item.strip()))
        elif isinstance(item, dict):
            try:
                out.append(Speaker.from_dict(item))
            except (TypeError, ValueError):
                raise GroupError(["speaker volume must be a number"]) from None
        else:
            raise GroupError(["each speaker must be a name or an object"])
    return out


def validate(
    name: str,
    speakers: list[Speaker],
    existing: list[Group],
    *,
    exclude_id: str | None = None,
    max_groups: int = 16,
    reserved_names: set[str] = frozenset(),
) -> list[str]:
    """Return a list of problems (empty when valid).

    ``reserved_names`` holds the (lower-cased) names of real speakers: a group must not share a name
    with one, because both would be advertised as AirPlay services and OwnTone would hide the real one.
    """
    errors: list[str] = []
    name = name.strip()
    if not name:
        errors.append("name is required")
    elif len(name.encode()) > MAX_NAME_BYTES:
        errors.append(f"name must be at most {MAX_NAME_BYTES} bytes")
    others = [g for g in existing if g.id != exclude_id]
    if any(g.name.strip().lower() == name.lower() for g in others):
        errors.append("a group with that name already exists")
    if name.lower() in reserved_names or any(s.name.lower() == name.lower() for s in speakers):
        errors.append("a speaker already has that name; give the group a different name")
    if exclude_id is None and len(others) >= max_groups:
        errors.append(f"at most {max_groups} groups are supported")
    if not speakers:
        errors.append("pick at least one speaker")
    seen: set[str] = set()
    for s in speakers:
        if not s.name:
            errors.append("speaker names must not be empty")
        elif s.name.lower() in seen:
            errors.append(f"speaker {s.name!r} is listed twice")
        seen.add(s.name.lower())
        if not 0 <= s.volume <= 100:
            errors.append(f"volume for {s.name!r} must be between 0 and 100")
    return errors


class GroupStore:
    """Owns the list of groups and persists it as JSON."""

    def __init__(self, path: str, max_groups: int = 16) -> None:
        self.path = path
        self.max_groups = max_groups
        self._groups: list[Group] = []

    def load(self) -> list[Group]:
        data = persist.load(self.path)
        groups = []
        for raw in data.get("groups", []):
            try:
                groups.append(Group.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        self._groups = groups
        return list(groups)

    def save(self) -> None:
        persist.save(self.path, {"version": 1, "groups": [g.as_dict() for g in self._groups]})

    def all(self) -> list[Group]:
        return list(self._groups)

    def get(self, group_id: str) -> Group | None:
        return next((g for g in self._groups if g.id == group_id), None)

    def _free_slot(self) -> int:
        used = {g.slot for g in self._groups}
        slot = 0
        while slot in used:
            slot += 1
        return slot

    def create(self, name: str, speakers: list[Speaker], reserved_names: set[str] = frozenset()) -> Group:
        name = name.strip()
        errors = validate(name, speakers, self._groups, max_groups=self.max_groups, reserved_names=reserved_names)
        if errors:
            raise GroupError(errors)
        group = Group(
            id=unique_slug(name, {g.id for g in self._groups}), name=name, slot=self._free_slot(), speakers=speakers
        )
        self._groups.append(group)
        self.save()
        return group

    def update(
        self, group_id: str, name: str, speakers: list[Speaker], reserved_names: set[str] = frozenset()
    ) -> Group:
        group = self.get(group_id)
        if group is None:
            raise KeyError(group_id)
        name = name.strip()
        errors = validate(
            name, speakers, self._groups, exclude_id=group_id, max_groups=self.max_groups, reserved_names=reserved_names
        )
        if errors:
            raise GroupError(errors)
        group.name = name
        group.speakers = speakers
        self.save()
        return group

    def delete(self, group_id: str) -> Group:
        group = self.get(group_id)
        if group is None:
            raise KeyError(group_id)
        self._groups.remove(group)
        self.save()
        return group
