FROM ubuntu:18.04@sha256:152dc042452c496007f07ca9127571cb9c29697f42acbfad72324b2bb2e43c98
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential git python python3 bc bison flex fakeroot cpio zip unzip \
    rsync file wget curl ca-certificates pkg-config autoconf automake libtool \
    patch gawk texinfo help2man device-tree-compiler u-boot-tools mtools \
    libncurses5-dev libssl-dev libtinfo5 libx11-6 libxext6 libxrender1 \
    libxtst6 libxi6 libglib2.0-0 libgtk2.0-0 libc6-i386 lib32stdc++6 lib32z1 \
    zlib1g-dev locales xz-utils && \
    locale-gen en_US.UTF-8 && \
    useradd --create-home --uid 1000 --user-group builder && \
    rm -rf /var/lib/apt/lists/*
USER builder
WORKDIR /work/firmware
