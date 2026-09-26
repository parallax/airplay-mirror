"""Configuration loading.

Precedence (lowest to highest): built-in defaults, ``/data/options.json`` (written by the
Home Assistant Supervisor from the add-on options), then ``AM_*`` environment variables for
standalone use and development.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENV_PREFIX = "AM_"


@dataclass
class Settings:
    log_level: str = "info"
    web_port: int = 8099

    # Receivers (one shairport-sync per group). RTSP port = port_base + slot, UDP = udp_port_base + slot*10.
    port_base: int = 5000
    udp_port_base: int = 6001
    max_groups: int = 16

    # Sender (OwnTone)
    owntone_port: int = 3689
    owntone_ws_port: int = 3688
    owntone_ready_timeout: float = 60.0

    # Paths and binaries
    data_dir: str = ""
    owntone_bin: str = "/usr/sbin/owntone"
    shairport_bin: str = "/usr/bin/shairport-sync"
    hook_script: str = "/usr/local/bin/am-hook"

    # Behaviour
    supervise: bool = True  # False: web UI and config generation only (development on a laptop)
    hook_timeout_seconds: float = 5.0
    play_verify_seconds: float = 4.0

    @property
    def pipes_dir(self) -> str:
        return os.path.join(self.data_dir, "pipes")

    @property
    def owntone_dir(self) -> str:
        return os.path.join(self.data_dir, "owntone")

    @property
    def shairport_dir(self) -> str:
        return os.path.join(self.data_dir, "shairport")

    @property
    def groups_file(self) -> str:
        return os.path.join(self.data_dir, "groups.json")

    @property
    def owntone_conf(self) -> str:
        return os.path.join(self.owntone_dir, "owntone.conf")

    @property
    def owntone_url(self) -> str:
        return f"http://127.0.0.1:{self.owntone_port}"

    @property
    def owntone_ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.owntone_ws_port}"


def _coerce(value: Any, target_type: type) -> Any:
    if target_type is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    if target_type is list:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return list(value)
    if target_type is str:
        return "" if value is None else str(value)
    return value


def _field_type(f) -> type:
    t = f.type
    if isinstance(t, str):
        return {"str": str, "int": int, "bool": bool, "float": float, "list[str]": list}[t]
    origin = getattr(t, "__origin__", None)
    return origin or t


def load_settings(options_path: str | None = None) -> Settings:
    """Build settings from defaults, the Supervisor options file and environment variables."""
    settings = Settings()
    known = {f.name: _field_type(f) for f in fields(Settings)}

    path = Path(options_path or os.environ.get(f"{ENV_PREFIX}OPTIONS", "/data/options.json"))
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.error("Could not parse %s: %s", path, exc)
            data = {}
        for key, value in data.items():
            if key in known and value is not None:
                setattr(settings, key, _coerce(value, known[key]))
            elif key not in known:
                log.warning("Ignoring unknown option %r", key)

    for name, typ in known.items():
        env_key = f"{ENV_PREFIX}{name.upper()}"
        if env_key in os.environ:
            setattr(settings, name, _coerce(os.environ[env_key], typ))

    if not settings.data_dir:
        settings.data_dir = "/data" if Path("/data").is_dir() else "dev-data"

    settings.max_groups = max(1, settings.max_groups)
    return settings
