#!/bin/sh

DEVICE=e310v2
RUNDIR=/run

start_daemon() {
	[ -x "$1" ] || { echo "Missing executable: $1" >&2; return 1; }
	executable=$1
	pidfile=$2
	shift 2
	start-stop-daemon --start --quiet --oknodo --background --make-pidfile \
		--pidfile "$pidfile" --exec "$executable" -- "$@" 9>&-
}

running() {
	start-stop-daemon --stop --test --quiet --pidfile "$2" --exec "$1"
}

stop_daemon() {
	start-stop-daemon --stop --quiet --oknodo --pidfile "$2" --exec "$1" || return 1
	# Do not remove the PID file or restart while the old process still exists.
	attempt=0
	while running "$1" "$2"; do
		attempt=$((attempt + 1))
		[ "$attempt" -lt 5 ] || return 1
		sleep 1
	done
	rm -f "$2"
}

stop() {
	result=0
	stop_daemon /bin/ping "$RUNDIR/antsdr-ping.pid" || result=1
	stop_daemon /sbin/net_set "$RUNDIR/net_set.pid" || result=1
	stop_daemon "/sbin/$DEVICE" "$RUNDIR/$DEVICE.pid" || result=1
	return "$result"
}

start() {
	if ! start_daemon "/sbin/$DEVICE" "$RUNDIR/$DEVICE.pid" ||
		! start_daemon /sbin/net_set "$RUNDIR/net_set.pid"; then
		stop
		return 1
	fi
	# BusyBox starts background processes asynchronously. Detect an immediate
	# exec failure or crash before reporting successful startup.
	sleep 1
	if ! running "/sbin/$DEVICE" "$RUNDIR/$DEVICE.pid" ||
		! running /sbin/net_set "$RUNDIR/net_set.pid"; then
		stop
		return 1
	fi
	# Use the actual broadcast address, including for non-/24 networks.
	broadcast=$(ip -o -4 addr show dev eth0 | awk '{for(i=1;i<NF;i++) if($i=="brd") {print $(i+1); exit}}')
	if [ -n "$broadcast" ]; then
		start_daemon /bin/ping "$RUNDIR/antsdr-ping.pid" "$broadcast" || { stop; return 1; }
	fi
}

if [ "$1" != status ]; then
	mkdir -p "$RUNDIR" || exit 1
	exec 9>"$RUNDIR/antsdr-service.lock" || exit 1
	flock -n 9 || { echo 'Device service is busy' >&2; exit 1; }
fi
case "$1" in
	start) start; result=$? ;;
	stop) stop; result=$? ;;
	restart) stop && start; result=$? ;;
	status)
		running "/sbin/$DEVICE" "$RUNDIR/$DEVICE.pid" && running /sbin/net_set "$RUNDIR/net_set.pid"
		result=$?
		;;
	*) echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
esac
if [ "$result" -eq 0 ]; then echo OK; else echo FAIL; fi
exit "$result"
