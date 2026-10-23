# E200 runtime management

Runtime 2 replaces the E200 discovery and network binaries with source-built
services. The modern E200 image includes matching FPGA and NIC-driver support;
the E316 runtime and its FPGA behavior are unchanged.

`/sbin/e200` handles discovery and device ownership. `/sbin/net_set` watches
`NETLINK_ROUTE` link and IPv4 notifications, resnapshots `eth0`, verifies FPGA
readback, and performs a one-second watchdog resync in case notifications were
lost. Address removal, interface removal, or carrier loss writes IPv4 zero and
revokes ownership. A restored address allows a fresh session; an existing host
must reconnect. `/bin/ip_set ADDRESS` remains an administrator's one-shot helper.

The daemon reads eight board-revision bytes at EEPROM offset 100 without writing
EEPROM. Valid printable E200 revisions are returned in the unchanged 56-byte
discovery response. Missing or invalid revision data is reported as `E200 ??`
with `board_source=unavailable`, rather than claiming a known revision. DNA and
its existing MD5 serial algorithm remain unchanged.

## Driver boundary and compatibility

The NIC driver owns all management MMIO and exposes only these files under
`/sys/class/net/eth0/device/`:

| File | Access | Operation |
| --- | --- | --- |
| `antsdr_status` | Read | Signature, ABI, IPv4, MAC-enable, owner IPv4, remaining lease milliseconds, expiry count |
| `antsdr_dna` | Read | Read-only 64-bit FPGA DNA |
| `antsdr_network` | Root write | One hexadecimal IPv4 value; changing it revokes the lease |
| `antsdr_enable` | Root write | Exactly zero or one |
| `antsdr_lease` | Root write | Hexadecimal owner IPv4 and decimal timeout, bounded to 3000 ms |

The driver first checks the existing MAC-enable register's readback for marker
`0x414d4700` (with bit 0 retaining its enable meaning). Older FPGA images return
no marker, so the driver avoids reads of unsupported addresses and their AXI
bus errors. The driver and runtime then require signature `0x414d4731` (`AMG1`) and ABI
`0x00010000`. Incompatible FPGA images leave the management API unavailable and
produce a kernel error; the daemons fail startup instead of enabling unknown
register layouts. The NIC can still serve Linux networking for recovery.

Each daemon opens only its necessary attributes and sockets as root, then clears
supplementary groups, changes its UID/GID, disables dumps, and sets
`no_new_privs`. Radio and network use UIDs **65301** and **65302**, respectively.
The held file descriptors authorize narrowly defined driver operations after
the privilege drop; neither daemon maps physical memory or has arbitrary
register access. Reserve these numeric UIDs for the services.

## Device ownership

The host driver acquires ownership **before** creating radio control/data
transports. It renews every 500 ms and releases ownership after controllers and
transports are destroyed. A rejected or unanswered renewal invalidates that
session; it never automatically reacquires or steals another owner's lease.
Existing same-host process locks remain in place.

UDP **49101** carries eight network-endian 32-bit words. The request is
`magic=0x414c5331, version=1, operation, nonce, token_hi, token_lo, 0, 0`.
Operations are query=0, acquire=1, renew=2, release=3. Acquire uses token zero;
successful acquire returns a random 64-bit token. Renew/release require both
that token and the same source IPv4. Reply words 0–3 echo the request, words 4–5
contain the token only for successful acquire/renew, word 6 is status, and word 7
is remaining milliseconds. Status values are OK=0, busy=1, not-owner=2,
invalid=3, hardware/network-unavailable=4. Retrying acquire with the same nonce
and source is idempotent and does not extend the lease. Invalid sizes/versions
are discarded. Tokens never appear in diagnostics or busy/query responses.

The FPGA gates the radio UDP fast path by owner source IPv4 and independently
expires leases, even if the daemon or host stops. Expiry resets the radio and
closes TX sample/amplifier outputs. Discovery, ownership, SSH, ARP, and other
Linux traffic remain available. A handover may discard an in-flight Ethernet
packet; normal networking retries continue.

The DDR TX queue stores only complete packets with a private session-epoch and
length prefix. Old packets finish their DDR writes and drain without reaching
the radio. Partial packets, backpressure, and a new owner do not turn an old
queued or timed burst into a new session's transmission. The 64 MiB queue at
`0x18000000` is reserved from Linux allocation.

Leases coordinate cooperative clients on a trusted LAN. Source IPv4 filtering
and UDP tokens are **not cryptographic authentication or protection against a
hostile LAN/spoofed radio packets**. There is no remote force-takeover command.
Older E200 firmware needs explicit `legacy_no_lease=1` in UHD device arguments;
that flag cannot bypass a new FPGA's lease gate. E316 retains its legacy path.

## Diagnostics and verification

Atomic JSON reports are published under `/run/antsdr-runtime/radio/status.json`
and `/run/antsdr-runtime/network/status.json`. They include daemon UID, board
identity/source, discovery totals/rejections, lease acquisitions/denials/expiry,
last network update time, faults, and FPGA readback. The host health API and
dashboard show them; reports older than five seconds are marked stale. Fatal
startup and hardware failures also reach syslog as `antsdr-runtime`.

Run native protocol tests with:

```sh
make -C firmware/runtime OUT=../build/runtime-native
python3 -m unittest discover -s firmware/runtime/tests -v
```

`firmware/fpga/tests/run_lease.sh` uses Vivado XSim to test independent expiry,
renewal, owner changes, stalled/partial DDR queue handovers, oversized packets,
source filtering on every offload port, and continued CPU access without a
lease. The focused C++ `ant_control_test` includes lease lifetime, competing
owner, and lost-renewal tests. Physical external PPS triggering still needs a
connected pulse source.
