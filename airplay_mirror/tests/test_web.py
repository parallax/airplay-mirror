from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from airplay_mirror.config import Settings
from airplay_mirror.engine import Engine
from airplay_mirror.groups import GroupStore
from airplay_mirror.owntone import OwnToneError
from airplay_mirror.procs import Supervisor
from airplay_mirror.runner import Runner
from airplay_mirror.web import build_app
from tests.conftest import OUTPUTS


class FakeOwnTone:
    """Records calls; behaves like a healthy OwnTone with the conftest outputs."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.out = [dict(o) for o in OUTPUTS]
        self.fail_pin = False

    async def ready(self):
        return True

    async def wait_ready(self, timeout, interval=1.0):
        return True

    async def outputs(self):
        return [dict(o) for o in self.out]

    async def set_outputs(self, ids):
        self.calls.append(("set_outputs", list(ids)))
        for o in self.out:
            o["selected"] = o["id"] in ids

    async def update_output(self, output_id, *, selected=None, volume=None, pin=None, offset_ms=None):
        self.calls.append(
            ("update_output", output_id, selected, volume, pin) + ((offset_ms,) if offset_ms is not None else ())
        )
        if pin is not None and self.fail_pin:
            raise OwnToneError("PUT /api/outputs/4 -> 500 pairing failed")

    async def player(self):
        return {"state": "stop"}

    async def queue_item_path(self, item_id):
        return None

    async def find_track_id(self, path):
        return 7

    async def queue_track(self, track_id, *, clear=True, playback=True):
        self.calls.append(("queue_track", track_id))

    async def stop(self):
        self.calls.append(("stop",))

    async def rescan(self):
        self.calls.append(("rescan",))

    async def listen(self, on_notify, stop, kinds=()):
        await stop.wait()


@pytest.fixture
async def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), supervise=False, hook_timeout_seconds=2)
    engine = Engine(settings)
    store = GroupStore(settings.groups_file)
    fake = FakeOwnTone()
    runner = Runner(settings, engine, store, Supervisor(), fake)  # type: ignore[arg-type]
    await runner.start()
    app = build_app(runner)
    server = TestServer(app)
    async with TestClient(server) as c:
        c.fake = fake  # type: ignore[attr-defined]
        c.runner = runner  # type: ignore[attr-defined]
        yield c
    await runner.stop()


async def test_state_and_outputs(client):
    r = await client.get("/api/state")
    data = await r.json()
    assert data["status"] == "idle"
    assert [o["name"] for o in data["outputs"]] == ["Bedroom", "Kitchen", "Living Room TV", "Terrace"]
    assert data["version"]


async def test_group_crud_and_validation(client):
    r = await client.post("/api/groups", json={"name": "", "speakers": []})
    assert r.status == 400
    errors = (await r.json())["errors"]
    assert "name is required" in errors

    r = await client.post(
        "/api/groups", json={"name": "Kitchen + Terrace", "speakers": ["Kitchen", {"name": "Terrace", "volume": 70}]}
    )
    assert r.status == 201
    group = (await r.json())["group"]
    assert group["id"] == "kitchen-terrace" and group["slot"] == 0

    r = await client.post("/api/groups", json={"name": "kitchen + terrace", "speakers": ["Kitchen"]})
    assert r.status == 400

    # A group may not take the name of a real speaker.
    r = await client.put(f"/api/groups/{group['id']}", json={"name": "Kitchen", "speakers": ["Kitchen"]})
    assert r.status == 400
    assert "speaker already has that name" in (await r.json())["errors"][0]

    r = await client.put(
        f"/api/groups/{group['id']}",
        json={"name": "Kitchen Zone", "speakers": [{"name": "Kitchen", "airplay2": False}]},
    )
    assert r.status == 200
    assert (await r.json())["group"]["speakers"] == [
        {"name": "Kitchen", "volume": 50, "airplay2": False, "offset_ms": 0}
    ]

    r = await client.put("/api/groups/nope", json={"name": "X", "speakers": ["Kitchen"]})
    assert r.status == 404

    data = await (await client.get("/api/state")).json()
    assert data["groups"][0]["name"] == "Kitchen Zone"
    assert data["groups"][0]["port"] == 5000

    r = await client.delete(f"/api/groups/{group['id']}")
    assert r.status == 200
    assert (await (await client.get("/api/groups")).json())["groups"] == []


async def test_hook_selects_outputs_and_is_loopback_only(client):
    await client.post(
        "/api/groups", json={"name": "Kitchen + Terrace", "speakers": [{"name": "Kitchen", "volume": 40}, "Terrace"]}
    )
    client.fake.calls.clear()
    r = await client.post("/api/hook/kitchen-terrace/start")
    assert r.status == 200
    assert ("set_outputs", ["1", "2"]) in client.fake.calls
    assert ("update_output", "1", True, 80, None) in client.fake.calls  # 40 relative to Terrace's 50
    assert ("update_output", "2", True, 100, None) in client.fake.calls
    data = await (await client.get("/api/state")).json()
    assert data["status"] == "starting"
    assert data["session"]["group_name"] == "Kitchen + Terrace"

    client.fake.calls.clear()
    r = await client.post("/api/hook/kitchen-terrace/volume", json={"value": "-15.0"})
    assert r.status == 200
    assert ("update_output", "1", None, 40, None) in client.fake.calls
    assert ("update_output", "2", None, 50, None) in client.fake.calls
    r = await client.post("/api/hook/kitchen-terrace/volume?value=-144.0")
    assert r.status == 200
    assert ("update_output", "1", None, 0, None) in client.fake.calls

    r = await client.post("/api/hook/kitchen-terrace/stop")
    assert r.status == 200
    assert ("stop",) in client.fake.calls
    assert (await (await client.get("/api/state")).json())["session"] is None

    r = await client.post("/api/hook/kitchen-terrace/bogus")
    assert r.status == 400

    # A non-loopback caller is refused.
    r = await client.post("/api/hook/kitchen-terrace/start", headers={"X-Forwarded-For": "10.0.0.5"})
    assert r.status == 200  # headers do not fool it; the peer is still loopback
    client.runner.settings.hook_timeout_seconds = 2
    from airplay_mirror import web as webmod

    saved = set(webmod.LOOPBACK)
    webmod.LOOPBACK.clear()
    try:
        r = await client.post("/api/hook/kitchen-terrace/start")
        assert r.status == 403
    finally:
        webmod.LOOPBACK.update(saved)


async def test_pair_and_select(client):
    r = await client.post("/api/outputs/4/pair", json={"pin": "1234"})
    assert r.status == 200
    assert ("update_output", "4", None, None, "1234") in client.fake.calls
    r = await client.post("/api/outputs/4/pair", json={"pin": ""})
    assert r.status == 400
    client.fake.fail_pin = True
    r = await client.post("/api/outputs/4/pair", json={"pin": "0000"})
    assert r.status == 502
    r = await client.post("/api/outputs/1/select", json={"selected": True})
    assert r.status == 200
    assert ("update_output", "1", True, None, None) in client.fake.calls


async def test_volume_preview_and_reapply(client):
    await client.post(
        "/api/groups", json={"name": "Kitchen + Terrace", "speakers": [{"name": "Kitchen", "volume": 40}]}
    )
    await client.post("/api/hook/kitchen-terrace/start")
    client.fake.calls.clear()
    r = await client.post(
        "/api/session/preview", json={"levels": {"Kitchen": 70, "Terrace": 35}, "offsets": {"Terrace": 120}}
    )
    assert r.status == 200
    assert ("update_output", "1", None, 100, None) in client.fake.calls
    assert ("update_output", "2", None, 50, None) in client.fake.calls
    assert ("update_output", "2", None, None, None, 120) in client.fake.calls
    r = await client.post("/api/session/preview", json={"levels": {"Kitchen": "loud"}})
    assert r.status == 400
    r = await client.post("/api/session/preview", json={"levels": [1, 2]})
    assert r.status == 400
    client.fake.calls.clear()
    r = await client.post("/api/session/reapply")
    assert r.status == 200
    assert ("update_output", "1", True, 100, None) in client.fake.calls


async def test_session_stop_and_rescan(client):
    await client.post("/api/groups", json={"name": "Bedroom Zone", "speakers": ["Bedroom"]})
    await client.post("/api/hook/bedroom-zone/start")
    r = await client.post("/api/session/stop")
    assert r.status == 200
    assert (await (await client.get("/api/state")).json())["session"] is None
    r = await client.post("/api/rescan")
    assert r.status == 200
    assert ("rescan",) in client.fake.calls
