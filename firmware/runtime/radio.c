// SPDX-License-Identifier: GPL-2.0-or-later
/* E200 discovery and FPGA network setup. Main UHD control/data stay in FPGA. */
#define _DEFAULT_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>
#include "md5.h"

#define VERSION "antsdr-runtime 1.0"
#define PAGE 4096
static volatile sig_atomic_t stopping;
static void stop(int sig) { (void)sig; stopping = 1; }
static void fail(const char *message) { perror(message); exit(EXIT_FAILURE); }

static volatile uint32_t *map_register(off_t address, bool write)
{
    int fd = open("/dev/mem", (write ? O_RDWR : O_RDONLY) | O_SYNC | O_CLOEXEC);
    if (fd < 0) fail("/dev/mem");
    void *p = mmap(NULL, PAGE, PROT_READ | (write ? PROT_WRITE : 0), MAP_SHARED, fd, address);
    int saved = errno;
    close(fd);
    errno = saved;
    if (p == MAP_FAILED) fail("FPGA register mapping");
    return p;
}

static void write_register(volatile uint32_t *reg, uint32_t value)
{
    __sync_synchronize();
    *reg = value;
    __sync_synchronize();
}

static void serial_from_dna(uint64_t dna, char serial[33])
{
    unsigned char digest[16];
    char text[17];
    int n = snprintf(text, sizeof(text), "%" PRIx64, dna);
    md5((unsigned char *)text, n, digest);
    for (int i = 0; i < 16; i++) snprintf(serial + 2*i, 3, "%02x", digest[i]);
}

static uint64_t parse_dna(const char *text)
{
    char *end;
    errno = 0;
    if (!text[0] || strlen(text) > 16 || strspn(text, "0123456789abcdefABCDEF") != strlen(text)) {
        fprintf(stderr, "DNA must be 1 to 16 hexadecimal digits\n"); exit(2);
    }
    uint64_t dna = strtoull(text, &end, 16);
    if (errno || *end) fail("DNA");
    return dna;
}

static int discovery(int argc, char **argv)
{
    bool simulated = false;
    const char *bind_ip = "0.0.0.0", *test_dna = NULL;
    unsigned port = 49100;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--no-hardware")) simulated = true;
        else if (!strcmp(argv[i], "--dna") && i+1 < argc) test_dna = argv[++i];
        else if (!strcmp(argv[i], "--bind") && i+1 < argc) bind_ip = argv[++i];
        else if (!strcmp(argv[i], "--port") && i+1 < argc) {
            char *end; errno = 0; unsigned long p = strtoul(argv[++i], &end, 10);
            if (errno || !argv[i][0] || *end || p > 65535) return 2;
            port = p;
        } else { fprintf(stderr, "Unknown or incomplete argument: %s\n", argv[i]); return 2; }
    }
    if ((simulated && (!test_dna || strcmp(bind_ip, "127.0.0.1"))) || (!simulated && test_dna)) {
        fprintf(stderr, "Test DNA requires --no-hardware --bind 127.0.0.1\n"); return 2;
    }
    char serial[33];
    if (simulated) serial_from_dna(parse_dna(test_dna), serial);
    else {
        volatile uint32_t *dna = map_register(0x43c00000, false);
        uint32_t lo = dna[0], hi = dna[1];
        munmap((void *)dna, PAGE);
        serial_from_dna(((uint64_t)hi << 32) | lo, serial);
    }
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) fail("discovery socket");
    struct sockaddr_in local = {.sin_family = AF_INET, .sin_port = htons(port)};
    if (inet_pton(AF_INET, bind_ip, &local.sin_addr) != 1) { close(fd); return 2; }
    if (bind(fd, (struct sockaddr *)&local, sizeof(local))) fail("discovery bind");
    // Bind succeeds before enabling the FPGA. Never write EEPROM or change IP here.
    if (!simulated) {
        volatile uint32_t *mac = map_register(0x40005000, true);
        write_register(mac + 2, 1);
        munmap((void *)mac, PAGE);
    }
    socklen_t len = sizeof(local);
    if (getsockname(fd, (struct sockaddr *)&local, &len)) fail("getsockname");
    printf("%s discovery port=%u serial=%s\n", VERSION, ntohs(local.sin_port), serial);
    fflush(stdout);
    const uint32_t request[4] = {htonl('1'), htonl('m'), htonl('9'), htonl('j')};
    unsigned char response[56] = {0};
    uint32_t header[4] = {htonl('1'), htonl('M'), htonl('0'), htonl('c')};
    memcpy(response, header, 16); memcpy(response+16, serial, 32);
    memcpy(response+48, "E200  v1", 8);
    struct pollfd wait = {.fd = fd, .events = POLLIN};
    while (!stopping) {
        int ready = poll(&wait, 1, 500);
        if (ready < 0) { if (errno == EINTR) continue; fail("discovery poll"); }
        if (!ready) continue;
        unsigned char packet[2048];
        struct sockaddr_in peer; socklen_t peer_len = sizeof(peer);
        ssize_t n = recvfrom(fd, packet, sizeof(packet), MSG_TRUNC, (struct sockaddr *)&peer, &peer_len);
        if (n < 0) { if (errno == EINTR) continue; fail("discovery receive"); }
        if ((n != 48 && n != 56) || memcmp(packet, request, sizeof(request))) continue;
        if (sendto(fd, response, sizeof(response), 0, (struct sockaddr *)&peer, peer_len) < 0 && errno != EINTR)
            perror("discovery reply");
    }
    close(fd); return 0;
}

static int network(bool once, const char *requested_ip)
{
    struct in_addr ip;
    if (requested_ip && inet_pton(AF_INET, requested_ip, &ip) != 1) return 2;
    volatile uint32_t *reg = map_register(0x40006000, true);
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) fail("network socket");
    uint32_t previous = UINT32_MAX;
    do {
        struct ifreq interface = {0};
        memcpy(interface.ifr_name, "eth0", 5);
        if (!requested_ip && ioctl(fd, SIOCGIFADDR, &interface)) {
            if (once || (errno != EADDRNOTAVAIL && errno != ENODEV)) fail("eth0 IPv4 address");
        } else {
            uint32_t value = ntohl(requested_ip ? ip.s_addr : ((struct sockaddr_in *)&interface.ifr_addr)->sin_addr.s_addr);
            if (value != previous || *reg != value) {
                write_register(reg, value); previous = value;
                printf("%s FPGA IPv4=%u.%u.%u.%u\n", VERSION, value>>24, (value>>16)&255, (value>>8)&255, value&255);
                fflush(stdout);
            }
        }
        if (!once && !stopping) poll(NULL, 0, 1000);
    } while (!once && !stopping);
    munmap((void *)reg, PAGE); close(fd); return 0;
}

int main(int argc, char **argv)
{
    struct sigaction action = {.sa_handler = stop};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) || sigaction(SIGINT, &action, NULL)) fail("signals");
    if (argc == 2 && !strcmp(argv[1], "--version")) { puts(VERSION); return 0; }
    const char *name = strrchr(argv[0], '/'); name = name ? name+1 : argv[0];
    if (!strcmp(name, "net_set")) return argc == 1 ? network(false, NULL) : 2;
    if (!strcmp(name, "ip_set")) return argc == 2 ? network(true, argv[1]) : 2;
    return discovery(argc, argv);
}
