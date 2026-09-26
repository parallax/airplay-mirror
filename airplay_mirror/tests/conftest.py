from __future__ import annotations

import pytest

from airplay_mirror.config import Settings
from airplay_mirror.engine import Engine
from airplay_mirror.groups import Group, Speaker


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def output(id_: str, name: str, **kw):
    return {
        "id": id_,
        "name": name,
        "type": "AirPlay",
        "selected": False,
        "volume": 30,
        "requires_auth": False,
        "needs_auth_key": False,
        **kw,
    }


OUTPUTS = [
    output("1", "Kitchen"),
    output("2", "Terrace"),
    output("3", "Bedroom"),
    output("4", "Living Room TV", needs_auth_key=True),
]

KITCHEN_TERRACE = Group(
    id="kitchen-terrace",
    name="Kitchen + Terrace",
    slot=0,
    speakers=[Speaker("Kitchen", 40), Speaker("Terrace", 60, airplay2=False)],
)
BEDROOM = Group(id="bedroom", name="Bedroom Zone", slot=1, speakers=[Speaker("Bedroom", 25)])
GROUPS = [KITCHEN_TERRACE, BEDROOM]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=str(tmp_path / "data"), play_verify_seconds=4.0)


@pytest.fixture
def engine(settings, clock):
    e = Engine(settings, now=clock)
    e.set_groups(GROUPS)
    e.on_owntone_state(True)
    e.on_outputs(OUTPUTS)
    return e
