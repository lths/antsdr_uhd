/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#ifndef HEALTH_ROOT
#define HEALTH_ROOT ""
#endif
#define PATH(p) HEALTH_ROOT p
#ifndef HEALTH_INTERVAL
#define HEALTH_INTERVAL 10
#endif
#ifndef HEALTH_SLEEP_NS
#define HEALTH_SLEEP_NS 1000000000
#endif
#ifndef HEALTH_PROBE_US
#define HEALTH_PROBE_US 500000
#endif
static volatile sig_atomic_t stopping;
static void stop(int sig) { (void)sig; stopping = 1; }
static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static int read_text(const char *path, char *buffer, size_t capacity) {
    FILE *f = fopen(path, "r");
    if (!f) { buffer[0] = 0; return -1; }
    size_t n = fread(buffer, 1, capacity - 1, f);
    buffer[n] = 0;
    fclose(f);
    return (int)n;
}
static bool alive(const char *name) {
    char path[256], text[128], executable[256], expected[256];
    snprintf(path, sizeof(path), PATH("/run/%s.pid"), name);
    if (read_text(path, text, sizeof(text)) < 0) return false;
    char *end;
    long pid = strtol(text, &end, 10);
    if (pid <= 1 || (*end && *end != '\n') || kill((pid_t)pid, 0)) return false;
    snprintf(path, sizeof(path), PATH("/proc/%ld/exe"), pid);
    ssize_t n = readlink(path, executable, sizeof(executable)-1);
    if (n < 0) return false;
    executable[n] = 0;
    snprintf(expected, sizeof(expected), "/sbin/%s", name);
    return !strcmp(executable, expected);
}
static bool probe(const char *address) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return false;
    struct timeval timeout = {.tv_sec=0, .tv_usec=HEALTH_PROBE_US};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    struct sockaddr_in peer = {.sin_family=AF_INET, .sin_port=htons(49100)};
    if (inet_pton(AF_INET, address, &peer.sin_addr) != 1
        || connect(fd, (struct sockaddr *)&peer, sizeof(peer))) { close(fd); return false; }
    uint32_t request[14] = {htonl('1'), htonl('m'), htonl('9'), htonl('j')};
    uint32_t response[15] = {0};
    bool result = send(fd, request, sizeof(request), 0) == sizeof(request)
        && recv(fd, response, sizeof(response), 0) == sizeof(request)
        && ntohl(response[0]) == '1' && ntohl(response[1]) == 'M'
        && ntohl(response[2]) == '0' && ntohl(response[3]) == 'c';
    close(fd);
    return result;
}
static bool local_address(char *address, size_t capacity) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return false;
    struct ifreq request;
    memset(&request, 0, sizeof(request));
    strcpy(request.ifr_name, "eth0");
    bool ok = ioctl(fd, SIOCGIFADDR, &request) == 0;
    if (ok) ok = inet_ntop(AF_INET,
        &((struct sockaddr_in *)&request.ifr_addr)->sin_addr, address, capacity) != NULL;
    close(fd);
    return ok;
}
static void string(FILE *out, const char *s) {
    fputc('"', out);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { fputc('\\', out); fputc(c, out); }
        else if (c < 32) fprintf(out, "\\u%04x", c);
        else fputc(c, out);
    }
    fputc('"', out);
}
static unsigned long counter(const char *name) {
    char path[256], buffer[64];
    snprintf(path, sizeof(path), PATH("/sys/class/net/eth0/statistics/%s"), name);
    if (read_text(path, buffer, sizeof(buffer)) < 0) return 0;
    return strtoul(buffer, NULL, 10);
}
struct health { bool radio, network, discovery, link, maintenance; };
static struct health check(void) {
    char carrier[8], address[INET_ADDRSTRLEN];
    struct health h = {0};
    h.radio = alive("e200") || alive("e310v2");
    h.network = alive("net_set");
    h.maintenance = access(PATH("/run/antsdr-maintenance"), F_OK) == 0;
    h.link = read_text(PATH("/sys/class/net/eth0/carrier"), carrier, sizeof(carrier)) > 0 && carrier[0] == '1';
    h.discovery = local_address(address, sizeof(address)) && probe(address);
    return h;
}
static bool healthy(struct health h) { return h.radio && h.network && h.discovery; }
static void report(FILE *out, struct health h, unsigned restarts, unsigned failures,
    bool limited) {
    char firmware[4096], mac[64], load[128], uptime[128], memory[4096];
    if (read_text(PATH("/etc/antsdr-release.json"), firmware, sizeof(firmware)) <= 0) strcpy(firmware, "null");
    read_text(PATH("/sys/class/net/eth0/address"), mac, sizeof(mac));
    mac[strcspn(mac, "\r\n")] = 0;
    read_text(PATH("/proc/loadavg"), load, sizeof(load));
    read_text(PATH("/proc/uptime"), uptime, sizeof(uptime));
    read_text(PATH("/proc/meminfo"), memory, sizeof(memory));
    unsigned long available = 0;
    char *field = strstr(memory, "MemAvailable:");
    if (field) sscanf(field, "MemAvailable: %lu", &available);
    fprintf(out, "{\"schema_version\":1,\"healthy\":%s,\"radio_running\":%s,\"network_running\":%s,"
        "\"discovery\":%s,\"link_up\":%s,\"maintenance\":%s,\"restarts\":%u,"
        "\"consecutive_failures\":%u,\"recovery_limited\":%s,\"uptime_seconds\":%.2f,"
        "\"load_1m\":%.2f,\"memory_available_kb\":%lu,\"mac\":",
        healthy(h)?"true":"false",h.radio?"true":"false",h.network?"true":"false",
        h.discovery?"true":"false",h.link?"true":"false",h.maintenance?"true":"false",
        restarts, failures, limited?"true":"false", atof(uptime), atof(load), available);
    string(out, mac);
    fprintf(out, ",\"rx_errors\":%lu,\"tx_errors\":%lu,\"rx_dropped\":%lu,\"firmware\":%s}\n",
        counter("rx_errors"),counter("tx_errors"),counter("rx_dropped"),firmware);
}
static int run_program(const char *program, const char *argument) {
    pid_t child = fork();
    if (child < 0) return -1;
    if (!child) {
        setsid();
        execl(program, program, argument, (char *)NULL);
        _exit(127);
    }
    double deadline = now() + 25;
    int status;
    while (!stopping && now() < deadline) {
        if (waitpid(child, &status, WNOHANG) == child)
            return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
        struct timespec delay = {.tv_sec=0, .tv_nsec=100000000};
        nanosleep(&delay, NULL);
    }
    kill(-child, SIGKILL);
    waitpid(child, &status, 0);
    return -1;
}
static int supervise(void) {
    signal(SIGTERM, stop); signal(SIGINT, stop);
    openlog("antsdr-health", LOG_PID, LOG_DAEMON);
    unsigned failures=0, restarts=0, window_restarts=0;
    double window=now(), next=0;
    bool keys_saved=false;
    while (!stopping) {
        if (now() >= next) {
            if (now()-window >= 600) { window=now(); window_restarts=0; }
            struct health h=check();
            if (healthy(h) || h.maintenance || !h.link) failures=0;
            else failures++;
            if (failures >= 3 && window_restarts < 3) {
                restarts++; window_restarts++;
                syslog(LOG_WARNING, "Radio health failure; recovery attempt %u", restarts);
                int result=run_program(PATH("/etc/init.d/S90device_start.sh"), "restart");
                syslog(result?LOG_ERR:LOG_NOTICE, "Recovery completed with status %d", result);
                failures=0; h=check();
            }
            if (!keys_saved && access(PATH("/etc/dropbear/dropbear_ecdsa_host_key"),R_OK)==0)
                keys_saved=run_program(PATH("/usr/sbin/device_persistent_keys"),NULL)==0;
            FILE *out=fopen(PATH("/run/antsdr-health.json.new"), "w");
            if (out) {
                report(out,h,restarts,failures,window_restarts>=3);
                if (!fclose(out)) rename(PATH("/run/antsdr-health.json.new"),PATH("/run/antsdr-health.json"));
            }
            next=now()+HEALTH_INTERVAL;
        }
        struct timespec delay={.tv_sec=HEALTH_SLEEP_NS/1000000000,
            .tv_nsec=HEALTH_SLEEP_NS%1000000000}; nanosleep(&delay,NULL);
    }
    closelog();
    return 0;
}
int main(int argc, char **argv) {
    if (argc==3 && !strcmp(argv[1],"--probe")) return probe(argv[2])?0:1;
    if (argc==2 && !strcmp(argv[1],"--supervise")) return supervise();
    if (argc==2 && !strcmp(argv[1],"--json")) {
        struct health h=check(); report(stdout,h,0,0,false); return healthy(h)?0:1;
    }
    fprintf(stderr,"Usage: antsdr-health --json | --probe IPv4 | --supervise\n");
    return 2;
}
