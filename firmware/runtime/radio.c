// SPDX-License-Identifier: GPL-2.0-or-later
/* E200 discovery, device ownership, and event-driven network synchronization. */
#define _DEFAULT_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <inttypes.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>
#include "md5.h"
#include "management.h"

#define VERSION "antsdr-runtime 2.0"
#define DEVICE "/sys/class/net/eth0/device/"
static volatile sig_atomic_t stopping;
static int status_dir = -1, status_fd = -1, lease_fd = -1;
static bool simulated;
static const char *fault = "";
static uint32_t owner_ip, acquire_nonce;
static uint64_t token, deadline, cooldown;
static unsigned long requests, rejected, replies, acquired, denied, expired, updates;
static uint64_t last_update;
static char serial[33], board[9] = "E200  ??";
static const char *board_source = "unavailable";
static void stop(int sig) { (void)sig; stopping = 1; }
static void fail(const char *message) {
    int saved=errno;
    openlog("antsdr-runtime", LOG_PID, LOG_DAEMON);
    syslog(LOG_ERR, "%s: %s", message, strerror(saved));
    errno=saved; perror(message); exit(EXIT_FAILURE);
}
static uint64_t now_ms(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("monotonic clock");
    return (uint64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static int attribute(const char *name, int flags)
{
    char path[128];
    snprintf(path, sizeof(path), DEVICE "antsdr_%s", name);
    int fd = open(path, flags | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) fail(path);
    return fd;
}
static int write_text(int fd, const char *text)
{
    size_t n = strlen(text);
    if (lseek(fd, 0, SEEK_SET) < 0 || write(fd, text, n) != (ssize_t)n) return -1;
    return 0;
}
struct registers { unsigned magic, version, ip, enabled, owner, ms, expiries; };
static struct registers read_registers(void)
{
    struct registers r = {0};
    if (simulated) {
        r.magic = ANTSDR_MGMT_MAGIC; r.version = ANTSDR_MGMT_VERSION;
        r.ip = 0x7f000001; r.enabled = 1; r.owner = owner_ip;
        r.ms = deadline > now_ms() ? deadline - now_ms() : 0;
        return r;
    }
    char buf[160];
    ssize_t n = pread(status_fd, buf, sizeof(buf)-1, 0);
    if (n <= 0) fail("management readback");
    buf[n] = 0;
    if (sscanf(buf, "%x %x %x %u %x %u %u", &r.magic, &r.version,
               &r.ip, &r.enabled, &r.owner, &r.ms, &r.expiries) != 7) {
        errno = EPROTO; fail("management readback format");
    }
    if (r.magic != ANTSDR_MGMT_MAGIC || r.version != ANTSDR_MGMT_VERSION) {
        errno = EPROTO; fail("incompatible FPGA management ABI");
    }
    return r;
}
static void report(bool network)
{
    if (status_dir < 0) return;
    struct registers r = read_registers();
    int fd = openat(status_dir, "status.tmp", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW, 0644);
    if (fd < 0) fail("diagnostic file");
    FILE *f = fdopen(fd, "w");
    if (!f) fail("diagnostic stream");
    fprintf(f, "{\"schema_version\":1,\"runtime_version\":\"2.0\",\"role\":\"%s\","
        "\"updated_monotonic_ms\":%" PRIu64 ",\"uid\":%u,\"fault\":\"%s\","
        "\"board\":\"%.8s\",\"board_source\":\"%s\",\"serial\":\"%s\","
        "\"discovery_requests\":%lu,\"discovery_rejected\":%lu,\"discovery_replies\":%lu,"
        "\"leases_acquired\":%lu,\"leases_denied\":%lu,\"leases_expired\":%lu,"
        "\"network_updates\":%lu,\"last_network_update_ms\":%" PRIu64 ","
        "\"fpga\":{\"signature\":%u,\"version\":%u,\"ipv4\":%u,\"enabled\":%u,"
        "\"owner_ipv4\":%u,\"lease_remaining_ms\":%u,\"expirations\":%u}}\n",
        network ? "network" : "radio", now_ms(), (unsigned)getuid(), fault,
        board, board_source, serial, requests, rejected, replies, acquired, denied,
        expired, updates, last_update, r.magic, r.version, r.ip, r.enabled,
        r.owner, r.ms, r.expiries);
    if (fclose(f) || renameat(status_dir, "status.tmp", status_dir, "status.json")) fail("diagnostic publish");
}
static void drop_privileges(bool network)
{
    if (simulated) return;
    uid_t uid = network ? 65302 : 65301;
    const char *path = network ? "/run/antsdr-runtime/network" : "/run/antsdr-runtime/radio";
    if (mkdir("/run/antsdr-runtime", 0755) && errno != EEXIST) fail("runtime directory");
    if (mkdir(path, 0755) && errno != EEXIST) fail("status directory");
    status_dir = open(path, O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
    if (status_dir < 0 || fchown(status_dir, uid, uid) || fchmod(status_dir, 0755)) fail("status permissions");
    if (setgroups(0, NULL) || setgid(uid) || setuid(uid) ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) || prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)) fail("drop privileges");
}
static void serial_from_dna(uint64_t dna, char out[33])
{
    unsigned char digest[16]; char text[17];
    int n = snprintf(text, sizeof(text), "%" PRIx64, dna);
    md5((unsigned char *)text, n, digest);
    for (int i = 0; i < 16; i++) snprintf(out + 2*i, 3, "%02x", digest[i]);
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

static void identity(void)
{
    int fd = attribute("dna", O_RDONLY);
    char text[32]; ssize_t n = read(fd, text, sizeof(text)-1); close(fd);
    if (n <= 0) fail("FPGA DNA");
    text[n] = 0; text[strcspn(text, "\n")] = 0;
    serial_from_dna(parse_dna(text), serial);
    fd = open("/sys/bus/i2c/devices/0-0050/eeprom", O_RDONLY|O_CLOEXEC);
    char version[8];
    if (fd >= 0) {
        n = pread(fd, version, 8, 100); close(fd);
        if (n == 8 && !memcmp(version, "E200", 4) &&
            strspn((char[9]){version[0],version[1],version[2],version[3],version[4],version[5],version[6],version[7],0},
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._-") == 8) {
            memcpy(board, version, 8); board_source = "eeprom";
        }
    }
}
static int bind_udp(const char *address, unsigned port)
{
    int fd = socket(AF_INET, SOCK_DGRAM|SOCK_CLOEXEC, 0);
    if (fd < 0) fail("UDP socket");
    struct sockaddr_in a = {.sin_family=AF_INET, .sin_port=htons(port)};
    if (inet_pton(AF_INET, address, &a.sin_addr) != 1) { errno=EINVAL; fail("bind address"); }
    if (bind(fd, (struct sockaddr *)&a, sizeof(a))) fail("UDP bind");
    return fd;
}
static int hardware_lease(uint32_t ip, unsigned ms)
{
    if (simulated) return 0;
    char text[64]; snprintf(text, sizeof(text), "%08x %u\n", ip, ms);
    if (write_text(lease_fd, text)) { fault="lease_write_failed"; return -1; }
    struct registers r = read_registers();
    if ((ms && (r.owner != ip || !r.ms)) || (!ms && r.ms)) {
        fault="lease_readback_failed"; return -1;
    }
    return 0;
}
static void revoke_lease(void)
{
    if (hardware_lease(0, 0)) fail("lease revoke");
    owner_ip=0; token=0; deadline=0; cooldown=now_ms()+50;
}
static void expire_lease(void)
{
    if (token && (now_ms() >= deadline || (!simulated && read_registers().ms == 0))) {
        expired++; revoke_lease();
    }
}
static void lease_packet(int fd)
{
    uint32_t packet[8]; struct sockaddr_in peer; socklen_t len=sizeof(peer);
    ssize_t n=recvfrom(fd, packet, sizeof(packet), MSG_TRUNC, (struct sockaddr *)&peer, &len);
    if (n < 0) { if (errno != EINTR) fail("lease receive"); return; }
    if (n != sizeof(packet)) return;
    for (unsigned i=0;i<8;i++) packet[i]=ntohl(packet[i]);
    if (packet[0]!=ANTSDR_LEASE_MAGIC || packet[1]!=1) return;
    expire_lease();
    unsigned op=packet[2], result=0;
    uint64_t presented=((uint64_t)packet[4]<<32)|packet[5];
    uint32_t ip=ntohl(peer.sin_addr.s_addr);
    bool owns=token && presented==token && ip==owner_ip;
    if (packet[6] || packet[7] || op>3 || (op==1 && presented)) result=3;
    else if (op==0) result=token?1:0;
    else if (op==1 && token && ip==owner_ip && packet[3]==acquire_nonce) {
        // Idempotent retry after a lost acquire reply; no extra lifetime.
    } else if (op==1) {
        struct registers r=read_registers();
        if (token || now_ms()<cooldown) result=1;
        else if (!r.ip || r.enabled!=1) result=4;
        else {
            uint64_t fresh;
            if (getrandom(&fresh, sizeof(fresh), 0)!=sizeof(fresh) || !fresh) fail("lease random token");
            if (hardware_lease(ip, ANTSDR_LEASE_MS)) result=4;
            else { token=fresh; owner_ip=ip; acquire_nonce=packet[3]; deadline=now_ms()+ANTSDR_LEASE_MS; acquired++; }
        }
    } else if (!owns) result=2;
    else if (op==2) {
        if (hardware_lease(ip, ANTSDR_LEASE_MS)) { result=4; revoke_lease(); }
        else deadline=now_ms()+ANTSDR_LEASE_MS;
    } else revoke_lease();
    if (result) denied++;
    // Never expose another owner's token, including in query/busy replies.
    uint64_t answer = result==0 && (op==1 || op==2) ? token : 0;
    packet[4]=answer>>32; packet[5]=answer; packet[6]=result;
    packet[7]=deadline>now_ms()?deadline-now_ms():0;
    for (unsigned i=0;i<8;i++) packet[i]=htonl(packet[i]);
    if (sendto(fd, packet, sizeof(packet), 0, (struct sockaddr *)&peer, len)<0 && errno!=EINTR)
        perror("lease reply");
}
static int discovery(int argc, char **argv)
{
    const char *bind_ip="0.0.0.0", *test_dna=NULL, *test_status=NULL;
    unsigned port=49100, lease_port=ANTSDR_LEASE_PORT;
    for (int i=1;i<argc;i++) {
        if (!strcmp(argv[i], "--no-hardware")) simulated=true;
        else if (!strcmp(argv[i], "--status-dir") && i+1<argc) test_status=argv[++i];
        else if (!strcmp(argv[i], "--dna") && i+1<argc) test_dna=argv[++i];
        else if (!strcmp(argv[i], "--bind") && i+1<argc) bind_ip=argv[++i];
        else if ((!strcmp(argv[i], "--port") || !strcmp(argv[i], "--lease-port")) && i+1<argc) {
            bool lease=!strcmp(argv[i], "--lease-port"); char *end;
            errno=0; unsigned long p=strtoul(argv[++i], &end, 10);
            if (errno || !argv[i][0] || *end || p>65535) return 2;
            if (lease) lease_port=p; else port=p;
        } else return 2;
    }
    if ((simulated && (!test_dna || strcmp(bind_ip,"127.0.0.1"))) || (!simulated && (test_dna || test_status))) return 2;
    if (simulated) { serial_from_dna(parse_dna(test_dna),serial); memcpy(board,"E200  v1",8); board_source="simulated"; }
    else { status_fd=attribute("status",O_RDONLY); (void)read_registers(); identity(); lease_fd=attribute("lease",O_WRONLY); }
    int fd=bind_udp(bind_ip,port), leases=bind_udp(bind_ip,lease_port);
    if (!simulated) {
        revoke_lease(); int enable=attribute("enable",O_WRONLY);
        if (write_text(enable,"1\n")) fail("FPGA enable");
        close(enable);
    }
    if (simulated && test_status) {
        status_dir=open(test_status,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
        if (status_dir<0) fail("test status directory");
    }
    drop_privileges(false);
    struct sockaddr_in local; socklen_t len=sizeof(local);
    if (getsockname(fd,(struct sockaddr *)&local,&len)) fail("getsockname");
    printf("%s discovery port=%u serial=%s", VERSION,ntohs(local.sin_port),serial);
    if (getsockname(leases,(struct sockaddr *)&local,&len)) fail("getsockname");
    printf(" lease_port=%u\n",ntohs(local.sin_port)); fflush(stdout);
    const uint32_t request[4]={htonl('1'),htonl('m'),htonl('9'),htonl('j')};
    unsigned char response[56]={0}; uint32_t header[4]={htonl('1'),htonl('M'),htonl('0'),htonl('c')};
    memcpy(response,header,16); memcpy(response+16,serial,32); memcpy(response+48,board,8);
    struct pollfd wait[2]={{.fd=fd,.events=POLLIN},{.fd=leases,.events=POLLIN}};
    report(false);
    while (!stopping) {
        int ready=poll(wait,2,100);
        if (ready<0) { if (errno==EINTR) continue; fail("runtime poll"); }
        expire_lease();
        if (wait[0].revents&POLLIN) {
            unsigned char packet[2048]; struct sockaddr_in peer; socklen_t peer_len=sizeof(peer);
            ssize_t n=recvfrom(fd,packet,sizeof(packet),MSG_TRUNC,(struct sockaddr *)&peer,&peer_len);
            if (n<0) { if (errno!=EINTR) fail("discovery receive"); }
            else {
                requests++;
                if ((n!=48 && n!=56) || memcmp(packet,request,16)) rejected++;
                else if (sendto(fd,response,sizeof(response),0,(struct sockaddr *)&peer,peer_len)==sizeof(response)) replies++;
            }
        }
        if (wait[1].revents&POLLIN) lease_packet(leases);
        report(false);
    }
    revoke_lease(); report(false); close(fd); close(leases); return 0;
}
static uint32_t interface_ip(int fd)
{
    struct ifreq i={0}; memcpy(i.ifr_name,"eth0",5);
    if (ioctl(fd,SIOCGIFFLAGS,&i)) {
        if (errno==ENODEV) return 0;
        fail("interface flags");
    }
    if (!(i.ifr_flags&IFF_UP) || !(i.ifr_flags&IFF_RUNNING)) return 0;
    if (ioctl(fd,SIOCGIFADDR,&i)) {
        if (errno==EADDRNOTAVAIL || errno==ENODEV) return 0;
        fail("interface address");
    }
    return ntohl(((struct sockaddr_in *)&i.ifr_addr)->sin_addr.s_addr);
}
static int network(bool once, const char *requested_ip)
{
    struct in_addr ip;
    if (requested_ip && inet_pton(AF_INET,requested_ip,&ip)!=1) return 2;
    status_fd=attribute("status",O_RDONLY); (void)read_registers();
    int output=attribute("network",O_WRONLY);
    int fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0);
    if (fd<0) fail("network socket");
    int events=socket(AF_NETLINK,SOCK_RAW|SOCK_CLOEXEC|SOCK_NONBLOCK,NETLINK_ROUTE);
    if (events<0) fail("netlink socket");
    struct sockaddr_nl local={.nl_family=AF_NETLINK,.nl_groups=RTMGRP_LINK|RTMGRP_IPV4_IFADDR};
    if (bind(events,(struct sockaddr *)&local,sizeof(local))) fail("netlink bind");
    if (!once) drop_privileges(true);
    uint32_t previous=UINT32_MAX;
    do {
        uint32_t value=requested_ip?ntohl(ip.s_addr):interface_ip(fd);
        struct registers r=read_registers();
        if (value!=previous || r.ip!=value) {
            char text[32]; snprintf(text,sizeof(text),"%08x\n",value);
            if (write_text(output,text)) fail("FPGA IPv4 update");
            if (read_registers().ip!=value) { errno=EIO; fail("FPGA IPv4 readback"); }
            previous=value; updates++; last_update=now_ms();
            printf("%s FPGA IPv4=%u.%u.%u.%u\n",VERSION,value>>24,(value>>16)&255,(value>>8)&255,value&255); fflush(stdout);
        }
        fault=value?"":"link_or_address_unavailable"; report(true);
        if (!once && !stopping) {
            struct pollfd wait={.fd=events,.events=POLLIN};
            int ready=poll(&wait,1,1000); // watchdog resync also covers dropped notifications
            if (ready<0 && errno!=EINTR) fail("netlink poll");
            if (ready>0) {
                char messages[16384];
                // Drain a bounded batch, then resnapshot; ENOBUFS also resyncs.
                for (unsigned n=0;n<64;n++) {
                    ssize_t size=recv(events,messages,sizeof(messages),MSG_DONTWAIT);
                    if (size<0) break;
                }
            }
        }
    } while (!once && !stopping);
    close(events); close(output); close(fd); return 0;
}
int main(int argc,char **argv)
{
    struct sigaction action={.sa_handler=stop}; sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM,&action,NULL) || sigaction(SIGINT,&action,NULL)) fail("signals");
    if (argc==2 && !strcmp(argv[1],"--version")) { puts(VERSION); return 0; }
    const char *name=strrchr(argv[0],'/'); name=name?name+1:argv[0];
    if (!strcmp(name,"net_set")) return argc==1?network(false,NULL):2;
    if (!strcmp(name,"ip_set")) return argc==2?network(true,argv[1]):2;
    return discovery(argc,argv);
}
