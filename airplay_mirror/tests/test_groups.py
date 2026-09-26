from __future__ import annotations

import json

import pytest

from airplay_mirror.groups import GroupError, GroupStore, Speaker, parse_speakers, slugify, validate


def test_slugify():
    assert slugify("Kitchen + Terrace") == "kitchen-terrace"
    assert slugify("  Salle à manger ") == "salle-a-manger"
    assert slugify("!!!") == "group"


def test_create_allocates_slots_and_unique_ids(tmp_path):
    store = GroupStore(str(tmp_path / "groups.json"))
    a = store.create("Kitchen + Terrace", [Speaker("Kitchen")])
    b = store.create("Bedroom Zone", [Speaker("Bedroom")])
    assert (a.id, a.slot) == ("kitchen-terrace", 0)
    assert (b.id, b.slot) == ("bedroom-zone", 1)
    with pytest.raises(GroupError):
        store.create("Bedroom", [Speaker("Bedroom")])  # same name as the speaker itself
    with pytest.raises(GroupError):
        store.create("Study", [Speaker("Study 2")], reserved_names={"study"})  # same name as a known speaker
    store.delete(a.id)
    c = store.create("Study", [Speaker("Study Speaker")])
    assert c.slot == 0  # lowest free slot is reused
    assert b.slot == 1  # existing groups never renumber
    d = store.create("kitchen terrace", [Speaker("Kitchen")])
    assert d.id == "kitchen-terrace"  # the old id is free again
    e = store.create("Kitchen-Terrace!", [Speaker("Kitchen")])  # different name, same slug -> suffixed id
    assert e.id == "kitchen-terrace-2"
    assert json.loads((tmp_path / "groups.json").read_text())["groups"][0]["name"] == "Bedroom Zone"


def test_round_trip(tmp_path):
    path = str(tmp_path / "groups.json")
    store = GroupStore(path)
    store.create("Kitchen + Terrace", [Speaker("Kitchen", 40), Speaker("Terrace", 60, airplay2=False)])
    fresh = GroupStore(path)
    groups = fresh.load()
    assert len(groups) == 1
    assert groups[0].speakers[1] == Speaker("Terrace", 60, airplay2=False)
    assert fresh.get("kitchen-terrace") is groups[0] or fresh.get("kitchen-terrace").id == "kitchen-terrace"


def test_validation():
    errs = validate("", [], [])
    assert "name is required" in errs
    assert "pick at least one speaker" in errs
    errs = validate("x" * 64, [Speaker("A", 101), Speaker("a"), Speaker("B", offset_ms=2500)], [])
    assert any("63 bytes" in e for e in errs)
    assert any("-2000 and 2000" in e for e in errs)
    assert any("between 0 and 100" in e for e in errs)
    assert any("listed twice" in e for e in errs)


def test_update_rejects_duplicate_name_and_max_groups(tmp_path):
    store = GroupStore(str(tmp_path / "groups.json"), max_groups=2)
    a = store.create("A", [Speaker("x")])
    store.create("B", [Speaker("x")])
    with pytest.raises(GroupError) as exc:
        store.create("C", [Speaker("x")])
    assert "at most 2 groups" in exc.value.errors[0]
    with pytest.raises(GroupError):
        store.update(a.id, "b", [Speaker("x")])
    store.update(a.id, "A2", [Speaker("y", 10)])  # renaming yourself is fine
    assert store.get(a.id).name == "A2"
    with pytest.raises(KeyError):
        store.update("nope", "Z", [Speaker("x")])


def test_parse_speakers():
    assert parse_speakers(["Kitchen", {"name": "Terrace", "volume": 70, "airplay2": False, "offset_ms": -40}]) == [
        Speaker("Kitchen"),
        Speaker("Terrace", 70, False, -40),
    ]
    with pytest.raises(GroupError):
        parse_speakers("Kitchen")
    with pytest.raises(GroupError):
        parse_speakers([{"name": "Kitchen", "volume": "loud"}])
