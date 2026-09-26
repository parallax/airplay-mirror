#!/bin/sh
# Container entrypoint: bring up D-Bus and Avahi (needed by both OwnTone and shairport-sync for
# Bonjour), then hand over to the controller which supervises the audio processes.
set -eu

mkdir -p /run/dbus /run/avahi-daemon /var/cache/owntone /data/pipes /data/meta /data/owntone /data/shairport
rm -f /run/dbus/pid /run/avahi-daemon/pid

dbus-daemon --system --fork
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -S /run/dbus/system_bus_socket ] && break
  sleep 0.5
done

# On Home Assistant OS avahi warns about "another IPv4 mDNS stack" (HA's own zeroconf). Harmless.
avahi-daemon -D

exec python3 -m airplay_mirror
