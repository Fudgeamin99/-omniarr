#!/bin/sh
# Run as PUID:PGID (like linuxserver.io images) so files in your library belong to you.
set -e
umask "${UMASK:-002}"
mkdir -p /config

if [ "$(id -u)" = "0" ] && [ -n "${PUID:-}" ]; then
  PGID="${PGID:-$PUID}"
  chown -R "$PUID:$PGID" /config 2>/dev/null || true
  export HOME=/config
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
