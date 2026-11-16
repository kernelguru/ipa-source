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
exec "$@"
