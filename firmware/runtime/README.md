# Source radio runtime

The E200 firmware builds `radio.c` and the public-domain MD5 implementation into
`/sbin/e200`, `/sbin/net_set`, and `/bin/ip_set`. E316 retains its vendor runtime.
These processes handle discovery and FPGA network setup. UHD control and sample
streaming remain in the FPGA and host driver.

`e200` replies to 48-byte and 56-byte legacy discovery requests on UDP 49100.
It checks all four header words and the exact datagram length, returns the same
56-byte E200 response, and preserves the existing DNA-derived serial:
lowercase MD5 of the lowercase hexadecimal FPGA DNA without leading zeroes.
MD5 is used for compatibility with the existing identity scheme.

Hardware access is restricted to the existing DNA, MAC-enable, and IPv4 MMIO
registers. The runtime never writes EEPROM. `net_set` follows the IPv4 address
assigned to `eth0`; `ip_set ADDRESS` performs a single FPGA address update.
SIGTERM and SIGINT terminate cleanly. The existing PID-file service scripts and
health supervisor continue to own startup, shutdown and recovery.

For a local UDP protocol test without hardware access:

```sh
make -C firmware/runtime OUT=../build/runtime-native
python3 -m unittest discover -s firmware/runtime/tests -v
```

The tests bind localhost and check packet compatibility, malformed datagrams,
identity derivation, and signal shutdown. `--no-hardware` requires an explicit
test DNA and localhost bind; a supplied DNA cannot override real hardware mode.
