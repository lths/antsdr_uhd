/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef ANTSDR_MANAGEMENT_H
#define ANTSDR_MANAGEMENT_H
#define ANTSDR_MGMT_MAGIC 0x414d4731u /* AMG1 */
#define ANTSDR_MGMT_VERSION 0x00010000u
#define ANTSDR_LEASE_MAGIC 0x414c5331u /* ALS1 */
#define ANTSDR_LEASE_PORT 49101
#define ANTSDR_LEASE_MS 3000
/* Wire format: eight network-endian u32 words. Version is exactly one.
 * Request: magic, version, operation, nonce, token_hi, token_lo, 0, 0.
 * Reply: same first six words; word6=status, word7=remaining milliseconds.
 * Ops: 0=query, 1=acquire, 2=renew, 3=release. Status: 0=OK, 1=busy,
 * 2=not owner, 3=invalid, 4=hardware/network unavailable.
 * Tokens coordinate ownership; this LAN protocol is not authentication. */
#endif
