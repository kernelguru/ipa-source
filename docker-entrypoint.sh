#!/bin/sh
set -e
# Optional: run usbmuxd inside the container (needs the USB bus mapped in, see README).
# Default is to use the host's usbmuxd via the mounted /var/run/usbmuxd socket.
if [ "${USBMUXD_EMBEDDED:-0}" = "1" ]; then
  mkdir -p /data/lockdown
  if [ ! -L /var/lib/lockdown ]; then rm -rf /var/lib/lockdown; ln -s /data/lockdown /var/lib/lockdown; fi
  mkdir -p /var/run
  usbmuxd -U root || echo "usbmuxd konnte nicht starten"
fi
# Default command: the web server. LISTEN_PORT matters with network_mode: host,
# when 8080 on the host is already taken by another service.
if [ "$#" -eq 0 ]; then
  set -- uvicorn app.main:app --host "${LISTEN_HOST:-0.0.0.0}" --port "${LISTEN_PORT:-8080}" \
    --proxy-headers --forwarded-allow-ips "*"
fi
exec "$@"
