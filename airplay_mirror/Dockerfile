# AirPlay Mirror = OwnTone (AirPlay 2 sender, from the official image) + shairport-sync classic receivers
# (Alpine package) + a small Python controller with the ingress UI.
ARG OWNTONE_TAG=29.3
FROM owntone/owntone:${OWNTONE_TAG}

ARG BUILD_VERSION=dev
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="addon" \
      org.opencontainers.image.source="https://github.com/parallax/airplay-mirror" \
      org.opencontainers.image.description="Virtual AirPlay speakers that relay audio to groups of real AirPlay speakers" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 YARL_NO_EXTENSIONS=1 \
    FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1

# Alpine's shairport-sync is a "classic" (AirPlay 1) build with the pipe and metadata backends we need.
# The checks make a base-image drift fail the build instead of failing on the Pi.
RUN apk add --no-cache shairport-sync python3 py3-pip curl \
 && echo "shairport-sync: $(shairport-sync -V)" \
 && shairport-sync -V | grep -qi pipe \
 && shairport-sync -V | grep -qi metadata \
 && shairport-sync -V | grep -qi avahi \
 && ! shairport-sync -V | grep -qi airplay2 \
 && echo "owntone: $(owntone --version 2>&1 | head -1)" \
 && for key in pipe_autostart speaker_autoselect airplay2_disable permanent reconnect exclude db_backup_path; do \
      grep -q "$key" /usr/share/doc/owntone/examples/owntone.conf || { echo "owntone.conf lacks $key"; exit 1; }; \
    done \
 # Drop the OpenRC boot the base image uses; our entrypoint starts things itself.
 && (rc-update del owntone default || true) \
 && rm -f /etc/init.d/owntone /etc/init.d/shairport-sync /etc/owntone/owntone.conf \
 && mkdir -p /run/dbus /run/avahi-daemon /var/cache/owntone \
 && dbus-uuidgen --ensure \
 && (adduser -S -D -H -s /sbin/nologin shairport-sync 2>/dev/null || true)

WORKDIR /app
COPY requirements.txt ./
RUN pip install --break-system-packages -r requirements.txt
COPY airplay_mirror ./airplay_mirror
COPY rootfs/ /
RUN chmod 755 /entrypoint.sh /usr/local/bin/am-hook

# The Supervisor keeps an app in "starting" until its container reports healthy (an inherited
# HEALTHCHECK NONE still counts as a healthcheck), so report on our own web endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf http://127.0.0.1:8099/api/health || exit 1
EXPOSE 8099
ENTRYPOINT ["/entrypoint.sh"]
