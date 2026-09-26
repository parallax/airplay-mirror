from __future__ import annotations

import os
import stat

from airplay_mirror.groups import Group, Speaker
from airplay_mirror.templates import (
    build_config_set,
    render_owntone_conf,
    render_shairport_conf,
    write_config_set,
)
from tests.conftest import BEDROOM, GROUPS, KITCHEN_TERRACE


def test_shairport_conf(settings):
    text = render_shairport_conf(settings, BEDROOM)
    assert 'name = "Bedroom Zone";' in text
    assert "port = 5001;" in text
    assert "udp_port_base = 6011;" in text
    assert "airplay_device_id_offset = 1;" in text
    assert 'output_backend = "pipe";' in text
    assert f'name = "{settings.pipes_dir}/bedroom";' in text
    assert f'pipe_name = "{settings.pipes_dir}/bedroom.metadata";' in text
    assert 'run_this_before_play_begins = "/usr/local/bin/am-hook start bedroom";' in text
    assert 'run_this_after_play_ends = "/usr/local/bin/am-hook stop bedroom";' in text
    assert 'wait_for_completion = "yes";' in text
    assert "log_verbosity = 0;" in text


def test_shairport_conf_escapes_quotes(settings):
    g = Group(id="odd", name='Say "hi" \\ there', slot=3, speakers=[Speaker("x")])
    text = render_shairport_conf(settings, g)
    assert 'name = "Say \\"hi\\" \\\\ there";' in text
    assert "port = 5003;" in text


def test_owntone_conf(settings):
    text = render_owntone_conf(settings, GROUPS)
    assert f'directories = {{ "{settings.pipes_dir}" }}' in text
    assert "pipe_autostart = true" in text
    assert 'type = "disabled"' in text
    assert "speaker_autoselect = false" in text
    assert f'db_path = "{settings.owntone_dir}/songs3.db"' in text
    # one permanent block per distinct speaker, protocol per flag
    assert text.count("permanent = true") == 3
    kitchen = text[text.index('airplay "Kitchen" {') :]
    assert "airplay2_disable = false" in kitchen.split("}")[0]
    terrace = text[text.index('airplay "Terrace" {') :]
    assert "airplay2_disable = true" in terrace.split("}")[0]
    # our own virtual speakers are excluded
    assert 'airplay "Kitchen + Terrace" {\n\texclude = true\n}' in text
    assert 'airplay "Bedroom Zone" {\n\texclude = true\n}' in text


def test_owntone_conf_airplay2_wins_when_groups_disagree(settings):
    g2 = Group(id="g2", name="G2", slot=2, speakers=[Speaker("Terrace", airplay2=True)])
    text = render_owntone_conf(settings, [KITCHEN_TERRACE, g2])
    terrace = text[text.index('airplay "Terrace" {') :].split("}")[0]
    assert "airplay2_disable = false" in terrace


def test_owntone_loglevel(settings):
    settings.log_level = "debug"
    assert 'loglevel = "debug"' in render_owntone_conf(settings, [])
    settings.log_level = "info"
    assert 'loglevel = "log"' in render_owntone_conf(settings, [])


def test_write_config_set_diffs(settings):
    diff = write_config_set(settings, build_config_set(settings, GROUPS))
    assert diff.owntone_changed
    assert diff.changed_groups == {"kitchen-terrace", "bedroom"}
    assert diff.new_pipes == {"kitchen-terrace", "bedroom"}
    assert diff.removed_groups == set()
    for gid in ("kitchen-terrace", "bedroom"):
        assert stat.S_ISFIFO(os.stat(os.path.join(settings.pipes_dir, gid)).st_mode)
        assert stat.S_ISFIFO(os.stat(os.path.join(settings.pipes_dir, gid + ".metadata")).st_mode)
        assert os.path.isfile(os.path.join(settings.shairport_dir, gid + ".conf"))

    # Nothing changed: nothing reported.
    diff = write_config_set(settings, build_config_set(settings, GROUPS))
    assert not diff.owntone_changed and not diff.changed_groups and not diff.new_pipes

    # Speaker-only change touches OwnTone but not the receivers.
    changed = Group(id="bedroom", name="Bedroom Zone", slot=1, speakers=[Speaker("Bedroom", 90)])
    diff = write_config_set(settings, build_config_set(settings, [KITCHEN_TERRACE, changed]))
    assert not diff.owntone_changed  # same speaker set, only volume differs
    assert not diff.changed_groups
    changed = Group(id="bedroom", name="Bedroom Zone", slot=1, speakers=[Speaker("Study")])
    diff = write_config_set(settings, build_config_set(settings, [KITCHEN_TERRACE, changed]))
    assert diff.owntone_changed and not diff.changed_groups

    # Rename changes the receiver conf; removal cleans up conf and pipes.
    renamed = Group(id="bedroom", name="Bedroom 2", slot=1, speakers=[Speaker("Study")])
    diff = write_config_set(settings, build_config_set(settings, [KITCHEN_TERRACE, renamed]))
    assert diff.changed_groups == {"bedroom"}
    diff = write_config_set(settings, build_config_set(settings, [KITCHEN_TERRACE]))
    assert diff.removed_groups == {"bedroom"}
    assert not os.path.exists(os.path.join(settings.shairport_dir, "bedroom.conf"))
    assert not os.path.exists(os.path.join(settings.pipes_dir, "bedroom"))
    assert not os.path.exists(os.path.join(settings.pipes_dir, "bedroom.metadata"))
