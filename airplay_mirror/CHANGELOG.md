# Changelog

## 0.1.7

- Fix cover art only appearing from the second track: a picture the phone sends just before the track's metadata bundle is now kept with that track, and art is replayed to OwnTone after the bundle.

## 0.1.6

- MQTT: publishes what's playing (retained state and artwork topics), accepts stop/rescan/volume commands, and creates Home Assistant entities via discovery. Uses the Mosquitto add-on automatically.
- Cover art on the now-playing card, served by the add-on from the metadata relay (JPEG or PNG, as sent by the phone).

## 0.1.5

- Track title, artist, album and artwork now reach OwnTone (and the now-playing card): shairport-sync sends them before OwnTone starts listening, so a relay in the add-on remembers the current track and replays it.
- Redesigned interface in Apple's idiom: large title, grouped cards, capsule buttons, switches, a proper now-playing card, light and dark themes, and a phone-friendly layout. The OwnTone link is now a button.
- Hide Chromecast and other non-AirPlay outputs: they cannot be added to groups, and speakers that speak both protocols are no longer ambiguous. The Speakers card lists what was hidden.

## 0.1.4

- Per-speaker sync offsets (-2000..2000 ms) in the group editor, applied live while playing.
- Link to OwnTone's own web UI from the Speakers card.
- Fix speakers dropping much quieter when a group slider was moved: OwnTone also applies the phone's volume (from the metadata pipe) as its master volume, and the two volume models fought. Levels are now relative to the loudest speaker, matching OwnTone's model, and live previews apply the whole group at once.

## 0.1.3

- Editing the group that is playing applies the volume sliders to the speakers live; Save keeps them, Cancel restores the old levels. Saving a playing group also applies its new speakers and levels immediately.

## 0.1.2

- Pass the phone's volume changes straight through to the speakers instead of baking them into the audio, so they take effect immediately rather than after the buffer has played out. Group speaker levels now act as each speaker's level at 100%.

## 0.1.1

- Fix the add-on staying in "starting" forever (the ingress panel never opened): report a real container health check instead of inheriting OwnTone's.
- Log OwnTone readiness, the speakers it can see, and a hint when no groups exist yet.
- Fix the group editor losing ticked speakers on every status refresh.

## 0.1.0

- Initial release.
