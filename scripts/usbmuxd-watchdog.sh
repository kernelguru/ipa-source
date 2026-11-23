#!/bin/sh
# usbmuxd watchdog for the Docker host (Alpine/OpenRC or systemd).
#
# Detects a crashed OR hung usbmuxd (process still there but no longer
# answering), kills it hard, starts it again and restarts the ipa-source
# container, whose bind-mounted socket otherwise keeps pointing at the old one.
#
# Install (as root):
#   cp scripts/usbmuxd-watchdog.sh /usr/local/sbin/usbmuxd-watchdog
#   chmod 755 /usr/local/sbin/usbmuxd-watchdog
#   echo '* * * * * /usr/local/sbin/usbmuxd-watchdog' >> /etc/crontabs/root   # Alpine
#   rc-update add crond default; rc-service crond start
# Test: usbmuxd-watchdog -v      Log: grep usbmuxd-watchdog /var/log/messages
#
# Settings (environment or /etc/default/usbmuxd-watchdog):
#   CONTAINER=ipa-source    container to restart ("" = none)
#   PROBE_TIMEOUT=8         seconds until usbmuxd counts as hung
set -u

CONTAINER=${CONTAINER-ipa-source}
PROBE_TIMEOUT=${PROBE_TIMEOUT:-8}
[ -r /etc/default/usbmuxd-watchdog ] && . /etc/default/usbmuxd-watchdog
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1
LOCK=/run/usbmuxd-watchdog.lock
PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log() {
    logger -t usbmuxd-watchdog "$*" 2>/dev/null
    [ "$VERBOSE" = 1 ] && echo "usbmuxd-watchdog: $*"
    return 0
}

mkdir "$LOCK" 2>/dev/null || { [ "$VERBOSE" = 1 ] && echo "already running"; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

container_running() {
    [ -n "$CONTAINER" ] && command -v docker >/dev/null 2>&1 &&
        [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]
}

# probe <where>: 0 = usbmuxd answered, 1 = no answer/hung, 2 = cannot probe
probe() {
    case "$1" in
        host)
            command -v idevice_id >/dev/null 2>&1 || return 2
            timeout "$PROBE_TIMEOUT" idevice_id -l >/dev/null 2>&1 ;;
        container)
            container_running || return 2
            timeout "$PROBE_TIMEOUT" docker exec "$CONTAINER" idevice_id -l >/dev/null 2>&1 ;;
    esac
    [ $? -eq 0 ] && return 0 || return 1
}

# twice, a few seconds apart, so a single slow answer does not trigger a restart
probe_twice() {
    probe "$1"; r=$?
    [ $r -ne 1 ] && return $r
    sleep 3
    probe "$1"
}

svc() {  # svc start|stop|zap
    if command -v rc-service >/dev/null 2>&1; then
        timeout 15 rc-service usbmuxd "$1" >/dev/null 2>&1
    elif command -v systemctl >/dev/null 2>&1; then
        [ "$1" = zap ] && return 0
        timeout 15 systemctl "$1" usbmuxd >/dev/null 2>&1
    fi
}

restart_usbmuxd() {
    log "usbmuxd antwortet nicht ($1) – starte neu"
    svc stop
    sleep 1
    pids=$(pidof usbmuxd 2>/dev/null)
    if [ -n "$pids" ]; then
        log "usbmuxd hängt noch (PID $pids) – kill -9"
        kill -9 $pids 2>/dev/null
        sleep 1
    fi
    rm -f /var/run/usbmuxd.pid /run/usbmuxd.pid
    svc zap            # OpenRC: reset "crashed"/"started" state after a hard kill
    svc start
    sleep 2
    if [ -z "$(pidof usbmuxd 2>/dev/null)" ]; then
        log "usbmuxd startet nicht – bitte manuell prüfen (usbmuxd -f -v)"
        return 1
    fi
    log "usbmuxd läuft wieder (PID $(pidof usbmuxd))"
}

restart_container() {
    container_running || return 0
    log "starte Container $CONTAINER neu ($1)"
    timeout 60 docker restart "$CONTAINER" >/dev/null 2>&1 || log "Container-Neustart fehlgeschlagen"
}

# 1) process gone entirely?
if [ -z "$(pidof usbmuxd 2>/dev/null)" ]; then
    restart_usbmuxd "Prozess läuft nicht" && restart_container "usbmuxd neu gestartet"
    exit 0
fi

# 2) process there but hung?
probe_twice host; host=$?
if [ $host -eq 1 ]; then
    restart_usbmuxd "Abfrage auf dem Host > ${PROBE_TIMEOUT}s" && restart_container "usbmuxd neu gestartet"
    exit 0
fi

probe_twice container; cont=$?
if [ $cont -eq 1 ]; then
    if [ $host -eq 0 ]; then
        # host side fine → only the container still has the old socket
        restart_container "Socket im Container veraltet"
    else
        restart_usbmuxd "Abfrage im Container > ${PROBE_TIMEOUT}s" && restart_container "usbmuxd neu gestartet"
    fi
    exit 0
fi

[ "$VERBOSE" = 1 ] && echo "usbmuxd ok (host=$host container=$cont; 0=ok 2=nicht prüfbar)"
exit 0
