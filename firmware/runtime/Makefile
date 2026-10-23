# SPDX-License-Identifier: GPL-2.0-or-later
CC = $(CROSS_COMPILE)gcc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Werror -fstack-protector-strong -D_FORTIFY_SOURCE=2
OUT ?= build
.PHONY: all install clean
all: $(OUT)/e200
$(OUT)/e200: radio.c md5.c md5.h management.h
	mkdir -p $(OUT)
	$(CC) $(CFLAGS) radio.c md5.c $(LDFLAGS) -o $@
install: all
	install -D -m 0755 $(OUT)/e200 $(DESTDIR)/sbin/e200
	install -D -m 0755 $(OUT)/e200 $(DESTDIR)/sbin/net_set
	ln -sf ../sbin/e200 $(DESTDIR)/bin/ip_set
clean:
	rm -f $(OUT)/e200
