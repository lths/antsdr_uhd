#!/bin/sh

. /etc/device_config || exit 1

FRM_FILE="$1"

flash_indication_on() {
	echo timer > /sys/class/leds/led0:green/trigger
	echo 40 > /sys/class/leds/led0:green/delay_off
	echo 40 > /sys/class/leds/led0:green/delay_on
}

flash_indication_off() {
	echo heartbeat > /sys/class/leds/led0:green/trigger
}

handle_frimware_frm () {
	FILE="$1"
	MAGIC="$2"
	[ -n "$MAGIC" ] || return 1
	md5=$(tail -c 33 "$FILE") || return 1
	head -c -33 "$FILE" > /opt/firmware.frm || return 1
	FRM_SIZE=$(wc -c < /opt/firmware.frm) || return 1
	FRM_SIZE=$(printf '%X' "$FRM_SIZE") || return 1
	frm=$(md5sum /opt/firmware.frm) || return 1
	frm=${frm%% *}
	if [ "$frm" != "$md5" ]; then
		echo "Failed Checksum error: $frm $md5" >&2
		return 1
	fi

	flash_indication_on
	if grep -Fq -- "$MAGIC" /opt/firmware.frm &&
		dd if=/opt/firmware.frm of=/dev/mtdblock3 bs=64k &&
		fw_setenv fit_size "$FRM_SIZE"; then
		result=0
	else
		result=1
	fi
	# Flush even after a failed write or environment update, while preserving
	# the failure status through LED reset and scratch-file cleanup.
	sync || result=1
	flash_indication_off
	if [ "$result" -eq 0 ]; then
		echo "Done"
	else
		echo "Failed" >&2
	fi
	return "$result"
}

case "$FRM_FILE" in
	*.frm)
		if [ -f "$FRM_FILE" ] && [ -s "$FRM_FILE" ]; then
			trap 'rm -f /opt/firmware.frm' 0
			handle_frimware_frm "$FRM_FILE" "$FRM_MAGIC"
			exit $?
		fi
		;;
esac

echo "Failed" >&2
exit 1
