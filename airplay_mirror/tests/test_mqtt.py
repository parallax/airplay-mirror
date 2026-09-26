from __future__ import annotations

import json

from airplay_mirror.engine import Engine, SetVolume
from airplay_mirror.mqtt import discovery_messages, removal_message, state_payload


def test_state_payload_idle_and_playing(engine: Engine):
    idle = state_payload(engine.snapshot())
    assert idle["status"] == "idle" and idle["group"] is None and idle["volume"] is None
    assert idle["groups"] == ["Kitchen + Terrace", "Bedroom Zone"]
    engine.on_hook_start("kitchen-terrace")
    engine.on_volume("kitchen-terrace", "-15.0")
    engine.on_track(
        "kitchen-terrace",
        {"title": "So What", "artist": "Miles Davis", "album": "", "has_artwork": True, "artwork_id": "abc"},
    )
    p = state_payload(engine.snapshot())
    assert p["group_id"] == "kitchen-terrace" and p["group"] == "Kitchen + Terrace"
    assert (p["title"], p["artist"], p["artwork_id"], p["volume"]) == ("So What", "Miles Davis", "abc", 50)
    assert p["speakers"] == ["Kitchen", "Terrace"]
    json.dumps(p)  # serialisable


def test_discovery_messages_cover_entities_and_groups():
    msgs = discovery_messages(
        "homeassistant", "airplay-mirror", [{"id": "kitchen-terrace", "name": "Kitchen + Terrace"}]
    )
    topics = [m.topic for m in msgs]
    assert "homeassistant/sensor/airplay_mirror/status/config" in topics
    assert "homeassistant/image/airplay_mirror/artwork/config" in topics
    assert "homeassistant/number/airplay_mirror/volume/config" in topics
    assert "homeassistant/button/airplay_mirror/stop/config" in topics
    assert "homeassistant/binary_sensor/airplay_mirror/group_kitchen-terrace/config" in topics
    by_topic = {m.topic: json.loads(m.payload) for m in msgs}
    assert by_topic["homeassistant/sensor/airplay_mirror/status/config"]["state_topic"] == "airplay-mirror/state"
    assert by_topic["homeassistant/image/airplay_mirror/artwork/config"]["image_topic"] == "airplay-mirror/artwork"
    assert "state_topic" not in by_topic["homeassistant/button/airplay_mirror/stop/config"]
    assert by_topic["homeassistant/number/airplay_mirror/volume/config"]["command_topic"] == "airplay-mirror/volume/set"
    group = by_topic["homeassistant/binary_sensor/airplay_mirror/group_kitchen-terrace/config"]
    assert group["name"] == "Kitchen + Terrace playing" and "kitchen-terrace" in group["value_template"]
    assert all(m.retain for m in msgs)
    assert all(json.loads(m.payload)["device"]["identifiers"] == ["airplay_mirror"] for m in msgs)
    rm = removal_message("homeassistant", "old")
    assert rm.topic.endswith("/group_old/config") and rm.payload == "" and rm.retain


def test_engine_set_volume(engine: Engine):
    assert engine.set_volume(50) == []  # nothing playing
    engine.on_hook_start("kitchen-terrace")
    assert engine.set_volume(50) == [SetVolume([("1", 33), ("2", 50)])]
    assert engine.session.volume_pct == 50
    assert engine.set_volume(500) == [SetVolume([("1", 67), ("2", 100)])]
