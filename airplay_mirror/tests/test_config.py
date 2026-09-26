from __future__ import annotations

import json

from airplay_mirror.config import load_settings


def test_options_file_then_env(tmp_path, monkeypatch):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"port_base": 6000, "log_level": "debug", "bogus": 1}))
    monkeypatch.setenv("AM_OWNTONE_PORT", "3690")
    monkeypatch.setenv("AM_SUPERVISE", "false")
    monkeypatch.setenv("AM_HOOK_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("AM_DATA_DIR", str(tmp_path / "d"))
    s = load_settings(str(opts))
    assert s.port_base == 6000
    assert s.log_level == "debug"
    assert s.owntone_port == 3690
    assert s.supervise is False
    assert s.hook_timeout_seconds == 2.5
    assert s.pipes_dir == str(tmp_path / "d" / "pipes")
    assert s.owntone_conf == str(tmp_path / "d" / "owntone" / "owntone.conf")
    assert s.owntone_url == "http://127.0.0.1:3690"


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AM_DATA_DIR", raising=False)
    s = load_settings(str(tmp_path / "missing.json"))
    assert s.port_base == 5000
    assert s.owntone_port == 3689
    assert s.supervise is True
    assert s.data_dir in ("/data", "dev-data")
