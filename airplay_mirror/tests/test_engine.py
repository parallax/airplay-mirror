from __future__ import annotations

from airplay_mirror.engine import Engine, PlayPipe, RestartReceiver, SelectOutputs, StopPlayer
from airplay_mirror.groups import Group, Speaker
from airplay_mirror.templates import pipe_path
from tests.conftest import GROUPS, KITCHEN_TERRACE, output


def only(actions, typ):
    return [a for a in actions if isinstance(a, typ)]


def test_resolve_exact_and_case_insensitive(engine: Engine):
    g = Group(id="g", name="G", slot=5, speakers=[Speaker("kitchen", 10), Speaker("Terrace", 20), Speaker("Attic", 5)])
    found, missing = engine.resolve(g)
    assert [(o.id, v) for o, v in found] == [("1", 10), ("2", 20)]
    assert missing == ["Attic"]


def test_own_group_names_are_not_outputs(engine: Engine):
    engine.on_outputs([*engine.snapshot()["outputs"], output("9", "Kitchen + Terrace"), output("10", "bedroom zone")])
    names = {o["name"] for o in engine.snapshot()["outputs"]}
    assert "Kitchen + Terrace" not in names and "bedroom zone" not in names
    assert names == {"Kitchen", "Terrace", "Bedroom", "Living Room TV"}


def test_hook_start_selects_outputs_with_volumes(engine: Engine):
    actions = engine.on_hook_start("kitchen-terrace")
    assert actions == [SelectOutputs([("1", 40), ("2", 60)])]
    assert engine.session.group_id == "kitchen-terrace"
    assert engine.session.state == "starting"
    assert engine.status() == "starting"


def test_hook_start_with_missing_speaker(engine: Engine):
    engine.on_outputs([output("1", "Kitchen")])
    actions = engine.on_hook_start("kitchen-terrace")
    assert actions == [SelectOutputs([("1", 40)])]
    assert engine.session.missing == ["Terrace"]
    assert any("Terrace" in e["message"] for e in engine.events)


def test_late_join_when_missing_speaker_appears(engine: Engine):
    engine.on_outputs([output("1", "Kitchen")])
    engine.on_hook_start("kitchen-terrace")
    assert engine.on_outputs([output("1", "Kitchen")]) == []
    actions = engine.on_outputs([output("1", "Kitchen"), output("2", "Terrace")])
    assert actions == [SelectOutputs([("1", 40), ("2", 60)])]
    assert engine.session.missing == []


def test_hook_stop_only_for_active_group(engine: Engine):
    engine.on_hook_start("kitchen-terrace")
    assert engine.on_hook_stop("bedroom") == []
    assert engine.session is not None
    assert engine.on_hook_stop("kitchen-terrace") == [StopPlayer()]
    assert engine.session is None
    assert engine.status() == "idle"


def test_takeover_restarts_previous_receiver(engine: Engine):
    engine.on_hook_start("kitchen-terrace")
    actions = engine.on_hook_start("bedroom")
    assert actions == [RestartReceiver("kitchen-terrace"), SelectOutputs([("3", 25)])]
    assert engine.session.group_id == "bedroom"
    # the stale stop hook from the restarted receiver is ignored
    assert engine.on_hook_stop("kitchen-terrace") == []
    assert engine.session.group_id == "bedroom"


def test_pending_when_owntone_down_then_applied(settings, clock):
    e = Engine(settings, now=clock)
    e.set_groups(GROUPS)
    assert e.on_hook_start("bedroom") == []
    assert e.session.state == "pending_owntone"
    assert e.status() == "owntone_down"
    e.on_outputs([output("3", "Bedroom")])
    actions = e.on_owntone_state(True)
    assert actions == [SelectOutputs([("3", 25)])]
    assert e.session.state == "starting"


def test_owntone_crash_mid_session_reapplies(engine: Engine, clock):
    engine.on_hook_start("bedroom")
    engine.on_player("play")
    assert engine.session.state == "playing"
    assert engine.on_owntone_state(False) == []
    assert engine.session.state == "pending_owntone"
    assert engine.tick() == []
    actions = engine.on_owntone_state(True)
    assert actions == [SelectOutputs([("3", 25)])]
    clock.advance(5)
    assert engine.tick() == [PlayPipe("bedroom")]


def test_tick_falls_back_to_explicit_play_once(engine: Engine, clock):
    engine.on_hook_start("bedroom")
    assert engine.tick() == []
    clock.advance(3.9)
    assert engine.tick() == []
    clock.advance(0.2)
    assert engine.tick() == [PlayPipe("bedroom")]
    clock.advance(10)
    assert engine.tick() == []
    assert any("did not start playing" in e["message"] for e in engine.events)
    assert engine.tick() == []


def test_playing_stops_the_fallback(engine: Engine, clock, settings):
    engine.on_hook_start("bedroom")
    engine.on_player("play", pipe_path(settings, engine.groups["bedroom"]))
    assert engine.session.state == "playing"
    clock.advance(10)
    assert engine.tick() == []


def test_player_playing_other_path_is_not_our_session(engine: Engine, settings):
    engine.on_hook_start("bedroom")
    engine.on_player("play", "/somewhere/else.mp3")
    assert engine.session.state == "starting"


def test_receiver_crash_clears_session(engine: Engine):
    engine.on_hook_start("bedroom")
    assert engine.on_receiver_exit("kitchen-terrace", expected=False) == []
    assert engine.session is not None
    assert engine.on_receiver_exit("bedroom", expected=True) == []
    assert engine.session is not None
    assert engine.on_receiver_exit("bedroom", expected=False) == [StopPlayer()]
    assert engine.session is None


def test_removing_active_group_stops(engine: Engine):
    engine.on_hook_start("bedroom")
    assert engine.set_groups([KITCHEN_TERRACE]) == [StopPlayer()]
    assert engine.session is None
    assert engine.on_hook_start("bedroom") == []  # unknown now


def test_snapshot_shape(engine: Engine):
    engine.on_outputs([output("1", "Kitchen")])
    engine.on_hook_start("kitchen-terrace")
    snap = engine.snapshot()
    assert snap["status"] == "starting"
    assert snap["session"]["group_name"] == "Kitchen + Terrace"
    assert [o["name"] for o in snap["session"]["outputs"]] == ["Kitchen"]
    assert snap["session"]["missing"] == ["Terrace"]
    g = next(g for g in snap["groups"] if g["id"] == "kitchen-terrace")
    assert g["port"] == 5000 and g["active"] is True
    assert [(s["name"], s["online"]) for s in g["speakers"]] == [("Kitchen", True), ("Terrace", False)]
    assert snap["outputs"][0]["name"] == "Kitchen"
