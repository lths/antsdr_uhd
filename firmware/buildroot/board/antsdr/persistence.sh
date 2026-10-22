# Shared by credential save/restore commands. Snapshot directories are private;
# an atomic pointer-file replacement publishes a complete, checksummed generation.
PERSIST_ROOT=/mnt/jffs2
PERSIST_STORE=$PERSIST_ROOT/.antsdr

persist_mounted() {
	awk '$2 == "/mnt/jffs2" && $3 == "jffs2" && $4 ~ /(^|,)rw(,|$)/ { found=1 }
		END { exit !found }' /proc/mounts
}

persist_require() {
	persist_mounted && return 0
	echo 'Persistent JFFS2 storage is not mounted read/write at /mnt/jffs2' >&2
	return 1
}

persist_lock() {
	exec 9>/run/antsdr-persistence.lock || return 1
	flock -n 9 || { echo 'Persistent settings are busy' >&2; return 1; }
}

persist_cleanup() {
	[ -z "$SNAPSHOT" ] || rm -rf "$SNAPSHOT" "$SNAPSHOT.current"
}

persist_begin() {
	umask 077
	mkdir -p "$PERSIST_STORE" || return 1
	KIND=$1
	SNAPSHOT=$(mktemp -d "$PERSIST_STORE/$KIND.XXXXXX") || return 1
	trap persist_cleanup 0
	trap 'exit 1' HUP INT TERM
}

persist_publish() {
	old=$(cat "$PERSIST_STORE/$KIND.current" 2>/dev/null) || old=
	# Flush the complete snapshot before publishing it. If either sync fails,
	# report failure; never remove a snapshot that has already been published.
	printf '%s\n' "${SNAPSHOT##*/}" > "$SNAPSHOT.current" || return 1
	sync || return 1
	mv -f "$SNAPSHOT.current" "$PERSIST_STORE/$KIND.current" || return 1
	SNAPSHOT=
	sync || return 1
	case "$old" in *[!A-Za-z0-9_.]*) return 0 ;; esac
	case "$old" in
		"$KIND".??????) rm -rf "$PERSIST_STORE/$old" ;;
	esac
}

# Resolve a published generation, or read the legacy layout when no snapshot
# has been saved yet. Treat an invalid pointer as corruption, not as first boot.
persist_directory() {
	HAS_SNAPSHOT=0
	dir=$2
	if [ -f "$PERSIST_STORE/$1.current" ]; then
		HAS_SNAPSHOT=1
		generation=$(cat "$PERSIST_STORE/$1.current") || return 1
		case "$generation" in *[!A-Za-z0-9_.]*) return 1 ;; esac
		case "$generation" in "$1".??????) ;; *) return 1 ;; esac
		dir=$PERSIST_STORE/$generation
		[ -d "$dir" ] && [ ! -L "$dir" ] || return 1
	fi
}
