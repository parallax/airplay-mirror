# AirPlay Mirror

Advertises named groups of AirPlay speakers as single virtual AirPlay speakers. AirPlay to "Kitchen + Terrace" from any phone, Mac or app and the audio is relayed in sync to every speaker in the group.

## How it works

1. For every group the add-on runs a **shairport-sync** receiver (classic AirPlay) named after the group. That is what your phone sees. It writes the received audio and track metadata to a named pipe under `/data/pipes`.
2. A single **OwnTone** instance watches those pipes. When audio arrives it starts playing the pipe to whichever speakers are currently selected.
3. When a receiver's session starts it calls the add-on, which selects exactly the group's speakers in OwnTone with their configured volumes. When the session ends the pipe closes and OwnTone stops.
   Track details (title, artist, album, artwork) go through a small relay in the add-on rather than straight into OwnTone: shairport-sync sends them before the audio starts, at a moment OwnTone is not yet listening, so the relay remembers the current track and hands it to OwnTone when it starts reading. The now-playing card and OwnTone's own interface both show what is playing, including cover art when the app on your phone sends it.
4. OwnTone sends to the speakers over **AirPlay 2** (PTP-timed, so they stay in sync). Any speaker can be switched to AirPlay 1 in the group editor if it misbehaves.

Only one group plays at a time. Starting a second group takes over: the first phone is disconnected and the new group's speakers are selected.

The receivers are AirPlay 1 on purpose: an AirPlay 2 receiver and an AirPlay 2 sender both need the PTP ports (UDP 319/320) in opposite roles, so they cannot share one host. You lose nothing for this use: the phone still streams fine, and grouping is done here rather than in iOS.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. `debug` also raises OwnTone's and shairport-sync's own verbosity. |
| `port_base` | `5000` | RTSP port of the first group's receiver. Each further group uses the next port (5001, 5002, ...). Change it if something else on the host uses 5000. |
| `owntone_port` | `3689` | OwnTone's HTTP/JSON API port. OwnTone's own web UI is also here, and Home Assistant's OwnTone integration can be pointed at it. |
| `mqtt_enabled` | `true` | Publish state over MQTT and accept commands. Uses the Mosquitto add-on's broker automatically. |
| `ha_discovery` | `true` | Create Home Assistant entities via MQTT discovery. |
| `ha_discovery_prefix` | `homeassistant` | Discovery prefix, if you changed it in the MQTT integration. |
| `status_topic` | `airplay-mirror` | Topic prefix for the add-on's MQTT messages. |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`, `mqtt_tls` | from Supervisor | Only needed if you don't use the Mosquitto add-on. |

Groups are managed in the web UI, not in these options, and are stored in `/data/groups.json`.

## Web UI

- **Now playing**: the active group, the speakers it is going to, and which ones are missing.
- **Groups**: each virtual speaker with its members, per-speaker level and receiver status. *Edit* opens the editor; *Delete* removes the group (your phone will stop seeing it).
- **Speakers seen by OwnTone**: every AirPlay device OwnTone has discovered. *Pair* appears for Apple TVs that need a PIN. *Select* lets you test a speaker directly.
- **Rescan** asks OwnTone to rescan its library (the pipes). Rarely needed.
- **Stop session** ends the current AirPlay session and kicks the phone off.

Editing a group's speakers restarts OwnTone briefly (a few seconds) to apply the per-speaker settings; if something is playing, the restart waits until playback stops and the UI says so. Renaming a group restarts only that group's receiver.

## Speakers and AirPlay 2

- **Apple TV**: needs one-off PIN verification. Press *Pair*, enter the PIN shown on the TV. The key is stored in OwnTone's database under `/data`.
- **Speakers that are part of a HomeKit Home** (HomePod, Apple TV, some Sonos): in the Home app set *Allow Speaker & TV Access* to **Everyone on the Same Network**, otherwise the speaker rejects the sender.
- **Sonos** and most third-party AirPlay 2 speakers work as is. If one refuses to play, drops out, or has no volume control, tick *AirPlay 1 fallback* for it in the group editor.
- **PTP ports**: AirPlay 2 sending needs UDP 319 and 320 on the host. Nothing on Home Assistant OS uses them by default, but another add-on or container doing AirPlay 2 (an AirPlay 2 receiver, or Music Assistant with AirPlay 2) will conflict. OwnTone logs a bind error if so.
- **Speaker names** are matched exactly as OwnTone lists them. A group cannot have the same name as a real speaker.
- **Chromecast** and other non-AirPlay outputs OwnTone discovers are hidden and cannot be put in a group: Chromecast does not keep sync with AirPlay, and a speaker that speaks both would otherwise be ambiguous. The Speakers card lists what was hidden.

## Volume

The phone's volume slider is forwarded straight to the speakers: the receiver passes the audio through at full scale and reports each volume change to the add-on, which sets the speakers' own volumes in OwnTone immediately. That avoids the delay you would get if the volume were baked into the audio before it goes through OwnTone's buffer.

The per-speaker levels in a group are a balance, relative to the loudest speaker: the speaker with the highest level plays at exactly the phone's volume and the others sit below it in proportion (levels 40 and 60 mean the first plays at two thirds of the second). This is the same master/relative model OwnTone uses internally, so the two never disagree. While the group is playing, open *Edit* and the sliders change the speakers live, so you can balance by ear. *Save* keeps the levels, *Cancel* restores the previous ones.

## MQTT and Home Assistant entities

With the Mosquitto add-on installed (or `mqtt_host` set) the add-on publishes to `airplay-mirror/state` (retained JSON: status, playing group, title, artist, album, phone volume, speakers) and `airplay-mirror/artwork` (retained image bytes). With discovery on you get an **AirPlay Mirror** device with:

- `sensor.airplay_mirror_status`, with the full state as attributes
- `sensor.airplay_mirror_playing_group`, `_title`, `_artist`, `_album`
- `image.airplay_mirror_artwork`, the current cover art
- `number.airplay_mirror_volume`: the volume of whatever is playing, 0-100, like moving the phone's slider
- `button.airplay_mirror_stop_playback` and `button.airplay_mirror_rescan_speakers`
- one `binary_sensor.<group>_playing` per group, handy for automations such as dimming the lights when "Kitchen + Terrace" starts

Commands: publish `stop`, `rescan` or `reapply` to `airplay-mirror/command`, or 0-100 to `airplay-mirror/volume/set`.

Home Assistant's built-in OwnTone integration is a good companion: point it at the OwnTone port and you get a full `media_player` for the relay, with the same track details.

## Sync offsets

If one speaker is consistently a little ahead of or behind the others (a TV's audio path through an Apple TV is a common case), give it a sync offset in the group editor: positive milliseconds play it later, negative earlier, up to 2 seconds either way. OwnTone applies the offset per speaker. Like the volume sliders, offsets are applied live while the group is playing, so you can tune by ear.

## OwnTone's own interface

OwnTone runs on the Home Assistant host on the port set by `owntone_port` (3689 by default) and has its own web interface with speaker details and pairing. The *Speakers* card links to it. Home Assistant's built-in OwnTone integration can also be pointed at that port if you want the group player as a `media_player` entity.

## Running outside Home Assistant

The image runs anywhere Docker does on Linux, with host networking:

```bash
docker run -d --name airplay-mirror --net host -v airplay-mirror:/data \
  -e AM_LOG_LEVEL=info -e AM_PORT_BASE=5000 \
  ghcr.io/parallax/airplay-mirror:latest
```

Every option can be set as an environment variable prefixed `AM_`. The UI is then at `http://host:8099/` with no authentication, so keep it on a trusted network.

## Troubleshooting

- **The phone doesn't list a group.** Check the group's receiver says *running* in the UI. Names appear within about 5 seconds of saving; toggle WiFi on the phone if it caches an old list. The add-on log shows shairport-sync's own output prefixed with `proc.sps:<group>`.
- **The phone connects but nothing plays.** Look at *Now playing*: if the speakers are listed as missing, OwnTone can't see them (power, WiFi, HomeKit permission). If they are listed but silent, check the OwnTone lines in the log for `ANNOUNCE`/`SETUP` errors, and try the AirPlay 1 fallback for that speaker.
- **"OwnTone down" in the header.** OwnTone is restarting or failed to start; the log has the reason. A bind error on port 319/320 means another PTP user on the host.
- **Speakers out of sync.** Make sure each speaker is on AirPlay 2 (no *AirPlay 1 fallback* ticked). Sonos stereo pairs and groups should be added by their single Sonos name.
- **Avahi warns about another mDNS stack.** Expected on Home Assistant OS; it coexists with Home Assistant's own discovery.
