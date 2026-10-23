FROM ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential cmake git ca-certificates pkg-config python3 python3-dev \
    python3-mako python3-numpy python3-setuptools python3-requests python3-ruamel.yaml python3-paramiko \
    python3-serial python3-scipy libboost-atomic-dev libboost-chrono-dev \
    libboost-date-time-dev libboost-filesystem-dev libboost-program-options-dev \
    libboost-regex-dev libboost-serialization-dev libboost-system-dev \
    libboost-thread-dev libboost-test-dev libusb-1.0-0-dev libudev-dev && \
    useradd --create-home --uid 1000 --user-group builder && \
    rm -rf /var/lib/apt/lists/*
USER builder
WORKDIR /work
ENV PYTHONPATH=/work/host/build-antsdr/python:/work/tools \
    LD_LIBRARY_PATH=/work/host/build-antsdr/lib
