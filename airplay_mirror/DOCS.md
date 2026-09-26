# AirPlay Mirror

Advertises named groups of AirPlay speakers as single virtual AirPlay speakers. AirPlay to "Kitchen + Terrace" from any phone, Mac or app and the audio is relayed in sync to every speaker in the group.

## How it works

1. For every group the add-on runs a **shairport-sync** receiver (classic AirPlay) named after the group. That is what your phone sees. It writes the received audio and track metadata to a named pipe under `/data/pipes`.
2. A single **OwnTone** instance watches those pipes. When audio arrives it starts playing the pipe to whichever speakers are currently selected.
3. When a receiver's session starts it calls the add-on, which selects exactly the group's speakers in OwnTone with their configured volumes. When the session ends the pipe closes and OwnTone stops.
4. OwnTone sends to the speakers over **AirPlay 2** (PTP-timed, so they stay in sync). Any speaker can be switched to AirPlay 1 in the group editor if it misbehaves.

Only one group plays at a time. Starting a second group takes over: the first phone is disconnected and the new group's speakers are selected.

The receivers are AirPlay 1 on purpose: an AirPlay 2 receiver and an AirPlay 2 sender both need the PTP ports (UDP 319/320) in opposite roles, so they cannot share one host. You lose nothing for this use: the phone still streams fine, and grouping is done here rather than in iOS.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `log_level` | `info` | `debug`, `info`, `warning` or `error`. `debug` also raises OwnTone's and shairport-sync's own verbosity. |
| `port_base` | `5000` | RTSP port of the first group's receiver. Each further group uses the next port (5001, 5002, ...). Change it if something else on the host uses 5000. |
| `owntone_port` | `3689` | OwnTone's HTTP/JSON API port. OwnTone's own web UI is also here, and Home Assistant's OwnTone integration can be pointed at it. |

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

## Volume

The phone's volume slider is forwarded straight to the speakers: the receiver passes the audio through at full scale and reports each volume change to the add-on, which sets the speakers' own volumes in OwnTone immediately. That avoids the delay you would get if the volume were baked into the audio before it goes through OwnTone's buffer.

The per-speaker levels in a group are the speaker's volume at 100% on the phone; lower phone volumes scale them down proportionally. Use them to balance a loud speaker against a quiet one.

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
